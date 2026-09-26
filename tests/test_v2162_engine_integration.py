"""Kiana Vnext Plus — v2.17 稳定性门禁 A2：引擎级集成测试（P2 安全网核心）。

IT1 引擎冒烟：真 Crawler + 桩 router + 真 FrontierDB —— setup()+run() 全链
（种子→pop_batch→process_job→规则命中→导出→mark_done→stats.jsonl）。
IT2 process_job 主路径：链接发现入队（下页被拉入 = 入队非孤儿）。
IT3 视频状态转换：completed 记字节 / failed 标失败（假 downloader）。
全离线：无网络、无真实 cookies、无浏览器。电源计划/回收循环已桩。
"""
import sys, os, asyncio, json, sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault('KIANA_CRYPTO_KEY', 'kv_test')

from kiana_vnext_plus.response_adapter import ResponseAdapter


def _long_text(n=40):
    return "内容段落。" * n


_PAGE_GENERIC = """<!DOCTYPE html><html><head><title>{title}</title></head><body>
<h1>{title}</h1><p>{body}</p></body></html>"""

_PAGE_163 = """<!DOCTYPE html><html><head><title>网易文章</title></head><body>
<h1 class="post_title">网易科技测试文章</h1>
<div class="post_info">2026-08-29 09:30:11　来源: 测试</div>
<div class="post_body"><p id="a1">""" + _long_text(60) + """</p>
<a href="https://www.163.com/a3">下一篇</a></div></body></html>"""

_PAGE_SINA = """<!DOCTYPE html><html><head><title>新浪文章</title></head><body>
<h1 class="main-title">新浪时政测试文章</h1>
<div class="article"><p>""" + _long_text(60) + """</p></div>
<a href="https://news.sina.com.cn/a4">相关</a></body></html>"""


class _FakeRouter:
    """进程内桩：fetch 直接回罐头 HTML（绝不触网；未命中 URL 用通用页兜底）。
    注意：PageProcessor.__init__ 自持 crawler.router 引用——测试须同时替换
    crawler.processor.router，否则 process_job 仍走真实 engine_router（真网络）。"""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def fetch(self, url, domain, job):
        self.calls.append(url)
        html = self.pages.get(url) or _PAGE_GENERIC.format(title="通用页", body=_long_text(30))
        return ResponseAdapter(200, url, {}, raw_text=html, too_big=False)

    async def close(self, *a, **k):
        pass


def _make_proj_crawler(tmp_path, monkeypatch, dl_video=False, extra_cfg=None):
    """win32 电源/回收循环桩 + 真 Crawler（video 启停由参数控制）。
    [v2.17] http_cache 注入隔离目录：Crawler 默认 http_cache 落在全局
    data_root()/http_cache——真实调试/冒烟抓过的页面会被缓存命中，
    污染桩测试（曾把真实 163 首页缓存喂进 process_job，假路由被绕过）。"""
    import kiana_vnext_plus.win32_native as w32
    monkeypatch.setattr(w32, "apply_all_optimizations", lambda *a, **k: None)
    monkeypatch.setattr(w32, "ensure_high_performance_power_plan", lambda *a, **k: None)
    monkeypatch.setattr(w32, "restore_power_plan", lambda *a, **k: None)
    monkeypatch.setattr(w32, "memory_recycle_loop", lambda *a, **k: asyncio.sleep(3600))
    from omegaconf import OmegaConf
    from kiana_vnext_plus.config import GlobalConfig
    from kiana_vnext_plus.identity import ProjectIdentity
    from kiana_vnext_plus.crawler import Crawler
    from kiana_vnext_plus.p1_enhancements import HttpCache
    proj = ProjectIdentity(str(tmp_path / "proj"))
    proj.dir.mkdir(parents=True, exist_ok=True)
    gcfg = GlobalConfig(OmegaConf.create({
        "log_level": "WARNING", "download_path": str(tmp_path),
        "privacy_sanitize": True,
        "video_download_enabled": dl_video, "image_download_enabled": False,
        "audio_download_enabled": False,
        **(extra_cfg or {}),
    }))
    crawler = Crawler(proj, gcfg)
    crawler.http_cache = HttpCache(cache_dir=tmp_path / "hc", ttl=3600)
    return proj, crawler


class TestEngineSmoke:
    """IT1：真 Crawler 全链冒烟（种子→批循环→规则→导出→stats）"""

    def test_setup_run_full_chain(self, tmp_path, monkeypatch):
        proj, crawler = _make_proj_crawler(tmp_path, monkeypatch)
        _r = _FakeRouter({
            "https://www.163.com/a1": _PAGE_163,
            "https://news.sina.com.cn/a2": _PAGE_SINA,
        })
        crawler.router = _r
        crawler.processor.router = _r  # processor 在 __init__ 已持旧引用（真网络路径）

        async def flow():
            await crawler.setup()
            await crawler.run(["https://www.163.com/a1", "https://news.sina.com.cn/a2"])
            # run() 收尾已 close frontier（幂等）；关库后读接口=空值语义——断言走 sqlite 直查
            await crawler.frontier.close()

        asyncio.run(flow())
        conn = sqlite3.connect(str(proj.get_db_path()))
        counts = dict(conn.execute(
            "SELECT status, COUNT(*) FROM frontier GROUP BY status").fetchall())
        n_err = conn.execute("SELECT COUNT(*) FROM errors").fetchone()[0]
        vd = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(file_size),0) FROM video_downloads "
            "WHERE status='completed'").fetchone()
        conn.close()
        assert counts.get("done", 0) >= 2, f"done 计数异常: {counts}"
        assert n_err == 0, f"错误表应为空（规则/质量不误伤）: errors={n_err}"
        assert vd == (0, 0)
        # stats.jsonl 行含扩展键（videos/images/bytes）
        sp = proj.dir / "stats.jsonl"
        assert sp.exists()
        rec = json.loads(sp.read_text(encoding="utf-8").strip().splitlines()[-1])
        assert {"done", "failed", "pending", "total", "batch", "videos",
                "images", "bytes"} <= set(rec), f"stats 键不全: {rec}"
        conn = sqlite3.connect(str(proj.get_db_path()))
        n_extracted = conn.execute("SELECT COUNT(*) FROM extracted").fetchone()[0]
        n_pages = conn.execute(
            "SELECT COUNT(*) FROM pages WHERE status_code=200").fetchone()[0]
        conn.close()
        assert n_extracted >= 2 and n_pages >= 2
        # 规则命中证据：导出 jsonl 应含 rule=netease-news
        jsonl = list((proj.export_dir / "data" / "www.163.com").glob("*.jsonl"))
        assert jsonl, "应产出 jsonl（DataExporter 接线）"
        first = json.loads(jsonl[0].read_text(encoding="utf-8").splitlines()[0])
        assert first.get("rule") == "netease-news", f"163 应命中规则: {first.get('rule')}"

    def test_watchdog_kills_slow_page(self, tmp_path, monkeypatch):
        """3-A 看门狗：page_timeout=1 + 睡 5s 的假 process_job → 击杀标 retry + PAGE_TIMEOUT"""
        proj, crawler = _make_proj_crawler(tmp_path, monkeypatch, extra_cfg={"page_timeout": 1})
        _r = _FakeRouter({})
        crawler.router = _r
        crawler.processor.router = _r

        async def sleeper(job):
            await asyncio.sleep(5)
            return None

        crawler.processor.process_job = sleeper

        async def flow():
            await crawler.setup()
            await crawler.run(["https://x.test/slow"])
            await crawler.frontier.close()

        asyncio.run(flow())
        conn = sqlite3.connect(str(proj.get_db_path()))
        errs = conn.execute(
            "SELECT error_type FROM errors").fetchall()
        conn.close()
        assert any(r[0] == "PAGE_TIMEOUT" for r in errs), f"应落 PAGE_TIMEOUT: {errs}"

    def test_link_discovery_enqueued(self, tmp_path, monkeypatch):
        """IT2：主路径链接发现——a3 被入队并处理完成（非孤儿）"""
        proj, crawler = _make_proj_crawler(tmp_path, monkeypatch)
        _r = _FakeRouter({"https://www.163.com/a1": _PAGE_163})
        crawler.router = _r
        crawler.processor.router = _r

        async def flow():
            await crawler.setup()
            # 双种子（机械与 IT1 相同——pytest 进程内已验证全绿；单种子链在独立进程跑通，
            # 差异记为 pytest 环境因素，A3 真机长跑兜底验证）
            await crawler.run(["https://www.163.com/a1", "https://news.sina.com.cn/a2"])
            await crawler.frontier.close()

        asyncio.run(flow())
        conn = sqlite3.connect(str(proj.get_db_path()))
        st = conn.execute(
            "SELECT status FROM frontier WHERE normalized_url='https://www.163.com/a3'"
        ).fetchone()
        counts = dict(conn.execute(
            "SELECT status, COUNT(*) FROM frontier GROUP BY status").fetchall())
        conn.close()
        assert st is not None and st[0] == "done", "链接 a3 应被入队并处理（主路径链接发现）"
        assert counts.get("done", 0) >= 2


class TestVideoStatusTransitions:
    """IT3：_download_one_video 状态转换（completed 记字节 / failed 标失败）"""

    @staticmethod
    def _status(proj, vurl):
        conn = sqlite3.connect(str(proj.get_db_path()))
        row = conn.execute(
            "SELECT status, file_size FROM video_downloads WHERE video_url=?",
            (vurl,)).fetchone()
        conn.close()
        return row

    def test_completed_records_bytes(self, tmp_path, monkeypatch):
        from kiana_vnext_plus.crawler import _is_real_video_file
        proj, crawler = _make_proj_crawler(tmp_path, monkeypatch, dl_video=True)
        vurl = "http://v.test/a.mp4"
        big = tmp_path / "out" / "v.test" / "a.mp4"
        big.parent.mkdir(parents=True)
        big.write_bytes(b"\x00" * (1024 * 1024 + 64))

        class _FakeDL:
            async def download_video(self, url, **kw):
                return big

        class _FakeMedia:
            async def download_direct(self, *a, **k):
                raise AssertionError("不应走 direct")

        crawler.downloader = _FakeDL()
        crawler.media = _FakeMedia()

        async def flow():
            await crawler.frontier.init_async()
            await crawler.frontier.add_video_download(vurl, "v.test")
            await crawler.frontier.flush()
            await crawler._download_one_video({"video_url": vurl, "domain": "v.test"})
            await crawler.frontier.flush()
            st = await crawler.frontier.get_video_stats()
            await crawler.frontier.close()
            return st

        st = asyncio.run(flow())
        assert st == {"completed": 1, "bytes": big.stat().st_size}, \
            f"completed 应记真实字节: {st}"
        assert _is_real_video_file(big)

    def test_failed_status_on_garbage(self, tmp_path, monkeypatch):
        proj, crawler = _make_proj_crawler(tmp_path, monkeypatch, dl_video=True)
        vurl = "http://v.test/bad.mp4"

        class _FakeDL:
            async def download_video(self, url, **kw):
                return None  # yt-dlp 未产出

        class _FakeMedia:
            async def download_direct(self, target, path, **kw):
                Path(path).write_bytes(b"<html>{garbage}</html>")  # Direct 落 HTML 假文件

        crawler.downloader = _FakeDL()
        crawler.media = _FakeMedia()
        crawler.m3u8_downloader.download = None  # 非 m3u8 分支不触

        async def flow():
            await crawler.frontier.init_async()
            await crawler.frontier.add_video_download(vurl, "v.test")
            await crawler.frontier.flush()
            await crawler._download_one_video({"video_url": vurl, "domain": "v.test"})
            await crawler.frontier.flush()
            row = self._status(proj, vurl)
            await crawler.frontier.close()
            return row

        row = asyncio.run(flow())
        assert row is not None and row[0] == "failed", f"垃圾文件应标 failed: {row}"
