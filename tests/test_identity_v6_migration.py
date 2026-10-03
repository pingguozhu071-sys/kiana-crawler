# -*- coding: utf-8 -*-
"""v6 迁移回归：accounts 表补列（额度记账 / 失败原因 / 最近成功 / 指纹标识）

覆盖四件事，都是"会毁老库"或"会静默失效"的方向：
  ① 老库（v5）升级后**老数据零丢失**，新列取默认值；
  ② 迁移**幂等**（连跑两次无变化）；
  ③ **打开顺序无关**——CookieArmory 与 FrontierDB 谁先打开同一个库，列集一致
     （v6 之前这两处各有一份逐字相同的 DDL，加列时只改一处即漏）；
  ④ 新库直接就是 v6 结构。

反证：`test_v5_fixture_really_lacks_v6_columns` 证明夹具**确实**是没有 v6 列的老库——
否则迁移测试是在一个本来就有新列的库上跑，等于假绿。
"""
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import kiana_vnext_plus.frontier as fm

V6_COLUMNS = ("quota_window_start", "quota_used", "quota_limit",
              "last_error_kind", "last_error_at", "last_ok_at", "identity_fingerprint")

# v5 当时的 accounts 结构（8 列）——刻意照抄旧版，用于构造老库
OLD_ACCOUNTS_DDL = """CREATE TABLE accounts (
    site TEXT, name TEXT, state_blob TEXT, health_score REAL DEFAULT 1.0,
    cooldown_until REAL DEFAULT 0, success_count INTEGER DEFAULT 0,
    fail_count INTEGER DEFAULT 0, updated_at REAL,
    PRIMARY KEY (site, name))"""


def _make_v5_db(path: str):
    """手工构造 v5 老库（SQL 全为固定字符串，无拼接/格式化）。"""
    c = sqlite3.connect(path)
    c.execute("""CREATE TABLE frontier (url_hash TEXT PRIMARY KEY, normalized_url TEXT,
        domain TEXT, depth INTEGER DEFAULT 0, priority INTEGER DEFAULT 5,
        status TEXT DEFAULT 'pending', scheduled_at REAL, retry_count INTEGER DEFAULT 0,
        max_retries INTEGER DEFAULT 3, leased_at REAL, lease_expires REAL,
        worker_id TEXT, created_at REAL, parent_hash TEXT, throttle_count INTEGER DEFAULT 0)""")
    c.execute("""CREATE TABLE pages (url_hash TEXT PRIMARY KEY, status_code INTEGER,
        content_length INTEGER, fetch_time REAL, headers TEXT, content_hash TEXT,
        simhash INTEGER, duplicate_of TEXT)""")
    c.execute("CREATE TABLE extracted (url_hash TEXT PRIMARY KEY, data_json TEXT)")
    c.execute("""CREATE TABLE errors (url_hash TEXT, error_type TEXT, error_message TEXT,
        timestamp REAL, platform TEXT DEFAULT '', code TEXT DEFAULT '')""")
    c.execute("""CREATE TABLE video_downloads (video_url TEXT PRIMARY KEY, domain TEXT,
        status TEXT DEFAULT 'pending', file_path TEXT, progress REAL,
        fail_count INTEGER DEFAULT 0, created_at REAL, file_size REAL DEFAULT 0)""")
    c.execute("""CREATE TABLE cooldowns (domain TEXT PRIMARY KEY, until_epoch REAL,
        tier INTEGER, updated_at REAL)""")
    c.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    c.execute(OLD_ACCOUNTS_DDL)
    c.execute("INSERT INTO frontier VALUES ('h1','https://old/a','old',0,5,'done',"
              "0,0,3,NULL,NULL,NULL,0,NULL,0)")
    c.execute("INSERT INTO frontier VALUES ('h2','https://old/b','old',1,5,'pending',"
              "0,0,3,NULL,NULL,NULL,0,NULL,0)")
    c.execute("INSERT INTO accounts VALUES ('bilibili','acc1','blob-1',0.75,0,3,2,111.0)")
    c.execute("INSERT INTO accounts VALUES ('xhs','acc2','blob-2',1.0,0,0,0,222.0)")
    c.execute("PRAGMA user_version = 5")
    c.commit()
    c.close()


class TestAccountsV6Migration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kiana_v6_")
        self.path = str(Path(self.tmp) / "old.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_v5_fixture_really_lacks_v6_columns(self):
        """反证：夹具必须是"真的没有 v6 列"的 v5 老库，否则后面的迁移断言是假绿。"""
        _make_v5_db(self.path)
        c = sqlite3.connect(self.path)
        cols = {r[1] for r in c.execute("PRAGMA table_info(accounts)")}
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        c.close()
        self.assertEqual(ver, 5, "夹具版本号必须是 5")
        for name in V6_COLUMNS:
            self.assertNotIn(name, cols, f"夹具不干净：已存在 {name} → 迁移测试会假绿")

    def test_old_v5_accounts_migrate_without_data_loss(self):
        _make_v5_db(self.path)
        fm.FrontierDB(self.path)                     # 构造即迁移

        c = sqlite3.connect(self.path)
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        cols = {r[1] for r in c.execute("PRAGMA table_info(accounts)")}
        rows = list(c.execute(
            "SELECT site, name, state_blob, health_score, success_count, fail_count,"
            " quota_used, quota_limit, last_error_kind, last_error_at, last_ok_at,"
            " identity_fingerprint FROM accounts ORDER BY site"))
        frows = list(c.execute("SELECT url_hash, status, depth FROM frontier"
                               " ORDER BY url_hash"))
        c.close()

        self.assertEqual(ver, 6, "应迁移到 v6")
        for name in V6_COLUMNS:
            self.assertIn(name, cols)
        # 老数据零丢失（身份键 / 密文 / 健康分 / 计数全部原样）
        self.assertEqual(rows[0][:6], ("bilibili", "acc1", "blob-1", 0.75, 3, 2))
        self.assertEqual(rows[1][:6], ("xhs", "acc2", "blob-2", 1.0, 0, 0))
        # 新列取默认：quota_used=0 / quota_limit=0，其余 **NULL**
        # （NULL 表示"从未失败过"；刻意不用非空默认值伪装成"有过记录"）
        self.assertEqual(rows[0][6:], (0, 0, None, None, None, None))
        self.assertEqual(rows[1][6:], (0, 0, None, None, None, None))
        # frontier 老数据不受影响
        self.assertEqual(frows, [("h1", "done", 0), ("h2", "pending", 1)])

    def test_migration_is_idempotent(self):
        _make_v5_db(self.path)
        fm.FrontierDB(self.path)
        fm.FrontierDB(self.path)                     # 二次迁移必须无变化
        c = sqlite3.connect(self.path)
        self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 6)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 2)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM frontier").fetchone()[0], 2)
        c.close()

    def test_fresh_db_is_v6(self):
        fm.FrontierDB(self.path)
        c = sqlite3.connect(self.path)
        cols = {r[1] for r in c.execute("PRAGMA table_info(accounts)")}
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        c.close()
        self.assertEqual(ver, 6)
        for name in V6_COLUMNS:
            self.assertIn(name, cols)

    def test_cookie_armory_first_is_order_independent(self):
        """[v6 收敛] 弹药库先打开同一个库，frontier 后打开——列集必须一致。

        v6 之前两处各有一份逐字相同的 DDL，加列时只改一处即漏；本用例锁死这条。
        """
        from kiana_vnext_plus.cookie_armory import CookieArmory
        CookieArmory(self.path, "test-master-pw")    # 先由弹药库建库
        c = sqlite3.connect(self.path)
        cols_before = {r[1] for r in c.execute("PRAGMA table_info(accounts)")}
        c.close()
        for name in V6_COLUMNS:
            self.assertIn(name, cols_before, "弹药库单独打开时也必须拿到 v6 列")

        fm.FrontierDB(self.path)                     # 再由 frontier 打开并推进版本
        c = sqlite3.connect(self.path)
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        cols_after = {r[1] for r in c.execute("PRAGMA table_info(accounts)")}
        c.close()
        self.assertEqual(ver, 6)
        self.assertEqual(cols_before, cols_after, "两个入口打开的列集必须一致")


if __name__ == "__main__":
    unittest.main()
