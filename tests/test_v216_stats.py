"""Kiana Vnext Plus — v2.16 修复：stats.jsonl 单页任务 0 字节（v2.15 遗留）+ 304 路径 await 丢失

根因：crawler.py 模块级无 import time，但 stats 写入块用 time.time() → NameError 恒被
except 吞掉 → 文件恒 0 字节（此前误判为"多批偶发"）。修复：默认计数改采 frontier 权威值
（_progress 的 pending/total 从不更新 → GUI 进度条恒跳 100%）。
"""
import sys, os, asyncio, json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault('KIANA_CRYPTO_KEY', 'kv_test')

from kiana_vnext_plus.crawler import append_stats_line


class TestStatsLineWriter:
    def test_writes_single_line(self, tmp_path):
        p = tmp_path / "stats.jsonl"
        assert append_stats_line(p, done=1, failed=0, pending=0, total=1, batch_size=1) is True
        lines = p.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["done"] == 1 and rec["failed"] == 0 and rec["pending"] == 0
        assert rec["total"] == 1 and rec["batch"] == 1
        assert isinstance(rec["ts"], float)  # time.time() epoch —— 回归：原 NameError 恒 0 字节

    def test_appends_multi_lines(self, tmp_path):
        p = tmp_path / "stats.jsonl"
        append_stats_line(p, 1, 0, 3, 4, 1)
        append_stats_line(p, 2, 1, 1, 4, 1)
        recs = [json.loads(l) for l in p.read_text(encoding="utf-8").strip().splitlines()]
        assert len(recs) == 2
        assert recs[1]["done"] == 2 and recs[1]["failed"] == 1

    def test_missing_dir_returns_false(self, tmp_path):
        assert append_stats_line(tmp_path / "nope" / "stats.jsonl", 1, 0, 0, 1, 1) is False


class Test304FinalizeAwaited:
    """v2.16 修复：304 增量命中路径 _finalize_done 缺 await（原成孤儿协程——
    adaptive/压力记录丢失 + RuntimeWarning）。用最小假体驱动 process_job，
    断言 adaptive.record 被执行（= _finalize_done 真正 await 完成）。"""

    @staticmethod
    def _make_fakes(cache=None):
        calls = {"adaptive": 0}

        class _Limits:
            max_pages_per_domain = 999
            max_pages = 999

        class _Config:
            limits = _Limits()

            def get(self, k, d=None):
                return d

        class _Project:
            config = _Config()

        class _Adaptive:
            def record(self, *a, **k):
                calls["adaptive"] += 1

        class _Frontier:
            async def count_done_by_domain(self, d):
                return 0

            async def count_done_total(self):
                return 0

            # [v2.19] 忠实模拟真实接口：mark_done 新增可选 leased_at（CAS 收口），
            # write_error 带 platform/code 关键字——桩不跟随会让被测代码误入 except
            async def mark_done(self, uh, leased_at=None):
                return True

            async def mark_done_checked(self, uh, leased_at):
                return True

            async def mark_failed(self, uh, retry=False, **k):
                pass

            async def write_error(self, *a, **k):
                pass

        class _ExitMgr:
            async def acquire_for_domain(self, *a, **k):
                return None

            async def report_result(self, *a, **k):
                pass

            async def release(self, *a, **k):
                pass

        class _HttpCache:
            def get(self, url):
                return {"content": "<html>x</html>", "stale": True,
                        "meta": {"etag": '"e1"'}}

            def store(self, url, html, meta):
                pass

        class _Router:
            async def fetch(self, url, domain, job):
                class _Resp:
                    status_code = 304
                return _Resp()

        class _Concurrency:
            async def acquire_all(self, *a, **k):
                pass

            async def release_all(self, *a, **k):
                pass

        import types as _t
        crawler = _t.SimpleNamespace(
            _fp_gen=None,
            _progress={"done": 0, "failed": 0, "pending": 0, "total": 0},
            _shielded_domains=set(),
            rate_limiter=None,
            http_cache=cache if cache is not None else _HttpCache(),
            concurrency=_Concurrency(),
            pressure_controller=None,
            autoscale_pool=None,
            project=_Project(),
            cfg=_Config(),
            frontier=_Frontier(),
            exit_mgr=_ExitMgr(),
            router=_Router(),
            adaptive=_Adaptive(),
        )
        return crawler, calls

    def test_304_path_awaits_finalize(self):
        from kiana_vnext_plus.page_processor import PageProcessor
        crawler, calls = self._make_fakes()
        proc = PageProcessor(crawler)

        async def _no_delay(domain=None):
            pass
        proc._human_delay = _no_delay  # 304 分支不测拟人延迟（原 3-45s 拖垮单测）

        asyncio.run(proc.process_job({
            "normalized_url": "http://site.example/p",
            "domain": "site.example",
            "url_hash": "h1",
        }))
        assert calls["adaptive"] == 1, "304 路径 _finalize_done 必须被 await（adaptive.record 生效）"

    def test_304_path2_awaits_finalize(self):
        """第二段 304 快速路径（http_cache 存在但首查未命中→重查命中）——
        v2.16.1 回归：该分支 _finalize_done 原来缺 await（漏掉 v2.16 修复）"""
        from kiana_vnext_plus.page_processor import PageProcessor

        class _LateHttp:
            """第一次 get 返回 None（未命中→走服务器 fetch→304），第二次返回带内容"""
            def __init__(self):
                self.calls = 0

            def get(self, url):
                self.calls += 1
                if self.calls == 1:
                    return None
                return {"content": "<html>x</html>"}

            def store(self, url, html, meta):
                pass

        crawler, calls = self._make_fakes(cache=_LateHttp())
        proc = PageProcessor(crawler)

        async def _no_delay(domain=None):
            pass
        proc._human_delay = _no_delay

        asyncio.run(proc.process_job({
            "normalized_url": "http://site.example/p",
            "domain": "site.example",
            "url_hash": "h1",
        }))
        assert calls["adaptive"] == 1, "第二段 304 路径 _finalize_done 必须被 await"
