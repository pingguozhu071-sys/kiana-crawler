# -*- coding: utf-8 -*-
"""求解进程隔离回归（M3-b）

要锁死的三条（方案点名，破一条这功能就是负资产）：
  ① **服务侧与引擎侧都要过 SSRF 闸**——只信一侧等于把闸挪了个位置；
  ② **拦截态必须是 400**——403 在本工程是"请升级到浏览器"的信号，占用它会
     让上层把"被闸拦下"误读成"该换浏览器通道"，于是拿同一个私网 URL 再走一遍
     **没有闸**的路径（v2.19.6 那次事故正是这个形状）；
  ③ **服务不可用按既有失败语义降级**——`retryable` 分清，且**绝不假装成功**。

本文件用**本机回环 HTTP 服务**做端到端，**不启动任何浏览器**（零显存）。
"""
import sys
import unittest
# [v6 修复·实测发现的不稳定] 本文件原先一律用 `timeout=5` 打本地回环。
# 全量跑时偶发失败（单独跑 15/15 全过）——**这些用例验的是 HTTP 状态码
# （400/404/504），不是延迟**，5 秒把"机器忙不忙"扯进了判据。
# 前序测试可能留下后台线程抢 GIL（引擎/浏览器都是多线程），5 秒会被拖爆。
# 改为 30 秒：**判据不变（状态码），只是不再顺带考核机器负载**。
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kiana_vnext_plus import solver_service as ss      # noqa: E402

PRIVATE_URLS = ("http://127.0.0.1/secret", "http://localhost:8080/x",
                "http://169.254.169.254/latest/meta-data/", "http://192.168.1.1/")


def _ok_solver(status=200, headers=None, html="<html>ok</html>"):
    def _s(url, proxy, timeout):
        return status, headers or {"content-type": "text/html"}, html
    return _s


class TestServiceBinding(unittest.TestCase):
    def test_binds_loopback_only(self):
        """服务**只监听回环**——不对外暴露"""
        srv, port, _ = ss.serve_in_thread(_ok_solver())
        try:
            self.assertEqual(srv.server_address[0], "127.0.0.1")
            self.assertEqual(ss.SERVICE_HOST, "127.0.0.1")
        finally:
            srv.shutdown()
            srv.server_close()


class TestEngineSideGate(unittest.TestCase):
    def test_private_urls_blocked_before_any_request(self):
        """① 引擎侧先拦：**在服务都连不上的端口上**也必须返回 blocked，
        说明闸在发请求之前就生效了。"""
        c = ss.SolverServiceClient(port=1)   # 没人监听的端口
        for url in PRIVATE_URLS:
            r = c.solve_sync(url)
            self.assertFalse(r.ok)
            self.assertEqual(r.reason, ss.REASON_BLOCKED, f"{url} 应被闸拦下")
            self.assertFalse(r.retryable, "被闸拦下**不可重试**")

    def test_bad_scheme_blocked(self):
        c = ss.SolverServiceClient(port=1)
        for url in ("ftp://x/y", "file:///etc/passwd", ""):
            r = c.solve_sync(url)
            self.assertFalse(r.ok)
            self.assertEqual(r.reason, ss.REASON_BLOCKED)


class TestServiceSideGate(unittest.TestCase):
    """② 服务侧独立过闸——**绕过客户端**直接打服务"""

    def setUp(self):
        self.called = []
        srv, self.port, _ = ss.serve_in_thread(
            lambda u, p, t: (self.called.append(u), (200, {}, "x"))[1])
        self.srv = srv

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def _raw_post(self, url):
        import json
        import urllib.error
        import urllib.request
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/solve",
            data=json.dumps({"url": url}).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    def test_private_url_gets_400_not_403(self):
        """**本文件最重要的一条**：拦截态必须是 **400**。403 是"升级到浏览器"的信号。

        注意这里断言的是**字面量 400**，不是 `ss.ST_BLOCKED`——
        状态码是对外**契约**，用定义它的常量去断言它，等于"改了常量测试还绿"
        （本轮已经踩过一次同类坑：靠源码文本/常量断言，变异注入了却测不出来）。
        """
        for url in PRIVATE_URLS:
            code, _ = self._raw_post(url)
            self.assertEqual(code, 400, f"{url} 应返回字面量 400")
            self.assertNotEqual(code, 403, "403 在本工程是'升级到浏览器'——绝不能占用")
        self.assertEqual(self.called, [], "被拦的 URL **不得**触达真正的求解器")

    def test_unknown_path_is_404(self):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/nope", data=b"{}",
                                     method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=30)
        self.assertEqual(cm.exception.code, 404)


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self.port = None

    def _start(self, solver):
        srv, port, _ = ss.serve_in_thread(solver)
        self.srv, self.port = srv, port
        return ss.SolverServiceClient(port=port, timeout=3)

    def tearDown(self):
        if getattr(self, "srv", None):
            self.srv.shutdown()
            self.srv.server_close()

    def test_success_roundtrip(self):
        c = self._start(_ok_solver(html="<html>hello</html>"))
        r = c.solve_sync("https://example.com/a")
        self.assertTrue(r.ok, r)
        self.assertEqual(r.html, "<html>hello</html>")
        self.assertEqual(r.status, 200)

    def test_solver_timeout_is_retryable(self):
        def _slow(url, proxy, timeout):
            raise TimeoutError("求解超时")
        c = self._start(_slow)
        r = c.solve_sync("https://example.com/a")
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, ss.REASON_TIMEOUT)
        self.assertTrue(r.retryable, "超时应可重试")

    def test_solver_crash_is_retryable(self):
        def _boom(url, proxy, timeout):
            raise RuntimeError("浏览器崩了")
        c = self._start(_boom)
        r = c.solve_sync("https://example.com/a")
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, ss.REASON_UNAVAILABLE)
        self.assertTrue(r.retryable, "服务侧求解失败应可重试")

    def test_service_killed_midrun_degrades_gracefully(self):
        """③ 服务中途被杀 → 客户端**降级为可重试**，不崩、也不假装成功"""
        c = self._start(_ok_solver())
        self.assertTrue(c.solve_sync("https://example.com/a").ok)
        self.srv.shutdown()
        self.srv.server_close()
        self.srv = None
        r = c.solve_sync("https://example.com/a")
        self.assertFalse(r.ok)
        self.assertTrue(r.retryable, "服务没了必须可重试")
        self.assertIn(r.reason, (ss.REASON_UNAVAILABLE, ss.REASON_ERROR))

    def test_async_entry_works(self):
        import asyncio
        c = self._start(_ok_solver(html="<html>async</html>"))
        r = asyncio.run(c.solve("https://example.com/a"))
        self.assertTrue(r.ok, r)
        self.assertEqual(r.html, "<html>async</html>")


class TestGateHelper(unittest.TestCase):
    def test_gate_passes_public_and_blocks_private(self):
        self.assertIsNone(ss.gate("https://example.com/x"))
        for url in PRIVATE_URLS:
            self.assertIsNotNone(ss.gate(url), f"{url} 应被拦")
            self.assertFalse(ss.gate(url).retryable, "闸拦截一律不可重试")


if __name__ == "__main__":
    unittest.main()
