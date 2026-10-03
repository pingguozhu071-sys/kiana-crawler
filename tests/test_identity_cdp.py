# -*- coding: utf-8 -*-
"""CDP 接管（"绕登录墙"）引擎侧回归

要锁死的行为：
  ① 端口探测**不抛异常**，永远给 `(ok, 可读说明)`——供 GUI 在**起任务之前**判断；
  ② 接管失败**不再静默回退**：必须留下 ERROR 日志 + 失败计数 + 可读原因，
     因为"静默回退成自管浏览器"的表现是"登录后才可见的内容取不到"，极难定位；
  ③ 接管成功时原因清空、`_cdp_attached` 为真。

**本文件不启动任何浏览器**：接管分支用桩替身（patchright 的 driver 进程
既慢又占内存），探测用本机回环 HTTP 服务。
"""
import asyncio
import json
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

import kiana_vnext_plus.solver_engine as se


def _closed_port() -> int:
    """占一个端口再释放 → 得到一个几乎确定没人监听的端口号"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _VersionHandler(BaseHTTPRequestHandler):
    def do_GET(self):                                  # noqa: N802 (http.server 约定)
        body = json.dumps({"Browser": "Chrome/131.0.6778"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):                         # 静音，别污染测试输出
        pass


class TestProbeCdpEndpoint(unittest.TestCase):
    def test_closed_port_returns_readable_failure(self):
        ok, msg = se.probe_cdp_endpoint(_closed_port(), timeout=0.6)
        self.assertFalse(ok, "没监听的端口必须判为不可用")
        self.assertTrue(msg, "必须给出可读原因，供界面直接展示")
        self.assertIn("remote-debugging-port", msg, "提示里要写清怎么修")

    def test_open_port_with_devtools_endpoint(self):
        srv = HTTPServer(("127.0.0.1", 0), _VersionHandler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            ok, msg = se.probe_cdp_endpoint(port, timeout=2.0)
            self.assertTrue(ok, f"应探测成功，实际: {msg}")
            self.assertIn("Chrome/131", msg, "说明里带上浏览器版本，便于确认接对了哪个")
        finally:
            srv.shutdown()
            srv.server_close()

    def test_probe_never_raises_on_weird_input(self):
        """端口非法也不能抛——调用方是 GUI，抛异常等于界面炸掉"""
        for bad in (0, -1, 99999):
            ok, msg = se.probe_cdp_endpoint(bad, timeout=0.3)
            self.assertIsInstance(ok, bool)
            self.assertIsInstance(msg, str)


class _FakeChromium:
    def __init__(self, fail: bool):
        self._fail = fail

    async def connect_over_cdp(self, url):
        if self._fail:
            raise RuntimeError("Connection refused")
        return f"ATTACHED:{url}"

    async def launch(self, **kw):
        return "SELF_MANAGED_BROWSER"


class _FakePW:
    def __init__(self, fail):
        self.chromium = _FakeChromium(fail)


class _FakeEngineStarter:
    def __init__(self, fail):
        self._fail = fail

    async def start(self):
        return _FakePW(self._fail)


class TestCdpFallbackIsNotSilent(unittest.TestCase):
    def _engine(self):
        return se.SolverEngine(pool_size=1, cdp_attach=True, cdp_port=9222)

    def _patch(self, fail: bool):
        """把 patchright 启动器换成桩——**不启动真浏览器**"""
        self._orig_pr, self._orig_has = se.pr_async, se.HAS_PATCHRIGHT
        se.pr_async = lambda: _FakeEngineStarter(fail)
        se.HAS_PATCHRIGHT = True

    def _unpatch(self):
        se.pr_async, se.HAS_PATCHRIGHT = self._orig_pr, self._orig_has

    def test_attach_success_sets_flags(self):
        self._patch(fail=False)
        try:
            eng = self._engine()
            asyncio.run(eng._launch_browser())
            self.assertTrue(eng._cdp_attached)
            self.assertEqual(eng.cdp_attach_failures, 0)
            self.assertEqual(eng.cdp_fallback_reason, "")
            self.assertTrue(eng.browser.startswith("ATTACHED:"))
        finally:
            self._unpatch()

    def test_attach_failure_is_observable(self):
        """接管失败必须**可观测**：计数 + 可读原因 + ERROR 日志（原实现只有 warning）"""
        self._patch(fail=True)
        try:
            eng = self._engine()
            with self.assertLogs("kiana_vnext_plus.solver_engine", level="ERROR") as cm:
                asyncio.run(eng._launch_browser())
            self.assertEqual(eng.cdp_attach_failures, 1)
            self.assertIn("接管失败", eng.cdp_fallback_reason)
            self.assertFalse(eng._cdp_attached)
            self.assertEqual(eng.browser, "SELF_MANAGED_BROWSER", "仍应回退，不能整个失败")
            joined = "\n".join(cm.output)
            self.assertIn("不带你的登录态", joined,
                          "日志必须说清后果——用户才知道为什么内容不对")
        finally:
            self._unpatch()

    def test_no_cdp_configured_never_touches_probe(self):
        eng = se.SolverEngine(pool_size=1, cdp_attach=False)
        self.assertFalse(eng.cdp_attach)
        self.assertEqual(eng.cdp_attach_failures, 0)
        self.assertEqual(eng.cdp_fallback_reason, "")


if __name__ == "__main__":
    unittest.main()
