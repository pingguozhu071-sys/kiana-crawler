"""Kiana Vnext Plus — v2.13 阶段 2：冷却/风控等级持久化回归"""
import sys
import os
import asyncio
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault('KIANA_CRYPTO_KEY', 'kv_test')

from kiana_vnext_plus.frontier import FrontierDB
from kiana_vnext_plus.defense_protocols import DefenseTier, ShieldSoldier


class TestCooldownPersistence:
    def test_set_load_clear(self, tmp_path):
        async def flow():
            db = FrontierDB(str(tmp_path / "f.db"))
            await db.init_async()
            await db.set_domain_cooldown("x.com", time.time() + 600, 2)
            await db.set_domain_cooldown("y.com", time.time() + 600, 3)
            await db.flush()
            active = await db.load_active_cooldowns()
            assert "x.com" in active and active["x.com"][1] == 2
            # 清除后不再返回
            await db.clear_domain_cooldown("x.com")
            await db.flush()
            active = await db.load_active_cooldowns()
            assert "x.com" not in active and "y.com" in active

        asyncio.run(flow())

    def test_expired_filtered(self, tmp_path):
        async def flow():
            db = FrontierDB(str(tmp_path / "f.db"))
            await db.init_async()
            await db.set_domain_cooldown("old.com", time.time() - 5, 1)   # 已过期
            await db.set_domain_cooldown("live.com", time.time() + 600, 2)
            await db.flush()
            active = await db.load_active_cooldowns()
            assert "old.com" not in active and "live.com" in active

        asyncio.run(flow())

    def test_restart_recovery(self, tmp_path):
        """重启模拟：实例 A 写冷却 → 实例 B（新连接）读回——风控状态跨启动"""
        async def flow():
            db1 = FrontierDB(str(tmp_path / "f.db"))
            await db1.init_async()
            await db1.set_domain_cooldown("target.com", time.time() + 600, 3)
            await db1.flush()
            db2 = FrontierDB(str(tmp_path / "f.db"))  # "重启"后的新实例
            await db2.init_async()
            active = await db2.load_active_cooldowns()
            assert active.get("target.com", (None, None))[1] == 3

        asyncio.run(flow())


class TestDefensePersistence:
    def test_escalate_persists(self, tmp_path):
        async def flow():
            db = FrontierDB(str(tmp_path / "f.db"))
            await db.init_async()

            class FakeConcurrency:
                async def adjust_global(self, v, source=None): pass
                async def adjust_domain(self, d, v, source=None): pass
                async def forget_source_async(self, source): pass
                async def forget_domain_claim_async(self, d, source): pass

            class FakeCrawler:
                frontier = db
                concurrency = FakeConcurrency()
                exit_mgr = None
                _shielded_domains = set()

            shield = ShieldSoldier()
            shield.crawler = FakeCrawler()
            # 高风险 → 升级 + 落库
            await shield.defend("risk.com", 9)
            await asyncio.sleep(0.2)  # fire-and-forget create_task 需要事件循环轮转
            await db.flush()
            active = await db.load_active_cooldowns()
            assert "risk.com" in active
            assert active["risk.com"][1] >= 2  # 至少 ORANGE

        asyncio.run(flow())

    def test_start_recovery_restores(self, tmp_path):
        """启动恢复：重启后 _domain_states 从 cooldowns 表还原且重新降并发"""
        async def flow():
            db = FrontierDB(str(tmp_path / "f.db"))
            await db.init_async()
            await db.set_domain_cooldown("hot.com", time.time() + 600, 3)
            await db.flush()

            calls = {"global": None}

            class FakeConcurrency:
                async def adjust_global(self, v, source=None):
                    calls["global"] = v
                async def adjust_domain(self, d, v, source=None): pass
                async def forget_source_async(self, source): pass
                async def forget_domain_claim_async(self, d, source): pass

            class FakeCrawler:
                frontier = db
                concurrency = FakeConcurrency()
                exit_mgr = None
                _shielded_domains = set()

            shield = ShieldSoldier()
            shield.crawler = FakeCrawler()
            await shield.start_recovery_loop()   # 应恢复 hot.com 的 RED 并降并发
            shield._recovery_task.cancel()
            assert "hot.com" in shield._domain_states
            assert shield._domain_states["hot.com"].tier == DefenseTier.RED
            assert calls["global"] is not None and calls["global"] <= 10  # RED 并发极低

        asyncio.run(flow())
