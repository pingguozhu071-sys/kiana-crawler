"""Kiana Vnext Plus — v2.13 阶段 3：租约 CAS + errors 视图 + duplicate 清理"""
import sys
import os
import asyncio
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault('KIANA_CRYPTO_KEY', 'kv_test')

from kiana_vnext_plus.frontier import FrontierDB
from kiana_vnext_plus.url_utils import url_hash


class TestLeaseCAS:
    def test_mark_done_checked_matches(self, tmp_path):
        async def flow():
            db = FrontierDB(str(tmp_path / "f.db"))
            await db.init_async()
            leased_at = time.time()
            uh_a = url_hash("https://x.com/a")
            await db.push("https://x.com/a")
            await db.flush()
            # 模拟出队租约
            await db._write_queue.put((
                "UPDATE frontier SET status='leased', leased_at=? WHERE url_hash=?",
                (leased_at, uh_a)))
            await db.flush()
            # CAS 成功：leased_at 匹配
            await db.mark_done_checked(uh_a, leased_at)
            await db.flush()
            async with db._read_conn.execute(
                    "SELECT status FROM frontier WHERE url_hash=?", (uh_a,)) as cur:
                assert (await cur.fetchone())[0] == "done"
        asyncio.run(flow())

    def test_mark_done_checked_stale_ignored(self, tmp_path):
        """租约已被回收重发（leased_at 变了）→ 旧持有者的 done 不生效"""
        async def flow():
            db = FrontierDB(str(tmp_path / "f.db"))
            await db.init_async()
            uh_b = url_hash("https://x.com/b")
            await db.push("https://x.com/b")
            await db.flush()
            old_lease = time.time() - 400
            await db._write_queue.put((
                "UPDATE frontier SET status='leased', leased_at=? WHERE url_hash=?",
                (old_lease, uh_b)))
            await db.flush()
            # pop_batch 回收： leased 超时 → pending → 再次租出（新的 leased_at）
            await db.pop_batch(10, worker_id="w2")
            # 旧持有者拿旧 leased_at 来标 done → 不应覆盖新租约
            await db.mark_done_checked(uh_b, old_lease)
            await db.flush()
            async with db._read_conn.execute(
                    "SELECT status FROM frontier WHERE url_hash=?", (uh_b,)) as cur:
                status = (await cur.fetchone())[0]
            assert status != "done"  # 新持有者的任务没被旧结果污染
        asyncio.run(flow())


class TestErrorsView:
    def test_get_recent_errors(self, tmp_path):
        async def flow():
            db = FrontierDB(str(tmp_path / "f.db"))
            await db.init_async()
            await db.push("https://x.com/err1")
            await db.flush()
            await db.write_error("x-err1", "HTTP_403", "HTTP 403 for https://x.com/err1")
            await db.flush()
            rows = await db.get_recent_errors(limit=10)
            assert len(rows) == 1
            assert rows[0]["error_type"] == "HTTP_403"

        asyncio.run(flow())


class TestDuplicateCleaned:
    def test_no_duplicate_status_written(self):
        from pathlib import Path as _P
        src = (_P(__file__).parent.parent / "kiana_vnext_plus" / "frontier.py").read_text(encoding="utf-8")
        assert "status='duplicate'" not in src
        assert "'done','duplicate'" not in src
