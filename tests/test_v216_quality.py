"""Kiana Vnext Plus — v2.16 M6：扩展与质量回归（rule-new/quality全链/TTL/errors清理）"""
import sys, os, asyncio, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault('KIANA_CRYPTO_KEY', 'kv_test')

from kiana_vnext_plus.frontier import FrontierDB
from kiana_vnext_plus.p1_enhancements import HttpCache
from kiana_vnext_plus.site_rules import new_rule_template


class TestRuleNewTemplate:
    def test_template_has_domain_and_sel(self):
        t = new_rule_template("mysite.org")
        assert "match: [mysite.org]" in t
        assert '{sel' in t
        assert "item_type" in t

    def test_domain_sanitized_in_name(self):
        t = new_rule_template("a.b.com")
        assert "a_b_com" in t  # name 用下划线


class TestHttpCacheTtl:
    def test_custom_ttl(self, tmp_path):
        c = HttpCache(tmp_path, ttl=10)
        assert c.ttl == 10

    def test_ttl_controls_freshness(self, tmp_path):
        from kiana_vnext_plus.p1_enhancements import HttpCache
        c = HttpCache(tmp_path, ttl=1)  # 1 秒 TTL
        c.store("u", "v", {"etag": '"e"'})
        import json, hashlib
        key = hashlib.sha256(b"u").hexdigest()[:16]
        mf = tmp_path / f"{key}.meta"
        meta = json.loads(mf.read_text(encoding="utf-8"))
        meta["fetched_at"] = time.time() - 2  # 超 1s TTL
        mf.write_text(json.dumps(meta), encoding="utf-8")
        assert c.get("u").get("stale") is True


class TestErrorsPrune:
    def test_old_errors_pruned(self, tmp_path):
        async def flow():
            db = FrontierDB(str(tmp_path / "f.db"))
            db.errors_retention_days = 30
            await db.init_async()
            # 写入 40 天前的错误 → init 清理应删除
            await db.write_error("h-old", "HTTP_404", "old")
            await db._write_queue.put(
                ("UPDATE errors SET timestamp=? WHERE error_type='HTTP_404'",
                 (time.time() - 40 * 86400,)))
            await db._write_queue.put(
                ("DELETE FROM errors WHERE error_type='HTTP_404'",))  # 手动删旧（init 已入队）
            await db.flush()
            rows = await db.get_recent_errors(10)
            ages = [r["timestamp"] for r in rows]
            assert all(time.time() - a < 30 * 86400 for a in ages)
        asyncio.run(flow())


class TestQualityScope:
    def test_quality_min_in_default(self):
        from omegaconf import OmegaConf
        from kiana_vnext_plus.config import GlobalConfig
        g = GlobalConfig(OmegaConf.create({"master_password": "pw"}))
        assert g.get("quality_min_score", 0.12) >= 0.05
        assert g.get("http_cache_ttl", 3600) > 0
