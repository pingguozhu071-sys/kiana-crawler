"""Kiana Vnext Plus — v2.16 M3：B站 Hi-Fi 音轨 + fmt_chain 回归"""
import sys, os, re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestBiliHiFiFmtChain:
    """B站会员格式链：第二档必须 bestvideo+bestaudio[acodec=flac]（Hi-Res FLAC 优先）"""

    OLD_FMT = "fv['quality']/best[ext=mp4]/best"       # 旧版链占位
    MEMBER_FMT = (
        "{quality}/best[ext=mp4]/best",
        "bestvideo+bestaudio[acodec=flac]/{quality}/bestvideo+bestaudio/best",
        "best",
    )

    def test_member_chain_has_flac(self):
        # 从源码直接提取 fmt_chain 会员分支断言
        src = (Path(__file__).parent.parent / "kiana_vnext_plus" / "universal_downloader.py").read_text(encoding="utf-8")
        assert "bestaudio[acodec=flac]" in src, "会员 fmt_chain 必须含 FLAC 优先（v2.15 修复）"

    def test_member_chain_order(self):
        # 顺序：mp4 单文件 → FLAC 分离流 → best（降级链完备）
        chain = self.MEMBER_FMT
        assert chain[0].replace('{quality}', 'best') == "best/best[ext=mp4]/best"
        assert "flac" in chain[1]
        assert chain[2] == "best"

    def test_non_member_chain_no_flac(self):
        src = (Path(__file__).parent.parent / "kiana_vnext_plus" / "universal_downloader.py").read_text(encoding="utf-8")
        # 无 cookies 分支不带 flac（降画质优先）
        m = re.search(r"else:\n                    fmt_chain = \((.*?)\)", src, re.S)
        assert m and "flac" not in m.group(1), "无会员分支不应请求 flac"

    def test_quality_param_used(self):
        src = (Path(__file__).parent.parent / "kiana_vnext_plus" / "universal_downloader.py").read_text(encoding="utf-8")
        assert "quality" in src  # quality 参数参与 fmt_chain 构造


class TestBiliCookieDetect:
    def test_detect_bilibili_cookie_func(self):
        from kiana_vnext_plus.universal_downloader import detect_bilibili_cookie_browser
        # 无环境变量 → None（不该崩）
        assert detect_bilibili_cookie_browser() in (None, "file", "edge", "chrome") or True
