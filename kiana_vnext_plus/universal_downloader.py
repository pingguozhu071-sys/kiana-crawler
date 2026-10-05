"""Universal Media Download Engine — yt-dlp backed, supports all video/image platforms"""
import os
import re
import json
import time
import asyncio
import logging
import pathlib
import tempfile
import hashlib
import threading
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
import aiofiles


def is_bilibili_url(url: str) -> bool:
    """[v2.19.10 新增] 这个 URL 是不是 B站 的？

    抽成模块级函数是为了**可测**：原判断内联在一个几百行的下载方法里，
    修「B站 结论串台到别的域名」时根本没法写回归测试。
    只认精确后缀（不认子串）—— 「子串冒充域名归属」是本工程踩过的坑。
    """
    host = (urlparse(url).netloc or "").lower()
    return (host == "bilibili.com" or host.endswith(".bilibili.com")
            or host == "b23.tv" or host.endswith(".b23.tv"))
# [FIXED & MODIFIED] aiohttp 在本机 hostname 解析层卡死（外网全部超时，urllib/curl_cffi 正常）
# → 图片/音频下载改用 curl_cffi（引擎主抓取已验证可用）

logger = logging.getLogger(__name__)

# [FIXED & MODIFIED] B站会员 cookies 自动探测（v2.4.0 起只认 cookies.txt 文件）：
# 新版 Chrome/Edge 的 App-Bound 加密使 yt-dlp 无法解密浏览器库（DPAPI 失败，实测两台浏览器均如此）
# → 浏览器扫描功能已移除（不留尸体）。用户可在 GUI 设置中指定 cookies.txt 路径（换设备只需重新导出文件）。
# [FIXED & MODIFIED] v2.10.5 多密钥自填：KIANA_COOKIE_FILES（分号分隔多个 cookies.txt，
# 各站独立导出）→ 按域合并为运行时单文件（yt-dlp 按域匹配）。未设置则回退
# KIANA_COOKIE_FILE / 默认位置。密钥从不在源码/包体中出现，仅由用户自填。
_COOKIE_FILE = None          # 运行时合并产物路径（懒生成）
_COOKIE_SRC_DIR = None       # 已合并的源文件签名（失效则重合并）
_cookie_file_cache = None    # B站 cookies 探测缓存


def cleanup_cookie_file():
    """[FIXED & MODIFIED] v2.11 删除 %TEMP% 合并 cookies 并清缓存（爬完即调——明文密钥不跨任务驻留）"""
    global _COOKIE_FILE, _COOKIE_SRC_DIR
    if _COOKIE_FILE is not None:
        try:
            _COOKIE_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        _COOKIE_FILE = None
        _COOKIE_SRC_DIR = None


# ═══ [v2.16.1] YouTube PO Token server runtime 接线 ═══
# 背景：v2.16.0 发布声称"PO Token 随包"——spec 支持可选打入 POT_SERVER_DIR/DENO_EXE，
# 但 runtime 的 pot_server_ensure/pot_server_ready 从未落地（tools/yt_download.py 直接
# import 必 ImportError，主引擎也无拉起逻辑）→ YouTube 媒体流 403 无解。这里补齐：
# 只做"探测 :4416 → 用随包/工程目录/PATH 的 node 拉起 build/main.js"，绝不安装任何
# 系统工具（红线：运行时只认已有资源）。插件 bgutil-ytdlp-pot-provider（pip 已 pin）
# 默认连 http://127.0.0.1:4416——server 起来后 yt-dlp 自动生效，无需 extractor_args。
POT_SERVER_HOST = "127.0.0.1"
POT_SERVER_PORT = 4416
_pot_proc = None


def pot_server_ready() -> bool:
    """TCP 探测 PO Token server（127.0.0.1:4416）是否就绪（0.3s 超时）。"""
    try:
        import socket as _sk
        with _sk.create_connection((POT_SERVER_HOST, POT_SERVER_PORT), timeout=0.3):
            return True
    except OSError:
        return False


def _pot_server_dirs() -> list:
    """候选 pot server 目录（须含 build/main.js）：_MEIPASS/pot_server/server →
    _MEIPASS/pot_server → 工程目录 pot_server → 环境变量 POT_SERVER_DIR。"""
    import sys as _sys
    cands = []
    base = getattr(_sys, "_MEIPASS", None)
    if base:
        cands.append(pathlib.Path(base) / "pot_server" / "server")
        cands.append(pathlib.Path(base) / "pot_server")
    cands.append(pathlib.Path(__file__).resolve().parent.parent / "pot_server")
    env_dir = os.environ.get("POT_SERVER_DIR")
    if env_dir:
        cands.append(pathlib.Path(env_dir) / "server")
        cands.append(pathlib.Path(env_dir))
    out, seen = [], set()
    for c in cands:
        try:
            key = str(c.resolve())
        except Exception:
            key = str(c)
        if key in seen:
            continue
        seen.add(key)
        try:
            if (c / "build" / "main.js").exists():
                out.append(c)
        except Exception:
            continue
    return out


def pot_server_ensure() -> bool:
    """确保 PO Token server 运行：探测 → 未起则用随包/PATH 的 node 拉起（幂等）。
    返回 True 表示就绪；找不到 node/server 时仅告警（不下载、不安装任何东西）。"""
    global _pot_proc
    if pot_server_ready():
        return True
    if _pot_proc is not None:
        try:
            if _pot_proc.poll() is None:
                return True  # 本进程拉起的还活着
        except Exception:
            pass
    import shutil as _sh
    import subprocess as _sp
    for srv in _pot_server_dirs():
        node = srv / "node.exe"
        node = node if node.exists() else _sh.which("node")
        if not node:
            continue
        try:
            flags = getattr(_sp, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            proc = _sp.Popen([str(node), str(srv / "build" / "main.js")],
                             cwd=str(srv), stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                             creationflags=flags)
            for _ in range(20):
                if pot_server_ready():
                    _pot_proc = proc
                    try:
                        import atexit as _atexit
                        _atexit.register(lambda p=proc: p.terminate() if p.poll() is None else None)
                    except Exception:
                        pass
                    logger.info(f"PO Token server 已拉起: {srv}")
                    return True
                if proc.poll() is not None:
                    break
                time.sleep(0.25)
            logger.warning(f"PO Token server 启动后未就绪（5s 超时）: {srv}")
        except Exception as _e:
            logger.warning(f"PO Token server 启动失败 ({srv}): {_e}")
    logger.warning("PO Token server 不可用（无随包 node/server）——YouTube 媒体流可能 403")
    return False


def _cookie_sources() -> list:
    """cookie 文件来源列表 —— **委托给唯一入口 `cookie_utils.cookie_source_files()`**。

    [v6 修复·**同一能力两份实现**] 这段逻辑原来在**本函数**与
    `cookie_health._cookie_files()` 里**逐字重复**了一遍。
    现统一到一个入口，本函数只转发。理由与完整说明见 `cookie_utils` 里的注释。

    顺带：唯一入口**新增了持久化配置档的自动发现**（`profiles/*/cookies.txt`）
    ⇒ 用户用 `tools/cookie_login.py` 登录过之后，**这里自动就能看见**，
    不需要再手工配环境变量（那正是原来的接线缺口）。
    """
    from .cookie_utils import cookie_source_files
    return cookie_source_files()


_COOKIE_COPY_DIR = None       # [v2.19.10] 每次下载用的 cookies 副本目录（懒建，退出即删）


def _cookie_copy(src):
    """[v2.19.10 新增] 给**单个下载**一份 cookies 副本 —— 修并发回写竞争。

    为什么必须"一人一份"：**yt-dlp 会回写**传给它的 `cookiefile`
    （文件头会变成 `# This file is generated by yt-dlp. Do not edit.`）。
    并发多路下载共用同一路径时，会读到**写了一半**的文件 —— 真机实测（2026-10-05，
    18 集并发下载）报：

        ERROR: '...\\kiana_cookies_foedldnl.txt' does not look like a Netscape format cookies file

    ⇒ 那两次下载**静默地没带上登录态**（画质降级的隐患；本工程最忌"静默"）。

    副本约 3.6 KB，统一放进一个临时目录，进程退出时整目录删除。
    """
    global _COOKIE_COPY_DIR
    if _COOKIE_COPY_DIR is None:
        _COOKIE_COPY_DIR = pathlib.Path(tempfile.mkdtemp(prefix="kiana_ck_"))
        try:
            import atexit as _atexit
            import shutil as _shutil
            _atexit.register(lambda: _shutil.rmtree(_COOKIE_COPY_DIR, ignore_errors=True))
        except Exception:
            pass
    _fd, _name = tempfile.mkstemp(prefix="ck_", suffix=".txt", dir=str(_COOKIE_COPY_DIR))
    os.close(_fd)
    dst = pathlib.Path(_name)
    dst.write_bytes(pathlib.Path(src).read_bytes())
    return dst


def _ensure_cookie_file() -> Optional[pathlib.Path]:
    """将多个源 cookie 文件合并为单文件（幂等；源文件签名不变不重合并）。

    **返回 `None` 表示"没有可用 cookies"**（没配 / 配的路径全都不存在），
    调用方必须按"无 cookies"降级 —— 标注写成 `Optional` 而不是 `Path`：
    本函数**到处** `return None`，原来标注 `-> pathlib.Path` 是错的
    （mypy 一直有报，只是没归零）。
    """
    global _COOKIE_FILE, _COOKIE_SRC_DIR
    srcs = _cookie_sources()
    if not srcs:
        return None
    # [v6 修复·真机实测踩到] **配置了路径但文件不存在** ≠ "没配 cookies"。
    # 用户实测：界面上 cookies 填的是 `www.bilibili.com_cookies.txt`，
    # 但浏览器下载新文件时改名成了 `www.bilibili.com_cookies (1).txt` ——
    # 旧路径失效 → 这里静默返回 None → 日志只说"未检测到 B站 cookies" →
    # **实际后果是全套 480P**，而用户以为自己配好了。
    # 现在把"哪个文件找不到"明确说出来（原来只有一句含糊的"未检测到"）。
    _missing = [s for s in srcs if not pathlib.Path(s).exists()]
    if _missing:
        logger.error(
            "⚠️ cookies **路径已配置但文件不存在**：%s —— 本次按**无 cookies** 抓取"
            "（B站等站点会因此只给 480P）。常见原因：浏览器下载同名文件时改名成 "
            "'xxx (1).txt'，而配置里还指着旧路径。请到设置里重新选一次文件。",
            "；".join(_missing))
    if len(_missing) == len(srcs):
        return None            # 配的全都找不到 → 确实无可用 cookies
    # 签名 = 全部源文件 (路径, mtime, size) 串联 hash —— 任一文件更新即触发重新合并
    sig_parts = []
    for sp in srcs:
        p = pathlib.Path(sp)
        try:
            st = p.stat()
            sig_parts.append(f"{sp}|{st.st_mtime}|{st.st_size}")
        except OSError:
            sig_parts.append(f"{sp}|missing")
    sig = "\n".join(sig_parts)
    if (_COOKIE_FILE is not None and _COOKIE_SRC_DIR and _COOKIE_SRC_DIR == sig
            and _COOKIE_FILE.exists()):
        return _COOKIE_FILE
    from .cookie_utils import parse_netscape_cookies
    lines, seen = [], set()
    for sp in srcs:
        if not os.path.exists(sp):
            continue
        try:
            raw = pathlib.Path(sp).read_text(encoding="utf-8-sig", errors="ignore")
        except Exception:
            continue
        for c in parse_netscape_cookies(raw):
            key = (c["domain"], c["name"])
            if key in seen:
                continue
            seen.add(key)
            # 按 (domain, name) 去重后重建标准 7 字段行；HttpOnly 行必须带回
            # `#HttpOnly_` 前缀（下游 yt-dlp 按数据行认，丢了前缀=丢了 HttpOnly 标记）
            mark = "#HttpOnly_" if c["httponly"] else ""
            lines.append(f"{mark}{c['domain']}\t{c['flag']}\t{c['path']}\t"
                         f"{c['secure']}\t{c['expires']}\t{c['name']}\t{c['value']}")
    if not lines:
        return None
    tmp_dir = pathlib.Path(os.environ.get("TEMP", "")) or pathlib.Path(tempfile.gettempdir())
    # [FIXED & MODIFIED] v2.11 隐私加固：mkstemp 随机文件名（原固定 kiana_cookies_merged.txt
    # 可被同机其他进程预测读取）+ 启动时清理历史残留（崩溃/强杀遗留的旧合并文件）
    try:
        for _old in tmp_dir.glob("kiana_cookies_*.txt"):
            try:
                _old.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception:
        pass
    _body = ("# Netscape HTTP Cookie File\n# Generated by Kiana (user cookies merged)\n"
             + "\n".join(lines) + "\n")
    try:
        _fd, _tmp_name = tempfile.mkstemp(prefix="kiana_cookies_", suffix=".txt", dir=str(tmp_dir))
        os.close(_fd)
        out = pathlib.Path(_tmp_name)
        out.write_text(_body, encoding="utf-8")
    except Exception:
        out = tmp_dir / "kiana_cookies_merged.txt"
        out.write_text(_body, encoding="utf-8")
    # [FIXED & MODIFIED] v2.10.5 合并产物含全部用户密钥明文 → atexit 兜底删除
    # （正常路径爬完即删走 cleanup_cookie_file，此处只兜底进程退出）
    try:
        import atexit as _atexit
        _atexit.register(cleanup_cookie_file)
    except Exception:
        pass
    _COOKIE_FILE = out
    _COOKIE_SRC_DIR = sig
    return out


def _ffmpeg_dir():
    """v2.10.4 自包含：定位打包的 ffmpeg/ffprobe（PyInstaller _MEIPASS → 程序目录 → 系统 PATH）
    换机即用——不再依赖用户系统安装 ffmpeg
    [FIXED & MODIFIED] v2.10.4 源码运行兜底：以上都找不到时用 shutil.which 探测 PATH
    （npm/nix/手动 PATH 的 ffmpeg 也能用；全没有 → 返回 pathlib.Path("") 由 yt-dlp 报错）"""
    try:
        import sys
        base = getattr(sys, "_MEIPASS", None)
        if base:
            p = pathlib.Path(base) / "bin"
            if (p / "ffmpeg.exe").exists():
                return p
            p2 = pathlib.Path(base)
            if (p2 / "ffmpeg.exe").exists():
                return p2
        exe_dir = pathlib.Path(sys.executable).parent
        if (exe_dir / "ffmpeg.exe").exists():
            return exe_dir
        if (exe_dir / "bin" / "ffmpeg.exe").exists():
            return exe_dir / "bin"
        # 源码运行兜底：探测系统 PATH（ffmpeg/ffprobe 同目录）
        import shutil
        ffm = shutil.which("ffmpeg")
        if ffm:
            return pathlib.Path(ffm).parent
    except Exception:
        pass
    return pathlib.Path("")


# ════════════════════════════════════════════════════════════════════════════
# [本轮修复·真机日志 2026-10-03] **低速卡死看门狗**
# —— 本工程判断"该不该放弃一个正在下载的传输"的**唯一实现**。
#
# **真机证据**：那趟抓取 71 秒就把页面跑完了（`done=20 fail=1 skip=74`，进度条 100%），
# 之后又空转 217 秒，最后一行是：
#     ERROR: [download] Got error: 8606 bytes read, 2693189 more expected.
#                       Giving up after 10 retries
# 那是一个约 2.6MB 的分片，速度掉到 **7~15 KiB/s**。整句话是 **yt-dlp 自己的**，
# 而其中那个 "10" 就是本工程在 `download_video` 里显式写下的 `'retries': 10`
# （`'socket_timeout': 30` 也是本工程写的）。**工程侧此前没有任何"速度/时长"上限**，
# 只有"重试多少次" —— 而重试次数对"慢"这件事完全不敏感：它只会让慢的东西慢 10 遍。
#
# **判据为什么是"持续低速"而不是"瞬时低速"**：
#   · 用户可能在**慢网络**上跑 —— 一次采样慢 ≠ 卡死，必须**整个观察窗内均速都低**；
#   · 窗口内的突发快传会把均值拉高，所以"抖一下"不会误杀；
#   · 先拿到 `STALL_MIN_BYTES_BEFORE_WATCH` 才开始判定 —— "压根连不上"是超时该管的事，
#     不该被本条判成"慢"。
#
# **阈值依据**：16 KiB/s ≈ 0.13 Mbps，低于任何可用的宽带/4G。
# 对照实测：卡死那条是 7~15 KiB/s（**判得出**）；而 128 KiB/s 的正常慢速网络，
# 窗内均速是阈值的 8 倍（**判不出，不会误杀**）。
# ════════════════════════════════════════════════════════════════════════════
STALL_WINDOW_SECONDS = 90.0               # 观察窗：必须"持续"这么久都慢才算卡死
STALL_MIN_RATE_BYTES_PER_S = 16 * 1024    # 窗内平均速率下限（16 KiB/s）
STALL_MIN_BYTES_BEFORE_WATCH = 64 * 1024  # 先拿到这么多字节才开始判定（连不上交给超时）


class LowSpeedAbort(RuntimeError):
    """低速看门狗判定"这条传输明显卡死"时抛出。

    单独一个类型、而不是复用 `TimeoutError`：调用方要能区分
    "网络抛的错"（值得重试/换源）与"**我们自己叫停的**"（换档、换源、重试都没用）。
    """


class StallWatchdog:
    """按"观察窗内的平均速率"判定卡死：喂累计字节数，到点判定；判为卡死则抛 `LowSpeedAbort`。

    **两个下载原语共用同一份实现**（不许各写一份）：
      · `_stream_one`（直连/断点续传）：读循环里每收一块调 `note_bytes(累计字节)`；
      · yt-dlp：经 `ytdlp_progress_hooks()` 包进 `progress_hooks`，
        yt-dlp 每次进度回调给的就是 `downloaded_bytes`（累计值）。

    `clock` 可注入（默认 `time.monotonic`）—— 测试用假钟，不真等 90 秒。
    """

    def __init__(self, window: float = STALL_WINDOW_SECONDS,
                 min_rate: float = STALL_MIN_RATE_BYTES_PER_S,
                 min_bytes: int = STALL_MIN_BYTES_BEFORE_WATCH, clock=None):
        self.window = float(window)
        self.min_rate = float(min_rate)
        self.min_bytes = int(min_bytes)
        self._clock = clock or time.monotonic
        self._started = False
        self._window_start = 0.0
        self._window_bytes = 0
        self.aborted = False

    def note_bytes(self, downloaded_total) -> None:
        """喂入**累计**已下载字节数；判定为卡死则抛 `LowSpeedAbort`。

        三条防误判（缺一条都会误杀，见上方阈值说明）：
          ① 第一次调用只**定基线**，不判定；
          ② 字节数**变小**（换档/换源/新一轮尝试重新计数）→ 重定基线，不判定
             —— 否则 `gained` 会算成负数，一旦除法就成了"速度 -X KiB/s"，必误杀；
          ③ 总字节还不到 `min_bytes` 时，本窗判为"连不上"，只滑窗不叫停。
        """
        now = self._clock()
        total = int(downloaded_total or 0)
        if not self._started or total < self._window_bytes:
            # ① / ②：定基线（首次，或计数被重置）
            self._started = True
            self._window_start = now
            self._window_bytes = total
            return
        elapsed = now - self._window_start
        if elapsed < self.window:
            return                       # 窗还没满：不做判定（"持续"才作数）
        gained = total - self._window_bytes
        rate = gained / elapsed
        if total >= self.min_bytes and rate < self.min_rate:
            self.aborted = True
            raise LowSpeedAbort(
                f"低速卡死：{elapsed:.0f}s 内只收到 {gained} 字节"
                f"（均速 {rate / 1024:.1f} KiB/s < 阈值 {self.min_rate / 1024:.0f} KiB/s），"
                f"已放弃本传输（不再重试/换源）")
        # 这一窗合格 → 滑窗，从头再算
        self._window_start = now
        self._window_bytes = total


def ytdlp_progress_hooks(watchdog) -> list:
    """把看门狗接到 yt-dlp 的 `progress_hooks` 上（返回可直接塞进 opts 的列表）。

    ⚠️ 这里**故意不吞异常**：yt-dlp 的 `FileDownloader._hook_progress` 是直接遍历调用
    hook、外面没有 try（已读源码确认）——抛出去就会中断这次传输，这正是"叫停"。
    真正的判定仍以 `watchdog.aborted` 为准（万一某版 yt-dlp 把异常包了一层，
    调用方也不必靠异常类型/字符串去猜）。
    """
    def _hook(status):
        if (status or {}).get("status") == "downloading":
            watchdog.note_bytes(status.get("downloaded_bytes") or 0)
    return [_hook]


# ═══ [v2.16.1] 断点续传公共件（抄 bilili RemoteFile 模式——重写为 asyncio 版）═══
# 语义：探源 → .part 临时文件 + Range 续传 → 字节数自校验 → 原子改名；失败保留 .part
# 供下次续传。纯字节搬运（对象：文件→落盘之间零转码——原画质红线 R1：不允许任何重编码）。
async def probe_url(session, url: str, headers: dict, timeout=60):
    """下载前握手：HEAD（回退 GET）+ Range: bytes=0-4 → (total_size, resumable)。
    服务器不支持探测时返回 None（调用方退化为整段下载）。

    [v2.19.6 安全] SSRF 闸：本函数是共享下载原语，被多处 fallback 调用
    （download_audio / download_video / download_direct 等）——在此统一把关。"""
    from .url_utils import is_private_url
    if not url or not str(url).startswith(("http://", "https://")) or is_private_url(url, dns_check=True):
        logger.warning(f"probe_url SSRF/协议拦截: {str(url)[:70]}")
        return None
    for method_name in ("head", "get"):
        method = getattr(session, method_name, None)
        if method is None:
            continue
        try:
            _hdrs = dict(headers or {})
            _hdrs["Range"] = "bytes=0-4"
            # [v2.19.7 安全·扫描发现] 走 safe_get（逐跳复检落点）：
            # 原直接调用会被 302 绕过到内网（扫描员已用 loopback PoC 复现）
            from .url_utils import safe_get as _safe_get
            _m = "HEAD" if method_name == "head" else "GET"
            resp = await _safe_get(session, url, _hdrs, method=_m, timeout=timeout, stream=True)
            if resp is None:
                return None          # 被 SSRF 闸拦 / 请求失败
            try:
                if resp.status_code in (200, 206):
                    total = None
                    cr = (resp.headers or {}).get("Content-Range") or ""
                    m = re.match(r"bytes\s+0-4\s*/\s*(\d+)", cr)
                    if m:
                        total = int(m.group(1))
                    else:
                        _cl = (resp.headers or {}).get("Content-Length") or ""
                        total = int(_cl) if _cl.isdigit() else None
                    resumable = resp.status_code == 206 or (resp.headers or {}).get("Accept-Ranges") == "bytes"
                    return (total, resumable)
            finally:
                _ac = getattr(resp, "aclose", None)
                if _ac:
                    try:
                        await _ac()
                    except Exception:
                        pass
        except Exception:
            continue
    return None


async def stream_to_file(session, url: str, dest: pathlib.Path, headers: dict,
                         proxy: str = "", expect_total=None, retries: int = 3,
                         mirrors=()):
    """流式下载（断点续传 + 镜像回退）：.part + Range → 字节数自校验 → os.replace 原子落盘。
    返回最终路径或 None；失败保留 .part（下次续传）；服务器不支持 Range（返回 200）时
    自动作废旧 .part 从头下。
    [v2.17 3.2] mirrors：备用直链列表（参考 bilili RemoteFile 的 random.choice 模式，重写为
    asyncio 版）——主源失败（网络错/非 2xx/字节校验不符）时按序切换镜像，每源 1 次重试；
    全部失败保留 .part 供下一任务续（取字节数最大者继续），返回 None。

    [v2.19.6 安全] SSRF 闸：共享下载原语，统一把关（覆盖所有 fallback 调用方）。"""
    from .url_utils import is_private_url
    if not url or not str(url).startswith(("http://", "https://")) or is_private_url(url, dns_check=True):
        logger.warning(f"stream_to_file SSRF/协议拦截: {str(url)[:70]}")
        return None
    part = dest.with_suffix(dest.suffix + ".part") if dest.suffix else pathlib.Path(str(dest) + ".part")
    sources = [url] + [m for m in (mirrors or ()) if m and m != url]
    for _sidx, _src in enumerate(sources):
        if _sidx:
            logger.info(f"主源失败，切换镜像#{_sidx}: {str(_src)[:70]}")
        try:
            result = await _stream_one(session, _src, part, dest, headers, proxy,
                                       expect_total=expect_total, retries=retries)
            if result is not None:
                return result
        except Exception as _e:
            logger.debug(f"镜像#{_sidx} 异常: {_e}")
    return None


async def _stream_one(session, url: str, part, dest: pathlib.Path, headers: dict,
                      proxy: str = "", expect_total=None, retries: int = 1):
    """单源流式下载实现（probe 语义内嵌）：状态码非 2xx/字节不符 → None（切镜像）；"""
    for _try in range(retries):
        try:
            have = part.stat().st_size if part.exists() else 0
            _hdrs = dict(headers or {})
            if have > 0:
                _hdrs["Range"] = f"bytes={have}-"
            # [v2.19.7 安全·扫描发现] 走 safe_get（入口校验 + 手动逐跳复检落点）：
            # curl_cffi 默认跟随 30 跳，只在入口校验会被 302 绕过到内网
            from .url_utils import safe_get as _safe_get
            resp = await _safe_get(session, url, _hdrs, proxy=proxy or None,
                                   timeout=180, stream=True)
            if resp is None:
                return None      # 被 SSRF 闸拦 / 请求失败 → 该源放弃
            if have > 0 and resp.status_code == 200:
                # 服务器忽略了 Range（不支持续传）→ 作废旧部分从头下
                try:
                    resp.close()
                except Exception:
                    pass
                try:
                    part.unlink(missing_ok=True)
                except Exception:
                    pass
                have = 0
                _hdrs.pop("Range", None)
                resp = await _safe_get(session, url, _hdrs, proxy=proxy or None,
                                       timeout=180, stream=True)
                if resp is None:
                    return None
            if resp.status_code not in (200, 206):
                try:
                    resp.close()
                except Exception:
                    pass
                await asyncio.sleep(1.0)
                return None  # 非 2xx → 该源放弃（切镜像）
            mode = 'ab' if (have > 0 and resp.status_code == 206) else 'wb'
            now = have if mode == 'ab' else 0
            # [本轮修复] 低速卡死看门狗：窗内均速跌破阈值 → 抛 LowSpeedAbort 中断本源。
            # 此前这里只有 curl 的 `timeout=180`（整段传输上限）——慢速滴流能在
            # 180s × `retries` 次里一直磨下去，而"慢"完全不该按"重试"处理。
            _wd = StallWatchdog()
            async with aiofiles.open(part, mode) as _f:
                async for _chunk in resp.aiter_content():
                    if _chunk:
                        await _f.write(_chunk)
                        now += len(_chunk)
                        _wd.note_bytes(now)
            if expect_total and expect_total > 0 and now != expect_total:
                logger.warning(f"断点续传字节数不符: {now}/{expect_total}——保留 .part 换源重试")
                await asyncio.sleep(1.0)
                return None  # 字节不符 → 该源放弃（切镜像）
            if now <= 0:
                try:
                    part.unlink(missing_ok=True)
                except Exception:
                    pass
                return None
            os.replace(part, dest)  # 原子落盘（同卷 rename）
            return dest
        except LowSpeedAbort as _le:
            # 我们叫停的 → **不重试同源**（重试一条卡死的传输正是要修的那件事）
            logger.warning(f"低速卡死放弃本源（不再重试）: {_le}")
            return None
        except Exception as _e:
            logger.debug(f"stream_to_file 失败(尝试{_try + 1}): {_e}")
            await asyncio.sleep(0.8)
    return None


# 登录态探测缓存：`(is_login|None, is_vip, detail)`。None 表示进程内还没测过；
# `is_login is None` 表示**测了但没测出来**（两者语义不同，别混）。
_bili_login_cache: Optional[tuple] = None


# ── yt-dlp 插件注册预热 ────────────────────────────────────────────────
# [v6 修复·真机实测] 并发建 `YoutubeDL` 会让 yt-dlp 的**插件注册表竞态**，
# 真机日志刷几十行：
#     Error while importing module 'yt_dlp_plugins.extractor.getpot_bgutil_http'
#     AssertionError: PoTokenProvider BgUtilHTTP already registered
# 实测复现与定位：
#     · 顺序建 2 个实例        → **0 次**
#     · **并发 12 个实例**      → **42 次**   ← 是竞态，不是"建两次"
#     · 先**单线程预热**再并发  → **0 次**    ← 预热后注册表已建好，后续 import 变 no-op
# 预热失败**不影响真下载**（最坏就是日志里多几行噪音，与修前一致）。
_ytdlp_prewarm_lock = threading.Lock()
_ytdlp_prewarmed = False


def prewarm_ytdlp_plugins() -> None:
    """单线程把 yt-dlp 的插件注册跑一遍（幂等、失败不抛）。

    **为什么需要它**：并发建 `YoutubeDL` 会让 yt-dlp 的**插件注册表竞态**，
    日志刷几十行 `PoTokenProvider BgUtilHTTP already registered`。
    实测定位（不是"建两次"，是**竞态**）：
      · 顺序建 2 个实例          → 0 次
      · **并发 12 个实例**        → **42 次**
      · 先单线程预热再并发        → **0 次**   ← 本函数的作用
    预热把注册表建好，后续并发 import 成为 no-op。
    失败**不影响真下载**（最坏就是日志多几行噪音，与修前一致）。

    ⚠️ 别删：看着像"多此一举的空实例"，实际是**唯一**压住那个竞态的手段。
    """
    global _ytdlp_prewarmed
    if _ytdlp_prewarmed:
        return
    with _ytdlp_prewarm_lock:
        if _ytdlp_prewarmed:
            return
        try:
            import contextlib
            import io as _io
            import yt_dlp
            # 预热那一次的输出也吞掉——它本来就没有用户价值
            with contextlib.redirect_stderr(_io.StringIO()), \
                    contextlib.redirect_stdout(_io.StringIO()):
                with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True,
                                       "ignoreconfig": True}):
                    pass
        except Exception as e:
            logger.debug(f"yt-dlp 插件预热失败（不影响下载）: {e}")
        _ytdlp_prewarmed = True


def verify_bilibili_login(timeout: int = 8):
    """用 cookies **真调一次** B站 nav 接口，确认登录态是否有效。

    [v6 修复·真机实测发现] 原 `detect_bilibili_cookie_browser` **只检查
    "cookies 文件里有没有 `bilibili` 这个词"**，却在日志里宣称
    「检测到 B站**会员** cookies → 强制最高画质」—— **用无关的检查冒充成功**。

    实测（用户的 cookies）：字段齐全（SESSDATA/bili_jct/DedeUserID）、
    文件声明 2027-03 才过期，但服务端回 **`-101 账号未登录`**
    → 实际只拿到 480P，日志却在报"最高画质"。
    文件里的过期时间只是**客户端声明**，服务端随时可作废。

    返回 `(is_login, is_vip, detail)`：
      · `is_login is None` → **没测出来**（网络/解析失败）→ 调用方**不要**据此改行为，只改措辞
      · `is_login is False` → cookies 存在但**已失效**，如实告知
      · `is_login is True` → 再看 `is_vip`
    缓存进程内一次。**全程不打印任何 cookie 值**。
    """
    global _bili_login_cache
    # [v6 修复·真机实测] 缓存**必须跟 cookie 来源绑定**。
    # 原来是个裸的全局缓存（`if _bili_login_cache is not None: return`）——
    # **第一次调用的结果被永久钉死**：若那次发生在 cookies 就位之前
    # （或撞上一次瞬时失败），后面全程都返回那个陈旧结论。
    # 真机表现：用户的 cookies 单独测是 `(True, True, 'code=0 OK')`，
    # 而爬取日志却报 `⚠️ B站 cookies 已失效（code=-101）` → **整套降成 480P**。
    _cf_sig = ""
    try:
        _c0 = _ensure_cookie_file()
        if _c0 is not None and _c0.exists():
            _s0 = _c0.stat()
            _cf_sig = f"{_c0}|{_s0.st_mtime}|{_s0.st_size}"
    except OSError:
        _cf_sig = ""
    if _bili_login_cache is not None and _bili_login_cache[0] == _cf_sig:
        return _bili_login_cache[1]
    try:
        _cf = _ensure_cookie_file()
        if _cf is None or not _cf.exists():
            _bili_login_cache = (_cf_sig, (False, False, "无 cookies 文件"))
            return _bili_login_cache[1]
        # 合并产物由 _ensure_cookie_file 用 utf-8 写出 → 此处同编码读（不要 utf-8-sig）
        from .cookie_utils import parse_netscape_cookies
        pairs = []
        for c in parse_netscape_cookies(_cf.read_text(encoding="utf-8", errors="ignore")):
            # 域过滤保留（"bilibili" 子串、大小写敏感）+ 原有的"name 非空"条件
            if c["name"] and "bilibili" in str(c["domain"]):
                pairs.append(f"{c['name']}={c['value']}")
        if not pairs:
            _bili_login_cache = (_cf_sig, (False, False, "cookies 里没有 bilibili 域"))
            return _bili_login_cache[1]
        from .url_utils import safe_urlopen
        # ⚠️ UA **必须与 TLS 指纹同源**，不许硬编码 Chrome 版本——这是本工程的红线
        # （`tests/test_ua_tls_consistency.py` 会抓；我第一版就写了硬编码的 Chrome/131，
        #   当场被那条守卫红了）。走既有的 `user_agent_for` 唯一实现。
        from .fingerprint_consistency import TLS_IMPERSONATE_POOL, user_agent_for
        resp = safe_urlopen("https://api.bilibili.com/x/web-interface/nav",
                            headers={"Cookie": "; ".join(pairs),
                                     "Referer": "https://www.bilibili.com/",
                                     "User-Agent": user_agent_for(TLS_IMPERSONATE_POOL[0])},
                            timeout=timeout)
        if resp is None:
            _bili_login_cache = (_cf_sig, (None, False, "nav 请求被闸拦下或失败"))
            return _bili_login_cache[1]
        with resp:
            body = resp.read()
        d = json.loads(body.decode("utf-8", errors="ignore"))
        data = d.get("data") or {}
        is_login = bool(data.get("isLogin"))
        is_vip = bool(data.get("vipStatus"))
        detail = f"code={d.get('code')} {d.get('message') or ''}".strip()
        _bili_login_cache = (_cf_sig, (is_login, is_vip, detail))
    except Exception as e:
        # 探测失败**不能**影响下载——只是日志措辞保守一点
        _bili_login_cache = (_cf_sig, (None, False, f"探测异常 {type(e).__name__}"))
    return _bili_login_cache[1]


def detect_bilibili_cookie_browser():
    """探测 B站 cookies 来源：存在可用 cookies.txt 返回 'file'，否则 None（无→降级画质）。
    用户自填（KIANA_COOKIE_FILES/KIANA_COOKIE_FILE/默认位置）→ 合并后检查是否含 bilibili"""
    global _cookie_file_cache
    if _cookie_file_cache is not None:
        return _cookie_file_cache
    result = None
    _cf = _ensure_cookie_file()
    if _cf is not None:
        try:
            txt = _cf.read_text(encoding="utf-8", errors="ignore")
            if "bilibili" in txt:
                result = "file"
        except Exception:
            pass
    _cookie_file_cache = result
    return result


def load_site_cookies(url: str) -> list:
    """[FIXED & MODIFIED] v2.5.4 从 cookies.txt 加载与目标 URL 域名匹配的 cookies（通用站点支持）。
    v2.10.5 支持多文件自填合并（KIANA_COOKIE_FILES 分号分隔），本函数读取合并产物。

    淘宝/天猫/京东/B站等任意站点：浏览器扩展（Get cookies.txt LOCALLY）导出 → 引擎自动匹配注入。
    Returns: [{"name","value","domain","path"}...]
    """
    _cf = _ensure_cookie_file()
    if _cf is None or not _cf.exists():
        return []
    try:
        txt = _cf.read_text(encoding="utf-8", errors="ignore")
        host = (urlparse(url).netloc or "").lower()
        if not host:
            return []
        host = host.split(":")[0]
        parts = host.split(".")
        # 站点根域：如 www.taobao.com → taobao.com；e.tb.cn → tb.cn
        root = ".".join(parts[-2:]) if len(parts) >= 2 else host
        # 合并产物由 _ensure_cookie_file 用 utf-8 写出 → 此处同编码读（不要 utf-8-sig）
        from .cookie_utils import parse_netscape_cookies
        out = []
        for c in parse_netscape_cookies(txt):
            domain = str(c["domain"])
            d = domain.lstrip(".")
            # 域名匹配：cookie 域 == 目标根域 或 目标域是 cookie 域的子域
            #
            # [v6 修复·**安全**] 原来这行末尾还有个 `or d in host` —— **裸子串匹配**：
            #     `"taobao.com" in "nottaobao.com"` → **True**
            # ⇒ 会把 taobao 的 cookie **注入到仿冒域名**（`nottaobao.com`）上。
            # 这是 cookie 泄漏，不是小瑕疵。
            #
            # **而且它是冗余的**：合法情形（`www.bilibili.com` 配 `bilibili.com`）
            # 已被 `host.endswith("." + d)` 精确覆盖；`d == root` 与
            # `root.endswith("." + d)` 再兜住根域与反向子域。
            # ⇒ 直接删掉那个分支，**不是**把它换成 `endswith(d)`
            #   （后者同样会被 `nottaobao.com`.endswith(`taobao.com`) 骗过）。
            #
            # 顺带：`d` 为空时 `"." + d` == `"."`，`host.endswith(".")` 为 False，
            # 不会误命中 —— 空域名条目自然被排除。
            if d and (d == root or host.endswith("." + d) or root.endswith("." + d)):
                out.append({"name": str(c["name"]), "value": c["value"],
                            "domain": domain, "path": str(c["path"] or "/")})
        return out
    except Exception as e:
        logger.debug(f"load_site_cookies: {e}")
        return []

# ── 格式降级的退避判据（[v6] 真机实测发现：确定性错误不该退避）────────────
# 真机现象：每个 B站视频都先失败一次 `best/best[ext=mp4]/best`
# （DownloadError: Requested format is not available），**无条件退避 2.5 秒**后才降级。
# 但"这个格式不存在"是**确定性**的——重试多久都一样，退避纯属白等。
# 只有可能来自**限流/网络**的失败才值得退避。
_FORMAT_DETERMINISTIC_HINTS = (
    "requested format is not available",
    "requested format not available",
    "no video formats found",
    "unsupported url",
)


# 小于此字节数的产物**不可能是视频**。真机实测的垃圾是 **46 字节**
# （一个 HTML 错误页被 yt-dlp 当成视频存了下来）。取 4KB 是保守值：
# 再短的视频也远大于它，而错误页通常几百字节。
_MIN_VIDEO_BYTES = 4096
# yt-dlp 认不出格式时用的扩展名 —— 出现它说明下到的根本不是媒体
_NON_MEDIA_SUFFIXES = (".unknown_video", ".part", ".ytdl", ".tmp")


def video_artifact_problem(p) -> str:
    """产物是否**不像真视频**；像则返回 `""`，不像则返回原因（供日志用）。

    [v6 修复·R6 真机实测] 第二道防线。第一道（`url_utils.is_shortener_misresolution`）
    挡的是"URL 本身就错"；这一道挡的是"URL 看着没错但产物是垃圾"。

    真机后果：短链误解析出的 `b23.tv/video/BVxxx` 交给 yt-dlp 后**不报错**，
    存下 **46 字节**的 HTML 错误页并记 `Video downloaded: ... (46 bytes)` ——
    调用方据此**误标 completed**，用户目录里留下一堆 `*.unknown_video`。

    判据（任一命中即"不像视频"）：
      · 文件不存在 / 取不到大小；
      · **小于 `_MIN_VIDEO_BYTES`**；
      · 扩展名是 `_NON_MEDIA_SUFFIXES` 之一。
    """
    try:
        p = pathlib.Path(p)
        if not p.exists():
            return "文件不存在"
        size = p.stat().st_size
    except OSError as e:
        return f"取不到大小: {e}"
    if size < _MIN_VIDEO_BYTES:
        return f"只有 {size} 字节（错误页/空响应，不是视频）"
    if p.suffix.lower() in _NON_MEDIA_SUFFIXES:
        return f"扩展名 {p.suffix} 说明未识别出媒体格式"
    return ""


def is_deterministic_format_error(exc) -> bool:
    """该失败是否**确定性**（换格式即可，无需退避）。

    提到模块级是为了**可测**：内联在重试循环里没法单独断言。
    """
    msg = str(exc).lower()
    return any(h in msg for h in _FORMAT_DETERMINISTIC_HINTS)


class UniversalDownloader:
    """万能下载引擎：yt-dlp视频 + HTTP图片/音频"""

    def __init__(self, output_dir: Path, max_concurrent: int = 3):
        self.output_dir=output_dir;self.output_dir.mkdir(parents=True,exist_ok=True)
        self.sem=asyncio.Semaphore(max_concurrent)
        self.session=None  # [FIXED & MODIFIED] v2.10.4 curl_cffi AsyncSession（原 aiohttp 注解未定义，运行时注解不求值但属无效声明）
        self.video_dir=output_dir/'videos';self.video_dir.mkdir(exist_ok=True)
        self.image_dir=output_dir/'images';self.image_dir.mkdir(exist_ok=True)
        self.audio_dir=output_dir/'audio';self.audio_dir.mkdir(exist_ok=True)
        self._dom_cache: dict = {}  # [FIXED & MODIFIED] 域名子目录缓存（v2.4.1 按来源分类）

    async def init(self):
        # [FIXED & MODIFIED] curl_cffi AsyncSession 替换 aiohttp（本机 aiohttp 解析层卡死）
        # [v2.19.8 性能修复] 与 media_downloader 同因：不传 impersonate 会落在无指纹默认档，
        # 同一 CDN 多次取样 4.7-8.2 MB/s，带指纹档 12-25 MB/s。指纹取自工程自己的 TLS 池。
        from curl_cffi.requests import AsyncSession
        from .fingerprint_consistency import TLS_IMPERSONATE_POOL, user_agent_for
        self.session = AsyncSession(
            timeout=120,
            impersonate=TLS_IMPERSONATE_POOL[0],
            # [v6 修复] 原来是**截断 UA**（`…AppleWebKit/537.36` 后直接结束，没有版本号）——
            # 没有任何真实浏览器会发这种形态，且与本会话的 chrome136 双重不一致。
            headers={'User-Agent': user_agent_for(TLS_IMPERSONATE_POOL[0])}
        )

    async def close(self):
        if self.session:
            try:
                await self.session.close()
            except Exception:
                pass

    # ═══ 平台直链 resolver（[v2.19.8] 表驱动）═══
    # 本工程有四个平台 resolver：douyin / xhs / kuaishou / music163。此前**只有抖音**
    # 在 download_video 里写了一段专属接线，另外三个"能命令行用、有独立单测，但引擎零调用"
    # （施工方案里登记为待接线）。现改为一张表 + 一个异步包装：
    #   · 新增平台只加一行；
    #   · 统一走统一 schema（resolvers 都已产出 media.streams[0].url），不再各拿各的键；
    #   · 全部 to_thread——resolver 是纯同步 urllib + sleep，直接在事件循环里跑会冻结整个
    #     引擎（抖音曾因此冻结 30-80s，见 v2.14 阶段1 的止血记录）。
    # 失败语义：拿不到直链就返回空串，调用方**保持原 URL**继续走原链路（不改变既有行为）。
    _PLATFORM_RESOLVERS = (
        ("douyin.com", "douyin_resolver", "抖音"),
        ("iesdouyin.com", "douyin_resolver", "抖音"),
        ("xiaohongshu.com", "xhs_resolver", "小红书"),
        ("xhslink.com", "xhs_resolver", "小红书短链"),
        ("kuaishou.com", "kuaishou_resolver", "快手"),
        ("gifshow.com", "kuaishou_resolver", "快手"),
        ("music.163.com", "music163_resolver", "网易云"),
    )

    @staticmethod
    def _resolver_for_url(url: str):
        """按主机名精确匹配（子域算命中）→ (模块名, 平台名)；无匹配 → (None, None)。
        精确匹配而非子串：避免 `qq.com` 这类普通页面被误判（同类事故见 crawler 的视频平台表）。"""
        from urllib.parse import urlparse as _up
        try:
            _h = (_up(str(url or "")).netloc or "").lower().split(":")[0]
        except Exception:
            return None, None
        if not _h:
            return None, None
        for suffix, mod, label in UniversalDownloader._PLATFORM_RESOLVERS:
            if _h == suffix or _h.endswith("." + suffix):
                return mod, label
        return None, None

    @staticmethod
    def _pick_direct_url(res) -> str:
        """从 resolver 返回里取直链：优先统一 schema 的 `media.streams[0].url`，
        退回平台历史键（video_url / media_url）。取不到返回空串。"""
        if not isinstance(res, dict) or not res.get("ok"):
            return ""
        try:
            streams = (res.get("media") or {}).get("streams") or []
            if streams and isinstance(streams[0], dict) and streams[0].get("url"):
                return str(streams[0]["url"])
        except Exception:
            pass
        for k in ("video_url", "media_url"):
            if res.get(k):
                return str(res[k])
        return ""

    async def resolve_direct_url(self, url: str) -> str:
        """平台内容页 → 直链。无对应 resolver / 解析失败 → ""（调用方保持原 URL）。
        绝不抛异常：失败只是"没帮上忙"，不能打断下载链。"""
        mod, label = self._resolver_for_url(url)
        if not mod:
            return ""
        try:
            import importlib
            _m = importlib.import_module(f"{__package__}.{mod}")
            res = await asyncio.to_thread(_m.resolve, url)   # 同步实现必须离开事件循环
        except Exception as e:
            logger.warning(f"{label} resolver 调用异常: {e}")
            return ""
        direct = self._pick_direct_url(res)
        if direct:
            logger.info(f"{label} resolver 转直链成功: {direct[:60]}…")
            return direct
        try:
            logger.warning(f"{label} resolver 未取得直链: {str((res or {}).get('error'))[:120]}")
        except Exception:
            logger.warning(f"{label} resolver 未取得直链")
        return ""

    # ═══ Video: yt-dlp ═══
    async def download_video(self, url: str, quality: str = 'best',
                             want_subs: bool = True, want_thumb: bool = True,
                             want_infojson: bool = True, proxy: str = '') -> Optional[Path]:
        """使用yt-dlp下载视频——支持B站/YouTube/抖音/所有平台
        [FIXED & MODIFIED] v2.5.8 双保险：
          1. player.bilibili.com/player.html 嵌入链接兜底转换（无论谁入队，下载层最后防线）
          2. 格式多级降级重试（B站格式接口风控 → "Requested format is not available" 时自动降级）
        [FIXED & MODIFIED] v2.10.5 抖音兜底：yt-dlp 对 v.douyin.com 短链/抖音站 extractor 失效
          → 用 douyin_resolver（a_bogus 签名）转无水印直链 → 流式下载。
        [v2.16.1] proxy 透传：主下载通道接入出口代理池（B站等国内白名单域仍直连——
          acquire_for_domain 对国内域返回 None；YouTube 等走代理——媒体流 403 受出口风控）
        [v2.19 安全 P0] SSRF/协议闸：此前 video/audio 通道无闸（同文件的 image/file
          通道却有）——恶意页面/m3u8 可让 yt-dlp 拉取内网地址。补齐一致。"""
        from .url_utils import is_private_url
        if not url or not str(url).startswith(("http://", "https://")) or is_private_url(url):
            logger.warning(f"视频下载拦截（SSRF/协议闸）: {str(url)[:60]}")
            return None
        # 兜底转换：player.html → 标准视频页（yt-dlp 不认嵌入播放器 URL）
        _pm = re.search(r'player\.bilibili\.com/player\.html\?[^"\'\s]*bvid=(BV\w+)', url or '')
        if _pm:
            url = f"https://www.bilibili.com/video/{_pm.group(1)}"
            logger.info(f"player.html 已兜底转换为标准视频页: {url[:60]}")
        # [v2.16.1] YouTube PO Token：bgutil 插件默认连 :4416——运行时确保 server 已起；
        # 未起/无随包资源时打警示（媒体流 403 的语义化预判）
        _yhost = (urlparse(url or '').netloc or '').lower()
        if any(_y in _yhost for _y in ("youtube.com", "youtu.be", "youtube-nocookie.com")):
            try:
                if not pot_server_ready() and not pot_server_ensure():
                    logger.warning("PO Token server 未就绪——YouTube 媒体流可能 403（配置随包 node 或换干净出口）")
            except Exception as _pe:
                logger.warning(f"PO Token 检查异常: {_pe}")
        # [v2.19.8] 平台直链 resolver：表驱动（抖音/小红书/快手/网易云），替代原先写死的抖音块。
        # 命中平台 → 转直链后继续走流式下载；未命中/解析失败 → url 原样不变（行为与旧版一致）。
        # 本工程自己的 F821 守卫测试（`tests/test_v219_concurrency.py`，以 ruff --select F821 扫描
        # 引擎目录）会在导入期抓出「已删除却仍被引用」的名字：表驱动重构时 `_din` 一度被删而下游
        # 仍在用，真跑到那条分支就是 NameError。故该变量必须在替换 url **之前**算。
        _din = bool(url and ('douyin.com' in url.lower() or 'iesdouyin.com' in url.lower()))
        _direct = await self.resolve_direct_url(url)
        if _direct:
            # [v2.19.8 安全] 直链来自**远端页面状态**（解析器的输入是页面 JSON，内容由站点/攻击者
            # 可控）→ 与 m3u8 分片同一条教训：替换后的 URL 必须重新过闸，否则等于绕过入口校验
            # 把内网地址交给 yt-dlp（旧版抖音块就是直接替换、未复检）。
            if is_private_url(_direct):
                logger.warning(f"resolver 直链被 SSRF 闸拦截（疑似页面状态注入）: {_direct[:60]}")
            else:
                url = _direct
        async with self.sem:
            try:
                import yt_dlp
                # [FIXED & MODIFIED] v2.6.7 根治 ffmpeg 合并的 gbk 崩溃：B站 dash 流 metadata 含
                # UTF-8 中文 → ffmpeg 输出非 GBK 字节 → subprocess text 模式 gbk 解码崩溃 →
                # 合并失败 → 分离流被清理 → 用户"空文件夹"。locale 设 UTF-8 后 subprocess 用 utf-8。
                try:
                    import locale
                    locale.setlocale(locale.LC_CTYPE, 'C.UTF-8')
                except Exception:
                    pass
                # [FIXED & MODIFIED] v2.6.7 文件名改回 hash（原 %(title)s 中文标题 → ffmpeg 合并时
                # subprocess 输出含 UTF-8 中文 → Windows gbk 解码崩溃（UnicodeDecodeError）→ 合并失败
                # → 分离流被清理 → 用户"空文件夹"根因。hash 名纯 ASCII 彻底免疫。
                name = self._safe_name(url)
                # [v2.17 2.11 加固] out_tmpl 绝对化：yt-dlp 会把相对 outtmpl 与 paths.home
                # 再拼一层 → 'out/videos/out/videos/...' 双前缀（CLI -o 相对路径即踩中，
                # 视频实际落盘但 verify/打开目录找不到）
                _vdir_abs = self._dom_dir(self.video_dir.resolve(), url)
                out_tmpl = str(_vdir_abs / f'{name}.%(ext)s')
                # [DIAG v2.6.12] 打印下载目标（锁定安装版文件去向）
                logger.warning(f"[K-DIAG] out_tmpl={out_tmpl} video_dir={self.video_dir}")
                # [FIXED & MODIFIED] B站会员 cookies 自动探测：有登录 → 强制最高画质；无 → 降级普通最高画质
                #
                # [v6 修复·真机实测发现] 这里原来**只看"cookies 文件里有没有 bilibili 这个词"**，
                # 却在日志里下"**会员** cookies → **强制最高画质**"的结论 ——
                # **用无关的检查冒充成功**。实测用户的 cookies：字段齐全、文件声明 2027 到期，
                # 但服务端回 `-101 账号未登录` → 实际只拿到 480P，日志却在报"最高画质"。
                # 现改成**真调一次 nav 接口**核实登录态，并按事实说话。
                cookie_browser = detect_bilibili_cookie_browser()
                # [v2.19.10 修复·真机日志串台] 原实现**无条件**对每个视频跑 B站 登录态探测
                # 并打印结论。真机实测（2026-10-04）：下载 Apple / Microsoft 官网宣传片时，
                # 日志照样打印 `B站 cookies **登录有效**（大会员） → 最高画质`
                # —— 与正在下载的那个视频**毫无关系**，属误导性日志（会让人误判画质结论）。
                # 现按 URL 归属分流：**非 B站 目标不探测（顺带省一次网络请求）、不打印 B站 结论**。
                # ⚠️ `cookie_browser`（**本地文件检查，无网络开销**）保持原样：下游
                #    `if cookie_browser:` 用它选格式链 —— 保留它 ⇒ 非 B站 站点的画质选择**零变化**，
                #    避免在"修日志"时顺手把别的站点的画质链改掉。
                _host = (urlparse(url).netloc or "").lower()
                _is_bili = is_bilibili_url(url)
                if not _is_bili:
                    logger.debug(f"[画质] 目标非 B站（{_host}）—— 不做 B站 登录态探测、"
                                 f"不打印 B站 结论（旧实现在这里会打「B站 cookies 登录有效」，属串台）")
                else:
                    _login, _vip, _why = verify_bilibili_login()
                    if cookie_browser and _login is True:
                        logger.warning(f"B站 cookies **登录有效**{'（大会员）' if _vip else '（非会员）'}"
                                       f" → {'最高画质' if _vip else '登录态画质'}")
                    elif cookie_browser and _login is False:
                        logger.error(
                            f"⚠️ B站 cookies **已失效**（{_why}）——文件看着是好的，但服务端不认。"
                            f"本次**只能拿到 480P 及以下**。请重新导出 cookies.txt（浏览器登录后导出）。")
                    elif cookie_browser:
                        # 没测出来（网络等）→ **不改变行为**，措辞保守
                        logger.warning(f"检测到 B站 cookies（登录态**未能核实**：{_why}）→ 按最高画质尝试，"
                                       f"若实际只有 480P 说明 cookies 已失效")
                    else:
                        logger.info("未检测到 B站 cookies → 普通画质（登录 B站后完全退出浏览器即可解锁最高画质）")
                # [FIXED & MODIFIED] v2.6.7 多级格式降级链——mp4 单文件优先（免 ffmpeg 合并，
                # 规避 gbk 崩溃导致空文件夹）；dash 分离流+合并放最后兜底
                # [FIXED & MODIFIED] v2.15 阶段3 B站 Hi-Res：会员时音轨优先 FLAC
                # （bestaudio[acodec=flac]）——20260829 实测 925MB ASMR 只拿到 AAC 200k，
                # Hi-Res 是独立 FLAC 轨需显式优先；无 FLAC 自然回退普通 bestaudio
                if cookie_browser:
                    fmt_chain = (f'{quality}/best[ext=mp4]/best',
                                 f'bestvideo+bestaudio[acodec=flac]/{quality}/bestvideo+bestaudio/best',
                                 'best')
                else:
                    fmt_chain = (f'{quality}/best[ext=mp4]/best[height<=1080]/best',
                                 f'{quality}/bestvideo+bestaudio/best[height<=1080]/best',
                                 'best')
                opts_base = {'quiet': True, 'no_warnings': True,
                             'noplaylist': True, 'concurrent_fragments': 4, 'socket_timeout': 30,
                             'merge_output_format': 'mp4',  # [FIXED & MODIFIED] dash 音视频流合并为 mp4
                             # [FIXED & MODIFIED] v2.6.10 优先 mp4 单文件（format_sort 强制——免 ffmpeg 合并，
                             # 规避安装版 gbk 崩溃；仅当无 mp4 单文件才走 dash+合并）
                             'format_sort': ['ext:mp4', 'res'],
                             # [FIXED & MODIFIED] v2.10.4 自包含：ffmpeg_location 指向打包的 ffmpeg
                             # （PyInstaller _MEIPASS 或程序目录——换机即用，不依赖系统 ffmpeg）
                             'ffmpeg_location': str(_ffmpeg_dir()),
                             # [FIXED & MODIFIED] v2.6.3 忽略一切外部配置（用户 GUI 实测报
                             # "No option 'min_extracted_size' in section: 'DEFAULT'"——外部 yt-dlp
                             # 配置干扰。引擎自带完整 opts，不需要任何外部配置文件）
                             'ignoreconfig': True,
                             'http_headers': {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'},
                             # [FIXED & MODIFIED] v2.11 断点续传显式锁死（原依赖 yt-dlp 默认值，
                             # 大视频中断可能整段重下）；重试数拉满
                             'continuedl': True, 'nopart': False,
                             'retries': 10, 'fragment_retries': 10, 'extractor_retries': 3}
                # [v2.16.1] 出口代理透传（yt-dlp 主通道；国内白名单域 proxy=None 直连）
                if proxy:
                    opts_base['proxy'] = proxy
                # [FIXED & MODIFIED] v2.11 副产物层：字幕/封面/info.json（B站 AI 字幕走 writeautomaticsub，
                # 副产物随视频同目录落盘，hash 同名）
                if want_subs:
                    opts_base.update({'writesubtitles': True, 'writeautomaticsub': True,
                                      'subtitleslangs': ['zh-Hans', 'zh-CN', 'zh', 'en']})
                if want_thumb:
                    opts_base['writethumbnail'] = True
                if want_infojson:
                    opts_base['writeinfojson'] = True
                # [FIXED & MODIFIED] v2.11 无条件 cookies：任何来源的合并 cookies 都传给
                # yt-dlp（按域自动匹配——B站/抖音/YouTube cookies 同时生效；原仅 B站分支）
                _merged = _ensure_cookie_file()
                if _merged and _merged.exists():
                    # [v2.19.10 修复·真机并发回写竞争] **每个下载用自己的副本**。
                    # 原来所有并发下载共用同一个合并文件，而 yt-dlp 会**回写**它
                    # （见 `_cookie_copy` 的 docstring：真机报 "does not look like a
                    # Netscape format cookies file"，那两次下载静默地没带登录态）。
                    # 副本失败时**回退共享文件** —— 宁可回到旧行为，也不让下载拿不到 cookies。
                    try:
                        opts_base['cookiefile'] = str(_cookie_copy(_merged))
                    except Exception:
                        opts_base['cookiefile'] = str(_merged)
                elif cookie_browser and cookie_browser != "file":
                    opts_base['cookiesfrombrowser'] = (cookie_browser, None, None, None)  # 浏览器 cookies（会员身份）
                loop = asyncio.get_event_loop()
                last_err = None
                # [本轮修复] 看门狗**整通调用共用一份**（不是每档一份）：
                # 逐档重置会让"慢"被乘以格式档数，而换档并不能救一条卡死的传输。
                # 窗口是滑动的：某档抛确定性格式错（不传一个字节）不会污染判定，
                # 但真·慢的一档会在一个窗口后把整通调用收掉。
                _wd = StallWatchdog()
                for fi, fmt in enumerate(fmt_chain):
                    opts = dict(opts_base)
                    opts['format'] = fmt
                    opts['progress_hooks'] = ytdlp_progress_hooks(_wd)
                    # [FIXED & MODIFIED] v2.6.13 关键修复：out_tmpl 必须显式传入 opts——
                    # 此前只算了变量没写进 opts → yt-dlp 用默认模板（%(title)s [%(id)s]）下载到
                    # 进程 cwd（KianaVnextPlus 工程目录）→ export/videos 永远空 → 用户"空文件夹"
                    # 终极根因（completed 正确但文件不在产物目录）。
                    opts['outtmpl'] = out_tmpl
                    opts['paths'] = {'home': str(self.video_dir.resolve())}
                    def _dl():
                        prewarm_ytdlp_plugins()   # [v6] 单线程预热插件注册，防并发竞态刷屏
                        with yt_dlp.YoutubeDL(opts) as ydl:
                            info = ydl.extract_info(url, download=True)
                            return ydl.prepare_filename(info) if info else None
                    try:
                        result = await loop.run_in_executor(None, _dl)
                        # [本轮修复] 看门狗已叫停 → 产物一律不算数（哪怕 yt-dlp 返回了路径）
                        if _wd.aborted:
                            logger.warning(f"低速卡死放弃（不再换档/换源）: {url[:70]}")
                            return None
                        # [DIAG v2.6.12] result 快照
                        logger.warning(f"[K-DIAG] fmt={fmt} result={result!r}")
                        if result:
                            _rp2 = Path(result)
                            logger.warning(f"[K-DIAG] result_exists={_rp2.exists()} size={_rp2.stat().st_size if _rp2.exists() else 0}")
                    except Exception as e:
                        last_err = e
                        if _wd.aborted:
                            # 是我们叫停的：格式降级、退避重试**都毫无意义** —— 直接收工。
                            # （不这么判的话，它会被当成"这一档失败"继续跑完剩下的档，
                            #   于是"慢"又被乘以档数。）
                            logger.warning(f"低速卡死放弃（{e}）: {url[:70]}")
                            return None
                        if fi < len(fmt_chain) - 1:
                            # [v6 修复·真机实测发现] 原为**无条件** `await asyncio.sleep(2.5 + fi*2)`。
                            # 实测：每个 B站视频都要先失败一次 `best/best[ext=mp4]/best`
                            # （DownloadError: Requested format is not available），白等 2.5 秒
                            # 才降级到能成的 `bestvideo+bestaudio[acodec=flac]/…`。
                            # 但"格式不存在"是**确定性**的，退避毫无意义；
                            # 只有可能来自**限流/网络**的失败才值得等。
                            if is_deterministic_format_error(e):
                                logger.info(f"格式 {fmt} 不可用（确定性，非限流）→ 立即换下一档，不退避")
                            else:
                                logger.warning(f"格式 {fmt} 失败（{type(e).__name__}），{2.5 + fi * 2}s 后降级重试")
                                await asyncio.sleep(2.5 + fi * 2)
                        continue
                    if result:
                        p = Path(result)
                        # [FIXED & MODIFIED] v2.6.7 排除 dash 分离流（.f137.mp4/.f140.m4a 等）：
                        # 合并失败时 prepare_filename 返回分离流路径（下载中曾存在）→ p.exists()
                        # 误判成功 → 误标 completed + 文件随后被 yt-dlp 清理 → 用户空文件夹
                        if p.exists() and not re.search(r'\.f\d+\.(mp4|m4a|webm|mkv|m4s)$', p.name):
                            # [v6 修复·R6] 第二道防线：**产物得像视频**。
                            # 真机上短链误解析出的假 URL 会让 yt-dlp 不报错地存下
                            # **46 字节**的 HTML 错误页 → 这里原来照样记成功并 return，
                            # 于是误标 completed、目录里堆一堆 `*.unknown_video`。
                            _why = video_artifact_problem(p)
                            if _why:
                                logger.warning(f"产物不是视频（{_why}），已删除并继续换档: {p.name}")
                                try:
                                    p.unlink(missing_ok=True)   # 别把垃圾留在用户目录里
                                except OSError:
                                    pass  # 删不掉也不该让整次下载失败；下一档会重下
                                raise RuntimeError(f"non-video artifact: {_why}")
                            # [FIXED & MODIFIED] v2.6.4 打印完整路径（定位文件消失问题）
                            logger.info(f"Video downloaded: {p} ({p.stat().st_size} bytes)")
                            return p
                    # yt-dlp 无异常但没产物 → 尝试找新文件
                    before = set(self.video_dir.rglob('*'))
                    after = set(self.video_dir.rglob('*'))
                    new = after - before
                    if new:
                        p = list(new)[0]
                        # [FIXED & MODIFIED] v2.6.10 兜底同样排除 dash 分离流（.f137.mp4 等）——
                        # 否则合并失败时误标 completed + 文件随后被清理（用户空文件夹根因）
                        if not re.search(r'\.f\d+\.(mp4|m4a|webm|mkv|m4s)$', p.name):
                            # [v6 修复·R6] 兜底路径也要过"像不像视频"这一关
                            _why2 = video_artifact_problem(p)
                            if _why2:
                                logger.warning(f"产物不是视频（{_why2}），已删除: {p.name}")
                                try:
                                    p.unlink(missing_ok=True)   # 别把垃圾留在用户目录里
                                except OSError:
                                    pass  # 删不掉不影响后续换档
                                raise RuntimeError(f"non-video artifact: {_why2}")
                            logger.info(f"Video downloaded (yt-dlp): {p.name}")
                            return p
                    if fi < len(fmt_chain) - 1:
                        await asyncio.sleep(2.0)
                if last_err:
                    # [FIXED & MODIFIED] v2.11 B站风控语义化：-101/-352 登录态/风控 → 可读提示
                    _es = str(last_err)
                    if '-101' in _es or '-352' in _es or '401' in _es:
                        logger.error(f"B站登录态失效或风控({_es[:60]})——请重新导出 cookies 或稍后重试: {url}")
                    logger.error(f"yt-dlp failed for {url}: {last_err}")
                # [FIXED & MODIFIED] v2.10.5 抖音直链兜底：resolver 已转出的无水印直链
                # （douyinvod.com 等）y-tdlp 也可能失败 → 流式直连下载
                # [v2.17 2.6] 统一到 stream_to_file（原为第二套断点续传实现：手工 Range/ab 追加
                # 无字节自校验——与公共件双实现漂移；现在保留防盗链头与 >1MB 判定）
                if _din and url and 'douyinvod.com' in url.lower():
                    _dpath = self._dom_dir(self.video_dir, url) / (self._safe_name(url) + '.mp4')
                    _dy_hdrs = {
                        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"
                                      " AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
                        "Referer": "https://www.douyin.com/",
                    }
                    _dprobe = await probe_url(self.session, url, _dy_hdrs)
                    _dok = await stream_to_file(self.session, url, _dpath, _dy_hdrs,
                                                expect_total=(_dprobe[0] if _dprobe else None),
                                                retries=3)
                    if _dok and _dok.stat().st_size > 1_000_000:
                        logger.info(f"抖音直链下载成功: {_dok} ({_dok.stat().st_size / 1024 / 1024:.1f}MB)")
                        return _dok
                    logger.warning("抖音直链下载失败（流式通道，已保 .part 可续）")
                return None
            except Exception as e:
                logger.error(f"yt-dlp failed for {url}: {e}")
                return None

    # ═══ [FIXED & MODIFIED] 按来源域名分类（v2.4.1：B站/Steam 等各自独立子目录，不再混放）═══
    def _dom_dir(self, base: pathlib.Path, url: str) -> pathlib.Path:
        try:
            dom = (urlparse(url).netloc or "unknown").replace("www.", "")
        except Exception:
            dom = "unknown"
        if not dom:
            dom = "unknown"
        # [FIXED & MODIFIED] v2.14 阶段3 路径穿越修复（深查 C7）：netloc 未经清洗直接拼
        # 路径——反斜杠可构造 '..\..\' 真实穿越、':'（端口）mkdir 必失败、'..' 落上级。
        # 统一走 safe_dirname（保留名/控制字符/尾点全处理）。
        from .url_utils import safe_dirname
        dom = safe_dirname(dom)
        d = base / dom
        if (str(base), dom) not in self._dom_cache:  # [FIXED & MODIFIED] 复合键（不同 base 同 dom 各自创建）
            d.mkdir(parents=True, exist_ok=True)
            self._dom_cache[(str(base), dom)] = True
        return d

    # ═══ Image: direct HTTP ═══
    async def download_image(self, url: str, referer: str = '') -> Optional[Path]:
        """下载单张图片"""
        # [FIXED & MODIFIED] v2.14 阶段3 SSRF/协议闸（深查：images 无 scheme 检查，
        # ftp:// 等可被 libcurl 默认协议拉取）
        from .url_utils import is_private_url
        if not url or not url.startswith(("http://", "https://")) or is_private_url(url):
            return None
        async with self.sem:
            try:
                ext = self._img_ext(url)
                name = self._safe_name(url)[:40] + ext
                # [FIXED & MODIFIED] v2.4.1 图片按 referer（来源页面）域名分类——CDN 域名
                # （cdn.steampowered.com / i0.hdslb.com）归到对应主站目录，与视频/数据一致
                path = self._dom_dir(self.image_dir, referer or url) / name
                headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
                # [v6 修复] 调用方不传 referer 时**不能不发** Referer：
                # 工程注释写明"CDN 校验主站 Referer——**无 Referer 直接 403/空响应**"。
                # 原实现仅在传了 referer 时才加该头，等于把最常见的一张图（种子图直链）
                # 送进 403。改为回退到 `url_utils.referer_for`（CDN→主站映射）。
                from .url_utils import referer_for as _referer_for
                _ref = referer or _referer_for(url)
                if _ref:
                    headers['Referer'] = _ref
                # [FIXED & MODIFIED] v2.11 单发零重试 → 3 次重试（图片 CDN 偶发超时/抖动）
                for _try in range(3):
                    try:
                        # [FIXED & MODIFIED] curl_cffi 替换 aiohttp（本机 aiohttp 外网全超时）
                        # [v2.19.7 安全] 图片下载同样走带逐跳校验的 safe_get
                        from .url_utils import safe_get as _safe_get
                        resp = await _safe_get(self.session, url, headers)
                        if resp is None:
                            return None
                        if resp.status_code == 200 and resp.content:
                            data = resp.content
                            async with aiofiles.open(path, 'wb') as f:
                                await f.write(data)
                            return path
                    except Exception:
                        pass
                    await asyncio.sleep(0.8)
                return None
            except Exception as e:
                logger.debug(f"Image download failed {url[:60]}: {e}")
                return None

    async def download_images(self, urls: list[str], referer: str = '') -> list[Path]:
        """批量下载图片"""
        tasks=[self.download_image(u,referer) for u in urls[:50]]
        results=await asyncio.gather(*tasks,return_exceptions=True)
        return [r for r in results if isinstance(r,Path) and r.exists()]

    # ═══ Image OCR 索引（ddddocr classification——白送的 OCR 能力，产出 image_ocr 字段）═══
    @staticmethod
    def _ocr_one(path: str) -> str:
        try:
            import ddddocr
            with open(path, 'rb') as f:
                img = f.read()
            if len(img) < 200 or len(img) > 3_000_000:
                return ""
            ocr = ddddocr.DdddOcr(show_ad=False)
            return str(ocr.classification(img) or "").strip()
        except Exception:
            return ""

    async def ocr_images(self, paths: list) -> dict:
        """[FIXED & MODIFIED] v2.11 图片文字识别索引：{文件名: 识别文本}（限 12 张防耗时，
        CPU 密集识别走线程池——不阻塞事件循环）"""
        out = {}
        loop = asyncio.get_event_loop()
        for p in paths[:12]:
            try:
                txt = await loop.run_in_executor(None, self._ocr_one, str(p))
                if txt and len(txt) > 1:
                    out[Path(p).name] = txt
            except Exception:
                continue
        return out

    # ═══ Audio ═══
    async def download_audio(self, url: str) -> Optional[Path]:
        """下载音频(yt-dlp audio-only)
        [v2.19 安全 P0] SSRF/协议闸：与 image/file/video 通道对齐（此前音频通道无闸）。
        [v2.19.8] 平台直链 resolver 同样接在这里：网易云等音频平台的直链来自 weapi
        （resolver 产物），否则 yt-dlp 对内容页多半拿不到试听档。"""
        from .url_utils import is_private_url
        if not url or not str(url).startswith(("http://", "https://")) or is_private_url(url):
            logger.warning(f"音频下载拦截（SSRF/协议闸）: {str(url)[:60]}")
            return None
        _direct = await self.resolve_direct_url(url)
        if _direct:
            url = _direct
            # 直链来自平台 CDN：解析结果可能带签名参数，但 SSRF 闸仍需复检一次
            if is_private_url(url):
                logger.warning(f"音频直链被 SSRF 闸拦截: {url[:60]}")
                return None
        async with self.sem:
            try:
                import yt_dlp
                name=self._safe_name(url)
                out_tmpl=str(self._dom_dir(self.audio_dir, url)/f'{name}.%(ext)s')
                _wd_a = StallWatchdog()   # [本轮修复] 低速看门狗（见模块顶部常量说明）
                opts={'outtmpl':out_tmpl,'format':'bestaudio/best','quiet':True,
                      'no_warnings':True,'noplaylist':True,'socket_timeout':30,
                      'ignoreconfig': True,  # [FIXED & MODIFIED] v2.6.3 忽略外部配置（防 min_extracted_size 类报错）
                      # [本轮修复] 看门狗同样装上 —— "yt-dlp 下载要有低速上限"是一个能力，
                      # 视频/音频两条路各写一份就是本工程连续栽过的那个坑（只改一处 = 修一半）。
                      'progress_hooks': ytdlp_progress_hooks(_wd_a),
                      'postprocessors':[{'key':'FFmpegExtractAudio','preferredcodec':'mp3'}]}
                loop=asyncio.get_event_loop()
                def _dl():
                    prewarm_ytdlp_plugins()   # [v6] 单线程预热插件注册，防并发竞态刷屏
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        ydl.download([url])
                try:
                    await loop.run_in_executor(None,_dl)
                except Exception as _ae:
                    if _wd_a.aborted:
                        # 我们叫停的：音频那条路本来就不重试，这里只是别让它伪装成"网络错误"
                        logger.warning(f"音频下载低速卡死放弃: {url[:70]}")
                        return None
                    raise
                if _wd_a.aborted:
                    logger.warning(f"音频下载低速卡死放弃（产物不算数）: {url[:70]}")
                    return None
                # [FIXED & MODIFIED] v2.9.1 路径修正：实际落盘在域名子目录
                # （_dom_dir(audio_dir)/<name>.mp3）——原检查漏了子目录 → 永远 None
                mp3 = self._dom_dir(self.audio_dir, url) / f'{name}.mp3'
                if mp3.exists():
                    return mp3
                # 兜底：audio_dir 下搜新产物（防止扩展名/子目录差异）
                _before = set(self.audio_dir.rglob('*'))
                _after = set(self.audio_dir.rglob('*'))
                for _f in (_after - _before):
                    if _f.is_file() and _f.stat().st_size > 0 and not re.search(r'\.part$|\.ytdl$', _f.name):
                        return _f
                return None
            except Exception as e:
                logger.error(f"Audio download failed: {e}")
                return None

    # ═══ File: generic HTTP ═══
    async def download_file(self, url: str, subdir: str = '') -> Optional[Path]:
        """[FIXED & MODIFIED] v2.6.2 通用文件下载（mp4/mp3/pdf/zip 等任意直链——用户直链输入的核心通道）
        增强：防盗链 headers + 真实扩展名保留（原 pdf/zip 被强制改名 .mp4）+ 域名分类落盘"""
        # [FIXED & MODIFIED] v2.14 阶段3 SSRF 闸：私网/环回/非 http(s) 一律拒绝
        # （攻击页可驱使爬虫抓 127.0.0.1/169.254.169.254 云元数据并落盘带回传通道）
        from .url_utils import is_private_url
        if not url or not url.startswith(("http://", "https://")) or is_private_url(url):
            logger.warning(f"download_file SSRF/协议闸拒绝: {str(url)[:80]}")
            return None
        async with self.sem:
            try:
                # 域名分类目录（export/files/<domain>/）
                dom = (urlparse(url).netloc or "unknown").replace("www.", "")
                target = self.output_dir / "files" / dom
                target.mkdir(parents=True, exist_ok=True)
                name = self._safe_name(url)
                # 真实扩展名：URL 路径后缀（保持原格式；无扩展名才默认 .mp4）
                from urllib.parse import urlparse as _up
                _p = _up(url).path.lower()
                ext = None
                for e in ('.mp4', '.mkv', '.webm', '.flv', '.mov', '.avi', '.ts',
                          '.mp3', '.flac', '.wav', '.aac', '.ogg', '.m4a',
                          '.pdf', '.zip', '.rar', '.7z', '.doc', '.docx', '.xls', '.xlsx',
                          '.ppt', '.pptx', '.txt', '.epub', '.apk', '.exe', '.json', '.csv'):
                    if _p.endswith(e):
                        ext = e
                        break
                if not ext:
                    # [FIXED & MODIFIED] 兜底用 URL 真实后缀（jpg/png 等未列出的类型——原默认 .mp4 导致图片被改名）
                    _url_ext = _p.rsplit(".", 1)[-1] if "." in _p.rsplit("/", 1)[-1] else ""
                    ext = f".{_url_ext}" if _url_ext and len(_url_ext) <= 6 else ".mp4"
                path = target / f"{name}{ext}"
                # [v6 修复] UA 与**本会话的 TLS 伪装版本**同源（原硬编码 Chrome/120.0，
                # 而会话 impersonate 是 chrome136 —— UA↔JA3 版本失配）。
                from .fingerprint_consistency import TLS_IMPERSONATE_POOL, user_agent_for
                # [v6 修复] Referer 走**唯一实现** `url_utils.referer_for`：
                # 原为 `f"https://{host}/"`（**CDN 自己的主机**），而工程注释写明
                # "CDN 校验主站 Referer——无 Referer/错 Referer 直接 403"。
                from .url_utils import referer_for
                _ref = referer_for(url)
                headers = {"User-Agent": user_agent_for(TLS_IMPERSONATE_POOL[0])}
                if _ref:
                    headers["Referer"] = _ref
                # [FIXED & MODIFIED] curl_cffi 标准用法（原 async with session.get() 不支持 →
                # coroutine never awaited → download_file 从未成功——用户文件直链输入的致命 bug）
                # [FIXED & MODIFIED] v2.6.4 header 组合降级：部分站点对 Chrome UA + 无 cookie 请求 403
                # （w3.org 实测仅 Referer/无头反而 200）——多组合尝试兜底
                # [v2.16.1] 断点续传：先探测（size/resumable），再走 .part+Range 流式；
                # 失败保留 .part，下次任务自动续传。
                _probe = await probe_url(self.session, url, headers)
                _expect = _probe[0] if _probe else None
                _probe_resumable = _probe[1] if _probe else False
                if _probe_resumable is False:
                    # 服务器不支持断点 → 旧 .part 作废，从零下
                    try:
                        _old_part = path.with_suffix(path.suffix + ".part")
                        _old_part.unlink(missing_ok=True)
                    except Exception:
                        pass
                # [v6 修复] 降级组合里的裸 `{"Referer": f"https://{dom}/"}`（CDN 自己的主机）
                # 换成唯一实现推导的值——否则"兜底"发的是**错的** Referer，救不了 403。
                for _h in (headers, {"Referer": referer_for(url)}, {}):
                    try:
                        _ok = await stream_to_file(self.session, url, path, _h,
                                                   expect_total=_expect, retries=1)
                        if _ok:
                            logger.info(f"File downloaded: {path.name} ({path.stat().st_size} bytes)")
                            return path
                    except Exception:
                        continue
                return None
            except Exception as e:
                logger.debug(f"File download failed: {e}")
                return None

    # ═══ Helpers ═══
    def _safe_name(self, url: str) -> str:
        h=hashlib.md5(url.encode()).hexdigest()[:12]
        return h

    def _img_ext(self, url: str) -> str:
        u=urlparse(url).path.lower()
        for e in ['.jpg','.jpeg','.png','.gif','.webp','.bmp','.svg']:
            if e in u:return e
        return '.jpg'
