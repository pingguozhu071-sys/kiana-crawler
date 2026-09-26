"""Kiana Vnext Plus — v2.17 稳定性门禁修复测试（A1 批次）。

覆盖：GUI→引擎断链（identity_bundle/llm_budget）、渲染预算制、OmegaConf dict 型代理、
.part/.ytdl 清理豁免、frontier.close 幂等、Retry-After 上限、_graceful_shutdown 幂等守卫。
全离线：无网络、无真实 cookies、无浏览器。
"""
import sys, os, asyncio, sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault('KIANA_CRYPTO_KEY', 'kv_test')


class TestGuiWiringChain:
    """GUI 断链修复：键必须穿透 EngineBridge 翻译表 → run_crawler gcfg"""

    def test_identity_bundle_in_bridge_table(self):
        src = (Path(__file__).parent.parent / "launcher_v8.py").read_text(encoding="utf-8")
        assert '"identity_bundle": bool(cfg.get("identity_bundle", False)),' in src, \
            "EngineBridge 键翻译表必须透传 identity_bundle（v17 门禁修复）"

    def test_llm_budget_in_run_crawler_gcfg(self):
        src = (Path(__file__).parent.parent / "run_crawler.py").read_text(encoding="utf-8")
        assert '"llm_budget_month": int(cfg.get("llm_budget_month", 500)),' in src, \
            "run_crawler gcfg 必须透传 llm_budget_month（GUI 预算曾被静默固定 500）"

    def test_launcher_v9_emits_both_keys(self):
        src = (Path(__file__).parent.parent / "launcher_v9.py").read_text(encoding="utf-8")
        assert '"identity_bundle": bool(self.config.get("identity_bundle", False)),' in src
        assert '"llm_budget_month": int(self.config.get("llm_budget_month", 500)),' in src


class TestRenderBudgetCfg:
    """渲染兜底预算纯函数：默认 20 / 0=禁用 / 负值=禁用 / 异常=回退 20"""

    def test_default_and_values(self):
        from kiana_vnext_plus.page_processor import _render_budget_cfg
        assert _render_budget_cfg(None) == 20
        assert _render_budget_cfg({"browser_render_max": 7}) == 7
        assert _render_budget_cfg({"browser_render_max": "12"}) == 12

    def test_zero_disables(self):
        from kiana_vnext_plus.page_processor import _render_budget_cfg
        assert _render_budget_cfg({"browser_render_max": 0}) == 0
        assert _render_budget_cfg({"browser_render_max": -3}) == 0

    def test_bad_cfg_falls_back(self):
        from kiana_vnext_plus.page_processor import _render_budget_cfg
        assert _render_budget_cfg(object()) == 20  # 无 get 的对象
        assert _render_budget_cfg({"browser_render_max": "oops"}) == 20


class TestDeadConfigKeysRemoved:
    """B1 死配置键清理锁定：全仓零消费者的"未接线键"不得回潮（v2.17 门禁 B1 删 26 键）"""

    def test_dead_keys_absent_from_default_global(self):
        from kiana_vnext_plus.config import DEFAULT_GLOBAL
        for k in ("dns_servers", "doh_endpoint", "use_stealth_enhanced",
                  "tls_rotation_enabled", "tls_impersonate_pool",
                  "challenge_solver_types", "health_check_interval",
                  "health_check_enabled", "predictive_slow_threshold",
                  "predictive_streak_limit", "concurrency_adjust_factor",
                  "agent_score_sliding_window", "export_formats",
                  "pressure_check_interval", "connection_pool_size",
                  "connection_keepalive_seconds", "dns_cache_ttl",
                  "retry_max_backoff", "defense_tier_yellow_threshold",
                  "defense_tier_red_threshold", "recovery_acceleration_enabled"):
            assert k not in DEFAULT_GLOBAL, f"死键回潮: {k}"


class TestPlaceholderRenderPreQuality:
    """0-5b 占位渲染前置于质量闸：真·空壳页也能得到渲染兜底机会（预算内）"""

    @staticmethod
    def _make_processor(budget):
        from kiana_vnext_plus.page_processor import PageProcessor
        p = object.__new__(PageProcessor)
        p._log = lambda *a, **k: None
        p._render_success_count = 0
        s = type("_S", (), {"_browser_available": True})()
        s.solve = None  # 测试后注入
        cfg = type("_G", (), {
            "get": staticmethod(lambda k, d=None: budget if k == "browser_render_max" else d)})()
        p.crawler = type("_C", (), {"solver": s, "cfg": cfg})()
        from kiana_vnext_plus import parser
        p.parser = parser
        return p, s

    def test_hollow_page_gets_rendered(self):
        p, s = self._make_processor(budget=20)

        async def fake_solve(url, challenge_wait=None, extra_wait=None):
            html = ("<html><head><title>渲染后标题</title></head><body><h1>渲染后标题</h1>"
                    + ("<p>正文内容段落。</p>" * 40) + "</body></html>")
            return html, 200, None

        s.solve = fake_solve
        data, html = asyncio.run(p._maybe_render_placeholder(
            {"text": "短"}, "<h1>x</h1>", "https://tieba.baidu.com/p/1"))
        assert data.get("rendered") and data.get("title") == "渲染后标题"
        assert p._render_success_count == 1

    def test_zero_budget_skips_render(self):
        p, s = self._make_processor(budget=0)
        s.solve = lambda *a, **k: (_ for _ in ()).throw(AssertionError("预算 0 不应渲染"))
        data, html = asyncio.run(p._maybe_render_placeholder(
            {"text": "短"}, "<h1>x</h1>", "https://tieba.baidu.com/p/1"))
        assert "rendered" not in data

    def test_normal_page_skips_render(self):
        p, s = self._make_processor(budget=20)
        s.solve = lambda *a, **k: (_ for _ in ()).throw(AssertionError("普通页不应渲染"))
        data, html = asyncio.run(p._maybe_render_placeholder(
            {"title": "T", "text": "正文" * 40}, "<h1>T</h1>", "https://x.test/a"))
        assert "rendered" not in data


class TestDynamicPriorityFrontier:
    """B4b frontier.adjust_priority：delta 叠加 + 钳制 0-9"""

    def test_adjust_and_clamp(self, tmp_path):
        from kiana_vnext_plus.frontier import FrontierDB
        from kiana_vnext_plus.url_utils import url_hash
        db = FrontierDB(str(tmp_path / "f.db"))

        async def flow():
            await db.init_async()
            await db.push("https://x.test/a", force=True)
            h = url_hash("https://x.test/a")
            await db.flush()
            await db.adjust_priority(h, -1)
            await db.adjust_priority(h, +2)
            await db.adjust_priority(h, +99)
            await db.flush()
            async with db._read_conn.execute(
                    "SELECT priority FROM frontier WHERE url_hash=?", (h,)) as cur:
                row = await cur.fetchone()
            await db.close()
            return row[0]

        assert asyncio.run(flow()) == 9  # 5-1+2+99 → 钳制 9


class TestStrategyDynamicMutualExclusion:
    """0-4 兼容纠偏：B4b 证据按 priority ASC（命中-1=提前），bff 按 DESC 出队——
    同开语义反转，开启动态优先级强制 bfs（纯函数 _resolve_crawl_strategy）。"""

    def test_dynamic_with_bff_forces_bfs(self):
        from kiana_vnext_plus.crawler import _resolve_crawl_strategy
        assert _resolve_crawl_strategy("bff", True) == "bfs"

    def test_dynamic_with_bfs_or_dfs_keeps(self):
        from kiana_vnext_plus.crawler import _resolve_crawl_strategy
        assert _resolve_crawl_strategy("bfs", True) == "bfs"
        assert _resolve_crawl_strategy("dfs", True) == "dfs"

    def test_no_dynamic_keeps_bff(self):
        from kiana_vnext_plus.crawler import _resolve_crawl_strategy
        assert _resolve_crawl_strategy("bff", False) == "bff"
        assert _resolve_crawl_strategy(None, False) == "bfs"


class TestPlaceholderTrigger:
    """稳定性门禁真 bug：占位页判定恒真子句曾让所有正文<200 字页面无条件开浏览器渲染
    （~13s/页、无预算上限）。修复后语义锁定：普通短页不再触发。"""

    def test_normal_short_page_not_trigger(self):
        from kiana_vnext_plus.page_processor import _placeholder_trigger
        # 正文 4 字、标题正常、非论坛 URL、无贴吧标记 —— 修复前 = True（恒真子句），
        # 修复后 = False（普通短页不开浏览器）
        data = {"title": "T", "text": "text"}
        assert _placeholder_trigger(data, "https://x.test/a", "<h1>T</h1><p>text</p>") is False

    def test_empty_page_trigger(self):
        from kiana_vnext_plus.page_processor import _placeholder_trigger
        assert _placeholder_trigger({}, "https://x.test/a", "") is True
        assert _placeholder_trigger({"title": ""}, "https://x.test/a", "<html></html>") is True

    def test_placeholder_title_trigger(self):
        from kiana_vnext_plus.page_processor import _placeholder_trigger
        assert _placeholder_trigger(
            {"title": "百度贴吧", "text": "很短的占位"},
            "https://tieba.baidu.com/p/123", "<html>占位</html>") is True

    def test_tieba_and_zhihu_platform(self):
        from kiana_vnext_plus.page_processor import _placeholder_trigger
        assert _placeholder_trigger(
            {"title": "标题", "text": "短"},
            "https://tieba.baidu.com/p/1", "<html></html>") is True
        assert _placeholder_trigger(
            {"title": "知乎问题", "text": "短"},
            "https://www.zhihu.com/question/1", "<html><body>无容器</body></html>") is True
        assert _placeholder_trigger(
            {"title": "知乎问题", "text": "短"},
            "https://www.zhihu.com/question/1",
            '<div class="QuestionRichText" id="q">有正文容器</div>') is False

    def test_long_text_never_trigger(self):
        from kiana_vnext_plus.page_processor import _placeholder_trigger
        assert _placeholder_trigger(
            {"title": "T", "text": "内容" * 120},
            "https://tieba.baidu.com/p/1",
            "<html><p>很长</p></html>") is False


class TestProxyNodeCollector:
    """OmegaConf DictConfig（Mapping 非 dict）dict 型代理不得被丢弃"""

    def test_str_and_dict_nodes(self):
        from kiana_vnext_plus.crawler import _collect_proxy_nodes
        nodes = _collect_proxy_nodes([
            "http://a:1",
            {"url": "http://b:2", "country": "US"},
            "   ",      # 空串/空白不入池（原实现把空串也 add_node）
            12345,      # 非 str/Mapping 忽略
        ])
        assert ("http://a:1", None) in nodes
        assert ("http://b:2", "US") in nodes
        assert len(nodes) == 2

    def test_omegaconf_listconfig(self):
        from omegaconf import OmegaConf
        from kiana_vnext_plus.crawler import _collect_proxy_nodes
        cfg = OmegaConf.create({"proxy_list": [
            "http://a:1",
            {"url": "http://b:2", "country": "JP"},
        ]})
        nodes = _collect_proxy_nodes(list(cfg.proxy_list))
        assert nodes == [("http://a:1", None), ("http://b:2", "JP")], \
            "DictConfig 型节点被丢弃 = 稳压门禁回归"

    def test_returns_empty_for_none(self):
        from kiana_vnext_plus.crawler import _collect_proxy_nodes
        assert _collect_proxy_nodes(None) == []


class TestPurgeKeepsPart:
    """清理豁免：.part/.ytdl（断点续传）与副产物保留，垃圾删除"""

    def test_keep_suffixes_include_part_and_ytdl(self):
        from kiana_vnext_plus.crawler import KEEP_SUFFIXES
        assert ".part" in KEEP_SUFFIXES and ".ytdl" in KEEP_SUFFIXES

    def test_purge_removes_garbage_keeps_exempt(self, tmp_path):
        from kiana_vnext_plus.crawler import _purge_non_video
        vdir = tmp_path / "videos"
        vdir.mkdir()
        (vdir / "big.mp4").write_bytes(b"\x00" * (1024 * 1024 + 64))   # 真实视频（>1MB）
        (vdir / "movie.part").write_bytes(b"\x00" * 100)
        (vdir / "movie.ytdl").write_bytes(b"\x00" * 100)
        (vdir / "subs.vtt").write_bytes(b"WEBVTT")
        (vdir / "junk.html").write_text("<html>垃圾</html>", encoding="utf-8")
        (vdir / "empty.mp4").write_bytes(b"")                          # 0 字节伪视频→垃圾
        _purge_non_video(vdir)
        surv = sorted(p.name for p in vdir.iterdir())
        assert "junk.html" not in surv and "empty.mp4" not in surv
        assert "movie.part" in surv and "movie.ytdl" in surv and "subs.vtt" in surv
        assert "big.mp4" in surv


class TestFrontierCloseIdempotent:
    """close 双跑：二次调用零动作不挂起（crawler.run 收尾 + run_crawler 双调曾双 put(None)）"""

    def test_double_close_safe(self, tmp_path):
        from kiana_vnext_plus.frontier import FrontierDB

        async def flow():
            db = FrontierDB(str(tmp_path / "f.db"))
            await db.init_async()
            await db.write_error("h", "HTTP_429", "lim")
            await db.flush()
            await db.close()
            await db.close()          # 二次：无新队列哨兵、无异常
            assert db._flush_task is None
            assert db._read_conn is None

        asyncio.run(flow())


class TestProxyFetcher:
    """3-C 代理源拉取器：源解析/负载解析/形态校验（全离线）"""

    def test_parse_proxy_source(self):
        from kiana_vnext_plus.proxy_fetcher import parse_proxy_source
        ent = parse_proxy_source("api:https://p.example/api?token=x, 1.2.3.4:8080")
        assert ent[0] == {"kind": "api", "value": "https://p.example/api?token=x",
                          "country": None}
        assert ent[1] == {"kind": "static", "value": "1.2.3.4:8080", "country": None}

    def test_parse_payload_shapes(self):
        from kiana_vnext_plus.proxy_fetcher import parse_proxy_payload
        assert parse_proxy_payload('["http://a:1", "http://b:2"]') == [
            {"url": "http://a:1", "country": None}, {"url": "http://b:2", "country": None}]
        assert parse_proxy_payload(
            '{"data":{"proxies":[{"url":"http://c:3","country":"US"}]}}') == [
            {"url": "http://c:3", "country": "US"}]
        assert parse_proxy_payload("not json") == []

    def test_validate_proxy(self):
        from kiana_vnext_plus.proxy_fetcher import validate_proxy
        assert validate_proxy("http://1.2.3.4:8080") is True
        assert validate_proxy("socks5://a.b.c:1080") is True
        assert validate_proxy("1.2.3.4:8080") is True
        assert validate_proxy("127.0.0.1:8080") is False      # 私网拒绝
        assert validate_proxy("notaproxy") is False


class TestRobotsCrawlDelay:
    """1-5 robots Crawl-delay 解析与读取（默认关=零影响）"""

    def _mock_robots(self, monkeypatch, text):
        import kiana_vnext_plus.robots_policy as rp
        rp.clear()
        monkeypatch.setattr(rp, "_fetch_robots", lambda host, timeout=8: text)

    def test_parse_and_read(self, monkeypatch):
        import kiana_vnext_plus.robots_policy as rp
        self._mock_robots(monkeypatch, "User-agent: *\nDisallow: /private/\nCrawl-delay: 7\n")
        assert rp.is_allowed("https://a.test/public/x") is True
        assert rp.is_allowed("https://a.test/private/x") is False
        assert rp.crawl_delay_for("a.test") == 7.0

    def test_invalid_delay_defaults_zero(self, monkeypatch):
        import kiana_vnext_plus.robots_policy as rp
        self._mock_robots(monkeypatch, "User-agent: *\nCrawl-delay: soon\n")
        rp.is_allowed("https://b.test/x")
        assert rp.crawl_delay_for("b.test") == 0.0

    def test_unreachable_no_delay(self, monkeypatch):
        import kiana_vnext_plus.robots_policy as rp
        self._mock_robots(monkeypatch, None)
        rp.is_allowed("https://c.test/x")
        assert rp.crawl_delay_for("c.test") == 0.0


class TestAdaptiveUnification:
    """0-6 自适应唯一化：smart_adaptive 调参循环默认停（三方互搏纠偏），
    v2+autoscale 为唯一调参源；_smart_adaptive_tuning=True 显式恢复（仅取证）。"""

    def test_run_no_longer_starts_smart_adaptive_task(self):
        src = (Path(__file__).parent.parent / "kiana_vnext_plus" / "crawler.py").read_text(
            encoding="utf-8")
        assert "self._adaptive_task = None" in src
        assert 'asyncio.create_task(self.adaptive.run(self.concurrency))' not in src, \
            "smart_adaptive 调参任务默认不得启动"

    def test_config_key_defaults_false(self):
        from kiana_vnext_plus.config import DEFAULT_GLOBAL
        assert DEFAULT_GLOBAL.get("smart_adaptive_tuning") is False


class TestTlsPoolConsistency:
    """0-7 TLS 一致性：get_tls_profile 只可能返回池内版本（否则主客户端池缺版/
    UA 主版本失配）"""

    def test_profiles_only_from_pool(self):
        from kiana_vnext_plus.session_pool import SessionPool
        from kiana_vnext_plus.fingerprint_consistency import TLS_IMPERSONATE_POOL
        pool = SessionPool()
        seen = {pool.get_tls_profile(i) for i in range(60)}
        assert seen <= set(TLS_IMPERSONATE_POOL), f"池外版本: {seen - set(TLS_IMPERSONATE_POOL)}"
        assert "chrome133a" not in seen and "chrome123" not in seen

    def test_tier3_uses_pick_same_source(self):
        src = (Path(__file__).parent.parent / "kiana_vnext_plus" / "engine_router.py").read_text(
            encoding="utf-8")
        assert "pick_tls_impersonate(proxy or \"\")" in src


class TestCapRetryAfter:
    """Retry-After 上限 300s（服务器可控值不得挂起任务数天）"""

    def test_cap(self):
        from kiana_vnext_plus.protocol_engine import cap_retry_after
        assert cap_retry_after(99999) == 300
        assert cap_retry_after(10) == 10
        assert cap_retry_after(300) == 300
        assert cap_retry_after(301) == 300

    def test_weird_values(self):
        from kiana_vnext_plus.protocol_engine import cap_retry_after
        assert cap_retry_after(-5) == 0
        assert cap_retry_after("abc") == 0
        assert cap_retry_after(None) == 0


class TestGracefulShutdownGuard:
    """_graceful_shutdown 幂等守卫：已跑过则直接返回（双跑曾被多次执行）"""

    def test_second_call_noop(self):
        from kiana_vnext_plus.crawler import Crawler
        c = object.__new__(Crawler)
        c._shutdown_done = True
        # 裸对象无任何组件 attr——若守卫失效会 AttributeError
        asyncio.run(c._graceful_shutdown())
