"""Kiana Vnext Plus — v2.14 深水区整固回归（事件循环/数据层/安全基线/供应链）"""
import sys
import os
import asyncio
import time
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault('KIANA_CRYPTO_KEY', 'kv_test')


class TestDBMigration:
    """v2.10 老库（无 duplicate_of/simhash 列）自动补列——防整批数据丢失"""

    def _make_old_db(self, path):
        conn = sqlite3.connect(str(path))
        conn.execute("""CREATE TABLE IF NOT EXISTS pages (
            url_hash TEXT PRIMARY KEY, status_code INTEGER, content_length INTEGER,
            fetch_time REAL, headers TEXT, content_hash TEXT)""")  # 无 simhash/duplicate_of
        conn.execute("PRAGMA user_version = 0")
        conn.commit()
        conn.close()

    def test_old_db_migrated(self, tmp_path):
        from kiana_vnext_plus.frontier import FrontierDB
        dbf = tmp_path / "old.db"
        self._make_old_db(dbf)
        db = FrontierDB(str(dbf))          # _init_db 应跑迁移
        conn = sqlite3.connect(str(dbf))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(pages)")}
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        assert "duplicate_of" in cols and "simhash" in cols
        assert ver >= 1
        # 迁移后 mark_duplicate 可用
        async def flow():
            await db.init_async()
            await db.write_page("h1", 200, 10, 0.1, "{}", "c", simhash=7)
            await db.mark_duplicate("h1", "h0")
            await db.flush()
        asyncio.run(flow())

    def test_new_db_idempotent(self, tmp_path):
        from kiana_vnext_plus.frontier import FrontierDB
        db = FrontierDB(str(tmp_path / "n.db"))   # 新库直接 v1
        conn = sqlite3.connect(str(tmp_path / "n.db"))
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        idx = {r[1] for r in conn.execute("PRAGMA index_list(frontier)")}
        idx_v = {r[1] for r in conn.execute("PRAGMA index_list(video_downloads)")}
        conn.close()
        assert ver >= 1
        assert "idx_frontier_domain_status" in idx
        assert "idx_video_status" in idx_v


class TestRetryBackoff:
    """重试风暴修复：指数 delay + 抖动"""

    def test_exponential_jittered(self, tmp_path):
        async def flow():
            from kiana_vnext_plus.frontier import FrontierDB
            from kiana_vnext_plus.url_utils import url_hash
            db = FrontierDB(str(tmp_path / "f.db"))
            await db.init_async()
            u = "https://x.com/r"
            uh = url_hash(u)
            await db.push(u)
            await db.flush()
            delays = []
            for _ in range(3):
                await db.mark_failed(uh, retry=True, delay=30)
                await db.flush()
                async with db._read_conn.execute(
                        "SELECT scheduled_at, retry_count FROM frontier WHERE url_hash=?", (uh,)) as cur:
                    row = await cur.fetchone()
                delays.append(row[0] - time.time())
            # 指数增长（30→54→97 量级，有 ±30% 抖动）
            assert delays[1] > delays[0]
            assert delays[2] > delays[1]
            assert all(15 < d < 250 for d in delays)
        asyncio.run(flow())


class TestSafeFilename:
    def test_traversal(self):
        from kiana_vnext_plus.url_utils import safe_dirname
        assert ".." not in safe_dirname("evil.com\\..\\..\\win")
        assert ".." not in safe_dirname("..")
        assert ":" not in safe_dirname("x.com:8080")

    def test_reserved_names(self):
        from kiana_vnext_plus.url_utils import safe_filename
        assert not safe_filename("CON").upper().startswith("CON")
        assert not safe_filename("NUL.mp4").split(".")[0].upper() == "NUL"
        assert safe_filename("normal.txt") == "normal.txt"

    def test_control_chars_and_len(self):
        from kiana_vnext_plus.url_utils import safe_filename
        s = safe_filename("a\x00b\x1fc" + "x" * 200, max_len=50)
        assert len(s) <= 50 and "\x00" not in s and "\x1f" not in s


class TestPrivateUrl:
    def test_private_blocked(self):
        from kiana_vnext_plus.url_utils import is_private_url
        assert is_private_url("http://127.0.0.1/x")
        assert is_private_url("http://169.254.169.254/latest/meta-data/")
        assert is_private_url("http://192.168.1.1/")
        assert is_private_url("http://10.0.0.5/")
        assert is_private_url("http://localhost/")
        assert is_private_url("file:///etc/passwd")
        assert is_private_url("ftp://x/")

    def test_public_allowed(self):
        from kiana_vnext_plus.url_utils import is_private_url
        assert not is_private_url("https://www.bilibili.com/video/BV1")
        assert not is_private_url("https://ruanyifeng.com/a")


class TestTooBigMark:
    def test_response_adapter_flag(self):
        from kiana_vnext_plus.response_adapter import ResponseAdapter
        r = ResponseAdapter(200, "u", {}, raw_text="x", too_big=True)
        assert r.too_big is True
        r2 = ResponseAdapter(200, "u", {}, raw_text="x")
        assert r2.too_big is False
