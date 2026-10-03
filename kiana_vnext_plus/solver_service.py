# -*- coding: utf-8 -*-
"""求解进程隔离：把浏览器求解做成**只监听 127.0.0.1 的本地服务**（M3-b）

**动机**：`solver_engine` 现在是**进程内池**。浏览器崩一次会拖累引擎——
工程用"渲染预算"（`browser_render_max`）在防，但那是**防量**，不是**防崩**。
本模块给的是"崩了重启服务、不重启引擎"的形态。

**三条必须守住的**（方案点名，任何一条破了这功能就是负资产）：

  ① **SSRF 闸不能因此失效**——**服务侧与引擎侧都要过 `is_private_url`**。
     只信一侧等于把闸挪了个位置；两处独立判定才是纵深防御
     （对齐 v2.19.6 那次"浏览器层单点闸"的教训）。
  ② **拦截态必须是 400**——403 在本工程里是"**请升级到浏览器**"的信号。
     求解服务恰恰**就是**浏览器层，回 403 会让上层把"被闸拦下"误读成"该升级"，
     于是拿同一个私网 URL 再走一遍没有闸的路径。**这是本工程最贵的一个语义坑。**
     504 = 求解超时（可重试）；503 = 服务不可用（可重试）。
  ③ **服务不可用时按既有失败语义降级**——可重试 vs 不可重试分清，
     **绝不静默假装成功**（返回 `ok=False` 且带 `retryable`）。

本模块**不改退出码契约**：客户端在引擎进程内，退出码仍由原链路决定。
"""
import asyncio
import json
import logging
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

from .url_utils import is_private_url

logger = logging.getLogger(__name__)

SERVICE_HOST = "127.0.0.1"          # **只监听回环**：不对外暴露
DEFAULT_PORT = 8765

# 状态码语义（与工程既有约定一致，改了就是事故）
ST_BLOCKED = 400      # 被 SSRF 闸拦下 —— **不可重试**（403 是"升级到浏览器"，不能占用）
ST_TIMEOUT = 504      # 求解超时 —— 可重试
ST_UNAVAILABLE = 503  # 服务不可用/浏览器崩 —— 可重试

REASON_BLOCKED = "blocked"
REASON_TIMEOUT = "timeout"
REASON_UNAVAILABLE = "unavailable"
REASON_ERROR = "error"


@dataclass(frozen=True)
class SolveOutcome:
    """求解结果。`retryable` 是**调用方唯一需要的失败语义**。"""
    ok: bool
    html: str = ""
    status: int = 0
    headers: dict = field(default_factory=dict)
    reason: str = ""
    retryable: bool = True
    detail: str = ""


def gate(url: str) -> Optional[SolveOutcome]:
    """SSRF 闸（复用于**服务侧与引擎侧**）。放行返回 None，否则返回不可重试的拦截结果。"""
    if not url or not str(url).startswith(("http://", "https://")):
        return SolveOutcome(False, reason=REASON_BLOCKED, retryable=False,
                            detail="只接受 http/https")
    try:
        if is_private_url(url):
            return SolveOutcome(False, reason=REASON_BLOCKED, retryable=False,
                                detail="私网/保留地址，拒绝求解")
    except Exception as e:
        # 判定失败按风险处理（与 url_utils 的既有取向一致：宁可拦错，不可放过）
        return SolveOutcome(False, reason=REASON_BLOCKED, retryable=False,
                            detail=f"闸判定异常，按拦截处理: {type(e).__name__}")
    return None


class SolverServiceClient:
    """引擎侧的客户端：**先过闸**，再请求本地求解服务。

    客户端在引擎进程内（`solve()` 经 `to_thread`，不冻事件循环）。
    """

    def __init__(self, host: str = SERVICE_HOST, port: int = DEFAULT_PORT,
                 timeout: float = 30.0):
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def solve_sync(self, url: str, *, proxy: Optional[str] = None,
                   timeout: Optional[float] = None) -> SolveOutcome:
        """同步实现。**不抛异常**——所有失败都转成带 `retryable` 的结果。"""
        blocked = gate(url)                      # ① 引擎侧闸
        if blocked is not None:
            return blocked

        import urllib.error
        import urllib.request
        payload = json.dumps({"url": url, "proxy": proxy or "",
                              "timeout": float(timeout or self.timeout)}).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/solve", data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=(timeout or self.timeout) + 10) as r:
                data = json.loads(r.read().decode("utf-8", "ignore"))
            return SolveOutcome(True, html=data.get("html", ""),
                                status=int(data.get("status", 0)),
                                headers=data.get("headers") or {})
        except urllib.error.HTTPError as e:
            return self._from_http_error(e)
        except Exception as e:
            # 连不上/超时 —— **可重试**，且绝不假装成功
            logger.warning(f"[求解服务] 不可用，按可重试失败处理: {type(e).__name__}")
            return SolveOutcome(False, reason=REASON_UNAVAILABLE, retryable=True,
                                detail=f"服务不可达: {type(e).__name__}")

    @staticmethod
    def _from_http_error(e) -> SolveOutcome:
        code = getattr(e, "code", 0)
        try:
            body = json.loads(e.read().decode("utf-8", "ignore"))
            detail = str(body.get("detail", ""))[:200]
        except Exception:
            detail = ""
        if code == ST_BLOCKED:
            # **不可重试**：重试或换浏览器通道都绕不过闸，必须让上层看见
            return SolveOutcome(False, reason=REASON_BLOCKED, retryable=False,
                                detail=detail or "被求解服务侧的闸拦下")
        if code == ST_TIMEOUT:
            return SolveOutcome(False, reason=REASON_TIMEOUT, retryable=True, detail=detail)
        if code == ST_UNAVAILABLE:
            return SolveOutcome(False, reason=REASON_UNAVAILABLE, retryable=True, detail=detail)
        return SolveOutcome(False, reason=REASON_ERROR, retryable=True,
                            detail=f"未预期状态码 {code}: {detail}")

    async def solve(self, url: str, *, proxy: Optional[str] = None,
                    timeout: Optional[float] = None) -> SolveOutcome:
        """异步入口：同步 IO 一律经 `to_thread`（工程硬要求）。"""
        return await asyncio.to_thread(self.solve_sync, url, proxy=proxy, timeout=timeout)


def make_handler(solver: Callable[..., tuple]):
    """构造 HTTP handler。`solver(url, proxy, timeout) -> (status, headers, html)`。"""

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, code: int, obj: dict):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):                       # noqa: N802 (http.server 约定)
            if self.path != "/solve":
                self._send(404, {"ok": False, "detail": "未知路径"})
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(n).decode("utf-8", "ignore") or "{}")
            except Exception as e:
                self._send(400, {"ok": False, "detail": f"请求体不可解析: {e}"})
                return
            url = str(req.get("url") or "")
            # **服务侧也要过闸**（纵深防御）——只信引擎侧等于把闸挪了个位置
            blocked = gate(url)
            if blocked is not None:
                self._send(ST_BLOCKED, {"ok": False, "detail": blocked.detail})
                return
            try:
                status, headers, html = solver(url, req.get("proxy") or "",
                                               float(req.get("timeout") or 30.0))
            except TimeoutError as e:
                self._send(ST_TIMEOUT, {"ok": False, "detail": str(e)[:200]})
                return
            except Exception as e:
                logger.warning(f"[求解服务] 求解失败: {type(e).__name__}")
                self._send(ST_UNAVAILABLE, {"ok": False, "detail": f"{type(e).__name__}"})
                return
            self._send(200, {"ok": True, "status": status, "headers": headers or {},
                             "html": html or ""})

        def log_message(self, *a):               # 静音，别把服务日志混进引擎日志
            pass

    return _Handler


def serve_in_thread(solver: Callable[..., tuple], port: int = 0):
    """在后台线程起服务。返回 `(server, port, thread)`；`port=0` 由系统分配。

    **只绑定 127.0.0.1**——不对外暴露，也不随引擎的并发模型扩散。
    """
    srv = ThreadingHTTPServer((SERVICE_HOST, int(port)), make_handler(solver))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, srv.server_address[1], t
