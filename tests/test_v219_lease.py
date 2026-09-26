"""v2.19 租约一致性回归（批次 2）

覆盖：busy_timeout / 锁冲突区分 / mark_done CAS 收口 / 持久化前移 /
      DB 迁移 v5 与限流兜底 / page_timeout 默认值。
全部离线（临时 SQLite 库，不发网络请求）。
"""
import asyncio
import inspect
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiana_vnext_plus.frontier import FrontierDB


class _TmpDB:
    """临时库上下文（Windows 下 rmtree 容错）"""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="kiana_lease_")
        self.db = FrontierDB(str(Path(self.dir) / "f.db"))
        return self.db

    def __exit__(self, *a):
        shutil.rmtree(self.dir, ignore_errors=True)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class TestReadConnTimeout(unittest.TestCase):
    def test_busy_timeout_set_on_read_conn(self):
        async def go():
            with _TmpDB() as db:
                await db.init_async()
                try:
                    cur = await db._read_conn.execute("PRAGMA busy_timeout")
                    row = await cur.fetchone()
                    self.assertEqual(int(row[0]), 30000,
                                     "_read_conn 必须设 busy_timeout（否则 flusher 持锁时误判）")
                finally:
                    await db.close()
        _run(go())


class TestMarkDoneCas(unittest.TestCase):
    def test_mark_done_with_lease_uses_cas(self):
        """传 leased_at → 走 CAS；租约易主则不改状态且返回 False"""
        async def go():
            with _TmpDB() as db:
                await db.init_async()
                try:
                    await db.push("https://example.com/a")
                    await db.flush()
                    batch = await db.pop_batch(limit=1)
                    job = batch[0]
                    # 正确的租约值 → 成功
                    self.assertTrue(await db.mark_done("x", leased_at=job["leased_at"])
                                    is not None)
                    ok = await db.mark_done_checked(job["url_hash"], job["leased_at"])
                    self.assertTrue(ok)
                    # 已 done → 陈旧租约再打卡失败
                    self.assertFalse(await db.mark_done_checked(job["url_hash"], job["leased_at"]))
                finally:
                    await db.close()
        _run(go())

    def test_mark_done_without_lease_legacy(self):
        """不传 leased_at → 保持旧语义（无条件置 done），返回 True"""
        async def go():
            with _TmpDB() as db:
                await db.init_async()
                try:
                    await db.push("https://example.com/b")
                    await db.flush()
                    batch = await db.pop_batch(limit=1)
                    uh = batch[0]["url_hash"]
                    self.assertTrue(await db.mark_done(uh))
                    await db.flush()
                    cur = await db._read_conn.execute(
                        "SELECT status FROM frontier WHERE url_hash=?", (uh,))
                    self.assertEqual((await cur.fetchone())[0], "done")
                finally:
                    await db.close()
        _run(go())

    def test_stale_holder_cannot_override(self):
        """租约易主后，旧持有者不能把新持有者的 done 打回（CAS 收口核心目标）"""
        async def go():
            with _TmpDB() as db:
                await db.init_async()
                try:
                    await db.push("https://example.com/c")
                    await db.flush()
                    b1 = await db.pop_batch(limit=1)
                    old_leased = b1[0]["leased_at"]
                    # 模拟租约回收重发（新持有者拿到不同的 leased_at）
                    await db._read_conn.execute(
                        "UPDATE frontier SET status='pending' WHERE url_hash=?", (b1[0]["url_hash"],))
                    await db._read_conn.commit()
                    b2 = await db.pop_batch(limit=1)
                    new_leased = b2[0]["leased_at"]
                    self.assertNotEqual(old_leased, new_leased, "重发应产生新租约值")
                    # 旧持有者打卡 → 必须失败
                    self.assertFalse(await db.mark_done_checked(b1[0]["url_hash"], old_leased))
                    # 新持有者打卡 → 成功
                    self.assertTrue(await db.mark_done_checked(b1[0]["url_hash"], new_leased))
                finally:
                    await db.close()
        _run(go())


class TestMigrationV5AndThrottle(unittest.TestCase):
    def test_throttle_count_column(self):
        async def go():
            with _TmpDB() as db:
                await db.init_async()
                try:
                    cur = await db._read_conn.execute("PRAGMA table_info(frontier)")
                    cols = {r[1] for r in await cur.fetchall()}
                    self.assertIn("throttle_count", cols, "迁移 v5 应补 throttle_count 列")
                    cur = await db._read_conn.execute("PRAGMA user_version")
                    self.assertGreaterEqual((await cur.fetchone())[0], 5)
                finally:
                    await db.close()
        _run(go())

    def test_old_v4_db_migrates_without_data_loss(self):
        """[v2.19.3 质检] 老库（v4，无 throttle_count）升级必须**保留全部数据**且**幂等**。

        作者已装旧版，升级安装后会跑此迁移——丢数据不可接受。"""
        import sqlite3 as _sq
        import importlib
        import kiana_vnext_plus.frontier as fm

        tmp = tempfile.mkdtemp(prefix="kiana_mig_")
        path = str(Path(tmp) / "old.db")
        try:
            # 手工构造 v4 老库（SQL 全为固定字符串，无拼接/格式化）
            c = _sq.connect(path)
            c.execute("""CREATE TABLE frontier (url_hash TEXT PRIMARY KEY, normalized_url TEXT,
                domain TEXT, depth INTEGER DEFAULT 0, priority INTEGER DEFAULT 5,
                status TEXT DEFAULT 'pending', scheduled_at REAL, retry_count INTEGER DEFAULT 0,
                max_retries INTEGER DEFAULT 3, leased_at REAL, lease_expires REAL,
                worker_id TEXT, created_at REAL, parent_hash TEXT)""")
            c.execute("""CREATE TABLE pages (url_hash TEXT PRIMARY KEY, status_code INTEGER,
                content_length INTEGER, fetch_time REAL, headers TEXT, content_hash TEXT,
                simhash INTEGER, duplicate_of TEXT)""")
            c.execute("""CREATE TABLE errors (url_hash TEXT, error_type TEXT, error_message TEXT,
                timestamp REAL, platform TEXT DEFAULT '', code TEXT DEFAULT '')""")
            c.execute("""CREATE TABLE video_downloads (video_url TEXT PRIMARY KEY, domain TEXT,
                status TEXT DEFAULT 'pending', file_path TEXT, progress REAL,
                fail_count INTEGER DEFAULT 0, created_at REAL, file_size REAL DEFAULT 0)""")
            c.execute("INSERT INTO frontier VALUES ('h1','https://old/a','old',0,5,'done',"
                      "0,0,3,NULL,NULL,NULL,0,NULL)")
            c.execute("INSERT INTO frontier VALUES ('h2','https://old/b','old',1,5,'pending',"
                      "0,0,3,NULL,NULL,NULL,0,NULL)")
            c.execute("PRAGMA user_version = 4")
            c.commit()
            c.close()

            importlib.reload(fm)
            fm.FrontierDB(path)          # 构造即迁移

            c = _sq.connect(path)
            ver = c.execute("PRAGMA user_version").fetchone()[0]
            cols = {r[1] for r in c.execute("PRAGMA table_info(frontier)")}
            rows = list(c.execute("SELECT url_hash, status, depth, COALESCE(throttle_count,-1)"
                                  " FROM frontier ORDER BY url_hash"))
            c.close()
            self.assertEqual(ver, 5, "应迁移到 v5")
            self.assertIn("throttle_count", cols)
            self.assertEqual(rows, [("h1", "done", 0, 0), ("h2", "pending", 1, 0)],
                             "老数据必须完整保留（状态/深度不变，新列取默认 0）")

            fm.FrontierDB(path)          # 幂等：二次迁移
            c = _sq.connect(path)
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 5)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM frontier").fetchone()[0], 2)
            c.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_throttle_exhausts_to_dead(self):
        """连续限流达上限 → 转 dead（否则任务永驻 retry，主循环无法退出）"""
        async def go():
            with _TmpDB() as db:
                await db.init_async()
                try:
                    await db.push("https://example.com/d")
                    await db.flush()
                    batch = await db.pop_batch(limit=1)
                    uh = batch[0]["url_hash"]
                    # 连续限流 10 次（每次 mark_failed(throttled=True)）
                    for _ in range(10):
                        await db.mark_failed(uh, retry=True, throttled=True)
                        await db.flush()
                    cur = await db._read_conn.execute(
                        "SELECT status FROM frontier WHERE url_hash=?", (uh,))
                    self.assertEqual((await cur.fetchone())[0], "dead",
                                     "连续限流达上限应转 dead（保证主循环可退出）")
                finally:
                    await db.close()
        _run(go())

    def test_throttle_not_consuming_retry_count(self):
        """限流仍不消耗 retry_count（保留原设计意图）"""
        async def go():
            with _TmpDB() as db:
                await db.init_async()
                try:
                    await db.push("https://example.com/e")
                    await db.flush()
                    batch = await db.pop_batch(limit=1)
                    uh = batch[0]["url_hash"]
                    await db.mark_failed(uh, retry=True, throttled=True)
                    await db.flush()
                    cur = await db._read_conn.execute(
                        "SELECT retry_count, COALESCE(throttle_count,0) FROM frontier WHERE url_hash=?",
                        (uh,))
                    rc, tc = await cur.fetchone()
                    self.assertEqual(int(rc), 0, "限流不应消耗 retry_count")
                    self.assertEqual(int(tc), 1, "限流应累计 throttle_count")
                finally:
                    await db.close()
        _run(go())

    def test_done_resets_throttle_count(self):
        async def go():
            with _TmpDB() as db:
                await db.init_async()
                try:
                    await db.push("https://example.com/f")
                    await db.flush()
                    batch = await db.pop_batch(limit=1)
                    uh = batch[0]["url_hash"]
                    await db.mark_failed(uh, retry=True, throttled=True)
                    await db.flush()
                    await db.mark_done(uh)
                    await db.flush()
                    cur = await db._read_conn.execute(
                        "SELECT COALESCE(throttle_count,0) FROM frontier WHERE url_hash=?", (uh,))
                    self.assertEqual(int((await cur.fetchone())[0]), 0,
                                     "成功打卡应清零连续限流计数")
                finally:
                    await db.close()
        _run(go())


class TestPageTimeoutDefault(unittest.TestCase):
    def test_default_is_nonzero(self):
        from kiana_vnext_plus.config import DEFAULT_GLOBAL
        v = DEFAULT_GLOBAL.get("page_timeout")
        self.assertIsNotNone(v, "page_timeout 应有默认值")
        self.assertGreater(int(v), 0,
                           "默认应为非零（原 0 意味着单页挂起即冻住整批）")


class TestPersistAfterCas(unittest.TestCase):
    """结构断言：CAS 打卡必须早于持久化（否则失守时产物已落盘 → 重复导出）"""

    def test_cas_precedes_persist(self):
        from kiana_vnext_plus.page_processor import PageProcessor
        src = inspect.getsource(PageProcessor.process_job)
        # 用"真实调用"精确匹配（注释里也出现过这两个词，粗匹配会误判）
        i_cas = src.find("await self.frontier.mark_done_checked")
        i_persist = src.find("await self._persist_export")
        self.assertGreater(i_cas, -1, "应有 CAS 打卡调用")
        self.assertGreater(i_persist, -1, "应有持久化调用")
        self.assertLess(i_cas, i_persist,
                        "CAS 打卡必须先于 _persist_export（防 CAS 失守时重复落盘）")

    def test_exception_after_cas_does_not_refail(self):
        from kiana_vnext_plus.page_processor import PageProcessor
        src = inspect.getsource(PageProcessor.process_job)
        # [v2.19.6] 变量改为三态的 _cas（True 赢 / False 易主 / None 落库失败）
        self.assertIn("_cas", src, "异常分支需用 CAS 结果判断是否已置 done")
        self.assertIn("打卡后异常", src)
        self.assertIn("_cas is True", src, "仅当确实赢得 CAS 时才不标 failed")

    def test_all_result_paths_use_lease(self):
        """4 处写结果路径中，有租约的三处必须传 leased_at"""
        from kiana_vnext_plus.page_processor import PageProcessor
        src = inspect.getsource(PageProcessor)
        n_with_lease = src.count("mark_done(uh, leased_at=")
        self.assertGreaterEqual(n_with_lease, 3,
                                "质量拒收/304 两处应传 leased_at 走 CAS")


if __name__ == "__main__":
    unittest.main()
