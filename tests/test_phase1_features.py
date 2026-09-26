"""Kiana Vnext Plus — 阶段 1 功能拉满回归测试（v2.11）

覆盖：SimHash 内容去重、Markdown 快照导出、WBI 签名（mixinKey/md5 参考实现核对）、
画质档映射、BV 号提取、B站 cookies 域提取。
"""
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ['KIANA_CRYPTO_KEY'] = 'kv_test'

from kiana_vnext_plus.parser import simhash_64, simhash_hamming


class TestSimHash:
    """内容级去重（frontier simhash/duplicate_of 列接线后落库）"""

    def test_deterministic(self):
        a = simhash_64(" ".join(f"token{i}" for i in range(100)))
        assert a != 0
        assert a == simhash_64(" ".join(f"token{i}" for i in range(100)))

    def test_same_text_zero_distance(self):
        t = "今天天气很好 适合出门散步 顺便拍点照片 记录一下生活"
        assert simhash_hamming(simhash_64(t), simhash_64(t)) == 0

    def test_disjoint_text_far(self):
        a = simhash_64(" ".join(f"alpha{i}" for i in range(200)))
        b = simhash_64(" ".join(f"beta{i}" for i in range(200)))
        assert simhash_hamming(a, b) > 3

    def test_empty(self):
        assert simhash_64("") == 0
        assert simhash_64(None) == 0


class TestMarkdownExport:
    def test_markdown_written(self, tmp_path):
        from kiana_vnext_plus.enhancements import DataExporter
        ex = DataExporter(tmp_path)
        ex.add_markdown("example.com", {
            "title": "测试标题", "text": "正文内容" * 50,
            "url": "https://example.com/a", "images": [],
        })
        mds = list((tmp_path / "example.com" / "markdown").glob("*.md"))
        assert len(mds) == 1
        content = mds[0].read_text(encoding="utf-8")
        assert "# 测试标题" in content
        assert "正文内容" in content
        assert "https://example.com/a" in content

    def test_no_crash_on_empty(self, tmp_path):
        from kiana_vnext_plus.enhancements import DataExporter
        ex = DataExporter(tmp_path)
        ex.add_markdown("x.com", {})  # 空数据不崩


class TestWbiSign:
    """B站 WBI 签名（mixinKey 映射表 + md5——B站协议强制 md5，非工程选择）"""

    def test_mixin_key_len(self):
        from kiana_vnext_plus.comment_danmaku import get_mixin_key
        assert len(get_mixin_key("a" * 64)) == 32
        assert len(get_mixin_key("7cd084941338484aae1ad9425b84077c" + "1234567890abcdefghijklmnopqrstuv")) == 32

    def test_sign_wbi_reference(self, monkeypatch):
        from kiana_vnext_plus.comment_danmaku import sign_wbi
        monkeypatch.setattr("time.time", lambda: 1700000000.0)
        mixin = "a" * 32
        p = sign_wbi({"oid": 123, "type": 1}, mixin)
        assert p["wts"] == 1700000000
        import hashlib
        from urllib.parse import urlencode
        q = urlencode({"oid": "123", "type": "1", "wts": "1700000000"})
        assert p["w_rid"] == hashlib.md5((q + mixin).encode()).hexdigest()

    def test_sign_filters_special_chars(self, monkeypatch):
        from kiana_vnext_plus.comment_danmaku import sign_wbi
        monkeypatch.setattr("time.time", lambda: 1700000000.0)
        p = sign_wbi({"x": "a!b'c(d)e*f"}, "k" * 32)
        assert p["w_rid"]  # 过滤后签名正常产出


class TestBiliCookies:
    def test_domain_extraction(self, tmp_path, monkeypatch):
        from kiana_vnext_plus import comment_danmaku as cd
        src = tmp_path / "bili.txt"
        src.write_text("# Netscape HTTP Cookie File\n"
                       ".bilibili.com\tTRUE\t/\tFALSE\t0\tSESSDATA\tdeadbeef\n"
                       ".douyin.com\tTRUE\t/\tFALSE\t0\tk\tv\n", encoding="utf-8")
        monkeypatch.setenv("KIANA_COOKIE_FILES", str(src))
        jar = cd._bili_cookies()
        assert jar.get("SESSDATA") == "deadbeef"
        assert "k" not in jar  # 只取 bilibili 域


class TestQualityMapping:
    def test_mapping(self):
        from kiana_vnext_plus.crawler import _quality_for
        assert _quality_for("highest") == "best"
        assert _quality_for("1080") == "best[height<=1080]"
        # [v2.16.1] 4K/2K 改组合选择器（原纯 bestvideo → 无声视频，已修复）
        assert _quality_for("2160") == "bestvideo[height<=2160]+bestaudio"
        assert _quality_for("4k") == "bestvideo[height<=2160]+bestaudio"
        assert _quality_for("2k") == "bestvideo[height<=1440]+bestaudio"
        assert _quality_for(None) == "best"
        assert _quality_for("garbage") == "best"

    def test_extract_bvid(self):
        from kiana_vnext_plus.crawler import _extract_bvid
        assert _extract_bvid("https://www.bilibili.com/video/BV1oEuZ6mEVS") == "BV1oEuZ6mEVS"
        assert _extract_bvid("https://www.bilibili.com/video/av170001") == "av170001"
        assert _extract_bvid("https://b23.tv/duOgtrs") is None  # 短链诚实降级
        assert _extract_bvid("https://example.com/") is None
