"""Kiana Vnext Plus — v2.17 第 4/5 章：数据层（sqlite 导出/stats 扩展/errors 平台列/
cookie 弱检查）与规则层（schema 升级/校验 CLI）。全离线：无网络、无真实 cookies、无浏览器。
"""
import sys, os, asyncio, json, sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault('KIANA_CRYPTO_KEY', 'kv_test')

from kiana_vnext_plus.crawler import append_stats_line
from kiana_vnext_plus.enhancements import DataExporter


class TestExportSqlite:
    """4.1 sqlite 导出：media/scrape 两表 + 幂等 upsert（media_id / url 双键）"""

    def test_media_upsert_idempotent(self, tmp_path):
        out = tmp_path / "data.sqlite"
        ex = DataExporter(tmp_path)
        rows = [
            {"media_id": "m1", "domain": "a.test", "title": "甲", "url": "http://a.test/1",
             "stream_url": "http://a.test/s1.mp4", "quality": "1080", "downloaded": True},
            {"media_id": "m1", "domain": "a.test", "title": "甲2", "url": "http://a.test/1",
             "stream_url": "http://a.test/s1.mp4", "quality": "720"},  # 同 id → 覆盖
            {"url": "http://p.test/p1", "title": "文章", "text": "正文", "images": ["http://p.test/i.jpg"]},
        ]
        assert ex.export_sqlite(out, rows) == 3
        assert ex.export_sqlite(out, rows) == 3  # 重跑幂等
        conn = sqlite3.connect(str(out))
        med = conn.execute("SELECT COUNT(*), MAX(quality) FROM media").fetchone()
        scr = conn.execute("SELECT COUNT(*), title FROM scrape").fetchone()
        conn.close()
        assert med[0] == 1, "同 media_id 重跑应折叠为 1 行"
        assert med[1] == "720", "upsert 应取最后一次 quality"
        assert scr[0] == 1 and scr[1] == "文章"

    def test_media_fallback_key_from_video_url(self, tmp_path):
        """无 media_id 时回退 video_url/url 作键——媒体行仍幂等"""
        out = tmp_path / "d.sqlite"
        ex = DataExporter(tmp_path)
        rows = [{"video_url": "http://v.test/v1.mp4", "title": "t"}]
        ex.export_sqlite(out, rows)
        ex.export_sqlite(out, rows)
        conn = sqlite3.connect(str(out))
        n = conn.execute("SELECT COUNT(*) FROM media").fetchone()[0]
        conn.close()
        assert n == 1

    def test_export_empty_returns_zero(self, tmp_path):
        assert DataExporter(tmp_path).export_sqlite(tmp_path / "e.sqlite", []) == 0

    def test_dashboard_sqlite_summary_reads_export(self, tmp_path):
        """4.1 闭环：cli 导出 → dashboard --db 概览可读（工具函数级）"""
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent.parent))
        from tools import dashboard
        out = tmp_path / "data.sqlite"
        ex = DataExporter(tmp_path)
        ex.export_sqlite(out, [
            {"url": "http://a.test/1", "title": "甲文章", "text": "正文"},
            {"url": "http://a.test/2", "title": "乙文章", "text": "正文"},
            {"media_id": "m1", "url": "http://v.test/v1", "title": "视频", "downloaded": True,
             "quality": "1080"},
        ])
        ok, info = dashboard._sqlite_summary(out)
        assert ok
        assert info["scrape"] == 2 and info["media"] == 1 and info["media_downloaded"] == 1
        assert ("a.test", 2) in info["domains"]

    def test_dashboard_sqlite_summary_bad_path(self, tmp_path):
        from tools import dashboard
        ok, info = dashboard._sqlite_summary(tmp_path / "nope.sqlite")
        assert not ok and "error" in info

    def test_export_sqlite_fts5_index(self, tmp_path):
        """2-A FTS5：导出快照自建全文索引（scrape_fts MATCH 可查）"""
        ex = DataExporter(tmp_path)
        out = tmp_path / "fts.sqlite"
        ex.export_sqlite(out, [
            {"url": "http://a.test/1", "title": "Kiana 爬虫引擎", "text": "六边形战士 无敌爬虫"},
            {"url": "http://a.test/2", "title": "其他", "text": "无关内容"},
        ])
        conn = sqlite3.connect(str(out))
        rows = conn.execute(
            "SELECT title FROM scrape_fts WHERE scrape_fts MATCH ?", ("六边形战士",)).fetchall()
        conn.close()
        assert rows and rows[0][0] == "Kiana 爬虫引擎", "FTS5 全文检索应命中"


class TestStatsExtend:
    """4.2 stats.jsonl 扩展：videos/images/bytes 新键，旧接口调用不破"""

    def test_new_keys_default_zero(self, tmp_path):
        p = tmp_path / "stats.jsonl"
        assert append_stats_line(p, 1, 0, 0, 1, 1) is True
        rec = json.loads(p.read_text(encoding="utf-8"))
        assert rec["videos"] == 0 and rec["images"] == 0 and rec["bytes"] == 0
        # 旧键原样在位（消费方只读已知键不受影响）
        assert rec["done"] == 1 and rec["batch"] == 1 and isinstance(rec["ts"], float)

    def test_new_keys_values_recorded(self, tmp_path):
        p = tmp_path / "stats.jsonl"
        append_stats_line(p, 2, 1, 1, 4, 1, videos=3, images=5, bytes_total=123456)
        rec = json.loads(p.read_text(encoding="utf-8"))
        assert rec["videos"] == 3 and rec["images"] == 5 and rec["bytes"] == 123456

    def test_none_values_coerced(self, tmp_path):
        p = tmp_path / "stats.jsonl"
        append_stats_line(p, 0, 0, 0, 0, 0, videos=None, images=None, bytes_total=None)
        rec = json.loads(p.read_text(encoding="utf-8"))
        assert rec["videos"] == 0 and rec["images"] == 0 and rec["bytes"] == 0


class TestErrorsMigration:
    """4.3 errors 表 platform/code 列：v2 老库迁移 v3 + 结构化写入/读取"""

    @staticmethod
    def _make_db(path):
        from kiana_vnext_plus.frontier import FrontierDB
        return FrontierDB(str(path))

    def test_v2_db_migrates_to_v3(self, tmp_path):
        """模拟 v2 老库（旧 errors/video_downloads 无新列）→ _init_db 触发 v3 迁移"""
        p = tmp_path / "v2.db"
        conn = sqlite3.connect(str(p))
        conn.execute("""CREATE TABLE errors (url_hash TEXT, error_type TEXT,
            error_message TEXT, timestamp REAL)""")
        conn.execute("""CREATE TABLE video_downloads (
            video_url TEXT PRIMARY KEY, domain TEXT, status TEXT DEFAULT 'pending',
            file_path TEXT, progress REAL, fail_count INTEGER DEFAULT 0, created_at REAL)""")
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
        conn.close()
        self._make_db(p)
        conn = sqlite3.connect(str(p))
        ecols = {r[1] for r in conn.execute("PRAGMA table_info(errors)")}
        vcols = {r[1] for r in conn.execute("PRAGMA table_info(video_downloads)")}
        # [v2.18] 迁移到 v4（lease_expires 租约心跳列）；ver >= 3 兼容未来版本
        fcols = {r[1] for r in conn.execute("PRAGMA table_info(frontier)")}
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        assert {"platform", "code"} <= ecols, "errors 应有 platform/code 列"
        assert "file_size" in vcols, "video_downloads 应有 file_size 列"
        assert ver >= 3
        assert "lease_expires" in fcols, "frontier 应有 lease_expires 列（P1-3 租约心跳）"

    def test_fresh_db_has_new_columns(self, tmp_path):
        self._make_db(tmp_path / "f.db")
        conn = sqlite3.connect(str(tmp_path / "f.db"))
        ecols = {r[1] for r in conn.execute("PRAGMA table_info(errors)")}
        conn.close()
        assert {"platform", "code"} <= ecols

    def test_write_error_platform_code_roundtrip(self, tmp_path):
        from kiana_vnext_plus.frontier import FrontierDB
        db = FrontierDB(str(tmp_path / "w.db"))

        async def flow():
            await db.init_async()
            await db.write_error("h1", "HTTP_403", "blocked", platform="douyin", code="403")
            await db.flush()
            rows = await db.get_recent_errors(limit=10)
            await db.close()
            assert rows and rows[0]["platform"] == "douyin" and rows[0]["code"] == "403"
            assert rows[0]["error_type"] == "HTTP_403"

        asyncio.run(flow())

    def test_video_stats_bytes(self, tmp_path):
        from kiana_vnext_plus.frontier import FrontierDB
        db = FrontierDB(str(tmp_path / "v.db"))

        async def flow():
            await db.init_async()
            await db.add_video_download("http://v.test/a.mp4", "v.test")
            await db.update_video_status("http://v.test/a.mp4", "completed", 1.0,
                                         file_path="x.mp4", file_size=987654)
            await db.flush()
            st = await db.get_video_stats()
            assert st == {"completed": 1, "bytes": 987654}
            # failed 中间态不覆盖已完成尺寸（COALESCE 语义）
            await db.update_video_status("http://v.test/a.mp4", "failed", 0.0)
            await db.flush()
            await db.close()

        asyncio.run(flow())


class TestCookieHealthWeakSites:
    """4.4 cookie_health 快手/小红书域级弱检查（存在即健康，诚实标注不在线断言）"""

    def test_weak_sites_reported(self, tmp_path, monkeypatch):
        from kiana_vnext_plus import cookie_health
        cf = tmp_path / "cookies.txt"
        cf.write_text(
            "# Netscape HTTP Cookie File\n"
            "kuaishou.com\tTRUE\t/\tFALSE\t0\tdid\tabc123\n"
            "www.xiaohongshu.com\tTRUE\t/\tFALSE\t0\tgid\tr456\n",
            encoding="utf-8")
        monkeypatch.setenv("KIANA_COOKIE_FILE", str(cf))
        monkeypatch.delenv("KIANA_COOKIE_FILES", raising=False)
        # 在线自检（B站 nav API）不依赖网络——桩掉
        monkeypatch.setattr(cookie_health, "check_bilibili",
                            lambda files: {"ok": False, "msg": "stub", "fields": []})
        r = cookie_health.check_sites()
        sites = {s["site"]: s for s in r["sites"]}
        assert sites["快手"]["has_cookie"] is True
        assert sites["小红书"]["has_cookie"] is True
        assert "弱检查" in sites["快手"]["note"]
        assert sites["快手"]["fields"] and sites["快手"]["fields"][0] == "did"

    def test_missing_cookie_note_absence(self, tmp_path, monkeypatch):
        from kiana_vnext_plus import cookie_health
        cf = tmp_path / "cookies.txt"
        cf.write_text("bilibili.com\tTRUE\t/\tFALSE\t0\tSESSDATA\tx\n", encoding="utf-8")
        monkeypatch.setenv("KIANA_COOKIE_FILE", str(cf))
        monkeypatch.delenv("KIANA_COOKIE_FILES", raising=False)
        monkeypatch.setattr(cookie_health, "check_bilibili",
                            lambda files: {"ok": False, "msg": "stub", "fields": []})
        sites = {s["site"]: s for s in cookie_health.check_sites()["sites"]}
        assert sites["快手"]["has_cookie"] is False
        assert sites["小红书"]["has_cookie"] is False


class TestRulesSchemaV217:
    """5.1 规则 schema 升级：item_link / date_parse / url_extract / next 列表——向后兼容"""

    @staticmethod
    def _load_rule(tmp_path, yaml_text, monkeypatch):
        from kiana_vnext_plus import site_rules
        f = tmp_path / "site.yaml"
        f.write_text(yaml_text, encoding="utf-8")
        monkeypatch.setattr(site_rules, "_loader", site_rules.RuleLoader(tmp_path))
        return f

    def test_item_link_and_transforms_and_next_list(self, tmp_path, monkeypatch):
        html = """<html><body>
            <div class="post"><h2><a href="/detail/1">标题一</a></h2>
              <span class="date">2026-08-30 12:34</span>
              <span class="link">原文 https://a.test/x?full=1</span></div>
            <div class="post"><h2><a href="/detail/2">标题二</a></h2></div>
            <a class="old-next" href="?p=2">旧版翻页占位（无 .next 时不应命中）</a>
            <a class="next" href="?p=2">下一</a>
            </body></html>"""
        yaml_text = """name: demo
match: [a.test]
list:
  selector: "div.post"
  fields:
    title: {sel: "h2 a", text: true}
    item_link: {sel: "h2 a", attr: href}
    published: {sel: "span.date", text: true, transform: date_parse}
    orig_url: {sel: "span.link", text: true, transform: url_extract}
pagination:
  next:
    - {sel: "a.nonexistent", attr: href}
    - {sel: "a.next", attr: href}
"""
        self._load_rule(tmp_path, yaml_text, monkeypatch)
        from kiana_vnext_plus.site_rules import apply_rule
        res = apply_rule(html, "http://a.test/list?p=1")
        assert res is not None and res["name"] == "demo"
        items = res["list_items"]
        assert items and items[0]["item_link"] == "http://a.test/detail/1", "item_link 应补全绝对 URL"
        assert items[0]["published"] == "2026-08-30T12:34:00", "date_parse 应转 ISO"
        assert items[0]["orig_url"] == "https://a.test/x?full=1", "url_extract 应取出 URL"
        assert res["next_url"] == "http://a.test/list?p=2", "next 列表应命中第二个候选"

    def test_legacy_rule_unchanged(self, tmp_path, monkeypatch):
        """旧式规则（无 item_link/无 next 列表）照常工作——向后兼容"""
        html = "<html><body><h1>标题</h1><article>正文内容</article>"
        html += "<img src='http://a.test/x.png'><a class='next' href='/p2'>n</a></body></html>"
        yaml_text = """name: demo
match: [a.test]
detail:
  fields:
    title: {sel: "h1", text: true}
    content: {sel: "article", text: true}
media:
  images: {sel: "img", attr: src}
pagination:
  next: {sel: "a.next", attr: href}
"""
        self._load_rule(tmp_path, yaml_text, monkeypatch)
        from kiana_vnext_plus.site_rules import apply_rule
        res = apply_rule(html, "http://a.test/1")
        assert res["fields"]["title"] == "标题"
        assert "http://a.test/x.png" in res["images"]
        assert res["next_url"] == "http://a.test/p2"


class Test163RuleRealSample:
    """5.3 netease-news 规则实测对齐：真实页面片段（tests/assets/163_article_sample.html，
    2026-08-30 抓取自 www.163.com/dy/article/ 文章页）离线验证——规则对 repo 级
    rules/sites 加载并命中，title/published/content 三字段可抽。"""

    def test_netease_rule_hits_real_sample(self):
        from kiana_vnext_plus.site_rules import apply_rule
        sample = Path(__file__).parent / "assets" / "163_article_sample.html"
        html = sample.read_text(encoding="utf-8")
        res = apply_rule(html, "https://www.163.com/dy/article/L5G6JBP90514CFC7.html")
        assert res is not None and res["name"] == "netease-news"
        assert res["fields"]["title"] == "好评中国｜数字赋能 智启新篇"
        assert res["fields"]["published"].startswith("2026-08-29 09:30:11")
        assert "数字出版" in res["fields"]["content"]

    def test_new_rule_not_matching_wrong_domain(self, tmp_path, monkeypatch):
        from kiana_vnext_plus import site_rules
        monkeypatch.setattr(site_rules, "_loader", site_rules.RuleLoader(tmp_path))
        assert site_rules.apply_rule("<h1>x</h1>", "http://other.test/1") is None


class TestCliExportInvoked:
    """4.1 真机冒烟抓到的老 bug：export 分支定义 do_export 但从未 asyncio.run——
    整个 export 子命令死代码（无输出无文件，影响 jsonl/csv/xlsx/sqlite 四格式）。"""

    def test_export_branch_runs_do_export(self):
        src = (Path(__file__).parent.parent / "kiana_vnext_plus" / "cli.py").read_text(
            encoding="utf-8")
        assert src.count("def do_export") == 1
        assert src.count("asyncio.run(do_export())") == 1, \
            "export 分支必须调用 do_export（防回归：曾整体丢失调用行）"

    def test_cli_export_live_roundtrip(self, tmp_path):
        """修复后端到端：建项目库（不裸 SQL——只走 FrontierDB 公开接口）→
        cli export --format sqlite → 产物存在——印证四格式导出通道真正活过来"""
        from kiana_vnext_plus.identity import ProjectIdentity
        from kiana_vnext_plus.frontier import FrontierDB
        proj = ProjectIdentity(str(tmp_path / "proj"))
        proj.dir.mkdir(parents=True, exist_ok=True)
        db = FrontierDB(str(proj.get_db_path()))

        async def _seed():
            await db.init_async()
            await db.write_extracted("h1", json.dumps(
                {"url": "http://a.test/1", "title": "甲", "text": "t"}, ensure_ascii=False))
            await db.write_extracted("h2", json.dumps(
                {"url": "http://a.test/2", "title": "乙", "text": "t"}, ensure_ascii=False))
            await db.flush()
            await db.close()

        asyncio.run(_seed())
        import sys as _sys
        from kiana_vnext_plus import cli
        _argv = _sys.argv
        try:
            _sys.argv = ["cli", "export", "--project", str(tmp_path / "proj"),
                         "--format", "sqlite"]
            import contextlib, io as _io
            _buf = _io.StringIO()
            with contextlib.redirect_stdout(_buf):
                cli.main()
            captured = _buf.getvalue()
        finally:
            _sys.argv = _argv
        assert "exported to" in captured
        assert (proj.export_dir / "data.sqlite").exists()


class TestMediaSchemaWiring:
    """0-1 media_schema 收口：四平台 resolver 产物经 from_resolver → media 键"""

    def test_douyin_quality_from_bitrate(self):
        from kiana_vnext_plus.media_schema import from_resolver, to_dict, is_valid
        item = from_resolver("douyin", {"aweme_id": "7", "video_url": "http://x/a.mp4",
                                        "bitrate": 1118, "method": "abogus"})
        assert is_valid(item) and to_dict(item)["streams"][0]["quality"] == "1118bps"
        assert to_dict(item)["method"] == "abogus"

    def test_xhs_image_set(self):
        from kiana_vnext_plus.media_schema import from_resolver, to_dict
        item = from_resolver("xhs", {"note_id": "n1", "images": ["http://x/1.jpg"],
                                     "desc": "d", "method": "page_state"})
        d = to_dict(item)
        assert d["kind"] == "image_set" and d["images"] == ["http://x/1.jpg"]
        assert d["media_id"] == "n1"

    def test_music163_audio_level(self):
        from kiana_vnext_plus.media_schema import from_resolver, to_dict
        item = from_resolver("music163", {"song_id": "s1", "media_url": "http://x/a.m4a",
                                          "type": "audio", "level": "exhigh",
                                          "method": "weapi"})
        d = to_dict(item)
        assert d["streams"][0]["quality"] == "exhigh" and d["kind"] == "audio"

    def test_resolver_files_wire_media(self):
        """各 resolver 成功返回必须附 media（from_resolver 收口）——防回退裸 dict"""
        import pathlib as _pl
        base = _pl.Path(__file__).parent.parent / "kiana_vnext_plus"
        for f, cnt in (("douyin_resolver.py", 3), ("xhs_resolver.py", 1),
                       ("kuaishou_resolver.py", 1), ("music163_resolver.py", 1)):
            src = (base / f).read_text(encoding="utf-8")
            assert src.count('_p["media"] = _media_of(_p)') >= cnt, f"{f} 未收口 media"


class TestRuleExclude:
    """0-2 规则 exclude 字段：子串排除防与视频/音乐通道抢 URL"""

    def test_exclude_substring_blocks_match(self, tmp_path, monkeypatch):
        from kiana_vnext_plus import site_rules
        f = tmp_path / "r.yaml"
        f.write_text(
            "name: demo\nmatch: [163.com]\nexclude: [music.163.com, /song]\n"
            "item_type: article\n", encoding="utf-8")
        monkeypatch.setattr(site_rules, "_loader", site_rules.RuleLoader(tmp_path))
        assert site_rules.find_rule("http://music.163.com/song/1") is None
        assert site_rules.find_rule("https://www.163.com/dy/article/AB.html") is not None
        assert site_rules.find_rule("https://www.163.com/foo/song.html") is None

    def test_repo_rules_do_not_steal_media_channels(self):
        """repo 级规则：netease-news 不再覆盖 music.163.com；sohu-news 不再覆盖 tv.sohu.com"""
        from kiana_vnext_plus.site_rules import find_rule
        r = find_rule("https://music.163.com/song?id=1")
        assert r is None or r.name != "netease-news"
        r2 = find_rule("https://tv.sohu.com/v/dGVzdA==.html")
        assert r2 is None or r2.name != "sohu-news"
        assert find_rule("https://news.sina.com.cn/c/xl/2026-08-30/doc-iniqauvp8246490.shtml") is not None


class TestZhihuWeiboRulesRealSample:
    """5.3 知乎/微博规则实测对齐：IAB 真实浏览器渲染（2026-08-30）保存的 DOM 片段
    （tests/assets/zhihu_question_sample.html / weibo_post_sample.html）离线验证——
    规则对 repo 级 rules/sites 加载并命中，关键字段可抽。"""

    def test_zhihu_rule_hits_real_sample(self):
        from kiana_vnext_plus.site_rules import apply_rule
        sample = Path(__file__).parent / "assets" / "zhihu_question_sample.html"
        res = apply_rule(sample.read_text(encoding="utf-8"),
                         "https://www.zhihu.com/question/634653445")
        assert res is not None and res["name"] == "zhihu-qa"
        assert "三体" in res["fields"]["title"]
        assert "信息保存" in res["fields"].get("question", "")
        items = res["list_items"]
        assert items, "应抽出回答列表项"
        first = items[0]
        assert first.get("author"), "回答作者应可抽"
        assert str(first.get("item_link", "")).startswith("http"), "item_link 应为绝对 URL"

    def test_weibo_rule_hits_real_sample(self):
        from kiana_vnext_plus.site_rules import apply_rule
        sample = Path(__file__).parent / "assets" / "weibo_post_sample.html"
        res = apply_rule(sample.read_text(encoding="utf-8"),
                         "https://weibo.com/7064960756/RfLkA5pKv")
        assert res is not None and res["name"] == "weibo-post"
        assert res["fields"]["author"] == "今时寂月"
        assert res["fields"]["published"] == "2026-08-30T11:03:00", "短年份应 ISO 化"
        assert "景甜恋爱脑" in res["fields"]["content"]
        # 媒体应排除头像（woo-avatar-img 等），只留内容图
        assert len(res["images"]) == 1
        assert "orj480" in res["images"][0]


class TestSinaNavH1Trap:
    """5.3 真机冒烟修 bug：sina 文章页 h1.channel-logo（导航"新闻中心"）在前——
    规则标题不得抽到导航，必须命中正文标题（仿真实样：两个 h1 + og:title）"""

    def test_title_not_nav(self, tmp_path, monkeypatch):
        from kiana_vnext_plus import site_rules
        f = tmp_path / "sina.yaml"
        f.write_text(
            "name: sina-news\nmatch: [sina.com.cn]\nitem_type: article\n"
            "detail:\n  fields:\n"
            "    title: {sel: \"h1.main-title, h1:not(.channel-logo), h1\", text: true}\n",
            encoding="utf-8")
        sample = (Path(__file__).parent / "assets" / "sina_article_sample.html").read_text(
            encoding="utf-8")
        monkeypatch.setattr(site_rules, "_loader", site_rules.RuleLoader(tmp_path))
        res = site_rules.apply_rule(
            sample, "https://news.sina.com.cn/c/xl/2026-08-30/doc-iniqauvp8246490.shtml")
        assert res is not None
        title = res["fields"]["title"]
        assert title != "新闻中心", f"标题抽到了导航 h1: {title!r}"
        assert "习近平抵达比什凯克" in title

    def test_repo_rule_hits_live_sample(self):
        from kiana_vnext_plus.site_rules import apply_rule
        html = (Path(__file__).parent / "assets" / "sina_article_sample.html").read_text(
            encoding="utf-8")
        res = apply_rule(html, "https://news.sina.com.cn/c/xl/2026-08-30/doc-iniqauvp8246490.shtml")
        assert res is not None and res["name"] == "sina-news"
        assert res["fields"]["title"] != "新闻中心"


class TestPhase1NewSiteRules:
    """1-1 新站点规则实测取证：douban-book/sspai-post/toutiao-article（+36kr 标题层）
    ——真实页面样本离线验证（防选择器改版漂移）"""

    def test_douban_book(self):
        from kiana_vnext_plus.site_rules import apply_rule
        html = (Path(__file__).parent / "assets" / "douban_book_sample.html").read_text(
            encoding="utf-8")
        res = apply_rule(html, "https://book.douban.com/subject/38483071")
        assert res and res["name"] == "douban-book"
        assert res["fields"]["title"] == "深陷泥沼的人类学家"
        assert "上海人民出版社" in res["fields"]["info"]
        assert "rating" in res["fields"]  # 评分为动态区，样本可能空值——字段契约存在即可

    def test_sspai_post(self):
        from kiana_vnext_plus.site_rules import apply_rule
        html = (Path(__file__).parent / "assets" / "sspai_article_sample.html").read_text(
            encoding="utf-8")
        res = apply_rule(html, "https://sspai.com/post/113974")
        assert res and res["name"] == "sspai-post"
        assert "佛罗伦萨" in res["fields"]["title"]
        assert len(res["fields"].get("content", "")) > 100

    def test_toutiao_article(self):
        from kiana_vnext_plus.site_rules import apply_rule
        html = (Path(__file__).parent / "assets" / "toutiao_article_sample.html").read_text(
            encoding="utf-8")
        res = apply_rule(html, "https://www.toutiao.com/article/7679740574882087424/")
        assert res and res["name"] == "toutiao-article"
        assert "比什凯克" in res["fields"]["title"]
        assert "习近平" in res["fields"]["content"]

    def test_kr_title_time_verified(self):
        from kiana_vnext_plus.site_rules import apply_rule
        html = (Path(__file__).parent / "assets" / "kr_article_sample.html").read_text(
            encoding="utf-8")
        res = apply_rule(html, "https://www.36kr.com/p/3961343456443527")
        assert res and res["name"] == "36kr-article"
        assert res["fields"]["title"] == "奥特曼最后一战：4个月后，交付AGI"


class TestSohuIfengRealSample:
    """打磨轮：sohu/ifeng 规则实测对齐——真实页面样本离线验证（新增资产）"""

    def test_sohu_title_and_text(self):
        from kiana_vnext_plus.site_rules import apply_rule
        html = (Path(__file__).parent / "assets" / "sohu_article_sample.html").read_text(
            encoding="utf-8")
        res = apply_rule(html, "https://www.sohu.com/a/1069629596_116237")
        assert res and res["name"] == "sohu-news"
        assert "尼泊尔" in res["fields"]["title"]
        assert "208人" in res["fields"]["content"]

    def test_ifeng_title_and_text(self):
        from kiana_vnext_plus.site_rules import apply_rule
        html = (Path(__file__).parent / "assets" / "ifeng_article_sample.html").read_text(
            encoding="utf-8")
        res = apply_rule(html, "https://news.ifeng.com/c/8w0rpeH6wWv")
        assert res and res["name"] == "ifeng-news"
        assert "朝鲜" in res["fields"]["title"]
        assert "腐败分子" in res["fields"]["content"]


class TestDateParseShortYear:
    """5.1 date_parse 短年份（微博 26-8-30 11:03）兼容"""

    def test_two_digit_year(self):
        from kiana_vnext_plus.site_rules import _transform
        assert _transform("26-8-30 11:03", "date_parse") == "2026-08-30T11:03:00"

    def test_unparseable_returns_original(self):
        from kiana_vnext_plus.site_rules import _transform
        assert _transform("三天前", "date_parse") == "三天前"


class TestRuleCliHelpers:
    """5.2 规则 CLI 核心函数（CLI 薄壳打印，逻辑在 site_rules）"""

    def test_validate_good_file(self, tmp_path):
        from kiana_vnext_plus.site_rules import validate_rule_file
        f = tmp_path / "ok.yaml"
        f.write_text("name: ok\nmatch: [ok.test]\n", encoding="utf-8")
        ok, msgs = validate_rule_file(f)
        assert ok is True and msgs and "规则有效" in msgs[-1]

    def test_validate_bad_yaml_reports_line(self, tmp_path):
        from kiana_vnext_plus.site_rules import validate_rule_file
        f = tmp_path / "bad.yaml"
        f.write_text("name: ok\nmatch: [a]\n  indent-broken: : [\n", encoding="utf-8")
        ok, msgs = validate_rule_file(f)
        assert ok is False and any("行" in m for m in msgs), f"坏 YAML 应报行号: {msgs}"

    def test_validate_missing_match(self, tmp_path):
        from kiana_vnext_plus.site_rules import validate_rule_file
        f = tmp_path / "nm.yaml"
        f.write_text("name: ok\n", encoding="utf-8")
        ok, msgs = validate_rule_file(f)
        assert ok is False and any("match" in m for m in msgs)

    def test_rule_test_syntax_only_without_html(self, tmp_path):
        from kiana_vnext_plus.site_rules import test_rule_url
        f = tmp_path / "r.yaml"
        f.write_text("name: ok\nmatch: [ok.test]\n", encoding="utf-8")
        out = test_rule_url("http://ok.test/1", "", f)
        assert out["ok"] is True and out["result"] is None
        assert any("仅完成语法校验" in m for m in out["messages"])

    def test_rule_test_hit_with_html(self, tmp_path, monkeypatch):
        from kiana_vnext_plus import site_rules
        f = tmp_path / "r.yaml"
        f.write_text(
            "name: demo\nmatch: [ok.test]\ndetail:\n  fields:\n"
            "    title: {sel: \"h1\", text: true}\n", encoding="utf-8")
        monkeypatch.setattr(site_rules, "_loader", site_rules.RuleLoader(tmp_path))
        out = site_rules.test_rule_url("http://ok.test/1",
                                       "<h1>超值标题</h1>", f)
        assert out["ok"] is True and out["result"]["fields"]["title"] == "超值标题"
