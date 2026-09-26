"""v2.18 P1 批量修复回归（P1-1/3/4/9/10/11）

- P1-1  LLM 链接打分分片并发 + 总预算超时（原串行 64 条 ≈ 35 分钟启动阻塞）
- P1-3  租约心跳续期 lease_expires（CAS 在 leased_at，不破坏 mark_done_checked）
- P1-4  CSV 追加 BOM 中部污染
- P1-9  裸 <link rel="canonical"> 整页 TypeError
- P1-11 邮箱正则 O(n²) 卡死 worker
"""
import asyncio
import csv
import time
import unittest
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiana_vnext_plus.llm_client import async_llm_score_links
from kiana_vnext_plus.frontier import FrontierDB
from kiana_vnext_plus.enhancements import DataExporter
from kiana_vnext_plus.parser import extract_metadata
from kiana_vnext_plus.sanitizer import sanitize_text


class _SlowClient:
    """每条 chat 固定耗时 0.15s 的假 LLM 客户端"""

    async def chat(self, messages):
        await asyncio.sleep(0.15)
        return "2"


class TestLLMScoreLinks(unittest.TestCase):
    def test_concurrent_not_serial(self):
        """8 条 × 0.15s：串行 1.2s；并发（8 路）应 <0.8s"""
        async def go():
            t0 = time.monotonic()
            out = await async_llm_score_links(_SlowClient(), [f"https://u/{i}" for i in range(8)])
            dt = time.monotonic() - t0
            self.assertEqual(len(out), 8)
            self.assertTrue(all(1 <= v <= 5 for v in out.values()))
            self.assertLess(dt, 0.8, f"打分仍疑似串行（耗时 {dt:.2f}s）")
        asyncio.new_event_loop().run_until_complete(go())

    def test_total_budget_timeout_degrades_honestly(self):
        """总预算超时：未完成条目降级默认分 3 而不是无限等待/抛异常"""
        async def go():
            out = await async_llm_score_links(
                _SlowClient(), [f"https://u/{i}" for i in range(8)],
                total_timeout=0.05)
            self.assertEqual(len(out), 8, "超时也必须返回全量条目（默认分）")
            self.assertTrue(all(v == 3 for v in out.values()))
        asyncio.new_event_loop().run_until_complete(go())


class TestLeaseHeartbeat(unittest.TestCase):
    def test_heartbeat_extends_without_breaking_cas(self):
        """心跳续期 lease_expires；leased_at 不变 → mark_done_checked 仍命中"""
        async def go():
            import shutil
            td = tempfile.mkdtemp(prefix="kiana_hb_")
            try:
                db = FrontierDB(str(Path(td) / "f.db"))
                await db.init_async()
                try:
                    await db.push("https://example.com/a")
                    await db.flush()
                    batch = await db.pop_batch(limit=1)
                    self.assertEqual(len(batch), 1)
                    job = batch[0]
                    self.assertIsNotNone(job.get("leased_at"),
                                         "pop_batch 返回的 job 必须携带本次租约的 leased_at")

                    cur = await db._read_conn.execute(
                        "SELECT lease_expires, leased_at FROM frontier WHERE url_hash=?",
                        (job["url_hash"],))
                    row = await cur.fetchone()
                    old_expires, old_leased = row[0], row[1]

                    ok = await db.heartbeat_lease(job["url_hash"], job["leased_at"])
                    self.assertTrue(ok, "当前持有者心跳必须成功")
                    cur = await db._read_conn.execute(
                        "SELECT lease_expires, leased_at FROM frontier WHERE url_hash=?",
                        (job["url_hash"],))
                    row = await cur.fetchone()
                    self.assertGreater(row[0], old_expires, "lease_expires 必须被推后")
                    self.assertEqual(row[1], old_leased, "leased_at 不得被心跳改动（CAS 依据）")

                    # CAS 闭环仍工作
                    self.assertTrue(await db.mark_done_checked(job["url_hash"], job["leased_at"]))
                    # 旧租约持有者再心跳 → 失败（易主语义）
                    self.assertFalse(await db.heartbeat_lease(job["url_hash"], job["leased_at"]))
                finally:
                    await db.close()
            finally:
                # Windows 下 aiosqlite 后台线程释放句柄略滞后于 close() 返回，
                # TemporaryDirectory 严格清理会 WinError 32——容错清理
                shutil.rmtree(td, ignore_errors=True)
        asyncio.new_event_loop().run_until_complete(go())


class TestCsvBom(unittest.TestCase):
    def test_append_flush_keeps_single_bom(self):
        """同秒两次 flush 追加同一 CSV：BOM 只在文件头（原 utf-8-sig 中部再插 BOM）"""
        with tempfile.TemporaryDirectory() as td:
            exp = DataExporter(Path(td))
            exp.buffer["a.com"] = [{"url": "https://a/1", "title": "t1"}]
            exp._flush_domain("a.com")
            exp.buffer["a.com"] = [{"url": "https://a/2", "title": "t2"}]
            exp._flush_domain("a.com")  # 同秒 → 同名文件追加
            csvs = list(Path(td).rglob("*.csv"))
            self.assertEqual(len(csvs), 1)
            raw = csvs[0].read_bytes()
            self.assertEqual(raw.count(b"\xef\xbb\xbf"), 1, "BOM 必须只出现一次（文件头）")
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))


class TestParserCanonical(unittest.TestCase):
    def test_bare_canonical_no_href_does_not_crash(self):
        """裸 <link rel="canonical">（无 href）→ 不再 TypeError 打穿整页"""
        html = '<html><head><link rel="canonical"></head><body>ok</body></html>'
        data = extract_metadata(html, "https://example.com/x")
        self.assertIsNone(data.get("canonical"), "无 href 的 canonical 不得产出 URL")


class TestSanitizerEmailPerf(unittest.TestCase):
    def test_long_token_linear(self):
        """1MB 无 @ 长串（base64 场景）：原 O(n²) 卡死数分钟 → 现亚秒"""
        big = "A1b2C3d4._%" * 100_000  # 1MB 全是邮箱局部合法字符（无 @）
        t0 = time.monotonic()
        out = sanitize_text(big)
        dt = time.monotonic() - t0
        self.assertEqual(out, big, "无邮箱内容不得改动")
        self.assertLess(dt, 2.0, f"1MB 文本脱敏耗时 {dt:.2f}s——疑似回溯爆炸")

    def test_email_still_sanitized(self):
        self.assertEqual(sanitize_text("联系 foo.bar@example.com 了解"), "联系 [邮箱] 了解")


if __name__ == "__main__":
    unittest.main()
