"""Kiana Vnext Plus — v2.15 阶段 2：真增量爬取回归（stale 返回/条件头/304 刷新）"""
import sys, os, asyncio, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestHttpCacheStale:
    def test_fresh_hit(self, tmp_path):
        from kiana_vnext_plus.p1_enhancements import HttpCache
        c = HttpCache(tmp_path)
        c.store("u", "content-v1", {"etag": '"abc"'})
        r = c.get("u")
        assert r["content"] == "content-v1"
        assert "stale" not in r

    def test_stale_returns_with_meta(self, tmp_path):
        """TTL 过期 → stale 标记返回（携带 etag/last_modified 供条件请求）——
        原实现过期返回 None 连条件头都拿不到（增量爬取不可能）"""
        from kiana_vnext_plus.p1_enhancements import HttpCache
        c = HttpCache(tmp_path)
        c.store("u", "content-v1", {"etag": '"abc"', "last-modified": "Tue, 01 Jan 2030 00:00:00 GMT"})
        # 篡改 fetched_at 使其过期
        import json
        key = __import__("hashlib").sha256(b"u").hexdigest()[:16]
        meta_f = tmp_path / f"{key}.meta"
        meta = json.loads(meta_f.read_text(encoding="utf-8"))
        meta["fetched_at"] = time.time() - 7200
        meta_f.write_text(json.dumps(meta), encoding="utf-8")
        r = c.get("u")
        assert r is not None and r.get("stale") is True
        assert r["meta"]["etag"] == '"abc"'
        assert r["content"] == "content-v1"   # 304 时可复用

    def test_304_store_refreshes_fetched_at(self, tmp_path):
        """304 分支的 store 刷新：同内容重存后不再 stale"""
        from kiana_vnext_plus.p1_enhancements import HttpCache
        c = HttpCache(tmp_path)
        c.store("u", "v1", {"etag": '"e1"'})
        import json, hashlib
        key = hashlib.sha256(b"u").hexdigest()[:16]
        meta_f = tmp_path / f"{key}.meta"
        meta = json.loads(meta_f.read_text(encoding="utf-8"))
        meta["fetched_at"] = time.time() - 7200
        meta_f.write_text(json.dumps(meta), encoding="utf-8")
        assert c.get("u").get("stale") is True
        c.store("u", "v1", {"etag": '"e1"'})   # 304 分支的刷新动作
        assert c.get("u").get("stale") is not True


class TestCondHeadersPassthrough:
    """engine_router Tier1 透传 job['_cond_headers']"""

    def test_try_protocol_passes_extra(self, tmp_path):
        import asyncio
        from kiana_vnext_plus.engine_router import EngineRouter

        captured = {}

        class FakeProtocol:
            async def fetch(self, url, proxy=None, extra_headers=None):
                captured["extra"] = extra_headers
                from kiana_vnext_plus.response_adapter import ResponseAdapter
                return ResponseAdapter(200, url, {}, raw_text="ok")

        class FakeExit:
            async def acquire_for_domain(self, d): return None
            async def release(self, p): pass

        router = EngineRouter(FakeProtocol(), None, None, FakeExit(), 30)
        job = {"_cond_headers": {"If-None-Match": '"e1"'}}
        resp = asyncio.run(router.fetch("https://x.com/", "x.com", job))
        assert resp.status_code == 200
        assert captured["extra"] == {"If-None-Match": '"e1"'}

    def test_no_cond_headers_is_none(self, tmp_path):
        import asyncio
        from kiana_vnext_plus.engine_router import EngineRouter

        captured = {}

        class FakeProtocol:
            async def fetch(self, url, proxy=None, extra_headers=None):
                captured["extra"] = extra_headers
                from kiana_vnext_plus.response_adapter import ResponseAdapter
                return ResponseAdapter(200, url, {}, raw_text="ok")

        class FakeExit:
            async def acquire_for_domain(self, d): return None
            async def release(self, p): pass

        router = EngineRouter(FakeProtocol(), None, None, FakeExit(), 30)
        asyncio.run(router.fetch("https://x.com/", "x.com", {"depth": 0}))
        assert captured["extra"] is None
