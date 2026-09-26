"""Kiana Vnext Plus — 阶段 4 性能拉满回归测试（v2.11）

覆盖：并发主控 min-wins 仲裁（4 控制器不再抢写）、硬件自适应并发、
timeBeginPeriod 高精度计时器、multiprocess_runner 诚实删除、压测工具可加载。
"""
import sys
import os
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ['KIANA_CRYPTO_KEY'] = 'kv_test'

PROJECT = Path(__file__).parent.parent


class TestMinWinsArbitration:
    """adjust_global 按来源 min-wins：defense 熔断不被其他控制器悄悄抬高"""

    def test_min_wins(self):
        from kiana_vnext_plus.concurrency import ConcurrencyController
        cc = ConcurrencyController(global_max=30)

        async def flow():
            await cc.adjust_global(24, source="autoscale")
            assert cc.global_sem.max_permits == 24
            await cc.adjust_global(10, source="adaptive_v2")
            assert cc.global_sem.max_permits == 10
            # defense RED 降到 5
            await cc.adjust_global(5, source="defense")
            assert cc.global_sem.max_permits == 5
            # adaptive 想抬回 20 → min 仍被 defense 压在 5（原实现会被抬到 20）
            await cc.adjust_global(20, source="adaptive_v2")
            assert cc.global_sem.max_permits == 5
            # defense 恢复到 30 → 生效值 = min(30, 20, 24) = 20
            await cc.adjust_global(30, source="defense")
            assert cc.global_sem.max_permits == 20

        asyncio.run(flow())

    def test_forget_source(self):
        from kiana_vnext_plus.concurrency import ConcurrencyController
        cc = ConcurrencyController(global_max=30)

        async def flow():
            await cc.adjust_global(5, source="defense")
            assert cc.global_sem.max_permits == 5
            cc.forget_source("defense")
            await cc.adjust_global(20, source="adaptive_v2")
            assert cc.global_sem.max_permits == 20  # defense 诉求移除后不再压低

        asyncio.run(flow())

    def test_sources_tracked(self):
        from kiana_vnext_plus.concurrency import ConcurrencyController
        cc = ConcurrencyController(global_max=30)

        async def flow():
            await cc.adjust_global(24, source="autoscale")
            await cc.adjust_global(12, source="smart_adaptive")
            r = cc.global_requests
            assert r["autoscale"] == 24 and r["smart_adaptive"] == 12

        asyncio.run(flow())


class TestHwAdaptiveConcurrency:
    """硬件自适应：占位默认 200 → f(cores)；显式配置不动"""

    def test_placeholder_replaced(self, monkeypatch):
        from omegaconf import OmegaConf
        from kiana_vnext_plus.config import GlobalConfig
        from kiana_vnext_plus.identity import ProjectIdentity
        from kiana_vnext_plus.crawler import Crawler
        proj = ProjectIdentity("t_hw", base_dir=PROJECT / ".tmp_test_projects")
        g = GlobalConfig(OmegaConf.create({"master_password": "pw",
                                           "global_concurrency": 200}))  # 占位默认
        c = Crawler(proj, g, worker_id="t")
        assert 8 <= c.concurrency.global_sem.max_permits <= 32
        import shutil
        shutil.rmtree(PROJECT / ".tmp_test_projects" / "t_hw", ignore_errors=True)

    def test_explicit_kept(self):
        from omegaconf import OmegaConf
        from kiana_vnext_plus.config import GlobalConfig
        from kiana_vnext_plus.identity import ProjectIdentity
        from kiana_vnext_plus.crawler import Crawler
        proj = ProjectIdentity("t_hw2", base_dir=PROJECT / ".tmp_test_projects")
        g = GlobalConfig(OmegaConf.create({"master_password": "pw",
                                           "global_concurrency": 64}))  # 用户显式
        c = Crawler(proj, g, worker_id="t")
        assert c.concurrency.global_sem.max_permits == 64
        import shutil
        shutil.rmtree(PROJECT / ".tmp_test_projects" / "t_hw2", ignore_errors=True)


class TestHighPrecisionTimer:
    def test_time_begin_period(self):
        from kiana_vnext_plus.win32_native import set_high_precision_timer
        if os.name != "nt":
            import pytest
            pytest.skip("仅 Windows")
        assert set_high_precision_timer() is True

    def test_called_by_apply_all(self, monkeypatch):
        # apply_all_optimizations 不传 init_power_plan 时也应调用计时器（爬虫路径）
        import kiana_vnext_plus.win32_native as w
        called = {"n": 0}
        monkeypatch.setattr(w, "set_high_precision_timer", lambda: called.__setitem__("n", 1) or True)
        w.apply_all_optimizations({}, init_power_plan=False)
        assert called["n"] == 1


class TestMultiprocessRemoved:
    def test_module_gone(self):
        assert not (PROJECT / "kiana_vnext_plus" / "multiprocess_runner.py").exists()

    def test_cli_no_run_subcommand(self):
        import kiana_vnext_plus.cli as cli
        import inspect
        src = inspect.getsource(cli.main)
        assert "MultiProcessRunner" not in src
        assert 'add_parser("run"' not in src


class TestStressTool:
    def test_loadable(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("stress_bench",
                                                      PROJECT / "tools" / "stress_bench.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        assert hasattr(m, "_run")
