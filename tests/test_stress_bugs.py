"""Kiana Vnext Plus — 压测暴露 bug 回归（v2.11）

覆盖：SimHash 63 位钳制（SQLite INTEGER 溢出 → flush 整批丢失）、
响应头回写会话池的伪头/hop-by-hop 过滤（curl 43 出厂级 bug）。
"""
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ['KIANA_CRYPTO_KEY'] = 'kv_test'


class TestSimhashClamp:
    """simhash_64 最高位钳制：永远 < 2^63（可安全写 SQLite INTEGER）"""

    def test_never_overflow(self):
        from kiana_vnext_plus.parser import simhash_64
        LIMIT = (1 << 63) - 1
        # 全部为 1 的极端 token 也要保证最高位被钳
        for n in range(0, 400, 7):
            v = simhash_64(" ".join(f"t{i}" for i in range(n)) or "x")
            assert 0 <= v <= LIMIT, f"simhash 溢出: {v}"

    def test_write_page_accepts_simhash(self, tmp_path):
        # frontier.write_page 入口钳制（防其它来源的大整数）
        import asyncio
        from kiana_vnext_plus.frontier import FrontierDB
        db = FrontierDB(str(tmp_path / "f.db"))

        async def flow():
            await db.init_async()
            await db.write_page("h1", 200, 100, 0.1, "{}", "c", simhash=(1 << 63) | 0b111)
            await db.flush()
            async with db._read_conn.execute("SELECT simhash FROM pages WHERE url_hash='h1'") as cur:
                row = await cur.fetchone()
            assert row is not None
            assert int(row[0]) <= (1 << 63) - 1  # int() 兼容旧库 TEXT affinity

        asyncio.run(flow())


class TestResponseHeaderSanitize:
    """渲染响应头回写会话池 → 伪头/hop-by-hop 必须被过滤（curl 43 出厂级 bug）"""

    def test_sanitize_filters(self):
        from kiana_vnext_plus.session_pool import SessionPool
        dirty = {
            ":status": "200",                       # HTTP/2 伪头 → 必须丢弃
            "content-length": "4701",               # 实体头 → 丢弃
            "transfer-encoding": "chunked",         # hop-by-hop → 丢弃
            "content-encoding": "br",               # 实体头 → 丢弃
            "set-cookie": "a=1",                    # 响应专用 → 丢弃
            "x-custom-hint": "ok-value",            # 自定义可复用 → 保留
        }
        clean = SessionPool._sanitize_reusable_headers(dirty)
        assert ":status" not in clean
        assert "content-length" not in clean
        assert "transfer-encoding" not in clean
        assert "content-encoding" not in clean
        assert "set-cookie" not in clean
        assert clean.get("x-custom-hint") == "ok-value"

    def test_non_ascii_value_dropped(self):
        from kiana_vnext_plus.session_pool import SessionPool
        clean = SessionPool._sanitize_reusable_headers({"x-bad": "值含中文\n"})
        assert "x-bad" not in clean

    def test_update_then_fetch_clean(self):
        # 端到端：update_session 带脏响应头 → get_session 返回的 headers 无垃圾成员
        from kiana_vnext_plus.session_pool import SessionPool
        sp = SessionPool()
        sp.update_session("https://example.com/a", cookies="k=v",
                          headers={":status": "200", "content-length": "1",
                                   "x-keep": "yes"})
        sess = sp.get_session("https://example.com/b")  # 同域另一页
        assert sess is not None
        h = sess.get("headers") or {}
        assert ":status" not in h and "content-length" not in h
        assert h.get("x-keep") == "yes"
