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


def extract_domain(url):
    netloc = urlparse(url).netloc.lower()
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
            return resp
        # 中间跳响应必须关闭（流式模式下不关会泄漏连接）
        try:
            resp.close()
        except Exception:
            pass
        if _hops >= max_hops:
            logger.warning(f"safe_get 重定向超过 {max_hops} 跳上限，放弃: {_cur[:70]}")
            return resp
        _loc = (resp.headers.get("Location") or resp.headers.get("location") or "")
        if not _loc:
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
