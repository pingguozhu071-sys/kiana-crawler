"""Kiana Vnext Plus — v2.13 阶段 1：快赢三连回归

覆盖：限流器域桶归还语义、探测提速持久化、对数正态延迟分布、8-12Hz 生理震颤。
"""
import sys
import asyncio
import math
import statistics
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from kiana_vnext_plus.rate_limiter import RateLimiter


class TestTokenReturn:
    """域桶不足时归还全局令牌（v2.13 修复：原全局令牌被白吃）"""

    def test_global_token_returned_when_domain_empty(self):
        async def flow():
            rl = RateLimiter(global_rate=100.0, global_burst=100.0,
                             domain_rate=0.05, domain_burst=1)  # 域桶极小且恢复极慢
            rl._domain_buckets["x.com"].tokens = 0.0             # 排空域名桶
            before = rl._global.tokens
            try:
                await asyncio.wait_for(rl.acquire("https://x.com/a", 1), timeout=0.15)
            except asyncio.TimeoutError:
                pass
            after = rl._global.tokens
            return before, after
        before, after = asyncio.run(flow())
        # 域桶在等 → 全局令牌应已归还（after ≈ before；原实现会白扣 1 个）
        assert after >= before - 1e-6

    def test_normal_acquire_consumes_both(self):
        async def flow():
            rl = RateLimiter(global_rate=1000.0, global_burst=1000.0,
                             domain_rate=1000.0, domain_burst=1000.0)
            await rl.acquire("https://x.com/a", 1)
            assert rl._global.tokens < 1000.0      # 全局扣了
            assert rl._domain_buckets["x.com"].tokens < 1000.0  # 域名扣了
        asyncio.run(flow())


class TestProbeFix:
    """探测提速持久化到具体桶（原写错变量对新桶不生效）"""

    def test_probe_rate_persists_on_success(self):
        async def flow():
            rl = RateLimiter(global_rate=100.0, domain_rate=10.0, probe_interval=1)
            await rl.record("https://x.com/a", 200)  # 1 次成功即触发探测（interval=1）
            assert rl._domain_buckets["x.com"].rate > 10.0  # 提速落到桶上
        asyncio.run(flow())

    def test_probe_holds_on_backoff(self):
        async def flow():
            rl = RateLimiter(global_rate=100.0, domain_rate=10.0, probe_interval=1)
            await rl.record("https://x.com/a", 429)   # 先退避
            await rl.record("https://x.com/a", 200)   # 再成功触发探测
            assert rl._domain_buckets["x.com"].rate == 10.0  # 有退避不提速
        asyncio.run(flow())


class TestLognormalDelay:
    def test_right_skewed(self):
        from kiana_vnext_plus.trajectory_engine import lognormal_delay
        vals = [lognormal_delay(0.1, sigma=0.6, min_v=0.01) for _ in range(500)]
        med = statistics.median(vals)
        mean = statistics.fmean(vals)
        # 对数正态特征：均值 > 中位数（右偏长尾）
        assert mean > med * 1.05
        assert all(v >= 0.01 for v in vals)

    def test_never_below_min(self):
        from kiana_vnext_plus.trajectory_engine import lognormal_delay
        assert all(lognormal_delay(0.05, min_v=0.02) >= 0.02 for _ in range(200))


class TestTremor:
    def test_frequency_in_8_12hz_band(self, monkeypatch):
        from kiana_vnext_plus import trajectory_engine as te
        # 虚拟时钟：以 80Hz 采样 1 秒。12Hz 正弦在 80Hz 采样下的相邻样点差
        # 有物理上限：2*amp*sin(pi*12/80) ≈ 0.93 —— 超过它就意味着频率越界
        clock = {"t": 1000.0}

        def fake_mono():
            return clock["t"]
        monkeypatch.setattr(te.time, "monotonic", fake_mono)
        samples = []
        for _ in range(80):
            clock["t"] += 1 / 80
            x, y = te._physiological_tremor(100.0, 100.0, 1.0)
            samples.append(x - 100.0)
        # 1) 是振荡信号（正负两半都出现）
        assert any(v > 0.3 for v in samples) and any(v < -0.3 for v in samples)
        # 2) 频率不越界：相邻样点最大差 ≤ 12Hz 物理上限（留 15% 余量）
        max_step = max(abs(samples[i] - samples[i - 1]) for i in range(1, len(samples)))
        assert max_step <= 2.0 * math.sin(math.pi * 14 / 80) + 0.05  # ≤14Hz 上限
        # 3) 80 步内完整周期数应在 8-12 个（数峰值：局部极大且 >0.8）
        peaks = sum(1 for i in range(1, len(samples) - 1)
                    if samples[i] > samples[i - 1] and samples[i] >= samples[i + 1]
                    and samples[i] > 0.8)
        assert 7 <= peaks <= 14  # 8-12Hz 容差

    def test_amplitude_bounded(self):
        from kiana_vnext_plus.trajectory_engine import _physiological_tremor
        for _ in range(100):
            x, y = _physiological_tremor(100.0, 100.0, 1.0)
            assert abs(x - 100.0) <= 1.5 and abs(y - 100.0) <= 1.0
