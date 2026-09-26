"""Kiana Vnext Plus — 阶段 2 反反爬拉满回归测试（v2.11）

覆盖：滑块阈值参数化（set/get + bezier 步数联动）、iframe 检测的 content_override 通道、
bench/stealth_ab 工具可加载。
"""
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ['KIANA_CRYPTO_KEY'] = 'kv_test'

PROJECT = Path(__file__).parent.parent


class TestSliderParams:
    """阈值参数化（bench --tune 网格搜索入口）"""

    def test_defaults(self):
        from kiana_vnext_plus.slider_vision import get_slider_params
        p = get_slider_params()
        assert p["template_threshold"] == 0.6
        assert p["edge_threshold"] == 0.45
        assert p["track_steps_min"] == 18

    def test_set_get_roundtrip(self):
        from kiana_vnext_plus.slider_vision import set_slider_params, get_slider_params
        orig = get_slider_params()
        try:
            set_slider_params(template_threshold=0.55, edge_threshold=0.40)
            p = get_slider_params()
            assert p["template_threshold"] == 0.55
            assert p["edge_threshold"] == 0.40
            assert p["track_steps_min"] == 18  # 未更新键保持
        finally:
            set_slider_params(**orig)

    def test_bezier_steps_follows_params(self):
        from kiana_vnext_plus.slider_vision import set_slider_params, get_slider_params, bezier_track
        orig = get_slider_params()
        try:
            set_slider_params(track_steps_min=5, track_steps_div=2)
            track = bezier_track(60)
            # 步数 = max(5, 60/2=30) = 30（+回退/停顿追加）→ 至少 30 步
            assert len(track) >= 30
            set_slider_params(track_steps_min=18, track_steps_div=6)
            track2 = bezier_track(60)
            assert len(track2) >= 18
        finally:
            set_slider_params(**orig)


class TestIframeDetection:
    """content_override 通道：挑战在子 frame 时聚合 HTML 检测"""

    def test_detect_extended_override(self):
        import asyncio
        from kiana_vnext_plus.captcha_solver_extended import ExtendedChallengeSolver

        class FakePage:
            url = "https://example.com/"

            async def content(self):
                return "<html>plain page no captcha</html>"

        s = ExtendedChallengeSolver(FakePage(), 30, None, None, None)
        # 主页面无挑战 → None
        assert asyncio.run(s.detect_extended()) is None
        # iframe 聚合内容含滑块关键词 → 检出（原实现只查主 page.content 必漏）
        r = asyncio.run(s.detect_extended(content_override="<html>nc_1_n1z slider</html>"))
        assert r == "slider_captcha"

    def test_unified_detect_passes_extra(self):
        # UnifiedChallengeSolver.detect(extra_html=...) 签名存在且默认值安全
        import inspect
        from kiana_vnext_plus.challenge_solver import UnifiedChallengeSolver
        params = inspect.signature(UnifiedChallengeSolver.detect).parameters
        assert "extra_html" in params
        assert params["extra_html"].default == ""


class TestToolsLoadable:
    def test_bench_imports(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("captcha_bench",
                                                      PROJECT / "tools" / "captcha_bench.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        assert len(m.DEFAULT_URLS) == 3
        assert m.SAMPLES_ROOT.name == "captcha_samples"

    def test_stealth_ab_imports(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("stealth_ab",
                                                      PROJECT / "tools" / "stealth_ab.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        assert m.DEFAULT_URLS


class TestDeadCodeRemoved:
    def test_combined_biometrics_gone(self):
        import kiana_vnext_plus.behavioral_biometrics as bb
        assert not hasattr(bb, "build_combined_biometrics")

    def test_stealth_init_gone(self):
        import kiana_vnext_plus.source_level_stealth as sls
        assert not hasattr(sls, "init_stealth_context")
        assert not hasattr(sls, "init_enhanced_stealth_context")
        assert not hasattr(sls, "build_persistent_context_options")
        # 已接线的代理信号清理/WebRTC 欺骗必须保留
        assert hasattr(sls, "apply_proxy_signal_cleanup")
        assert hasattr(sls, "apply_webrtc_ip_spoof")
