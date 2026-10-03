# -*- coding: utf-8 -*-
"""[v2.19.7 安全·扫描发现] 残余裸请求点收口 + 刻意豁免项的边界测试。

覆盖：parse_sitemap / discover_sitemap / proxy_fetcher.fetch_source / media_downloader._download
豁免：llm_client（用户自填端点，含本地推理服务——加闸会拦死正当用法）
"""
import ast
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


def _fn_code(rel_path: str, *names: str) -> str:
    src = (ROOT / rel_path).read_text(encoding="utf-8")
    code = ast.unparse(_StripProse().visit(ast.parse(src)))
    out = []
    for name in names:
        marker = f"def {name}("
        assert marker in code, f"{rel_path} 找不到 {name}"
        out.append(code.split(marker, 1)[1].split("\ndef ", 1)[0])
    return "\n".join(out)


class TestRawFetchPointsClosed:
    def test_parse_sitemap_uses_safe_get(self):
        body = _fn_code("kiana_vnext_plus/enhancements.py", "parse_sitemap")
        assert "safe_get" in body
        assert "session.get(url)" not in body

    def test_discover_sitemap_uses_safe_get(self):
        body = _fn_code("kiana_vnext_plus/enhancements.py", "discover_sitemap")
        assert body.count("safe_get") >= 2, "robots 与 sitemap 两条都得走闸"
        assert "await session.get(" not in body

    def test_proxy_fetch_source_uses_safe_get(self):
        body = _fn_code("kiana_vnext_plus/proxy_fetcher.py", "fetch_source")
        assert "safe_get" in body
        assert "await session.get(" not in body
        # 被拦截（None）不能当成 status_code 崩溃
        assert "is None" in body

    def test_media_downloader_worker_uses_safe_get(self):
        body = _fn_code("kiana_vnext_plus/media_downloader.py", "_download")
        assert "safe_get" in body
        assert "self.session.get(" not in body
        assert "resp is not None" in body


class TestDeliberateExemptions:
    def test_llm_client_not_gated_with_reason(self):
        """llm_client 刻意不加闸：本地推理服务（localhost）是明确支持的用法。
        但必须在代码里写明理由，避免后人'顺手补闸'把 Ollama/LM Studio 拦死。"""
        body = _fn_code("kiana_vnext_plus/llm_client.py", "_request")
        src = (ROOT / "kiana_vnext_plus/llm_client.py").read_text(encoding="utf-8")
        assert "safe_get" not in body, "llm_client 被加了闸 → 本地 LLM 端点会被拦死"
        assert "刻意不加 SSRF 闸" in src, "缺少豁免理由说明"

    def test_fixed_host_apis_stay_plain(self):
        """平台 API（硬编码主机字面量）不需要闸——主机不可被页面内容操纵"""
        for rel in ("kiana_vnext_plus/comment_danmaku.py", "kiana_vnext_plus/video_resolver.py"):
            src = (ROOT / rel).read_text(encoding="utf-8")
            assert "api.bilibili.com" in src or "comment.bilibili.com" in src


class TestSafeGetSemantics:
    """safe_get 是这些收口的共同底座——把它的关键语义钉住"""

    def test_retries_hops_manually(self):
        from kiana_vnext_plus import url_utils as uu
        import inspect
        src = inspect.getsource(uu.safe_get)
        assert "allow_redirects=False" in src, "未禁用自动跟随 → 逐跳校验形同虚设"
        assert "max_hops" in src

    def test_closes_intermediate_redirect_responses(self):
        from kiana_vnext_plus import url_utils as uu
        import inspect
        src = inspect.getsource(uu.safe_get)
        assert ".close()" in src, "中间跳响应未关闭（流式模式会泄漏连接）"

    def test_blocks_non_http_scheme(self):
        from kiana_vnext_plus import url_utils as uu
        import asyncio
        assert asyncio.run(uu.safe_get(None, "file:///C:/Windows/win.ini")) is None
        assert asyncio.run(uu.safe_get(None, "ftp://x/y")) is None

    def test_blocks_private_entry(self):
        from kiana_vnext_plus import url_utils as uu
        import asyncio
        assert asyncio.run(uu.safe_get(None, "http://169.254.169.254/latest/meta-data/")) is None
        assert asyncio.run(uu.safe_get(None, "http://127.0.0.1:8080/x")) is None
