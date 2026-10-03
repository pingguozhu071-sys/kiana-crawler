# -*- coding: utf-8 -*-
"""重试策略**单一实现**守卫（frontier 的 SQLite / Redis 两个后端）

**发现的真问题**：`mark_failed` 在两个后端里**各写了一份**，于是 SQLite 上历次修好的
三件事在 Redis 后端**全部缺失**：

  ① **指数+抖动退避**（v2.14"重试风暴"修复：原固定 delay=30 → 同域 50 任务 30s 后
     同步重新出队形成周期性打波）—— Redis 仍是 `time.time() + delay`；
  ② **限流不消耗 retry_count**（v2.17 E-P1-3：原实现 3 次限流即 dead；
     限流不是真失败）—— Redis 忽略 `throttled`；
  ③ **`status != 'done'` 守卫**（v2.19.6 看门狗修复：页面已赢 CAS 但仍在持久化阶段被
     取消 → 原实现把 done 打回 retry → 整页重爬 + 重复导出）—— Redis 无守卫。

**修法不是"这次记得同步改两边"**，而是把策略抽成纯函数 `frontier.next_retry_state`
**唯一实现**，两个后端都调它——再次分叉必须发生在同一个地方，从而能被本文件挡住。
"""
import ast
import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kiana_vnext_plus.frontier import (                     # noqa: E402
    RETRY_BACKOFF_CAP, THROTTLE_MAX, next_retry_state,
)

PKG = ROOT / "kiana_vnext_plus"


class _FixedRng:
    """确定性 rng：uniform 恒返回 1.0（消掉抖动，便于断言）"""
    @staticmethod
    def uniform(a, b):
        return 1.0


class TestPolicyPure(unittest.TestCase):
    def test_no_retry_is_dead(self):
        st = next_retry_state(False, 0, 3)
        self.assertEqual(st["status"], "dead")
        self.assertIsNone(st["scheduled_at"])

    def test_off_by_one_allows_exactly_max_retries(self):
        """`max_retries=3` 必须允许 **3** 次重试（0→1→2 是 retry，3 才是 dead）。

        这正是原实现 `retry_count+1 < max_retries` 的 off-by-one：
        那版实际只允许 2 次。
        """
        for rc in (0, 1, 2):
            self.assertEqual(next_retry_state(True, rc, 3)["status"], "retry",
                             f"retry_count={rc} 应还能重试")
        self.assertEqual(next_retry_state(True, 3, 3)["status"], "dead")

    def test_backoff_is_exponential_and_capped(self):
        """① 退避必须随重试次数指数增长，并有上限（防"重试风暴"）"""
        d0 = next_retry_state(True, 0, 9, delay=30, rng=_FixedRng())["scheduled_at"]
        d1 = next_retry_state(True, 1, 9, delay=30, rng=_FixedRng())["scheduled_at"]
        d5 = next_retry_state(True, 5, 9, delay=30, rng=_FixedRng())["scheduled_at"]
        import time as _t
        now = _t.time()
        gap0, gap1, gap5 = d0 - now, d1 - now, d5 - now
        self.assertAlmostEqual(gap0, 30, delta=2)
        self.assertAlmostEqual(gap1, 54, delta=3)          # 30 × 1.8
        self.assertGreater(gap5, gap1, "必须递增")
        huge = next_retry_state(True, 30, 99, delay=30, rng=_FixedRng())["scheduled_at"] - now
        self.assertLessEqual(huge, RETRY_BACKOFF_CAP * 1.31,
                             "退避必须封顶，否则等于永不重试")

    def test_jitter_spreads_the_herd(self):
        """抖动：不传 rng 时同参数两次结果不应完全相同（否则仍会同步打波）"""
        a = next_retry_state(True, 3, 9, delay=30)["scheduled_at"]
        b = next_retry_state(True, 3, 9, delay=30)["scheduled_at"]
        self.assertNotEqual(a, b, "没有抖动 = 同域任务会同步重新出队（打波）")

    def test_throttled_does_not_consume_retry_count(self):
        """② 限流≠重试：**不许**动 retry_count，改记 throttle_count"""
        st = next_retry_state(True, 1, 3, throttle_count=0, throttled=True)
        self.assertEqual(st["status"], "retry")
        self.assertEqual(st["retry_count"], 1, "限流不得消耗 retry_count")
        self.assertEqual(st["throttle_count"], 1)

    def test_throttle_burst_is_bounded(self):
        """连续限流必须有上限——否则任务永驻 retry，主循环退出条件永不成立"""
        st = next_retry_state(True, 0, 3, throttle_count=THROTTLE_MAX - 1, throttled=True)
        self.assertEqual(st["status"], "dead")
        self.assertEqual(st["throttle_count"], 0, "转 dead 时计数清零")

    def test_none_values_are_tolerated(self):
        """Redis 取回的字段可能是 None/字符串——不得抛异常"""
        for rc, mx, tc in ((None, None, None), ("2", "3", "1")):
            st = next_retry_state(True, rc, mx, tc)
            self.assertIn(st["status"], ("retry", "dead"))


class _FakeRedis:
    def __init__(self, h):
        self.h = dict(h)
        self.zadds, self.zrems = [], []

    async def hgetall(self, key):
        return dict(self.h)

    async def hset(self, key, *a, **kw):
        if "mapping" in kw:
            self.h.update({k: str(v) for k, v in kw["mapping"].items()})
        elif len(a) >= 2:
            self.h[a[0]] = str(a[1])

    async def zadd(self, key, mapping):
        self.zadds.append(mapping)

    async def zrem(self, key, member):
        self.zrems.append(member)


class _FakeDb:
    def __init__(self):
        self.calls = []

    async def mark_failed(self, url_hash, retry=True, delay=30, throttled=False):
        self.calls.append((url_hash, retry, delay, throttled))


def _rf(h):
    from kiana_vnext_plus.redis_frontier import RedisFrontier
    rf = RedisFrontier.__new__(RedisFrontier)      # 绕开 __init__（不起真 Redis）
    rf.redis = _FakeRedis(h)
    rf.result_db = _FakeDb()
    rf._fallback_mode = False
    return rf


class TestRedisBackendUsesSharedPolicy(unittest.TestCase):
    def test_done_guard_blocks_resurrection(self):
        """③ 已完成的任务**不得**被打回 retry（看门狗修复）"""
        rf = _rf({"status": "done", "retry_count": "0", "max_retries": "3"})
        asyncio.run(rf.mark_failed("h1", retry=True, delay=30))
        self.assertEqual(rf.redis.h["status"], "done", "done 被改写了")
        self.assertEqual(rf.redis.zadds, [], "done 的任务不该被重新入队")
        self.assertEqual(rf.result_db.calls, [], "不该降级到 SQLite")

    def test_throttled_does_not_consume_retry_count(self):
        """Redis 后端也必须遵守"限流不消耗 retry_count"（本次修复的核心）"""
        rf = _rf({"status": "pending", "retry_count": "1", "max_retries": "3",
                  "throttle_count": "0"})
        asyncio.run(rf.mark_failed("h2", retry=True, delay=30, throttled=True))
        self.assertEqual(rf.redis.h["status"], "retry")
        self.assertEqual(rf.redis.h["retry_count"], "1", "限流消耗了 retry_count（旧 bug）")
        self.assertEqual(rf.redis.h["throttle_count"], "1")

    def test_backoff_is_not_the_fixed_delay(self):
        """① 必须是抖动退避，不是固定 delay —— 固定 delay = 退回打波版本"""
        import time as _t
        rf = _rf({"status": "pending", "retry_count": "3", "max_retries": "9"})
        before = _t.time()
        asyncio.run(rf.mark_failed("h3", retry=True, delay=30))
        scheduled = float(rf.redis.zadds[0]["h3"])
        # retry_count=3 → 30×1.8³ ≈ 175s（含抖动 0.7–1.3），远超固定 delay=30
        self.assertGreater(scheduled - before, 30 * 1.5,
                           "退避仍是固定 delay —— 说明没走到共享策略")

    def test_fallback_carries_throttled_semantics(self):
        """降级到 SQLite 时必须把 `throttled` 带下去，否则语义在降级路径上丢失"""
        rf = _rf({})
        asyncio.run(rf.mark_failed("h4", retry=True, delay=30, throttled=True))
        self.assertEqual(rf.result_db.calls, [("h4", True, 30, True)])


class TestNoDivergenceStructure(unittest.TestCase):
    """结构性守卫：两个后端都**必须**走同一个实现"""

    def _src(self, name):
        return (PKG / name).read_text(encoding="utf-8")

    def test_both_backends_call_the_shared_policy(self):
        for name in ("frontier.py", "redis_frontier.py"):
            self.assertIn("next_retry_state", self._src(name),
                          f"{name} 没有使用共享重试策略——它又会分叉")

    def test_no_inline_backoff_formula_outside_the_policy(self):
        """别处不得再出现 `1.8 **` 这种内联退避公式（分叉的典型来源）"""
        for name in ("frontier.py", "redis_frontier.py"):
            n = self._src(name).count("1.8 **")
            if name == "frontier.py":
                self.assertEqual(n, 1, "退避公式只应出现在 next_retry_state 里")
            else:
                self.assertEqual(n, 0, f"{name} 出现内联退避公式——应调用共享策略")

    def test_no_fixed_delay_reschedule_in_redis(self):
        """Redis 后端不得再有 `time.time() + delay` 式的固定退避"""
        src = self._src("redis_frontier.py")
        self.assertNotIn("time.time() + delay", src)

    def test_done_guard_present_in_redis(self):
        src = self._src("redis_frontier.py")
        tree = ast.parse(src)
        found = any(
            isinstance(n, ast.Compare) and "done" in ast.unparse(n)
            for n in ast.walk(tree))
        self.assertTrue(found, "redis_frontier 缺少 done 守卫")


if __name__ == "__main__":
    unittest.main()
