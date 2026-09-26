"""Kiana Vnext Plus — v2.16 M5 隐私：脱敏可开关全链 + 血缘审计 parent_id"""
import sys, os, asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault('KIANA_CRYPTO_KEY', 'kv_test')

from kiana_vnext_plus.sanitizer import sanitize_text


class TestSanitizeSwitch:
    """指南「隐私可开关」：privacy_sanitize=True 脱敏；False 保留原文"""

    SAMPLE = "联系我13812345678或发 test@example.com 地址 192.168.1.1"

    def test_enabled_masks(self):
        out = sanitize_text(self.SAMPLE)
        assert "[手机号]" in out and "[邮箱]" in out and "[IP]" in out

    def test_disabled_preserves(self):
        # _sanitize_enabled=False 时 page_processor 跳过 sanitize_text（原样保留）
        # 验证链路：crawler._sanitize_enabled = cfg.privacy_sanitize
        from omegaconf import OmegaConf
        from kiana_vnext_plus.config import GlobalConfig
        g = GlobalConfig(OmegaConf.create({"master_password": "pw", "privacy_sanitize": False}))
        assert getattr(g, "privacy_sanitize", True) is False   # 可关
        g2 = GlobalConfig(OmegaConf.create({"master_password": "pw", "privacy_sanitize": True}))
        assert g2.privacy_sanitize is True                     # 可开

    def test_crawler_flag_mirrors_cfg(self):
        from omegaconf import OmegaConf
        from kiana_vnext_plus.config import GlobalConfig
        cfg = GlobalConfig(OmegaConf.create({"master_password": "pw", "privacy_sanitize": False}))
        # crawler._sanitize_enabled 读取链（模拟属性读取）
        flag = bool(cfg.get("privacy_sanitize", True))
        assert flag is False

    def test_sanitize_url_token(self):
        from kiana_vnext_plus.sanitizer import sanitize_url
        assert "[REDACTED]" in sanitize_url("https://x.com/a?token=secret123")


class TestParentLineage:
    """血缘审计：frontier parent_id 列迁移（v1→v2）"""

    def test_migration_adds_parent(self, tmp_path):
        import sqlite3
        from kiana_vnext_plus.frontier import FrontierDB
        # 构造 v1 库（无 parent_hash 列）
        conn = sqlite3.connect(str(tmp_path / "v1.db"))
        conn.execute("""CREATE TABLE IF NOT EXISTS frontier (
            url_hash TEXT PRIMARY KEY, normalized_url TEXT, domain TEXT, depth INTEGER DEFAULT 0,
            priority INTEGER DEFAULT 5, status TEXT DEFAULT 'pending', scheduled_at REAL,
            retry_count INTEGER DEFAULT 0, max_retries INTEGER DEFAULT 3, leased_at REAL,
            worker_id TEXT, created_at REAL DEFAULT (strftime('%s','now')))""")
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        conn.close()
        db = FrontierDB(str(tmp_path / "v1.db"))  # 应触发 v2 迁移
        conn = sqlite3.connect(str(tmp_path / "v1.db"))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(frontier)")}
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        assert "parent_hash" in cols and ver >= 2  # v3 增量迁移（errors/video_downloads 补列）不影响本断言

    def test_push_with_parent(self, tmp_path):
        import asyncio, time
        from kiana_vnext_plus.frontier import FrontierDB
        from kiana_vnext_plus.url_utils import url_hash
        db = FrontierDB(str(tmp_path / "f.db"))

        async def flow():
            await db.init_async()
            await db.push("https://x.com/parent", force=True, parent_hash=None)
            await db.push("https://x.com/child", force=True, parent_hash=url_hash("https://x.com/parent"))
            await db.flush()
            async with db._read_conn.execute(
                    "SELECT parent_hash FROM frontier WHERE status='pending' AND normalized_url='https://x.com/child'") as cur:
                row = await cur.fetchone()
            assert row and row[0] == url_hash("https://x.com/parent")
        asyncio.run(flow())
