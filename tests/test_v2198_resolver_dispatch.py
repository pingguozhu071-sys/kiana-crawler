# -*- coding: utf-8 -*-
"""[v2.19.8] 平台直链 resolver 表驱动接线回归测试。

背景：本工程有四个平台 resolver（douyin / xhs / kuaishou / music163），此前**只有抖音**
在 `download_video` 里写了一段专属接线，另外三个"能命令行用、有独立单测、引擎零调用"。
本次改为一张表 + 一个异步包装，并把 audio 通道也接上。

⚠️ 真机验证状态（如实标注）：三个新增平台在**本机无登录态**下拿不到直链
（小红书/快手需 cookies，网易云需试听档/会员），因此本文件只覆盖"接线与失败语义"，
**不声称它们在真实站点上可用**——真机解析仍需用户导入 cookies 后单独验。
"""
import ast
import asyncio
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class _StripProse(ast.NodeTransformer):
    def generic_visit(self, node):
        super().generic_visit(node)
        for field in ("body", "orelse", "finalbody"):
            lst = getattr(node, field, None)
            if isinstance(lst, list):
                kept = [s for s in lst if not (
                    isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)
                    and isinstance(s.value.value, str))]
                if field == "body" and not kept:
                    kept = [ast.Pass()]
                setattr(node, field, kept)
        return node


def _code_flat(rel_path: str) -> str:
    src = (ROOT / rel_path).read_text(encoding="utf-8")
    return ast.unparse(_StripProse().visit(ast.parse(src))).replace("'", "").replace('"', "")


def _ud():
    """构造一个不 init 会话的下载器实例（只测 resolver 分派，不触网）"""
    from kiana_vnext_plus.universal_downloader import UniversalDownloader
    import tempfile
    d = UniversalDownloader(pathlib.Path(tempfile.mkdtemp(prefix="kiana_ud_test_")))
    return d


# ════════════════════════════════════════════════════════════
# 1. 域名分派（精确匹配，子域算命中，不得误伤普通站点）
# ════════════════════════════════════════════════════════════
class TestResolverTable:
    def test_all_four_platforms_mapped(self):
        """四个平台都要在表里——本次修复的核心：不能只有抖音"""
        d = _ud()
        cases = {
            "https://www.douyin.com/video/123": "douyin_resolver",
            "https://v.douyin.com/abc/": "douyin_resolver",
            "https://www.xiaohongshu.com/explore/abc": "xhs_resolver",
            "https://xhslink.com/a/b": "xhs_resolver",
            "https://www.kuaishou.com/short-video/1": "kuaishou_resolver",
            "https://v.kuaishou.com/xyz": "kuaishou_resolver",
            "https://music.163.com/song?id=1": "music163_resolver",
            "https://y.music.163.com/m/song?id=1": "music163_resolver",
        }
        for url, mod in cases.items():
            got, _label = d._resolver_for_url(url)
            assert got == mod, f"{url} → {got}（期望 {mod}）"

    def test_non_platform_returns_none(self):
        """普通站点不得命中（子串匹配曾把 qq.com 这类误判为视频站）"""
        d = _ud()
        for url in ("https://example.com/a", "https://notdouyin.com/x",
                    "https://xiaohongshu.com.evil.tld/x", "ftp://x/y", "", None):
            got, _ = d._resolver_for_url(url)
            assert got is None, f"{url} 被误判为 {got}"


# ════════════════════════════════════════════════════════════
# 2. 直链提取：优先统一 schema，退回平台历史键
# ════════════════════════════════════════════════════════════
class TestPickDirectUrl:
    def test_prefers_unified_schema(self):
        d = _ud()
        res = {"ok": True, "video_url": "https://legacy/x.mp4",
               "media": {"streams": [{"url": "https://unified/x.mp4"}]}}
        assert d._pick_direct_url(res) == "https://unified/x.mp4"

    def test_falls_back_to_legacy_keys(self):
        d = _ud()
        assert d._pick_direct_url({"ok": True, "video_url": "https://l/v.mp4"}) == "https://l/v.mp4"
        assert d._pick_direct_url({"ok": True, "media_url": "https://l/a.mp3"}) == "https://l/a.mp3"

    def test_not_ok_returns_empty(self):
        d = _ud()
        assert d._pick_direct_url({"ok": False, "error": "x"}) == ""
        assert d._pick_direct_url(None) == ""
        assert d._pick_direct_url({}) == ""

    def test_malformed_media_does_not_raise(self):
        d = _ud()
        for bad in ({"ok": True, "media": {"streams": "notalist"}},
                    {"ok": True, "media": {"streams": [None]}},
                    {"ok": True, "media": {"streams": [{}]}}):
            assert d._pick_direct_url(bad) == ""


# ════════════════════════════════════════════════════════════
# 3. 异步包装的失败语义（绝不抛、绝不改变原链路）
# ════════════════════════════════════════════════════════════
class TestResolveDirectUrlSemantics:
    def test_no_resolver_returns_empty_without_network(self):
        d = _ud()
        assert asyncio.run(d.resolve_direct_url("https://example.com/a")) == ""

    def test_resolver_failure_returns_empty(self, monkeypatch):
        """resolver 报 ok=False → 空串（调用方保持原 URL，不改变既有行为）"""
        d = _ud()
        import kiana_vnext_plus.douyin_resolver as dr
        monkeypatch.setattr(dr, "resolve", lambda url: {"ok": False, "error": "风控"})
        assert asyncio.run(d.resolve_direct_url("https://www.douyin.com/video/1")) == ""

    def test_resolver_exception_swallowed(self, monkeypatch):
        """resolver 抛异常 → 也必须返回空串（下载链不能因它断掉）"""
        d = _ud()
        import kiana_vnext_plus.kuaishou_resolver as kr
        def _boom(url):
            raise RuntimeError("模拟崩溃")
        monkeypatch.setattr(kr, "resolve", _boom)
        assert asyncio.run(d.resolve_direct_url("https://www.kuaishou.com/short-video/1")) == ""

    def test_success_returns_direct_url(self, monkeypatch):
        d = _ud()
        import kiana_vnext_plus.xhs_resolver as xr
        monkeypatch.setattr(xr, "resolve", lambda url: {
            "ok": True, "media": {"streams": [{"url": "https://cdn.x/a.mp4"}]}})
        assert asyncio.run(d.resolve_direct_url("https://www.xiaohongshu.com/explore/x")) == "https://cdn.x/a.mp4"

    def test_runs_in_thread_not_in_loop(self, monkeypatch):
        """同步 resolver 必须离开事件循环：在 loop 里跑会冻结整个引擎（抖音曾冻结 30-80s）"""
        d = _ud()
        import kiana_vnext_plus.music163_resolver as mr
        seen = {}

        def _probe(url):
            import threading
            seen["thread"] = threading.current_thread().name
            return {"ok": True, "media": {"streams": [{"url": "https://cdn.m/a.mp3"}]}}

        monkeypatch.setattr(mr, "resolve", _probe)
        asyncio.run(d.resolve_direct_url("https://music.163.com/song?id=1"))
        assert "MainThread" not in seen.get("thread", ""), "resolver 跑在主线程/事件循环里"


# ════════════════════════════════════════════════════════════
# 4. 安全：替换后的直链必须复检（页面状态可注入内网地址）
# ════════════════════════════════════════════════════════════
class TestResolvedUrlRechecked:
    def test_private_direct_url_blocked_in_video_path(self):
        code = _code_flat("kiana_vnext_plus/universal_downloader.py")
        seg = code.split("_direct = await self.resolve_direct_url(url)", 1)
        assert len(seg) == 2
        body = seg[1][:600]
        assert "is_private_url(_direct)" in body, "替换后的直链未复检 → 页面状态注入可绕过 SSRF 闸"
        assert "SSRF 闸拦截" in body

    def test_private_direct_url_blocked_in_audio_path(self):
        code = _code_flat("kiana_vnext_plus/universal_downloader.py")
        seg = code.split("async def download_audio", 1)[1]
        assert "is_private_url(url)" in seg[:800], "audio 通道未复检直链"

    def test_both_paths_call_resolver(self):
        code = _code_flat("kiana_vnext_plus/universal_downloader.py")
        for fn in ("async def download_video", "async def download_audio"):
            seg = code.split(fn, 1)[1]
            assert "resolve_direct_url(url)" in seg[:1200], f"{fn} 未接 resolver"

    def test_legacy_douyin_block_removed(self):
        """旧的写死抖音块应已被表驱动取代（避免两套逻辑并存）"""
        code = _code_flat("kiana_vnext_plus/universal_downloader.py")
        assert "from .douyin_resolver import resolve as _dy_resolve" not in code
