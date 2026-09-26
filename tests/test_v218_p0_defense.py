"""v2.18 P0 回归：defense GREEN 恢复后 min-wins 诉求必须归还（P0-3）

旧雷：_apply_tier(GREEN) 因 TIER_CONCURRENCY[GREEN]=None 直接 return，
RED 期间 adjust_global(5,"defense") 永久残留 → 一次风控误判后全局并发钳 5、
域并发钳 1，直到重启（forget_source 全仓零调用）。
"""
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiana_vnext_plus.concurrency import ConcurrencyController
from kiana_vnext_plus.defense_protocols import DefenseTier, DomainDefenseState, ShieldSoldier


class FakeCrawler:
    def __init__(self):
        self.concurrency = ConcurrencyController(global_max=200, per_domain_default=5)
        self._shielded_domains = set()


class TestDefenseRecoveryReleasesClaims(unittest.TestCase):
    def _run(self, coro):
        return asyncio.new_event_loop().run_until_complete(coro)

    def test_red_then_green_restores_global_and_domain(self):
        """单域 RED→GREEN：全局回到默认、域并发回到默认（核心回归）"""
        async def go():
            crawler = FakeCrawler()
            shield = ShieldSoldier()
            shield.mount(crawler)
            cc = crawler.concurrency

            # 升级到 RED
            ds = DomainDefenseState("example.com")
            ds.tier = DefenseTier.RED
            shield._domain_states["example.com"] = ds
            await shield._apply_tier("example.com", DefenseTier.RED)
            self.assertEqual(cc.global_sem.max_permits, 5)
            self.assertEqual(cc.global_requests.get("defense"), 5)
            dom_sem = await cc._get_domain_sem("example.com")
            self.assertEqual(dom_sem.max_permits, 1)

            # 恢复到 GREEN
            ds.tier = DefenseTier.GREEN
            await shield._apply_tier("example.com", DefenseTier.GREEN)
            self.assertNotIn("defense", cc.global_requests,
                             "GREEN 恢复后 defense 全局诉求必须归还")
            self.assertEqual(cc.global_sem.max_permits, 200,
                             "一次风控误判后全局并发不得永久钳死")
            self.assertEqual(dom_sem.max_permits, cc.per_domain_default,
                             "域并发不得永久钳在 1")
        self._run(go())

    def test_multi_domain_strictest_global_survives_partial_recovery(self):
        """A 域 RED + B 域 ORANGE：全局=5；B 恢复 GREEN 后仍须 5；A 恢复后才归还"""
        async def go():
            crawler = FakeCrawler()
            shield = ShieldSoldier()
            shield.mount(crawler)
            cc = crawler.concurrency

            ds_a = DomainDefenseState("a.com")
            ds_a.tier = DefenseTier.RED
            ds_b = DomainDefenseState("b.com")
            ds_b.tier = DefenseTier.ORANGE
            shield._domain_states["a.com"] = ds_a
            shield._domain_states["b.com"] = ds_b
            await shield._apply_tier("a.com", DefenseTier.RED)
            await shield._apply_tier("b.com", DefenseTier.ORANGE)
            self.assertEqual(cc.global_sem.max_permits, 5,
                             "多域并存时全局诉求应取最严厉者")

            # B 恢复 GREEN：A 仍 RED，全局必须保持 5（旧雷：后写覆盖先写变 30/归还）
            ds_b.tier = DefenseTier.GREEN
            await shield._apply_tier("b.com", DefenseTier.GREEN)
            self.assertEqual(cc.global_sem.max_permits, 5)
            self.assertEqual(cc.global_requests.get("defense"), 5)

            # A 恢复 GREEN：归还诉求
            ds_a.tier = DefenseTier.GREEN
            await shield._apply_tier("a.com", DefenseTier.GREEN)
            self.assertNotIn("defense", cc.global_requests)
            self.assertEqual(cc.global_sem.max_permits, 200)
        self._run(go())

    def test_forget_source_async_recomputes(self):
        """forget_source_async 清诉求并重算生效上限"""
        async def go():
            cc = ConcurrencyController(global_max=100)
            await cc.adjust_global(5, source="defense")
            self.assertEqual(cc.global_sem.max_permits, 5)
            await cc.forget_source_async("defense")
            self.assertNotIn("defense", cc.global_requests)
            self.assertEqual(cc.global_sem.max_permits, 100)
        self._run(go())

    def test_domain_arbitration_min_wins(self):
        """分域仲裁：defense(1) 与 smart_adaptive(2) 并存取 min；defense 归还后回 2"""
        async def go():
            cc = ConcurrencyController(global_max=100, per_domain_default=5)
            await cc.adjust_domain("x.com", 2, source="smart_adaptive")
            await cc.adjust_domain("x.com", 1, source="defense")
            sem = await cc._get_domain_sem("x.com")
            self.assertEqual(sem.max_permits, 1)
            await cc.forget_domain_claim_async("x.com", "defense")
            self.assertEqual(sem.max_permits, 2,
                             "defense 归还后域并发应回到剩余诉求的 min")
        self._run(go())


if __name__ == "__main__":
    unittest.main()
