# -*- coding: utf-8 -*-
"""登录态探针三态 + 错误聚合审计回归（M1-e）

两个真缺陷：
  ① `check_bilibili` 把"网络超时"与"登录态失效"**都**返回 ok=False，二者不可区分。
     一旦拿它冷却身份，一次线路抖动就会**误杀健康身份**。本文件用三态锁死：
     **只有明确 expired 才允许判死**。
  ② `errors` 表有 platform/code 列与写入路径，文档也写着"可 GROUP BY 聚合审计"，
     但**全仓没有一条 SQL 做它**。本文件锁死聚合真的能出结果。
"""
import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path

import kiana_vnext_plus.cookie_health as ch
from kiana_vnext_plus.frontier import FrontierDB

BILI_COOKIE = ".bilibili.com\tTRUE\t/\tFALSE\t0\tSESSDATA\tabc\n"


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


class _Boom:
    def __init__(self, exc):
        self._exc = exc

    def json(self):
        raise self._exc


class TestProbeTriState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kiana_probe_")
        self.cf = Path(self.tmp) / "cookies.txt"
        self.cf.write_text(BILI_COOKIE, encoding="utf-8")
        import curl_cffi.requests as ccr
        self._ccr = ccr
        self._orig = ccr.get

    def tearDown(self):
        self._ccr.get = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _probe(self):
        return ch.check_bilibili([str(self.cf)])

    # ── ① 三态 ──
    def test_ok_when_logged_in(self):
        self._ccr.get = lambda *a, **k: _Resp({"code": 0, "data": {"isLogin": True, "vipStatus": 1}})
        got = self._probe()
        self.assertEqual(got["status"], ch.PROBE_OK)
        self.assertTrue(got["ok"])

    def test_expired_when_server_explicitly_says_not_logged_in(self):
        self._ccr.get = lambda *a, **k: _Resp({"code": 0, "data": {"isLogin": False}})
        got = self._probe()
        self.assertEqual(got["status"], ch.PROBE_EXPIRED)
        self.assertFalse(got["ok"])
        self.assertTrue(ch.probe_should_punish(got["status"]), "明确未登录**可以**判死")

    def test_timeout_is_unknown_and_must_not_kill(self):
        """**本文件最重要的一条**：超时 ≠ 登录失效，不许判死。"""
        def _raise(*a, **k):
            raise TimeoutError("read timed out")
        self._ccr.get = _raise
        got = self._probe()
        self.assertEqual(got["status"], ch.PROBE_UNKNOWN)
        self.assertFalse(ch.probe_should_punish(got["status"]),
                         "网络超时**不得**判死（否则一次线路抖动就误杀健康身份）")
        self.assertIn("未判定", got["msg"], "必须说清是'没判定'而不是'失效了'")

    def test_risk_control_code_is_unknown_not_expired(self):
        """风控码（如 -412）不是登录态结论 → 不判死"""
        self._ccr.get = lambda *a, **k: _Resp({"code": -412, "data": {}})
        got = self._probe()
        self.assertEqual(got["status"], ch.PROBE_UNKNOWN)
        self.assertFalse(ch.probe_should_punish(got["status"]))

    def test_unparsable_body_is_unknown(self):
        self._ccr.get = lambda *a, **k: _Boom(ValueError("not json"))
        got = self._probe()
        self.assertEqual(got["status"], ch.PROBE_UNKNOWN)

    def test_no_cookie_is_unknown(self):
        empty = Path(self.tmp) / "empty.txt"
        empty.write_text("# no cookies here\n", encoding="utf-8")
        got = ch.check_bilibili([str(empty)])
        self.assertEqual(got["status"], ch.PROBE_UNKNOWN)
        self.assertFalse(ch.probe_should_punish(got["status"]))

    def test_should_punish_only_for_expired(self):
        self.assertTrue(ch.probe_should_punish(ch.PROBE_EXPIRED))
        for s in (ch.PROBE_OK, ch.PROBE_UNKNOWN, "", "weird"):
            self.assertFalse(ch.probe_should_punish(s), f"{s!r} 不得判死")

    def test_registry_only_lists_sites_with_real_probe(self):
        """诚实标注：只有真做了在线验证的站点才在注册表里"""
        self.assertEqual(list(ch.PROBE_REGISTRY), ["B站"])
        sites = ch.check_sites()
        weak = [s for s in sites.get("sites", []) if "note" in s]
        for s in weak:
            self.assertIn("弱检查", s["note"])
            self.assertNotIn("health", s, "弱检查站点**不许**带在线健康结论")


class TestErrorsAggregation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kiana_agg_")
        self.db = str(Path(self.tmp) / "f.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_errors_group_by_platform_code(self):
        async def flow():
            db = FrontierDB(self.db)
            await db.init_async()
            await db.write_error("h1", "http", "m1", platform="bilibili", code="412")
            await db.write_error("h2", "http", "m2", platform="bilibili", code="412")
            await db.write_error("h3", "http", "m3", platform="xiaohongshu", code="300012")
            await db.write_error("h4", "http", "m4")          # 无 platform/code
            await db.flush()
            rows = await db.errors_by_platform_code()
            await db.close()
            return rows

        rows = asyncio.run(flow())
        got = {(r["platform"], r["code"]): r["count"] for r in rows}
        self.assertEqual(got.get(("bilibili", "412")), 2)
        self.assertEqual(got.get(("xiaohongshu", "300012")), 1)
        self.assertEqual(got.get(("(未标注)", "(未标注)")), 1,
                         "空 platform/code 应归到 (未标注)，不能混成空串")
        # 降序：出现最多的排前面
        self.assertEqual(rows[0]["platform"], "bilibili")
        self.assertIsNotNone(rows[0]["last_ts"])

    def test_aggregation_returns_empty_without_read_conn(self):
        """未 init_async 时返回空表，且**不抛异常**"""
        db = FrontierDB(self.db)
        rows = asyncio.run(db.errors_by_platform_code())
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
