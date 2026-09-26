# -*- coding: utf-8 -*-
"""[v2.19.7 安全·扫描发现] 导出副本脱敏 / 非 logging 出口 / DNS rebinding 回归测试。"""
import ast
import pathlib
import sys
import time

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


def _code_only(rel_path: str) -> str:
    src = (ROOT / rel_path).read_text(encoding="utf-8")
    return ast.unparse(_StripProse().visit(ast.parse(src)))


# ════════════════════════════════════════════════════════════
# 1. sanitize_record：导出副本脱敏
# ════════════════════════════════════════════════════════════
class TestSanitizeRecord:
    def test_url_token_masked(self):
        from kiana_vnext_plus.sanitizer import sanitize_record
        rec = {"url": "https://a.com/p?token=SECRET123&x=1",
               "title": "标题", "text": "正文"}
        out = sanitize_record(rec)
        assert "SECRET123" not in out["url"]
        assert "token=[REDACTED]" in out["url"]
        assert out["title"] == "标题"

    def test_nested_images_and_author(self):
        from kiana_vnext_plus.sanitizer import sanitize_record
        rec = {"images": [{"src": "https://cdn.x/i.jpg?sign=ABCDEF&t=1"}],
               "author": "联系 13812345678",
               "entities": [{"email": "a@b.com"}]}
        out = sanitize_record(rec)
        assert "ABCDEF" not in out["images"][0]["src"]
        assert "13812345678" not in out["author"]
        assert "[手机号]" in out["author"]
        assert "[邮箱]" in out["entities"][0]["email"]

    def test_original_not_mutated(self):
        from kiana_vnext_plus.sanitizer import sanitize_record
        rec = {"url": "https://a.com/?token=KEEP", "images": [{"src": "https://c/i?key=K2"}]}
        out = sanitize_record(rec)
        assert rec["url"].endswith("token=KEEP"), "原对象被就地修改（下载链会因此失效）"
        assert "key=K2" in rec["images"][0]["src"]
        assert out is not rec

    def test_non_dict_passthrough(self):
        from kiana_vnext_plus.sanitizer import sanitize_record
        assert sanitize_record("just text") == "just text"
        assert sanitize_record(None) is None
        assert sanitize_record(7) == 7

    def test_depth_capped_no_crash(self):
        from kiana_vnext_plus.sanitizer import sanitize_record
        rec = {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"url": "https://x/?token=Z"}}}}}}}}
        sanitize_record(rec)   # 不抛即通过（超深分支原样透传）

    def test_plain_url_untouched(self):
        """没有敏感参数的普通 URL 不该被改动（导出可读性/可用性）"""
        from kiana_vnext_plus.sanitizer import sanitize_record
        out = sanitize_record({"url": "https://a.com/article/123?page=2"})
        assert out["url"] == "https://a.com/article/123?page=2"


# ════════════════════════════════════════════════════════════
# 2. 导出链真的用上了脱敏副本（且运行态 data 不被改）
# ════════════════════════════════════════════════════════════
class TestPersistExportWiring:
    def test_persist_export_sanitizes_copies(self):
        code = _code_only("kiana_vnext_plus/page_processor.py")
        body = code.split("async def _persist_export", 1)[1].split("\n    async def ", 1)[0]
        assert "_safe_rec" in body, "导出链未走脱敏副本"
        # 四份产物都必须走脱敏副本
        for needle in ("write_extracted", "exporter.add", "add_markdown"):
            line = [l for l in body.splitlines() if needle in l]
            assert line and "_safe_rec" in line[0], f"{needle} 未脱敏"
        assert "return data" in body, "返回值被替换成脱敏副本 → 下载链接会被抹坏"

    def test_sanitize_record_imported(self):
        code = _code_only("kiana_vnext_plus/page_processor.py")
        assert "sanitize_record" in code

    def test_runtime_stores_deliberately_raw(self):
        """frontier / video_downloads 是运行态钥匙，必须保留原始 URL（不得被'顺手'脱敏）"""
        code = _code_only("kiana_vnext_plus/frontier.py")
        push_body = code.split("async def push", 1)[1].split("\n    async def ", 1)[0]
        assert "normalize_url(url)" in push_body
        assert "sanitize_url" not in push_body, "push 里出现了脱敏 → 续爬会 403"
        vid = code.split("async def add_video_download", 1)[1].split("\n    async def ", 1)[0]
        assert "sanitize_url" not in vid, "video_url 被脱敏 → 视频永远下不动（状态更新也匹配不到行）"

    def test_http_cache_meta_masked_but_key_intact(self):
        code = _code_only("kiana_vnext_plus/p1_enhancements.py")
        store = code.split("def store(self, url", 1)[1].split("\n    def ", 1)[0]
        assert "sanitize_url" in store, "缓存 meta 里的 url 未脱敏"
        # 查表键必须仍用原始 url（否则命中率归零）
        assert "sha256(url.encode())" in store


# ════════════════════════════════════════════════════════════
# 3. 非 logging 出口（Qt 信号 / print 桥）
# ════════════════════════════════════════════════════════════
class TestNonLoggingExits:
    def test_scrub_helper_exists_and_scrubs(self):
        import importlib
        try:
            l8 = importlib.import_module("launcher_v8")
        except Exception as e:
            pytest.skip(f"launcher_v8 不可导入: {e}")
        s = l8._scrub("GET https://x.com/a?token=SECRET9 失败 联系13812345678")
        assert "SECRET9" not in s
        assert "13812345678" not in s

    def test_scrub_never_raises(self):
        import importlib
        try:
            l8 = importlib.import_module("launcher_v8")
        except Exception as e:
            pytest.skip(f"launcher_v8 不可导入: {e}")
        assert l8._scrub(None) is not None
        assert isinstance(l8._scrub(12345), str)

    def test_stream_and_handler_use_scrub(self):
        code = _code_only("launcher_v8.py")
        ls = code.split("class _LineStream", 1)[1].split("\nclass ", 1)[0]
        assert "_scrub(" in ls, "print 桥出口未脱敏"
        h = code.split("class _QtLogHandler", 1)[1].split("\nclass ", 1)[0]
        assert "_scrub(" in h, "Qt logging handler 出口未脱敏（traceback 会带完整 URL）"
        eng = code.split("except Exception as e:", 1)[1].split("finally:", 1)[0]
        assert "_scrub(" in eng, "引擎异常 traceback 出口未脱敏"


# ════════════════════════════════════════════════════════════
# 4. DNS 判定缓存 TTL（防 rebinding）+ host 规范化
# ════════════════════════════════════════════════════════════
class TestDnsRebinding:
    def test_cache_entry_expires(self, monkeypatch):
        from kiana_vnext_plus import url_utils as uu
        uu._HOST_CACHE.clear()
        uu._cache_private("rebind.example", False)
        hit, priv = uu._cached_private("rebind.example")
        assert hit and priv is False
        # 把时间戳推到 TTL 之外 → 必须失效（强制重解析）
        priv_val, _ts = uu._HOST_CACHE["rebind.example"]
        uu._HOST_CACHE["rebind.example"] = (priv_val, time.monotonic() - uu._HOST_CACHE_TTL - 1)
        hit2, _ = uu._cached_private("rebind.example")
        assert not hit2, "缓存未过期 → DNS rebinding 窗口仍存在"
        assert "rebind.example" not in uu._HOST_CACHE

    def test_ttl_is_short(self):
        from kiana_vnext_plus import url_utils as uu
        assert uu._HOST_CACHE_TTL <= 300, "TTL 过长，rebinding 窗口过大"

    def test_trailing_dot_and_zone_normalized(self):
        from kiana_vnext_plus import url_utils as uu
        # FQDN 尾点写法必须与无尾点同判（此前 `foo.local.` 能绕过 .local 黑名单）
        assert uu.is_private_url("http://localhost./") is True
        assert uu.is_private_url("http://printer.local./") is True
        assert uu.is_private_url("http://intranet.internal./") is True
        # IPv6 zone id
        assert uu.is_private_url("http://[fe80::1%25eth0]/") is True

    def test_rebind_after_ttl_is_caught(self, monkeypatch):
        """同一域名：先公网（缓存放行）→ TTL 后改判私网 → 必须拦住"""
        from kiana_vnext_plus import url_utils as uu
        uu._HOST_CACHE.clear()
        uu._cache_private("flip.example", False)          # 初次判定：公网
        assert uu._resolve_host_private("flip.example") is False
        uu._HOST_CACHE["flip.example"] = (False, time.monotonic() - uu._HOST_CACHE_TTL - 1)
        monkeypatch.setattr(uu._socket, "getaddrinfo",
                            lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 0))])
        assert uu._resolve_host_private("flip.example") is True, "rebinding 后仍判放行"

    def test_disabling_dns_check_is_logged(self):
        """逃生门（private_dns_resolve_check=false）必须留痕，不能静默失效"""
        code = _code_only("kiana_vnext_plus/crawler.py")
        seg = code.split("DNS_CHECK_ENABLED = bool(", 1)[1].split("except Exception", 1)[0]
        assert "logger.warning" in seg, "关闭 SSRF 校验时未告警"


# ════════════════════════════════════════════════════════════
# 5. safe_get 仍强制 dns_check（下载通道不受逃生门影响）
# ════════════════════════════════════════════════════════════
class TestSafeGetHardPinned:
    def test_safe_get_passes_dns_check_true(self):
        from kiana_vnext_plus import url_utils as uu
        import inspect
        src = inspect.getsource(uu.safe_get)
        assert src.count("dns_check=True") >= 2, "入口/逐跳未强制 DNS 校验"
