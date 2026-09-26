"""Kiana Vnext Plus — v2.16 阶段L：LLM 批处理回归（纯数据流，不碰文件系统）"""
import sys, os, asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from kiana_vnext_plus.llm_client import async_llm_process_rows, _truncate


class _FakeClient:
    async def chat(self, messages):
        return "假结果摘要"
    def __init__(self):
        self.calls = 0


class TestAsyncBatch:
    def test_process_rows(self):
        async def flow():
            client = _FakeClient()
            rows = [{"url": "https://a.com/1", "title": "A", "text": "x" * 100},
                    {"url": "https://b.com/2", "title": "B", "text": "y" * 100},
                    {"url": "https://c.com/3", "title": "C", "text": "z" * 100}]
            r = await async_llm_process_rows(client, rows, task="summarize", budget_month=10)
            assert r["done"] == 3 and r["failed"] == 0
            assert all(o["ok"] for o in r["out_rows"])
            assert r["budget_left"] == 7
        asyncio.run(flow())

    def test_budget_stops(self):
        async def flow():
            client = _FakeClient()
            rows = [{"url": f"https://a.com/{i}", "title": "t", "text": "x"} for i in range(10)]
            r = await async_llm_process_rows(client, rows, budget_month=3)
            assert r["done"] == 3
            assert r["budget_left"] == 0
            assert len(r["out_rows"]) == 3
        asyncio.run(flow())

    def test_limit(self):
        async def flow():
            client = _FakeClient()
            rows = [{"url": f"https://a.com/{i}", "title": "t", "text": "x"} for i in range(5)]
            r = await async_llm_process_rows(client, rows, limit=2)
            assert r["done"] == 2
        asyncio.run(flow())

    def test_bad_task(self):
        async def flow():
            client = _FakeClient()
            rows = [{"url": "u", "title": "t", "text": "x"}]
            r = await async_llm_process_rows(client, rows, task="unknown")
            assert r["failed"] == 1
        asyncio.run(flow())

    def test_no_url_skipped(self):
        async def flow():
            client = _FakeClient()
            r = await async_llm_process_rows(client, [{"title": "no-url", "text": "x"}])
            assert r["done"] == 0 and r["failed"] == 0
        asyncio.run(flow())


class TestTruncate:
    def test_short_no_change(self):
        assert _truncate("short") == "short"

    def test_long_cut(self):
        assert len(_truncate("x" * 10000)) == 8000
