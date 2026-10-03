# -*- coding: utf-8 -*-
"""cookies 多文件解析 + **Netscape cookies.txt 的唯一解析入口**

GUI/CLI 使用同一规则：分号或换行分隔均认；空行/# 注释忽略；返回去重后的路径列表。

[v7 新增·**只读查询**] 除原有解析/写出外，本模块现在还提供"来源逐项视图"：
    resolve_cookie_sources(explicit)  → 现在生效的每个 cookies 文件 + **它从哪来**
    cookie_paths_add_front(cur, new)  → 加一项（新值排最前，盖得住旧的）
    cookie_paths_drop(cur, path)      → 删一项
    default_cookie_file()             → 默认位置（只算路径）
这些是给设置页「Cookies 管理」用的：**界面显示的来源 = 引擎真正读的来源**
（同一个实现），否则就是"界面改了、引擎没变"。
"""
from typing import Dict, List, Optional


def parse_cookie_file_list(text) -> List[str]:
    """用户输入（多行或分号分隔）→ 路径列表（去重、保序；./~ 不展开——按原样交文件层）。"""
    out, seen = [], set()
    for chunk in str(text or "").replace("\r", "\n").replace(";", "\n").split("\n"):
        p = chunk.strip().strip("\"'")
        if not p or p.startswith("#"):
            continue
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


# ══════════════════════════════════════════════════════════════════════
# Netscape cookies.txt 的**唯一解析实现**
#
# [v6 修复·**真机取证后收敛**] 此前全工程有 **9 处**各自写了一遍这个解析：
#     comment_danmaku.py:56   cookie_armory.py:458   cookie_health.py:55, 90
#     douyin_resolver.py:39   identity_session.py:90
#     universal_downloader.py:207, 523, 599
# 逐处抠源码比对：**9/9 全是同一句** `if not s or s.startswith("#"): continue`
# ⇒ **全都跳过 `#HttpOnly_` 开头的行**。
#
# 为什么这是真 bug：Netscape 格式里 **HttpOnly 的 cookie 就是以 `#HttpOnly_` 开头的**：
#     #HttpOnly_.bilibili.com<TAB>TRUE<TAB>/<TAB>TRUE<TAB><exp><TAB>SESSDATA<TAB><value>
# 而 **B站的 `SESSDATA` 恰好是 HttpOnly** ⇒ 拿这种文件时**登录态会被静默丢掉**，
# 表现为"文件看着是好的、服务端却说未登录"（正是本工程吃过的 `code=-101`）。
#
# 机主当前那份 hand-export 的文件**没有** `#HttpOnly_`（实测 0 行）所以没事；
# 但 **yt-dlp 的 `YoutubeDLCookieJar` 与多数浏览器扩展都会写它** ——
# 一旦换用那种导出方式，这个 bug 立刻发作，且**一声不响**。
# ══════════════════════════════════════════════════════════════════════

def _iter_netscape_lines(text: str):
    """逐行产出 `(domain, name, value, path, secure, expires)`，**含 HttpOnly 行**。

    跳过的是**真注释**（`# Netscape HTTP Cookie File` 这类），
    而不是"所有 `#` 开头的行" —— 后者会把 HttpOnly cookie 一起吃掉。
    """
    for raw in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        s = raw.strip()
        if not s:
            continue
        httponly = False
        if s.startswith("#HttpOnly_"):
            # ← **关键**：这是**数据行**，不是注释。剥掉前缀后照常解析。
            httponly = True
            s = s[len("#HttpOnly_"):]
        elif s.startswith("#"):
            continue          # 真注释（文件头那三行）
        parts = s.split("\t")
        if len(parts) < 7:
            continue
        yield {
            "domain": parts[0],
            "flag": parts[1],
            "path": parts[2],
            "secure": parts[3],
            "expires": parts[4],
            "name": parts[5],
            "value": parts[6],
            "httponly": httponly,
        }


def parse_netscape_cookies(text: str) -> List[Dict[str, object]]:
    """Netscape cookies.txt 文本 → cookie 字典列表（**含 HttpOnly**）。"""
    return list(_iter_netscape_lines(text))


def netscape_to_header(text: str) -> str:
    """Netscape cookies.txt 文本 → `k=v; k2=v2`（请求头用的形态）。"""
    return "; ".join(f"{c['name']}={c['value']}" for c in _iter_netscape_lines(text))


def parse_cookie_file_text(path: str) -> str:
    """读一个 cookies.txt → `k=v; k2=v2`。**唯一入口，别处不要再自己 split('\\t')。**"""
    with open(path, encoding="utf-8-sig", errors="ignore") as f:
        return netscape_to_header(f.read())


# ══════════════════════════════════════════════════════════════════════
# **cookies 来源解析的唯一实现**
#
# [v6 修复·**又一处"同一能力两份实现"**] 这段逻辑此前**逐字重复**在两处：
#     `cookie_health._cookie_files()`
#     `universal_downloader._cookie_sources()`
# 两处都是：KIANA_COOKIE_FILES → KIANA_COOKIE_FILE → 默认 cookies.txt。
# 后果：**任何改动都得改两遍**，漏一处就静默分叉
# （这正是本工程连续九轮里出事最多的模式）。
#
# [v6 新增] 顺带补上**持久化配置档的自动发现**：
# `tools/cookie_login.py` 把登录态导到 `<数据根>/profiles/<站>/cookies.txt`，
# 但引擎原先**只认环境变量** ⇒ "文件就位了但引擎看不见"（接线缺口）。
# 现在配置档里的 cookies **自动参与**，用户不用手工配环境变量。
# ══════════════════════════════════════════════════════════════════════

def profiles_cookie_files() -> List[str]:
    """扫描持久化配置档目录 → 其中的 `cookies.txt`（按站点名排序，保证确定性）。

    **为什么自动带上**：配置档登录是本工程推荐的拿登录态方式
    （`kiana_vnext_plus/cookie_profile.py`）。若还要用户手工把路径填进环境变量，
    就等于"功能做了但没接上" —— 本工程吃过好几次这个亏。
    """
    import os
    from pathlib import Path
    root = os.environ.get("LOCALAPPDATA", "")
    if not root:
        return []
    base = Path(root) / "KianaVnextPlus" / "profiles"
    if not base.is_dir():
        return []
    out: List[str] = []
    try:
        for d in sorted(base.iterdir(), key=lambda p: p.name):
            if not d.is_dir():
                continue
            f = d / "cookies.txt"
            try:
                if f.is_file() and f.stat().st_size > 0:
                    out.append(str(f))
            except OSError:
                continue
    except OSError:
        return []
    return out


# ══════════════════════════════════════════════════════════════════════
# **cookies 来源的"逐项视图" —— 给"管理多个 cookies"的界面用**
#
# [v7 新增·**只读查询，不改任何既有语义**]
#
# 背景：引擎一直就支持**多份** cookies（`KIANA_COOKIE_FILES` 分号/换行分隔，
# `_ensure_cookie_file` 合并时**先到先得**）。缺的从来不是引擎能力，而是
# **界面**：用户看不到"现在到底在读哪几个文件、每个从哪来、哪个是摆设"，
# 于是"我在界面上加了它"和"引擎真的用了它"是两件事 —— 本工程反复吃亏的模式。
#
# 所以这里把上面那条优先级链**拆成带来源标注的列表**，
# 供 GUI 逐项展示 + 增删。`cookie_source_files()` 转发到同一个实现：
# **"谁是生效来源"全工程只有这一处判断**，界面与引擎不可能各算一套。
# ══════════════════════════════════════════════════════════════════════

_SOURCE_EXPLICIT = "explicit"     # 用户显式指定（GUI 框 / KIANA_COOKIE_FILES）
_SOURCE_FILE_ENV = "file_env"     # KIANA_COOKIE_FILE（单文件，兼容旧写法）
_SOURCE_DEFAULT = "default"       # 默认位置 <数据根>/cookies.txt
_SOURCE_PROFILE = "profile"       # 持久化配置档 profiles/*/cookies.txt（兜底自动发现）

#: 来源种类 → 给人看的中文标注（GUI 直接引用；编在界面里就会与这里漂移）
SOURCE_ORIGIN_LABEL = {
    _SOURCE_EXPLICIT: "显式指定",
    _SOURCE_FILE_ENV: "环境变量 KIANA_COOKIE_FILE",
    _SOURCE_DEFAULT: "默认位置",
    _SOURCE_PROFILE: "配置档（自动发现）",
}


def default_cookie_file() -> str:
    """默认位置的 cookies.txt 路径（**不判存在**，只算路径）。

    与 `cookie_source_files()` 原来内联那条 `Path(LOCALAPPDATA)/.../cookies.txt`
    是同一个位置 —— 抽出来是为了让"界面能显示它"而不必再写一遍路径拼法。
    """
    import os
    import pathlib
    return str(pathlib.Path(os.environ.get("LOCALAPPDATA", ""))
               / "KianaVnextPlus" / "cookies.txt")


def _source_row(path: str, origin: str, detail: str = "") -> Dict[str, object]:
    """一条来源记录：路径 + 从哪来 + **这个文件现在到底在不在**。

    `exists` 必须如实探测：界面要能区分"配了但文件没了"（引擎会按无 cookies 跑）
    与"配了且在用" —— 这两者的界面表现必须不同，否则就是老毛病
    "看着配好了、其实全程没生效"。
    """
    import os
    p = str(path or "")
    try:
        ok = bool(p) and os.path.exists(p)
        size = os.path.getsize(p) if ok else 0
    except OSError:
        ok, size = False, 0
    return {"path": p, "origin": origin,
            "origin_label": SOURCE_ORIGIN_LABEL.get(origin, origin),
            "detail": detail, "exists": ok, "size": size}


def _sources_from_explicit(explicit: str) -> List[Dict[str, object]]:
    """已确定的"显式值" → 逐项来源列表（**不含环境变量读取**，纯函数，可单测）。

    优先级链照原 `cookie_source_files()`，**但第 ③④ 档已改成"合并"**（见下）：
      ① 显式多文件（分号/换行分隔，保序）
      ② 环境变量 `KIANA_COOKIE_FILE`（单文件）
      ③④ **[合并] 配置档 `profiles/*/cookies.txt` + 默认位置（存在才算）**

    ⚠️ ①② 是**用户显式指定**：一旦有任何一项就**不再追加**后面那些
    （尊重用户选择）。这条语义原样保留 —— 改了它就会变成
    "我没让它读那个，它却读了"，比不做更糟。

    ══ [v6 修复·**真机踩到的"影子文件"**] ══════════════════════════════
    第 ③④ 档原来写成"默认位置存在就 return 它，配置档永远轮不到"。**那是错的**：
    机主机器上躺着一个 **9 天前的失效 `%LOCALAPPDATA%\\KianaVnextPlus\\cookies.txt`**
    （实测服务端答 `code=-101 isLogin=False`），而他刚用 GUI 登录成功的新配置档
    （`profiles/bilibili/cookies.txt`，实测 `isLogin=True vip=1`）
    **被那个旧文件整个挡住了** ⇒ 表现就是"**登录了但还是 480P**"。

    **为什么第一版会写成那样**：把"默认位置"误当成了"用户指定"。
    **它不是** —— 用户没配任何环境变量时，这两个**都只是"我们发现的地方"**，
    地位相同，凭什么一个压另一个。
    ⇒ 两处**合并**，**配置档排前面**（本工程推荐的、GUI 刚写出的、通常最新那份）；
      引擎侧合并是**先到先得**，所以配置档的同名 cookie 会赢。
    """
    rows: List[Dict[str, object]] = [_source_row(p, _SOURCE_EXPLICIT)
                                     for p in parse_cookie_file_list(explicit)]
    if rows:
        return rows
    import os
    one = os.environ.get("KIANA_COOKIE_FILE")
    if one:
        return [_source_row(one, _SOURCE_FILE_ENV)]
    # ③④ 合并（顺序即优先级：配置档在前）
    merged: List[Dict[str, object]] = [
        _source_row(p, _SOURCE_PROFILE, detail="配置档（自动发现）")
        for p in profiles_cookie_files()]
    default = default_cookie_file()
    if default and os.path.exists(default):
        merged.append(_source_row(default, _SOURCE_DEFAULT))
    return merged


def resolve_cookie_sources(explicit=None) -> List[Dict[str, object]]:
    """**"引擎现在到底会读哪几个 cookies 文件" —— 唯一入口（带来源标注）。**

    `explicit` 给了就用它当"显式值"（GUI 传输入框的当前文本），
    不给就读进程环境 `KIANA_COOKIE_FILES`（= 引擎侧的真实视角）。

    **为什么要有这个参数**：GUI 里输入框的值**还没落进环境变量**（env 是
    `EngineBridge.start()` 那一刻才写的），若只能读 env，界面就会显示上一次任务的
    cookies —— 那是**假信息**，比不显示更糟。

    返回 `list[dict]`，每项：
        path / origin（见 `SOURCE_ORIGIN_LABEL`）/ origin_label / detail
        / exists / size

    本函数**只读磁盘**：不开浏览器、不联网、不写文件、不改环境变量。
    """
    import os
    if explicit is None:
        explicit = os.environ.get("KIANA_COOKIE_FILES") or ""
    return _sources_from_explicit(str(explicit or ""))


def cookie_paths_add_front(cur: str, new_path: str) -> str:
    """把一份 cookies 路径并进当前值，**新值排最前**（已在列表里也会被**移到最前**）。

    ⚠️ 顺序不是随手定的：引擎合并多份 cookies 时是**先到先得**
    （`universal_downloader._ensure_cookie_file`：`if key in seen: continue`），
    所以排在前面那一份才盖得住同名的旧 SESSDATA。

    [v7 修复·离屏测试抓到的真 bug] 原来写成"已在列表里就**原样返回**"（幂等）。
    那是**错的**：这样"把 B 提到最前"这个操作对 B 完全无效，
    而界面上的「设为最优先」按钮**正是**这条语义 ——
    用户点下去只会看到"已经在最前面了"（假话），顺序一点没变。
    屏上表现是"按钮没用"，排查起来却像是点击没接上。

    现在改成**移动语义**（先摘掉、再插到最前）：
      · 已在最前 → 原样返回（**这一种**才是真幂等，重复调用不会产生重复项）；
      · 在中间/末尾 → 提到最前；
      · 不在列表里 → 插到最前。
    "不会产生重复项"这条保证没变（`parse_cookie_file_list` 去重 + 先摘后插）。

    （`cookie_profile.merge_cookie_files` 是**后到覆盖前者** —— 两处语义相反，
    以**引擎实际消费的那条**为准，别照那个改这个。）
    """
    new_path = str(new_path or "").strip()
    cur = str(cur or "").strip()
    if not new_path:
        return cur
    if not cur:
        return new_path
    cur_list = parse_cookie_file_list(cur)
    if cur_list[:1] == [new_path]:
        # 已在最前 → **原样返回**（连分隔符都不碰）。
        # 为什么要这一句：返回值会被 `setText()` 写回输入框，
        # 而"换行分隔"也是引擎认的写法 —— 无脑重建会把它规范成单行，
        # 用户手写的多行清单会在一次"刷新"之后变形。
        return cur
    rest = [p for p in cur_list if p != new_path]
    return ";".join([new_path] + rest)


def cookie_paths_drop(cur: str, path: str) -> str:
    """从当前值里移除一项（分号/换行分隔都认，与引擎同一套解析）。

    **不猜、不做模糊匹配**：只有原样的路径才算命中 —— "看着像就删"
    会删掉用户另一份文件，而他不会立刻发现（引擎会静默降级到更少 cookies）。

    返回删干净后的值（可能为空串 = 改回"什么都不配"，引擎恢复自动兜底）。
    """
    target = str(path or "").strip()
    if not target:
        return str(cur or "").strip()
    rest = [p for p in parse_cookie_file_list(cur) if p != target]
    return ";".join(rest)


def cookie_source_files() -> List[str]:
    """**cookies 文件来源列表 —— 全工程唯一入口。**

    优先级（与改前一致，只在"用户什么都没配"时**兜底**，不改既有语义）：
      ① `KIANA_COOKIE_FILES`（分号/换行分隔，多文件）
      ② `KIANA_COOKIE_FILE`（单文件）
      ③ 默认 `<LOCALAPPDATA>/KianaVnextPlus/cookies.txt`
      ④ 持久化配置档 `<LOCALAPPDATA>/KianaVnextPlus/profiles/*/cookies.txt`

    [v7] 本函数**转发**到 `resolve_cookie_sources()` —— 那条优先级链的判定
    只有一份实现。界面要展示"每个来源从哪来"时读的也是它，
    于是"界面显示的"与"引擎读的"不可能分叉（本工程最稳定的缺陷模式就是同一能力两份实现）。
    """
    return [str(r["path"]) for r in resolve_cookie_sources()]


# ══════════════════════════════════════════════════════════════════════
# **写出** Netscape cookies.txt —— 字节级规格照 yt-dlp 的解析器对齐
#
# [v6] 规格出处：`yt_dlp/cookies.py` 的 `YoutubeDLCookieJar`（实读源码 + 三个官方
# fixture 逐字节比对），不是凭记忆：
#   · `_ENTRY_LEN = 7` —— **7 列，不是 9 列**（9 列是 curl 的格式，
#     yt-dlp 读到会 `LoadError: invalid length`）。**这是最容易翻车的一点。**
#   · 列序：domain / include_subdomains / path / https_only / expires / name / value
#   · `include_subdomains` 与 `https_only` 是**大写** TRUE/FALSE
#   · `include_subdomains` **由 domain 是否以 `.` 开头推导**，不是独立字段
#   · session cookie 的 expires 写 `0`（yt-dlp 两种写法都认：空串或 `0`）
#   · **HttpOnly 的 cookie 要在域名前加 `#HttpOnly_`**
#     —— B站的 `SESSDATA` 正是 HttpOnly；不加前缀的话，
#        **严格解析器会把整行当注释丢掉**（本工程就栽过，见文件上方注释）
#   · 行尾 **CRLF**、编码 **UTF-8**
# ══════════════════════════════════════════════════════════════════════

_NETSCAPE_HEADER = (
    "# Netscape HTTP Cookie File\n"
    "# 本文件由 Kiana Vnext Plus 生成（Netscape 格式，7 列）。\n"
    "# 格式：domain / include_subdomains / path / https_only / expires / name / value\n"
    "\n"
)


def _pick(d: dict, *names, default=None):
    """从 dict 里按**多个候选键名**取第一个非 None 的值。

    [v6 修复·**必然踩的集成坑**] 各家 cookie 字典的键名**不一致**：
      · Playwright / patchright 的 `ctx.cookies()` → **驼峰**：`httpOnly`
      · 本工程的 `parse_netscape_cookies()`          → **下划线**：`http_only`
      · yt-dlp 的 `http.cookiejar.Cookie`            → 属性 `has_nonstandard_attr`
    实测（真起 patchright 读回）：
        `ctx.cookies()` 返回 `{'domain','expires','httpOnly','name','path',
                               'sameSite','secure','value'}`
    若只认 `http_only`，**把 `ctx.cookies()` 直接喂进来就会丢掉 HttpOnly 标记**
    ⇒ `SESSDATA` 没有 `#HttpOnly_` 前缀 ⇒ 严格解析器整行丢掉。
    **那正是本工程刚花几轮修掉的那个 bug —— 换个入口又回来了。**
    所以这里**容忍两种写法**，而不是要求调用方先转换（转换点越多越容易漏）。
    """
    for n in names:
        v = d.get(n)
        if v is not None:
            return v
    return default


def write_netscape_cookies(cookies, path: Optional[str] = None) -> str:
    """把 cookie 列表写成 Netscape cookies.txt 文本（**并可选落盘**），返回文本。

    `cookies` 每项是 dict。**键名容忍两套写法**（见 `_pick` 的说明）：
        name(*)  value(*)  domain(*)
        path（默认 `/`）  secure（默认 False）  expires（默认 0=session）
        http_only / httpOnly（默认 False）—— 为 True 时域名前加 `#HttpOnly_`

    ⚠️ **路径/域名里的 TAB 与换行会被剔除** —— 它们会**撕裂列结构**，
    让整行变成畸形行被解析器跳过（比写错更隐蔽）。
    """
    lines = [_NETSCAPE_HEADER]
    for c in (cookies or []):
        if not isinstance(c, dict):
            continue
        name = str(_pick(c, "name", default="") or "").replace("\t", "").replace("\n", "")
        value = str(_pick(c, "value", default="") or "").replace("\t", "").replace("\n", "")
        if not name:
            continue                      # 无名 cookie 写出去也是畸形行
        domain = str(_pick(c, "domain", default="") or "").replace("\t", "").replace("\n", "")
        if not domain:
            continue
        path_ = str(_pick(c, "path", default="/") or "/").replace("\t", "").replace("\n", "") or "/"
        # include_subdomains **由域名前导点推导**（与 yt-dlp `_really_save` 一致）
        include_sub = "TRUE" if domain.startswith(".") else "FALSE"
        https_only = "TRUE" if _pick(c, "secure", default=False) else "FALSE"
        exp = _pick(c, "expires", default=0)
        try:
            exp = int(exp) if exp else 0
        except (TypeError, ValueError):
            exp = 0
        # [v6 修复·**实测发现的第二个集成坑**] Playwright 对 **session cookie**
        # 返回的是 **`expires = -1`**（不是 0、也不是空）。
        # 而 yt-dlp 对 expires 的校验是 `[0-9]+(?:\.[0-9]+)?` —— **负数不合法**，
        # 它会 `WARNING: skipping cookie file entry due to invalid expires at -1`
        # **把整行丢掉**。实测确认（用真实 Playwright 输出形状喂进去时复现）。
        # ⇒ 负数一律归 0（Netscape 里 `0` 就是 session cookie 的标准写法）。
        if exp < 0:
            exp = 0
        # ← **两种键名都认**（Playwright 给 `httpOnly`，本工程内部给 `http_only`）
        prefix = "#HttpOnly_" if _pick(c, "http_only", "httpOnly", default=False) else ""
        lines.append(
            f"{prefix}{domain}\t{include_sub}\t{path_}\t{https_only}\t{exp}\t"
            f"{name}\t{value}\n"
        )
    text = "".join(lines)
    if path:
        # **显式写 CRLF**：newline="" 关掉 Python 的自动转换，由我们自己控制行尾，
        # 这样无论在哪台机器、哪种打开方式，落盘字节都一致。
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text.replace("\n", "\r\n"))
    return text
