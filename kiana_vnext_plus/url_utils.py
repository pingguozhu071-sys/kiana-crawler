import hashlib
import logging
import re as _re
import socket as _socket
import time
import urllib.request as _urllib_request
import urllib.error as _urllib_error
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode, urljoin

logger = logging.getLogger(__name__)

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "ref", "spm", "session", "timestamp",
}


def normalize_url(url, keep_params=None):
    if keep_params is None:
        keep_params = set()
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    hostname = parsed.hostname or parsed.netloc
    # 修复：IDNA 编码失败时回退到原值
    try:
        netloc = hostname.lower().encode("idna").decode()
    except (UnicodeError, UnicodeDecodeError):
        netloc = hostname.lower()
    if parsed.port and not ((scheme == "http" and parsed.port == 80) or (scheme == "https" and parsed.port == 443)):
        netloc = f"{netloc}:{parsed.port}"
    path = parsed.path or "/"
    parts = [p for p in path.split('/') if p and p != '.']
    new_parts = []
    for p in parts:
        if p == '..' and new_parts:
            new_parts.pop()
        else:
            new_parts.append(p)
    path = '/' + '/'.join(new_parts) if new_parts else '/'
    query = parse_qsl(parsed.query, keep_blank_values=True)
    filtered = [(k, v) for k, v in query if k not in TRACKING_PARAMS or k in keep_params]
    filtered.sort()
    qs = urlencode(filtered)
    return urlunparse((scheme, netloc, path, parsed.params, qs, ""))


def url_hash(url):
    return hashlib.sha256(normalize_url(url).encode()).hexdigest()


# ── 短链误解析检测 ────────────────────────────────────────────────────
# [v6 修复·R6 真机实测] **短链服务的路径只能是一段短码**（`b23.tv/ybyASFu`）。
# 一旦看到 `b23.tv/video/BVxxx` 这种"站点路径"，就说明**相对链接被按短链主机解析了**——
# 该 URL 在真实世界里**不存在**。
#
# 真机后果（机主那次 8 个 B站种子的抓取）：这些假 URL 会被当成
#   ① 页面任务 → 抓取失败 → 走渲染兜底 → 白起浏览器；
#   ② **视频任务 → yt-dlp 去下 → 存下 46 字节的错误页**（`*.unknown_video`）。
# 日志里刷了十几次，落了一堆垃圾文件。
#
# 根因是解析基址：页面真实落点是 `www.bilibili.com/video/BVxxx`，
# 但短链种子进来时基址退回了 `b23.tv/xxx`（见 `page_processor._final_url_of` 的说明）。
# 基址那条线仍在修；**这个检测是防御纵深**：无论基址怎么错，
# 这种 URL 都进不了队列，也就不会再去浪费一次请求和一个 46 字节垃圾文件。
_SHORTENER_HOSTS = (
    "b23.tv", "t.cn", "dwz.cn", "suo.im", "url.cn", "sourl.cn", "xhslink.com",
    "v.douyin.com", "163cn.tv", "tb.cn", "jd.com",
)
# 站点栏目名：短链的第一段**绝不会**是这些词（短码是随机字母数字）
_SITE_SECTION_SEGS = frozenset((
    "video", "player", "bangumi", "space", "search", "read", "opus", "fav",
    "medialist", "blackboard", "match", "anime", "account", "passport",
))


def is_shortener_url(url: str) -> bool:
    """`url` 的主机是否是已知**短链服务**（`b23.tv` / `v.douyin.com` …）。

    与 `is_shortener_misresolution` 的区别：
      · 本函数只问"是不是短链主机"（**合法短链**也算）；
      · 那个函数问"是不是短链主机 + 站点路径这种**必然不存在**的组合"。

    用途：短链种子**不必**单独入视频队列 —— 紧跟其后的页面解析会拿到
    **规范 URL**（带 BV 号），那一条更好（见 `crawler._seed` 里的说明）。
    """
    try:
        p = urlparse(url if "://" in url else "http://" + url)
    except Exception:
        return False
    host = (p.netloc or "").lower().split(":")[0]
    return bool(host) and any(host == h or host.endswith("." + h) for h in _SHORTENER_HOSTS)


def is_shortener_misresolution(url: str) -> bool:
    """`url` 是否「短链主机 + 站点路径」这种**必然不存在**的组合。

    判据（任一命中即真）：
      1. 主机是已知短链服务，且路径**不止一段**（短链只有一段短码）；
      2. 主机是已知短链服务，且第一段是站点栏目名（`video` / `player` …）；
      3. 主机是已知短链服务，且唯一那段**含点号**（`player.html` 这种像文件名，
         而短码是纯字母数字 —— `ybyASFu` / `izsrwgd`）。

    ⚠️ 判据 3 的用例（`b23.tv/player.html`）是**我构造的**，真机上没见过；
    真机抓到的是判据 1（`b23.tv/video/BVxxx`）。留着是因为它表达的是同一条道理：
    **短链路径不该长得像站点路径**。

    取不到主机名、或主机不是短链服务 → 一律 **False**（宁可放过，不可误杀：
    误杀一个真视频的代价比多试一次大得多）。
    """
    try:
        p = urlparse(url if "://" in url else "http://" + url)
    except Exception:
        return False
    host = (p.netloc or "").lower().split(":")[0]
    if not host:
        return False
    if not any(host == h or host.endswith("." + h) for h in _SHORTENER_HOSTS):
        return False
    segs = [s for s in (p.path or "").split("/") if s]
    if not segs:
        return False                      # 裸短链（`b23.tv`）——交给它自己重定向
    if len(segs) > 1:
        return True                       # 短链不该有第二段
    seg = segs[0].lower()
    if "." in seg:
        return True                       # 像文件名 → 是站点路径，不是短码
    return seg in _SITE_SECTION_SEGS      # 或第一段就是栏目名


# ── 媒体流分片判定（"这是不是可解析的页面"）────────────────────────────
# [本轮修复·真机日志 2026-10-03] 机主那趟抓取的日志里反复出现：
#     yt-dlp 未产出文件: https://xy61x164x142x12xy.mcdn.bilivideo.cn:8082/v1/resource/
#                        upgcxcode/45/35/36062563545/36062563545-1-30032.m4s?...
#     ERROR: [generic] 36062563545-1-30032: Unable to download webpage: HTTP Error 403
#     Video download failed: Direct download failed
# 这些既**不是可解析的页面**，也**不是一个能独立交付的媒体文件** —— 它们是 B站 DASH
# 的**单条轨道分片**：签名短时效、必须带主站 Referer，而且单独一片放不出完整视频。
#
# ⚠️ 判据**刻意不用域名黑名单**：黑名单会漂。工程里现成的反例就是
# `link_scoring._MEDIA_CDN_DOMAINS` —— 它收了 `bilivideo.com` 却**没收 `bilivideo.cn`**，
# 而日志里那条主机恰恰是 `mcdn.bilivideo.cn`。域名会换、CDN 会迁移，**后缀不会**。
_STREAM_SEGMENT_EXTS = (".m4s", ".cmfv", ".cmfa", ".cmft")


def is_media_stream_url(url: str) -> bool:
    """`url` 是不是**媒体流的一片/一轨**（既不是可解析的页面，也不是可独立交付的媒体文件）。

    判据只有一条，且**刻意窄**：路径最后一段以**流分片容器后缀**结尾
    （`.m4s` / `.cmfv` / `.cmfa` / `.cmft`）。这几个后缀在业界**只**用于
    "某条媒体流的一个分片"，从不代表一个完整文件 —— 所以它既解析不出页面，
    也交付不出东西。

    **为什么不收 `.mp4` / `.ts` / `.m3u8` / 无后缀**（每一条都是刻意的，别"顺手补全"）：
      · `.mp4` / `.ts` / `.m3u8` —— 本工程**明确支持"给一个直链就直接下"**
        （`crawler._seed` 的媒体直链分支、`m3u8_downloader`）。把它们判成分片
        等于砍掉一条真能力；
      · 无后缀（`.../v1/resource/...` 这类）—— 缺证据，**宁可放过**：
        误杀一个真视频的代价（东西永远下不到）远大于多试一次的代价。

    本函数只回答"是不是分片"这一个问题；"该不该入队/该不该下载"由调用点各自决定
    （与 `is_private_url` 同一种用法：**判据一份，决策点多处**）。
    """
    if not url or not isinstance(url, str):
        return False
    try:
        p = urlparse(url)
    except Exception:
        return False
    if (p.scheme or "").lower() not in ("http", "https"):
        return False                       # 非 http(s) 一律"不是"（退化输入不许抛）
    return (p.path or "").lower().endswith(_STREAM_SEGMENT_EXTS)


# 形如主机名的输入（可选端口）——用于决定要不要补 scheme
_HOSTLIKE = _re.compile(r"^[A-Za-z0-9\-._]+(:\d+)?$")


def extract_domain(url):
    """取域名（IDNA 规范化）。**域名提取的唯一实现。**

    [v6 修复] 此前同一能力有**三份**（本函数、`rate_limiter._domain_of`、
    `session_pool._domain_of`），且三者在**真实输入上结论不一致**：

    | 输入 | 本函数（旧） | rate_limiter | session_pool |
    |---|---|---|---|
    | `example.com`（裸域名） | `''` ← **空串！** | `example.com` | `example.com` |
    | `https://[`（畸形） | **抛 ValueError** | `unknown` | **抛 ValueError** |

    两个后果都真实可达：

    · **裸域名种子**会让 `frontier.domain` 存成**空串**——所有裸域名种子挤进同一个空桶，
      而限流器与会话池却按 `example.com` 分桶 → **两侧对不上**（域名限额、域名级冷却、
      `count_done_by_domain` 全部错位）；
    · **畸形 URL 会抛异常**，而本函数在 `frontier.push` 里被调用、`push` 又在爬取主循环上。

    现统一为：**像主机名就补 scheme**（仅限 `_HOSTLIKE`，垃圾输入行为不变）、
    **绝不抛异常**（失败返回 `""`）。
    """
    try:
        s = str(url or "").strip()
        if s and "://" not in s and _HOSTLIKE.match(s):
            s = f"https://{s}"
        netloc = urlparse(s).netloc.lower()
    except Exception:
        return ""
    try:
        return netloc.encode("idna").decode()
    except (UnicodeError, UnicodeDecodeError):
        return netloc


# ═══ [FIXED & MODIFIED] v2.14 阶段3 安全基线：统一文件名/目录名清洗 + 私网判定 ═══
# [v2.16.1] SSRF 加固：数字字面用系统解析器（AI_NUMERICHOST，与 curl 同语义——
# 覆盖十进制/十六进制/八进制/段数变体：2130706433、0x7f000001、127.1、0177.0.0.1）；
# 域名走 DNS 解析校验（可配置开关，解析失败按放行——防误伤，与旧行为一致）。
DNS_CHECK_ENABLED = True                      # 域名解析校验总开关（engine 可配）
# [v2.19.7 安全·扫描发现·DNS rebinding] 原缓存 `host -> bool` **永不过期**：长跑任务里
# 一个域名先解析到公网（缓存"放行"）→ 攻击者随后把该域名改指向 127.0.0.1/169.254.169.254，
# 缓存仍判放行 → 校验形同虚设（rebinding 的经典窗口）。现改为 `host -> (bool, 判定时刻)`，
# 判定超过 _HOST_CACHE_TTL 秒即重新解析。TTL 取 90s：远短于 rebinding 的利用窗口，
# 又不至于给每页都加一次 DNS 查询（长跑爬虫的热点域名查得很少）。
_HOST_CACHE: dict = {}                        # host -> (bool(私网), ts)
_HOST_CACHE_TTL = 90.0
_HOST_CACHE_MAX = 512


def _cache_private(host: str, priv: bool) -> bool:
    if len(_HOST_CACHE) >= _HOST_CACHE_MAX:
        _HOST_CACHE.clear()
    _HOST_CACHE[host] = (priv, time.monotonic())
    return priv


def _cached_private(host: str):
    """命中且未过期 → (True, priv)；否则 (False, None) 由调用方重新解析"""
    ent = _HOST_CACHE.get(host)
    if not ent:
        return False, None
    priv, ts = ent
    if (time.monotonic() - ts) > _HOST_CACHE_TTL:
        _HOST_CACHE.pop(host, None)   # 过期即失效，强制重解析
        return False, None
    return True, priv


# [v2.19.3 质检补强] 标准 ipaddress 判定**未覆盖**的特殊用途 IPv4 段。
# 起因：外部评估报告曾专门指出 100.64.0.0/10 漏拦；v2.19 建 SSRF 闸时沿用了
# is_private_url 而未补此段 → 闸对 CGNAT 地址实际是漏的（质检轮发现并修复）。
#   · 100.64.0.0/10  CGNAT（RFC 6598 运营商级 NAT）—— Python 的 ip.is_private **不含**它
#     （CGNAT 名义上属"共享地址空间"而非"私有"），故必须单独判
#   · 192.88.99.0/24 6to4 中继（RFC 7526 已废弃）—— 常用作隧道类绕过
_EXTRA_BLOCKED_V4_RANGES = (
    (1681915904, 1686110207),   # 100.64.0.0/10
    (3227017984, 3227018239),   # 192.88.99.0/24
)


def _any_private(infos) -> bool:
    import ipaddress as _ip
    for info in infos:
        try:
            ip = _ip.ip_address(str(info[4][0]).split("%")[0])
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast):
            return True
        if ip.version == 4:
            _v = int(ip)
            for _lo, _hi in _EXTRA_BLOCKED_V4_RANGES:
                if _lo <= _v <= _hi:
                    return True
    return False


def _resolve_host_private(host: str) -> bool:
    """DNS 解析校验：域名解析到私网 → True（拒）；解析失败 → False（放行，防误伤）。
    [v2.19.7] 判定结果带 90s TTL（见 _HOST_CACHE 说明），过期自动重解析。"""
    hit, priv = _cached_private(host)
    if hit:
        return priv
    try:
        infos = _socket.getaddrinfo(host, None, proto=_socket.IPPROTO_TCP)
        return _cache_private(host, _any_private(infos))
    except _socket.gaierror:
        return _cache_private(host, False)


_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL",
                 *(f"COM{i}" for i in range(1, 10)),
                 *(f"LPT{i}" for i in range(1, 10))}


def clean_url_entity_residue(url: str) -> str:
    """修掉从 HTML 里抠 URL 时常见的**实体/编码残渣**。全工程统一入口。

    真机日志（2026-10-03 GUI 抓取）：
        `B站视频入队: https://www.bilibili.com/video/BV1PSL96YEwp?amp%3Btrackid=we`
    `amp%3B` 是 `&amp;` 被砍掉首字符、`;` 又被百分号编码成 `%3B` 的产物。

    **为什么要统一清**：这类 URL 能下（多个无用参数），但会被当成**独立的下载键** ——
    同一个视频可能因此入队两次、或与规范 URL 各下一份（本工程已经吃过"同视频下两遍"
    的亏，白耗过 82MB）。

    处理四类形态（都是"`&` 被实体化/编码后的残骸"）：
        `&amp;` → `&` ｜ `amp%3B` → `&` ｜ `amp;` → `&` ｜ `&#38;` / `&#x26;` → `&`

    ⚠️ **只动"参数分隔符位置"的残骸，不碰别的**：
      · 不 decode 其它百分号编码（`%20` 等要原样保留 —— 那是合法 URL 的一部分）；
      · 不删查询参数（签名参数是下载钥匙，见 `frontier.add_video_download` 的说明）。
    """
    if not url or not isinstance(url, str):
        return url
    s = url
    # `&amp;` / `&#38;` / `&#x26;` —— 标准实体（大小写都收）
    s = _re.sub(r"&(amp|#0*38|#x0*26);", "&", s, flags=_re.IGNORECASE)
    # `amp%3B` / `amp;` —— 被砍了首字符的残骸。
    # **必须限定在 `?` 之后的查询串里**，否则会误伤路径里恰好含 "amp;" 的正常 URL。
    if "?" in s:
        head, _, q = s.partition("?")
        q = _re.sub(r"(?:^|&)amp(?:%3B|;)", "&", q, flags=_re.IGNORECASE)
        # 清理 `&` 重复/开头/结尾
        q = _re.sub(r"&{2,}", "&", q).strip("&")
        s = head + "?" + q if q else head
    return s


def safe_filename(name: str, max_len: int = 80) -> str:
    """Windows 安全文件名：非法字符+控制字符清洗、保留名（CON/NUL/COM1..）、
    去结尾点/空格、限长。全工程统一入口（此前 4 处实现标准不一——_dom_dir 漏网
    反斜杠穿越与保留名）。"""
    s = _re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name))
    s = s.strip().rstrip(". ")
    stem = s.split(".")[0].upper()
    if stem in _WIN_RESERVED:
        s = "_" + s
    if not s:
        s = "_unnamed"
    return s[:max_len]


def safe_dirname(name: str, max_len: int = 80) -> str:
    """Windows 安全目录名：_dom_dir 此前直接用 netloc 拼路径——netloc 含反斜杠
    可构造 '..\\..\\' 真实穿越、含 ':'（端口）mkdir 必失败、'..' 落上级目录。"""
    s = safe_filename(name, max_len)
    s = s.replace("..", "_")
    if s in (".", ".."):
        s = "_" + s
    return s


def is_private_url(url: str, dns_check=None) -> bool:
    """SSRF 闸：http(s) 之外的 scheme、私网/环回/链路本地/保留/组播、本地 hostname → True（拒抓）。
    [v2.16.1 加固] ① 数字字面（十进制/十六进制/八进制/段数变体）经系统解析器判定——
    2130706433、0x7f000001、127.1、0177.0.0.1 均正确识别为 127.0.0.1（旧实现 ipaddress
    抛 ValueError 被当域名放行）；② 域名做 DNS 解析校验（dns_check/模块级 DNS_CHECK_ENABLED
    控制，解析失败按放行防误伤；判定带 90s TTL 防 rebinding）。
    [v2.19.7 安全·扫描发现] ③ host 规范化：IPv6 zone id（`[fe80::1%25eth0]` → 去 `%zone`）
    与 FQDN 尾点（`foo.local.`/`localhost.`）此前能把 `.local` 黑名单与解析判定一起绕开。
    """
    try:
        from urllib.parse import urlparse as _up
        p = _up(str(url))
        if p.scheme not in ("http", "https"):
            return True
        host = (p.hostname or "").lower().strip()
        if not host:
            return True
        # ③ 规范化：去 IPv6 zone id 后缀，再去 FQDN 尾点（两者都是等价写法，黑名单必须同判）
        host = host.split("%", 1)[0].rstrip(".")
        if not host:
            return True
        if host in ("localhost",) or host.endswith(".local") or host.endswith(".internal"):
            return True
        # [v2.19.7] 缓存改为 90s TTL（防 DNS rebinding，见 _HOST_CACHE 注释）
        _hit, _priv = _cached_private(host)
        if _hit:
            return _priv
        # 数字字面：AI_NUMERICHOST 与 curl 客户端同语义（不发 DNS）；非字面 → gaierror
        try:
            infos = _socket.getaddrinfo(host, None, flags=_socket.AI_NUMERICHOST,
                                        proto=_socket.IPPROTO_TCP)
            return _cache_private(host, _any_private(infos))
        except _socket.gaierror:
            pass  # 是域名不是 IP 字面 → 交给 DNS 解析校验
        if dns_check is None:
            dns_check = DNS_CHECK_ENABLED
        if dns_check:
            return _resolve_host_private(host)
        return False
    except Exception:
        return True  # 判定失败按风险处理


# ═══ [v2.16.1] 安全 HTTP 请求件（平台 resolver/通用请求共用）═══
# 平台专用解析器（抖音/网易云/快手等）的动态 URL 请求统一走 safe_urlopen：
# 协议限制 + 私网/环回/本地拒绝（复用 is_private_url，含进制 IP 变体与 DNS 解析校验）
def _stamp_final_url(resp, url: str):
    """把**本次请求实际落到的地址**挂到响应对象上（safe_get 逐跳后的终点）。

    [实测 v2.19.8 短链 bug] 起因：`safe_get` 为了逐跳过 SSRF 闸用了
    `allow_redirects=False`，自己维护 `_cur` 循环跟跳；**但返回 resp 时没把 `_cur`
    带出来**。短链种子（b23.tv → www.bilibili.com/video/BV…）因此在页处理器里
    仍然以**短链主机**为基址解析相对链接（`/video/BVxxx` → `https://b23.tv/video/BVxxx`），
    而该地址根本不存在 → 机主实测 12 页成功 / 19 页失败全是这一个原因。

    为什么用挂属性的方式而不是改返回类型：safe_get 的返回契约是"响应对象或 None"，
    调用点有 10+ 处（下载/m3u8/订阅源/代理源），改成元组会让每一处都得改。
    属性名带 `_kiana_` 前缀并只用 `getattr(..., None)` 读——**老代码零感知，
    新代码按需取用**（读不到就退回原 url，与改前行为完全一致）。
    """
    try:
        if resp is not None and url:
            resp._kiana_final_url = str(url)
    except Exception:
        pass          # 响应对象可能是 __slots__/代理等不可写实现——纯附加信息，绝不因此报错
    return resp


async def safe_get(session, url, headers=None, *, method: str = "GET", max_hops: int = 10,
                   timeout: int = 60, **kw):
    """[v2.19.7 安全·扫描发现] **带逐跳 SSRF 校验的异步 GET**（下载通道专用）。

    为什么需要：curl_cffi 默认 `allow_redirects=True`（跟随最多 30 跳）。
    只在**入口**做 `is_private_url` 校验会被重定向绕过——攻击者页面里的
    `<img src="http://attacker.com/a.jpg">` 回 302 到 `169.254.169.254`，
    libcurl 自行跟随，**内网正文以 .jpg 落盘**（扫描员已用本机 loopback 双服务
    PoC 复现：未传该参数时 status=200 + body=内网内容）。

    本函数统一：入口校验 + `allow_redirects=False` **手动逐跳**（每跳复检落点）。
    所有下载/取流通道都应经由此函数，而不是直接 `session.get`。

    返回最终响应对象；被拦或请求失败返回 None。
    """
    if not url or not str(url).startswith(("http://", "https://")):
        return None
    if is_private_url(str(url), dns_check=True):
        logger.warning(f"SSRF 拦截（下载通道入口）: {str(url)[:70]}")
        return None
    _hdrs = dict(headers or {})
    _cur, _hops = str(url), 0

    async def _dispatch(sess, *, with_redirect_flag: bool):
        """按 method 选对动词：有 `request` 用 `request(verb, ...)`；否则走同名
        小写方法（head/get/post）——**不能一律退化成 get**（HEAD 探测被悄悄改成
        整文件下载，会把 Range 探测变成真实流量；真实 curl 会话支持 request，
        这条主要是给轻量/桩会话兜底）。"""
        _kw = {"headers": _hdrs, "timeout": timeout, **kw}
        if with_redirect_flag:
            _kw["allow_redirects"] = False
        _req = getattr(sess, "request", None)
        if _req is not None:
            return await _req(str(method).upper(), _cur, **_kw)
        _verb = str(method).lower()
        _fn = getattr(sess, _verb, None) or getattr(sess, "get", None)
        if _fn is None:
            raise TypeError("会话既无 request 也无 get")
        return await _fn(_cur, **_kw)

    while True:
        try:
            resp = await _dispatch(session, with_redirect_flag=True)
        except TypeError as _te:
            # 会话实现不接受 allow_redirects（非 curl_cffi 的轻量会话）→ 少传该参数，
            # 但仍保持逐跳语义（下面靠 is_redirect 判定，是否真跟随取决于会话本身）
            try:
                resp = await _dispatch(session, with_redirect_flag=False)
            except TypeError:
                raise _te          # 两次都是"参数不认" → 是会话接口不匹配，交调用方看
            except Exception:
                raise              # [v2.19.7] 传输错误**照旧抛出**，不当成"被拦"
        # [v2.19.7 关键语义] 传输异常一律向上抛，不在这里吞：
        # 调用方（download_image/_stream_one/m3u8 分片…）的重试循环都是 `except → 重试`，
        # 若这里把异常吞成 None，第一次网络抖动就会被当成"永久失败/被拦"直接放弃
        # ——那是比泄漏更常见的静默降级（原先裸 session.get 正是抛异常的）。
        # None 只表示**被闸拦下/协议非法**（不可重试）。
        if not getattr(resp, "is_redirect", False):
            _stamp_final_url(resp, _cur)
            return resp
        # 中间跳响应必须关闭（流式模式下不关会泄漏连接）
        try:
            resp.close()
        except Exception:
            pass
        if _hops >= max_hops:
            logger.warning(f"safe_get 重定向超过 {max_hops} 跳上限，放弃: {_cur[:70]}")
            # [实测 v2.19.8 短链 bug] 超限时**也已发出真实请求**，_cur 就是当时的落点，
            # 与"非重定向"那条一样要带出去（少带一处 = 下游按错误主机解析相对链接）。
            _stamp_final_url(resp, _cur)
            return resp
        _loc = (resp.headers.get("Location") or resp.headers.get("location") or "")
        if not _loc:
            _stamp_final_url(resp, _cur)
            return resp
        _nxt = urljoin(_cur, _loc)
        if is_private_url(_nxt, dns_check=True):
            logger.warning(f"SSRF 拦截（下载通道重定向落点为私网）: {_nxt[:70]}")
            return None
        _cur, _hops = _nxt, _hops + 1


# + 主机白名单 + 重定向逐跳过闸（防重定向进内网 / DNS rebinding）。


def _http_target_ok(url: str, allowed_hosts=()) -> bool:
    """请求目标校验：http(s)；私网/环回/本地拒绝；允许主机白名单（后缀匹配，
    空=不限制域名但须非私网）。"""
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return False
    try:
        from urllib.parse import urlparse as _up
        host = (_up(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    if is_private_url(url):
        return False
    if allowed_hosts and not any(host == d or host.endswith("." + d) for d in allowed_hosts):
        return False
    return True


class _SafeRedirectHandler(_urllib_request.HTTPRedirectHandler):
    """重定向逐跳过闸：跳向私网/非白名单主机的重定向一律拒绝。"""

    def __init__(self, allowed_hosts=()):
        super().__init__()
        self._allowed = tuple(allowed_hosts)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _http_target_ok(newurl, self._allowed):
            raise _urllib_error.URLError("blocked redirect target: " + str(newurl)[:80])
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def safe_urlopen(url, allowed_hosts=(), headers=None, data=None, timeout=15,
                 method=None, proxy=""):
    """安全 HTTP 请求：协议+私网+白名单校验与重定向逐跳过闸后打开；
    目标非法返回 None（不抛，由调用方降级）。
    [v2.17 安全深扫] method/proxy 支持：覆盖 POST 类 fallback（urllib Request 原生能力，
    无 shell/注入面；proxy 经 ProxyHandler 仅限该次请求）。"""
    if not _http_target_ok(url, allowed_hosts):
        return None
    req = _urllib_request.Request(url, data=data, headers=dict(headers or {}), method=method)
    handlers = []
    if proxy:
        handlers.append(_urllib_request.ProxyHandler({"http": proxy, "https": proxy}))
    handlers.append(_SafeRedirectHandler(allowed_hosts))
    try:
        return _urllib_request.build_opener(*handlers).open(req, timeout=timeout)
    except Exception:
        return None


# ════════════════════════════════════════════════════════════════
# [v6] 防盗链 Referer 的**唯一实现**
#
#   背景：同一个能力此前散在三处、三种做法：
#     · `media_downloader._referer_for`  —— 有 **CDN→主站映射**（`i0.hdslb.com` → bilibili）
#     · `universal_downloader` 下载头      —— 只有 `f"https://{host}/"`（**CDN 自己的主机**）
#     · `universal_downloader.download_image` —— 调用方不传就**完全不发 Referer**
#   而工程自己的注释写着："B站图片 i0.hdslb.com 等 CDN 校验主站 Referer，
#   微博/公众号图床同理——**无 Referer 直接 403/空响应**"。
#   也就是说后两种做法**正好命中会 403 的那两种形态**（发 CDN 自己的 Referer / 不发）。
#   抽到一处后，"再次分叉"必须发生在同一个地方。
# ════════════════════════════════════════════════════════════════
_CDN_MAIN_SITE = (
    ("hdslb.com", "https://www.bilibili.com/"),
    ("bilibili.com", "https://www.bilibili.com/"),
    ("alicdn.com", "https://www.taobao.com/"),
    ("taobao.com", "https://www.taobao.com/"),
    ("sinaimg", "https://weibo.com/"),
    ("weibo.com", "https://weibo.com/"),
    ("douyinvod.com", "https://www.douyin.com/"),
    ("bytecdn.cn", "https://www.douyin.com/"),
    ("douyin", "https://www.douyin.com/"),
    ("zhimg.com", "https://www.zhihu.com/"),
    ("xiaohongshu.com", "https://www.xiaohongshu.com/"),
    # v2.4.1 的注释点名过 `cdn.steampowered.com` 应归主站，这里一并覆盖
    ("steamstatic.com", "https://store.steampowered.com/"),
    ("steampowered.com", "https://store.steampowered.com/"),
)


def referer_for(url: str) -> str:
    """按防盗链规则推导 Referer：**CDN 主机映射到主站**，其余回退自身主机。

    返回 `""` 表示推导不出（调用方应视情况决定是否省略该头）。
    """
    try:
        host = (urlparse(url).netloc or "").lower()
    except Exception:
        return ""
    if not host:
        return ""
    for needle, main in _CDN_MAIN_SITE:
        if needle in host:
            return main
    return f"https://{host}/"


def cookie_file_warning(path: str) -> str:
    """配置了 cookies 路径但文件不可用时，返回一句**给用户看**的说明；正常返回 `""`。

    [v6] 引擎侧早有这条判定（`universal_downloader._ensure_cookie_file` 会 logger.error），
    但**界面上一声不吭** —— 用户看着输入框里填着路径，以为配好了，
    实际全程按无 cookies 跑（B站只给 480P）。这里把它做成**界面也能用**的一份判定。

    与引擎那份**判据一致**（都是"路径非空但文件不存在"），但**不共用实现**的理由：
    引擎那份在导出侧被脱敏策略牵制（路径可能被抹），界面这份要显示**原始路径**给用户去改。
    两边都只有一行 `os.path.exists`，重复的代价远小于耦合的代价。
    """
    import os
    p = (path or "").strip()
    if not p:
        return ""
    # 分号分隔多文件：逐个查，把找不到的点名
    missing = [x.strip() for x in p.split(";") if x.strip() and not os.path.exists(x.strip())]
    if not missing:
        return ""
    return ("⚠️ 这些 cookies 文件**不存在**：" + "；".join(missing) +
            " —— 本次会按**无 cookies** 抓取（B站等站点只给 480P）。"
            "常见原因：浏览器下载同名文件时改名成 'xxx (1).txt'，配置里还指着旧路径。")
