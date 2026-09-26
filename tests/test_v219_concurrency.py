"""v2.19 并发回归：robots 门闸无竞态 / 渲染预算不超额 / 后台任务可追踪

对应 v2.19 优化批次 3.1-3.3。全部离线。
"""
import asyncio
import inspect
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class TestRobotsGateNoRace(unittest.TestCase):
    """robots Crawl-delay 门闸：原'读 last → await sleep → 写 now'跨 await 读改写，
    同域并发可击穿（合规问题）。改预约式占位后应严格串行。"""

    def test_polite_delay_serializes_same_domain(self):
        from kiana_vnext_plus.crawler import Crawler

        c = Crawler.__new__(Crawler)
        c._robots_respect = True
        c._robots_last_req = {}

        DELAY = 0.15
        # 注入固定 crawl-delay，避免依赖真实 robots 解析
        import kiana_vnext_plus.robots_policy as rp
        _orig = getattr(rp, "crawl_delay_for", None)
        rp.crawl_delay_for = lambda domain: DELAY
        try:
            async def go():
                t0 = time.monotonic()
                await asyncio.gather(*[c._polite_delay("example.com") for _ in range(4)])
                return time.monotonic() - t0

            elapsed = asyncio.new_event_loop().run_until_complete(go())
        finally:
            if _orig is not None:
                rp.crawl_delay_for = _orig

        # 4 个同域请求：第 1 个不等，后续 3 个各等 ~DELAY → 总计 >= 3*DELAY*0.85
        self.assertGreaterEqual(elapsed, DELAY * 3 * 0.85,
                                f"同域并发未被串行化（耗时 {elapsed:.3f}s）——Crawl-delay 被击穿")

    def test_polite_delay_disabled_is_noop(self):
        from kiana_vnext_plus.crawler import Crawler

        c = Crawler.__new__(Crawler)
        c._robots_respect = False
        t0 = time.monotonic()
        asyncio.new_event_loop().run_until_complete(c._polite_delay("example.com"))
        self.assertLess(time.monotonic() - t0, 0.05, "关闭 robots 时不得引入延迟")


class TestRenderBudgetNoOvershoot(unittest.TestCase):
    """渲染预算：原'读 → await solve → 写 +1'可超额突破 browser_render_max
    （而这个预算正是为防浏览器风暴）。改为先占位、失败回滚。"""

    def test_budget_increments_synchronously(self):
        """占位必须发生在 await 之前 —— 源码结构断言"""
        from kiana_vnext_plus.page_processor import PageProcessor
        for meth in (PageProcessor._maybe_render_placeholder, PageProcessor.process_job):
            src = inspect.getsource(meth)
            i_inc = src.find("_render_success_count = _rk + 1")
            if i_inc < 0:
                i_inc = src.find("_render_success_count = _render_ok + 1")
            i_await = src.find("await solver.")
            self.assertGreater(i_inc, -1, f"{meth.__name__} 应有预算占位")
            self.assertLess(i_inc, i_await,
                            f"{meth.__name__} 的预算占位必须早于 await solver（防并发超额）")

    def test_failed_render_rolls_back(self):
        """失败回滚逻辑存在"""
        from kiana_vnext_plus.page_processor import PageProcessor
        for meth in (PageProcessor._maybe_render_placeholder, PageProcessor.process_job):
            src = inspect.getsource(meth)
            self.assertIn("_render_success_count", src)
            self.assertTrue(("_ok2" in src) or ("_rendered_ok" in src),
                            f"{meth.__name__} 应有失败回滚标记")


class TestBackgroundTaskTracking(unittest.TestCase):
    """评论采集等 fire-and-forget 任务须持强引用并纳入 shutdown 取消名单"""

    def test_comment_task_tracked(self):
        from kiana_vnext_plus.crawler import Crawler
        src = inspect.getsource(Crawler)
        self.assertIn("_collect_bili_comments", src)
        # 创建处必须同时登记强引用（原裸 create_task 可被 GC 静默吞掉）
        self.assertIn("_emit_bg.add(_t_cmt)", src.replace(" ", ""),
                      "评论任务创建后必须加入强引用集合")

    def test_shutdown_cancels_bg_tasks(self):
        from kiana_vnext_plus.crawler import Crawler
        src = inspect.getsource(Crawler._graceful_shutdown)
        self.assertIn("_emit_bg", src, "shutdown 必须把后台任务集纳入取消名单")

    def test_emit_bg_initialized(self):
        from kiana_vnext_plus.crawler import Crawler
        src = inspect.getsource(Crawler)
        self.assertIn("_emit_bg", src, "必须初始化 _emit_bg 强引用集合")
        self.assertIn("add_done_callback(self._emit_bg.discard)", src.replace("  ", " "),
                      "强引用集合需在任务结束时自动剔除")


class TestBlockingOffload(unittest.TestCase):
    """simhash / sanitize / 文件 IO 不得直压事件循环"""

    def test_cpu_heavy_calls_use_to_thread(self):
        from kiana_vnext_plus.page_processor import PageProcessor
        src = inspect.getsource(PageProcessor.process_job)
        self.assertIn("to_thread(simhash_64", src, "simhash_64 应走线程池")
        self.assertIn("to_thread(sanitize_text", src, "全页 sanitize 应走线程池")

    def test_save_extracted_offloaded(self):
        from kiana_vnext_plus.page_processor import PageProcessor
        src = inspect.getsource(PageProcessor._persist_export)
        self.assertIn("to_thread(self._save_extracted_file", src,
                      "文件 IO 应走线程池")


class TestQualityCleanups(unittest.TestCase):
    """批次 4 的清理项（防回退）"""

    def test_no_undefined_domainstate(self):
        import subprocess
        r = subprocess.run([sys.executable, "-m", "ruff", "check", "--select", "F821",
                            str(Path(__file__).resolve().parents[1] / "kiana_vnext_plus")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, f"F821 未定义名必须为 0:\n{r.stdout[:400]}")

    def test_dead_accessors_removed(self):
        from kiana_vnext_plus.config import GlobalConfig
        self.assertFalse(hasattr(GlobalConfig, "get_tls_pool"),
                         "死访问器 get_tls_pool 应已删除")
        self.assertFalse(hasattr(GlobalConfig, "get_challenge_types"),
                         "死访问器 get_challenge_types 应已删除")

    def test_gate_baseline_locked(self):
        """门禁静态基线应为锁死值（84/104），不得回到 100/120 的宽松余量"""
        src = (Path(__file__).resolve().parents[1] / "tools" / "release_check.py").read_text(
            encoding="utf-8")
        self.assertIn("RUFF_MAX", src)
        self.assertIn("84", src)
        self.assertNotIn("≤100/≤120", src, "不得回退到旧的宽松基线")


if __name__ == "__main__":
    unittest.main()
