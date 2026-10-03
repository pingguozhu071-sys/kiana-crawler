# -*- coding: utf-8 -*-
"""身份层 acquire / report 原语回归（v6 收敛后的 CookieArmory 统一入口）

覆盖的都是"会静默失效"或"会误杀"的方向：
  ① 不可用时必须给**可读原因**（未提供 / 全冷却 / 额度用尽），且**不抛异常**；
  ② 反馈只影响**该身份**，不牵连同站其他身份；
  ③ **NETWORK 不惩罚身份**——网络抖动不等于身份坏了（防误杀）；
  ④ **冷板凳可复活**：连败 4 次到 0 分后，冷却到期必须能重新被选中（修永久拉黑）；
  ⑤ **并发不超发**：选择与占额在同一写事务内（否则 N 协程抢到同一身份）；
  ⑥ 记账失败**可读**（不再是 `except: pass`）；
  ⑦ 兼容 API（acquire / report_success / report_failure）语义不变。
"""
import threading
import unittest
import sqlite3
import tempfile
import shutil
from pathlib import Path

from kiana_vnext_plus.cookie_armory import (
    CookieArmory, Acquired, Unavailable, UnavailableReason, ReportResult,
)


class TestAcquireIdentity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kiana_arm_")
        self.db = str(Path(self.tmp) / "a.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── ① 可读原因 ──
    def test_no_identity_gives_not_provided(self):
        arm = CookieArmory(self.db, "pw")
        got = arm.acquire_identity("bilibili")
        self.assertIsInstance(got, Unavailable)
        self.assertEqual(got.reason, UnavailableReason.NOT_PROVIDED)
        self.assertTrue(got.detail, "不可用必须带可读说明")

    def test_all_cooling_gives_all_cooling(self):
        arm = CookieArmory(self.db, "pw")
        arm.add_account("bili", "acc1", "a=1")
        for _ in range(4):
            arm.report_failure("bili", "acc1")
        got = arm.acquire_identity("bili")
        self.assertIsInstance(got, Unavailable)
        self.assertEqual(got.reason, UnavailableReason.ALL_COOLING)

    def test_quota_exhausted_gives_quota_reason(self):
        arm = CookieArmory(self.db, "pw", quota_window_seconds=3600.0)
        arm.add_account("bili", "acc1", "a=1")
        with sqlite3.connect(self.db) as c:
            c.execute("UPDATE accounts SET quota_limit=1")
        self.assertIsInstance(arm.acquire_identity("bili"), Acquired)
        got = arm.acquire_identity("bili")          # 第 2 次：额度已用尽
        self.assertIsInstance(got, Unavailable)
        self.assertEqual(got.reason, UnavailableReason.QUOTA_EXHAUSTED)

    # ── ② 反馈隔离 ──
    def test_throttle_cools_only_that_identity(self):
        arm = CookieArmory(self.db, "pw")
        arm.add_account("bili", "bad", "a=1")
        arm.add_account("bili", "good", "a=2")
        arm.report("bili", "bad", ReportResult.THROTTLED)
        rows = {r["name"]: r for r in arm.list_accounts("bili")}
        self.assertLess(rows["bad"]["health"], 1.0, "被限流的身份应扣分")
        self.assertEqual(rows["good"]["health"], 1.0, "同站其他身份**不得**受影响")
        self.assertEqual(rows["good"]["cooldown_left"], 0)
        self.assertIsInstance(arm.acquire_identity("bili"), Acquired)

    # ── ③ NETWORK 不惩罚（防误杀）──
    def test_network_does_not_punish_identity(self):
        arm = CookieArmory(self.db, "pw")
        arm.add_account("bili", "acc1", "a=1")
        self.assertTrue(arm.report("bili", "acc1", ReportResult.NETWORK))
        rows = arm.list_accounts("bili")
        self.assertEqual(rows[0]["health"], 1.0, "网络问题**不得**扣分")
        self.assertEqual(rows[0]["cooldown_left"], 0, "网络问题**不得**冷却")
        self.assertIsInstance(arm.acquire_identity("bili"), Acquired)

    def test_network_is_recorded_as_reason(self):
        arm = CookieArmory(self.db, "pw")
        arm.add_account("bili", "acc1", "a=1")
        arm.report("bili", "acc1", ReportResult.NETWORK)
        with sqlite3.connect(self.db) as c:
            row = c.execute("SELECT last_error_kind, last_error_at FROM accounts").fetchone()
        self.assertEqual(row[0], "network", "原因仍要落库（便于审计），只是不扣分")
        self.assertIsNotNone(row[1])

    # ── ④ 冷板凳复活（修永久拉黑）──
    def test_cold_bench_revives_after_cooldown_expires(self):
        """连败 4 次到 0 分后，冷却到期必须能**重新被选中**。

        原实现：acquire 要求 health_score > 0.0，而扣分钳到 0.0 →
        该身份**永久**选不中，也永远等不到 report_success 的 +0.1 回血 = 永久拉黑。
        """
        arm = CookieArmory(self.db, "pw", cooldown_seconds=0.0)   # 冷却立即到期
        arm.add_account("bili", "acc1", "a=1")
        for _ in range(4):
            arm.report_failure("bili", "acc1")
        rows = arm.list_accounts("bili")
        self.assertEqual(rows[0]["health"], 0.0, "扣分到下限（惩罚效果保留）")
        got = arm.acquire_identity("bili")
        self.assertIsInstance(got, Acquired, "冷却到期后必须能复活，否则是永久拉黑")
        self.assertEqual(got.name, "acc1")
        # 复活分是**下限**而非满分：它应排在其他健康身份之后
        arm.add_account("bili", "acc2", "a=2")
        self.assertIsInstance(arm.acquire_identity("bili"), Acquired)

    def test_revived_identity_ranks_below_healthy(self):
        arm = CookieArmory(self.db, "pw", cooldown_seconds=0.0)
        arm.add_account("bili", "tired", "a=1")
        for _ in range(4):
            arm.report_failure("bili", "tired")
        arm.add_account("bili", "fresh", "a=2")
        got = arm.acquire_identity("bili")
        self.assertIsInstance(got, Acquired)
        self.assertEqual(got.name, "fresh", "复活的身份分低，必须排在健康身份之后")

    # ── ⑤ 并发不超发 ──
    def test_concurrent_acquire_does_not_overissue(self):
        """N 线程同时抢 **1 个**身份的 1 份额度 → 只能有 1 个成功。

        反证：若把选择与占额拆到两个事务（读→改→写跨界），这里会超发。
        """
        arm = CookieArmory(self.db, "pw", quota_window_seconds=3600.0)
        arm.add_account("bili", "only", "a=1")
        with sqlite3.connect(self.db) as c:
            c.execute("UPDATE accounts SET quota_limit=1")

        n = 8
        barrier = threading.Barrier(n)
        results = []
        lock = threading.Lock()

        def worker():
            barrier.wait()
            got = arm.acquire_identity("bili")
            with lock:
                results.append(got)

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(len(results), n)
        got_ok = [r for r in results if isinstance(r, Acquired)]
        self.assertEqual(len(got_ok), 1,
                         f"额度只有 1 份，却发出了 {len(got_ok)} 份（超发）")

    # ── ⑥ 记账失败可读 ──
    def test_report_returns_false_when_db_unwritable(self):
        """反证：落库失败必须**返回 False**，而不是像原来那样 `except: pass` 静默吞掉。"""
        arm = CookieArmory(self.db, "pw")
        arm.add_account("bili", "acc1", "a=1")
        arm.db_path = self.tmp          # 指向目录 → sqlite 打不开
        self.assertFalse(arm.report_success("bili", "acc1"))
        self.assertFalse(arm.report_failure("bili", "acc1"))
        self.assertFalse(arm.report("bili", "acc1", ReportResult.NETWORK))

    def test_acquire_reports_db_error_not_crash(self):
        """库故障必须给 DB_ERROR 且**不抛异常**（与"没有身份"区分开）"""
        arm = CookieArmory(self.db, "pw")
        arm.add_account("bili", "acc1", "a=1")
        arm.db_path = self.tmp
        got = arm.acquire_identity("bili")
        self.assertIsInstance(got, Unavailable)
        self.assertEqual(got.reason, UnavailableReason.DB_ERROR)

    # ── ⑦ 兼容 API ──
    def test_legacy_api_semantics_preserved(self):
        arm = CookieArmory(self.db, "pw")
        arm.add_account("bili", "acc1", "a=1")
        self.assertEqual(arm.acquire("bili"), "a=1")
        self.assertTrue(arm.report_success("bili", "acc1"))
        self.assertTrue(arm.report_failure("bili", "acc1"))
        self.assertIsNone(arm.acquire("bili"), "冷却中应返回 None（与旧语义一致）")

    def test_decrypt_failure_gives_readable_reason(self):
        """主密码不匹配 → DECRYPT_FAILED（可读），而不是含糊的「无可用账号」"""
        from cryptography.fernet import Fernet
        arm = CookieArmory(self.db, "pw")
        arm.add_account("bili", "acc1", "a=1")
        arm._fernet = Fernet(Fernet.generate_key())    # 换成另一把钥匙 → 解密必失败
        got = arm.acquire_identity("bili")
        self.assertIsInstance(got, Unavailable)
        self.assertEqual(got.reason, UnavailableReason.DECRYPT_FAILED)


if __name__ == "__main__":
    unittest.main()
