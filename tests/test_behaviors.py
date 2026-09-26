"""Kiana Vnext Plus — 关键行为测试（v2.10.5c）

补 test_core.py 只测常量的短板：覆盖种子分流、垃圾URL过滤、隐私脱敏、
抖音高码率选择、cookie 合并等核心行为。
"""
import sys
import os
import asyncio
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ['KIANA_CRYPTO_KEY'] = 'kv_test'
os.environ.setdefault('KIANA_COOKIE_FILES', '')

import pytest
from kiana_vnext_plus.sanitizer import sanitize_text
from kiana_vnext_plus.ultimate_core_v4 import FullSourceExtractor
from kiana_vnext_plus.crawler import _is_real_video_file
from kiana_vnext_plus.universal_downloader import UniversalDownloader
from kiana_vnext_plus.fingerprint_consistency import FingerprintGenerator


class TestSanitizer:
    """隐私脱敏边界（v2.10.5 修复的核心）"""

    def test_phone_adjacent_cjk(self):
        # 手机号紧贴中文必须脱敏（原 \b 在汉字/数字间不生效）
        assert sanitize_text("联系我13812345678或邮件") == "联系我[手机号]或邮件"

    def test_phone_standalone(self):
        assert "[手机号]" in sanitize_text("手机 13911112222 联系")

    def test_email(self):
        assert "[邮箱]" in sanitize_text("发 test@example.com 给我")

    def test_ip(self):
        assert "[IP]" in sanitize_text("地址 192.168.1.1 的服务器")

    def test_ip_invalid_range_not_masked(self):
        # 超范围 IP 不应误伤
        assert "[IP]" not in sanitize_text("版本 300.1.2.3 是无效IP")

    def test_email_adjacent_cjk(self):
        assert "[邮箱]" in sanitize_text("邮箱admin@qq.com.cn和中文")


class TestJunkLinkFilter:
    """垃圾 URL 过滤（v2.10.4/5 修复）"""

    def test_html_fragment_url(self):
        extractor = FullSourceExtractor()
        html = '<a href="https://www.ruanyifeng.com/li class=\'module-list-item\'>Email：</a>'
        links = extractor.extract(html, "https://www.ruanyifeng.com/")
        # 含空格的垃圾 URL 不应出现在结果中
        assert not any(' ' in u or '<' in u or '>' in u for u in links)

    def test_real_link_kept(self):
        extractor = FullSourceExtractor()
        html = '<a href="https://www.ruanyifeng.com/blog/2026/08/weekly.html">文章</a>'
        links = extractor.extract(html, "https://www.ruanyifeng.com/")
        assert 'https://www.ruanyifeng.com/blog/2026/08/weekly.html' in links

    def test_js_string_no_html_tail(self):
        extractor = FullSourceExtractor()
        html = '<script>var u="https://www.example.com/a?x=1"</script>'
        links = extractor.extract(html, "https://www.ruanyifeng.com/")
        assert 'https://www.example.com/a?x=1' in links


class TestFingerprintGenerator:
    """FingerprintGenerator 类存在且稳定（v2.10.5 补齐）"""

    def test_generate_keys(self):
        fp = FingerprintGenerator()
        d = fp.generate()
        assert 'user_agent' in d
        assert 'timezone' in d
        assert len(d) >= 30

    def test_deterministic(self):
        fp = FingerprintGenerator()
        assert fp.generate() == fp.generate()

    def test_tls_pick(self):
        fp = FingerprintGenerator()
        assert fp.tls() in ('chrome120', 'chrome123', 'chrome124', 'chrome131',
                            'chrome133a', 'chrome136')


class TestRealVideoFile:
    """真实视频文件判定（crawler 核心守卫）"""

    def test_html_fake_video(self, tmp_path):
        p = tmp_path / "fake.mp4"
        p.write_bytes(b"<html><head></head></html>")
        assert not _is_real_video_file(p)

    def test_too_small(self, tmp_path):
        p = tmp_path / "small.mp4"
        p.write_bytes(b"\x00" * 500_000)
        assert not _is_real_video_file(p)

    def test_real_mp4_head(self, tmp_path):
        p = tmp_path / "real.mp4"
        p.write_bytes(b"\x00" * 50 + b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 1_200_000)
        assert _is_real_video_file(p)


class TestDownloadSafeName:
    """下载文件名 hash 稳定性"""

    def test_safe_name_deterministic(self):
        dl = UniversalDownloader(Path(tempfile.mkdtemp()))
        assert dl._safe_name("https://a.com/v1") == dl._safe_name("https://a.com/v1")
        assert len(dl._safe_name("https://a.com/v1")) == 12


class TestUaTlsConsistency:
    """UA/TLS 同源绑定（v2.10.5 P0-1——UA 主版本必须等于 TLS 伪装版本）"""

    def test_ua_major_matches_tls(self):
        from kiana_vnext_plus.fingerprint_consistency import (
            generate_default_fingerprint, pick_tls_impersonate)
        for _ in range(50):
            fp = generate_default_fingerprint()
            ua = fp["user_agent"]
            tls = pick_tls_impersonate("default_local_session")
            ua_major = int(ua.split("Chrome/")[-1].split(".")[0])
            tls_num = int("".join(c for c in tls if c.isdigit()))
            assert ua_major == tls_num, f"UA={ua_major} TLS={tls}"

    def test_no_edge_ua_with_chrome_tls(self):
        from kiana_vnext_plus.fingerprint_consistency import generate_default_fingerprint
        fp = generate_default_fingerprint()
        assert "Edg/" not in fp["user_agent"]

    def test_client_hints_version_matches_ua(self):
        """Client Hints（v2.10.6 A2）：sec-ch-ua 版本号必须与 UA Chrome 主版本一致，且三头齐全"""
        import re
        from kiana_vnext_plus.session_pool import SessionPool
        sp = SessionPool()
        h = sp.get_stealth_headers("https://example.com")
        ua = h["User-Agent"]
        ua_major = re.search(r"Chrome/(\d+)\.", ua).group(1)
        ch = h.get("sec-ch-ua", "")
        assert f'v="{ua_major}"' in ch, f"sec-ch-ua={ch} 与 UA={ua} 失配"
        assert h.get("sec-ch-ua-platform", "").startswith('"')
        assert h.get("sec-ch-ua-mobile", "") in ("?0", "?1")


class TestDefenseFriendlyExempt:
    """403 渲染友好豁免（v2.10.5 P0-3——贴吧/csdn 等静态 403 不降级 RED）"""

    def test_render_friendly_list(self):
        from kiana_vnext_plus.defense_protocols import _RENDER_FRIENDLY  # noqa
        # 贴吧豁免
        assert any("tieba" in k for k in _RENDER_FRIENDLY)
        assert any("zhihu" in k for k in _RENDER_FRIENDLY)


class TestDomesticWhitelist:
    """国内直连白名单（v2.10.5 P0-3 扩充 40+）"""

    def test_domains_covered(self):
        from kiana_vnext_plus.exit_manager import _DOMESTIC_DOMAINS
        for d in ("bilibili.com", "csdn.net", "douban.com", "xiaohongshu.com",
                  "meituan.com", "gitee.com", "smzdm.com", "zhipin.com"):
            assert any(d == k or d.endswith("." + k) for k in _DOMESTIC_DOMAINS), d


class TestAutoscalePoolApply:
    """AutoscaledPool 写回接线（v2.10.5 P1-7——调优结果必须回到全局信号量）"""

    def test_set_apply_called(self):
        from kiana_vnext_plus.ultimate_core_v4 import AutoscaledPool
        pool = AutoscaledPool(min_con=1, max_con=10)
        calls = []
        pool.record(True, latency=0.5)
        pool.set_apply(lambda v: calls.append(v))
        # 手动调一次 _tune（注入回调）验证写回
        pool._tune(apply_con=lambda v: calls.append(v))
        assert calls, "apply 回调未被调用"


class TestCheckpointSave:
    """checkpoint.save 接口存在（v2.10.5 P2-12 真实落盘）"""

    def test_save_load_roundtrip(self, tmp_path):
        from kiana_vnext_plus.enhancements import CrawlCheckpoint
        cp = CrawlCheckpoint(tmp_path / "cp.json")
        cp.save({"done": 3, "pending": 5})
        loaded = cp.load()
        assert loaded is not None
        assert loaded["done"] == 3 and loaded["pending"] == 5


class TestMetadataFields:
    """parser 新字段（v2.10.6 C4——lang/site_name/published_time/favicon，13→17）"""

    def test_new_fields_extracted(self):
        from kiana_vnext_plus.parser import extract_metadata
        html = ('<html lang="zh-CN"><head><title>t</title>'
                '<link rel="icon" href="/fav.ico">'
                '<meta property="og:site_name" content="站点">'
                '<meta property="og:article:published_time" content="2026-08-01">'
                '</head><body><article>x</article></body></html>')
        d = extract_metadata(html, "https://example.com/p")
        assert len(d) == 17
        assert d["lang"] == "zh-CN"
        assert d["site_name"] == "站点"
        assert d["published_time"] == "2026-08-01"
        assert d["favicon"] == "https://example.com/fav.ico"


class TestExporterAddJsonl:
    """DataExporter.add_jsonl（v2.10.6 B4——论坛快速路径统一走 exporter）"""

    def test_add_jsonl_roundtrip(self, tmp_path):
        from kiana_vnext_plus.enhancements import DataExporter
        ex = DataExporter(tmp_path)
        ex.add_jsonl("tieba.baidu.com", {"title": "测试", "text": "文"})
        ex.add_jsonl("tieba.baidu.com", {"title": "测试2"})
        files = list((tmp_path / "tieba.baidu.com").glob("*.jsonl"))
        assert files, "jsonl 未落盘"
        lines = files[0].read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2


@pytest.mark.skipif(not os.environ.get("RUN_SOLVER_SMOKE"),
                    reason="solver 集成 smoke（真实浏览器，需 RUN_SOLVER_SMOKE=1 手动启用）")
class TestSolverSmoke:
    """solver 集成 smoke（v2.10.6 E1——真实浏览器渲染 about:blank 断言引擎就绪；需手动启用）"""

    def test_solver_renders(self):
        from kiana_vnext_plus.solver_engine import SolverEngine

        async def _run():
            solver = SolverEngine(pool_size=1, max_pages_per_context=2,
                                  memory_limit_mb=4096, headless=True)
            await solver.init()
            html, st, _ = await solver.render_simple("about:blank", extra_wait=0)
            await solver.close()
            return st, len(html or "")
        st, ln = asyncio.run(_run())
        assert st in (0, 200)
        assert ln >= 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
