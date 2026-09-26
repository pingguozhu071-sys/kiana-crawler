"""Kiana Vnext Plus — 阶段 0 止血 16 项回归测试（v2.11）

覆盖：代理凭据脱敏、idle 行为签名、cookies 合并随机文件名+爬完即删+残留清理、
redis 惰性导入、抖音错误码语义化与 body 未初始化、图片/直连下载重试、死配置键删除。
"""
import sys
import os
import asyncio
import inspect
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ['KIANA_CRYPTO_KEY'] = 'kv_test'

import pytest
from kiana_vnext_plus.sanitizer import sanitize_proxy


class TestSanitizeProxy:
    """代理凭据脱敏（crawler/exit_manager/defense_protocols 三处日志接入）"""

    def test_creds_masked(self):
        assert sanitize_proxy("http://user:pass123@1.2.3.4:7897") == "http://***@1.2.3.4:7897"

    def test_no_creds_unchanged(self):
        assert sanitize_proxy("http://1.2.3.4:7897") == "http://1.2.3.4:7897"

    def test_garbage_safe(self):
        assert sanitize_proxy("not-a-url") == "not-a-url"


class TestIdleBehaviorSignature:
    """solver_engine.py:849 传 duration=0.3 恒 TypeError 的回归（签名修复后不再静默吞）"""

    def test_duration_param_exists(self):
        from kiana_vnext_plus.trajectory_engine import perform_idle_behavior
        params = inspect.signature(perform_idle_behavior).parameters
        assert "duration" in params
        assert params["duration"].default == 0.3


class TestCookieFileHardening:
    """%TEMP% 合并 cookies：随机文件名 + 爬完即删 + 启动清残留（v2.11 隐私加固）"""

    def test_random_name_and_cleanup(self, tmp_path, monkeypatch):
        from kiana_vnext_plus import universal_downloader as ud
        monkeypatch.setenv("TEMP", str(tmp_path))
        monkeypatch.delenv("KIANA_COOKIE_FILE", raising=False)
        src = tmp_path / "src_cookies.txt"
        src.write_text("# Netscape HTTP Cookie File\n"
                       ".bilibili.com\tTRUE\t/\tFALSE\t0\tSESSDATA\tabc\n", encoding="utf-8")
        monkeypatch.setenv("KIANA_COOKIE_FILES", str(src))
        ud.cleanup_cookie_file()
        out = ud._ensure_cookie_file()
        assert out is not None and out.exists()
        assert out.name.startswith("kiana_cookies_")
        assert out.name != "kiana_cookies_merged.txt"  # 不再用固定可预测名
        assert "SESSDATA" in out.read_text(encoding="utf-8")
        ud.cleanup_cookie_file()  # 爬完即删
        assert not out.exists()

    def test_stale_files_purged_on_merge(self, tmp_path, monkeypatch):
        from kiana_vnext_plus import universal_downloader as ud
        monkeypatch.setenv("TEMP", str(tmp_path))
        monkeypatch.delenv("KIANA_COOKIE_FILE", raising=False)
        stale = tmp_path / "kiana_cookies_old.txt"  # 崩溃/强杀遗留
        stale.write_text("x", encoding="utf-8")
        src = tmp_path / "src2.txt"
        src.write_text("# Netscape HTTP Cookie File\n"
                       ".douyin.com\tTRUE\t/\tFALSE\t0\tk\tv\n", encoding="utf-8")
        monkeypatch.setenv("KIANA_COOKIE_FILES", str(src))
        ud.cleanup_cookie_file()
        out = ud._ensure_cookie_file()
        assert out is not None and out.exists()
        assert not stale.exists()  # 历史残留被清
        ud.cleanup_cookie_file()


class TestRedisLazyImport:
    """redis 不在构建依赖 → 模块必须可无条件 import（换机构建回归）"""

    def test_module_imports(self):
        from kiana_vnext_plus import redis_frontier
        assert isinstance(redis_frontier.HAS_REDIS, bool)

    def test_init_raises_when_no_redis(self, monkeypatch):
        from kiana_vnext_plus import redis_frontier
        monkeypatch.setattr(redis_frontier, "HAS_REDIS", False)
        with pytest.raises(RuntimeError):
            redis_frontier.RedisFrontier("redis://localhost:6379/0", None)


class TestDouyinStatusErr:
    """抖音 detail API 错误码语义化（原所有码塞进一条模糊文案）"""

    def test_risk_control(self):
        from kiana_vnext_plus.douyin_resolver import _status_err
        assert "风控" in _status_err(2155)

    def test_login_expired(self):
        from kiana_vnext_plus.douyin_resolver import _status_err
        assert "登录态过期" in _status_err(2192)

    def test_server_error(self):
        from kiana_vnext_plus.douyin_resolver import _status_err
        assert "服务端" in _status_err(8)


class TestDouyinAwemeId:
    """extract_aweme_id body 未初始化 UnboundLocalError 回归"""

    def test_video_path(self):
        from kiana_vnext_plus.douyin_resolver import extract_aweme_id
        assert extract_aweme_id("https://www.douyin.com/video/7381234567890123456") == "7381234567890123456"

    def test_user_page_no_crash(self):
        # 非短链 + 无 id 正则 → 原 body UnboundLocalError；现安全返回 None
        from kiana_vnext_plus.douyin_resolver import extract_aweme_id
        assert extract_aweme_id("https://www.douyin.com/user/MS4wLjABAAAAxx") is None


class TestDownloadRetries:
    """单发零重试 → 3 次重试（图片/直连下载）"""

    def test_download_image_retries(self, tmp_path):
        from kiana_vnext_plus.universal_downloader import UniversalDownloader
        dl = UniversalDownloader(tmp_path)
        calls = {"n": 0}

        class FakeResp:
            status_code = 200
            content = b"imgdata"

        class FakeSession:
            async def get(self, *a, **k):
                calls["n"] += 1
                if calls["n"] < 3:
                    raise RuntimeError("transient")
                return FakeResp()

        dl.session = FakeSession()
        r = asyncio.run(dl.download_image("https://i0.hdslb.com/bfs/x.jpg"))
        assert r is not None and r.exists()
        assert calls["n"] == 3

    def test_download_direct_retries(self, tmp_path):
        from kiana_vnext_plus.media_downloader import MediaDownloader
        md = MediaDownloader(tmp_path)
        calls = {"n": 0}

        class FakeResp:
            status_code = 200
            content = b"filedata"
            headers = {}

            async def aiter_content(self):
                yield self.content

            def close(self):
                pass

        class FakeSession:
            async def get(self, *a, **k):
                calls["n"] += 1
                if calls["n"] < 3:
                    raise RuntimeError("transient")
                return FakeResp()

        md.session = FakeSession()
        ok = asyncio.run(md.download_direct("https://example.com/f.mp4", tmp_path / "f.mp4"))
        assert ok is True
        assert calls["n"] == 3
        assert (tmp_path / "f.mp4").read_bytes() == b"filedata"
        assert (tmp_path / "f.mp4").read_bytes() == b"filedata"


class TestDeadConfigKeys:
    """v2.11 删除的 10 个零读取死键回归（假开关误导配置方）"""

    def test_dead_keys_removed(self):
        from omegaconf import OmegaConf
        from kiana_vnext_plus.config import GlobalConfig
        g = GlobalConfig(OmegaConf.create({"master_password": "test-pw"}))
        for k in ("challenge_detect_enabled", "extended_challenge_enabled",
                  "ocr_local_enabled", "behavior_simulation_enabled",
                  "webrtc_protection", "canvas_protection", "webgl_protection",
                  "audio_protection", "font_protection", "media_downloader_enabled"):
            assert k not in g.cfg, f"死配置键未删除: {k}"
