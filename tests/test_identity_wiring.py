# -*- coding: utf-8 -*-
"""M1-c 引擎接线回归：页面级身份租约 + acquire/report 回路

要锁死的：
  ① **未配置弹药库时行为零变化**（直接走原抓取路径）；
  ② 身份可用 → 抓取前装入租约、抓取后清除（**绝不跨页残留**）并 report；
  ③ 状态码 → 反馈语义：403/429/503 限流、401 登录失效、其余成功；
  ④ 抓取抛异常 → 报 **NETWORK**（身份不被惩罚）且异常照样上抛；
  ⑤ 身份不可用 → **诚实降级**（照常抓取、不崩），租约不装；
  ⑥ 租约优先级：租约 > 打包池；用户自填 cookie（extra_headers）仍然最高。
"""
import asyncio
import unittest

from kiana_vnext_plus.cookie_armory import (
    Acquired, Unavailable, UnavailableReason, ReportResult,
)
from kiana_vnext_plus.page_processor import PageProcessor
from kiana_vnext_plus.protocol_engine import ProtocolEngine


class _Resp:
    def __init__(self, status):
        self.status_code = status


class _FakeProto:
    def __init__(self):
        self.leases = {}

    def install_cookie_lease(self, d, c):
        self.leases[d] = c

    def clear_cookie_lease(self, d):
        self.leases.pop(d, None)


class _FakeRouter:
    def __init__(self, resp=None, boom=None):
        self.protocol = _FakeProto()
        self._resp, self._boom = resp, boom
        self.calls = 0

    async def fetch(self, url, domain, job):
        self.calls += 1
        if self._boom:
            raise self._boom
        return self._resp


class _FakeArmory:
    def __init__(self, lease=None):
        self.lease = lease
        self.reports = []

    def acquire_identity(self, domain, **kw):
        return self.lease

    def report(self, domain, name, result):
        self.reports.append((domain, name, result))


class _PP:
    """把真实方法绑到最小替身上——不构造整个 Crawler"""

    def __init__(self, router, armory):
        self.router, self.cookie_armory = router, armory


def _run(pp, url="https://example.com/a", domain="example.com"):
    # 运行时才解析真实方法：这样"实现被拿掉"时是**逐条用例失败**，
    # 而不是整个模块收集期就报错（后者的证据强度弱得多）
    return asyncio.run(PageProcessor._fetch_with_identity(pp, url, domain, {}))


class TestFetchWithIdentity(unittest.TestCase):
    def test_zero_change_without_armory(self):
        r = _FakeRouter(_Resp(200))
        got = _run(_PP(r, None))
        self.assertEqual(got.status_code, 200)
        self.assertEqual(r.calls, 1)
        self.assertEqual(r.protocol.leases, {}, "未配置弹药库时不许装租约")

    def test_lease_installed_then_cleared_and_reported_ok(self):
        r = _FakeRouter(_Resp(200))
        arm = _FakeArmory(Acquired("example.com", "acc1", "SESSDATA=x"))
        _run(_PP(r, arm))
        self.assertEqual(r.protocol.leases, {}, "抓完必须清除租约（否则跨页残留=用错账号）")
        self.assertEqual(arm.reports, [("example.com", "acc1", ReportResult.OK)])

    def test_throttle_and_login_status_mapping(self):
        for status, want in ((403, ReportResult.THROTTLED), (429, ReportResult.THROTTLED),
                             (503, ReportResult.THROTTLED), (401, ReportResult.LOGIN_EXPIRED),
                             (200, ReportResult.OK), (404, ReportResult.OK)):
            with self.subTest(status=status):
                r = _FakeRouter(_Resp(status))
                arm = _FakeArmory(Acquired("example.com", "acc1", "c"))
                _run(_PP(r, arm))
                self.assertEqual(arm.reports[0][2], want, f"status={status} 映射错")

    def test_network_error_reports_network_and_reraises(self):
        """网络抖动**不惩罚身份**（防误杀），但异常必须照旧上抛"""
        r = _FakeRouter(boom=TimeoutError("read timed out"))
        arm = _FakeArmory(Acquired("example.com", "acc1", "c"))
        with self.assertRaises(TimeoutError):
            _run(_PP(r, arm))
        self.assertEqual(arm.reports, [("example.com", "acc1", ReportResult.NETWORK)])
        self.assertEqual(r.protocol.leases, {}, "异常路径也必须清除租约")

    def test_unavailable_degrades_honestly(self):
        """身份不可用 → 照常抓取（不崩），且**不装租约**、不 report"""
        r = _FakeRouter(_Resp(200))
        arm = _FakeArmory(Unavailable("example.com", UnavailableReason.NOT_PROVIDED, "没提供"))
        got = _run(_PP(r, arm))
        self.assertEqual(got.status_code, 200, "不可用时必须照常抓取（诚实降级）")
        self.assertEqual(r.calls, 1)
        self.assertEqual(r.protocol.leases, {}, "不可用时不得装租约")
        self.assertEqual(arm.reports, [], "没有身份就没有可反馈的对象")

    def test_empty_domain_bypasses_identity(self):
        r = _FakeRouter(_Resp(200))
        arm = _FakeArmory(Acquired("", "acc1", "c"))
        _run(_PP(r, arm), domain="")
        self.assertEqual(r.calls, 1)
        self.assertEqual(arm.reports, [])


class TestLeaseInjection(unittest.TestCase):
    def _headers(self, eng, url, extra=None):
        return eng._build_headers(url, None, extra)

    def test_lease_wins_over_provider(self):
        eng = ProtocolEngine()
        eng.install_cookie_lease("example.com", "LEASE=1")
        eng.identity_pool_provider = type("P", (), {"cookies_for": lambda self, u: "POOL=2"})()
        h = self._headers(eng, "https://example.com/a")
        self.assertEqual(h.get("Cookie"), "LEASE=1", "租约应优先于打包池")

    def test_user_supplied_cookie_still_wins(self):
        eng = ProtocolEngine()
        eng.install_cookie_lease("example.com", "LEASE=1")
        h = self._headers(eng, "https://example.com/a", {"Cookie": "USER=1"})
        self.assertEqual(h.get("Cookie"), "USER=1", "用户自填 cookie 优先级最高（既有语义不变）")

    def test_clear_removes_lease(self):
        eng = ProtocolEngine()
        eng.install_cookie_lease("example.com", "LEASE=1")
        eng.clear_cookie_lease("example.com")
        h = self._headers(eng, "https://example.com/a")
        self.assertNotIn("Cookie", h)

    def test_subdomain_falls_back_to_parent(self):
        eng = ProtocolEngine()
        eng.install_cookie_lease("example.com", "LEASE=1")
        h = self._headers(eng, "https://www.example.com/a")
        self.assertEqual(h.get("Cookie"), "LEASE=1", "子域应回退到父域（与 cookie 作用域一致）")

    def test_no_leases_is_zero_change(self):
        eng = ProtocolEngine()
        h = self._headers(eng, "https://example.com/a")
        self.assertNotIn("Cookie", h)
        self.assertEqual(eng.cookie_leases, {})


class TestDefaults(unittest.TestCase):
    def test_armory_disabled_by_default(self):
        from kiana_vnext_plus.config import DEFAULT_GLOBAL
        self.assertIs(DEFAULT_GLOBAL["cookie_armory_enabled"], False,
                      "按站弹药库必须默认关（唯一默认源在 DEFAULT_GLOBAL）")


if __name__ == "__main__":
    unittest.main()
