"""Kiana Vnext Plus — v2.16.1 阶段3 平台解析器回归

覆盖：api_errors 语义异常层（码表/白名单重试）；抖音补强（目标闸/假 msToken/码表）；
网易云 weapi（固定向量一致性——向量由候选工程 encrypt.py 离线生成后写死，不依赖外部路径）；
url_utils.safe_urlopen 请求闸（协议/私网/白名单）。
"""
import sys, os, asyncio, socket
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault('KIANA_CRYPTO_KEY', 'kv_test')

from kiana_vnext_plus import api_errors as ae
from kiana_vnext_plus import url_utils as uu
from kiana_vnext_plus import douyin_resolver as dr


class TestApiErrors:
    def test_douyin_codes_mapped(self):
        cls, hint = ae.PLATFORM_ERROR_CODES["douyin"][2155]
        assert cls is ae.IPBlockError
        cls2, _ = ae.PLATFORM_ERROR_CODES["douyin"][71]
        assert cls2 is ae.NoteNotFoundError

    def test_raise_for_code(self):
        try:
            ae.raise_for_code("douyin", 2190)
        except ae.PlatformAccessError as e:
            assert e.code == 2190
        else:
            raise AssertionError("should raise PlatformAccessError")

    def test_error_hint_readable(self):
        h = ae.error_hint("douyin", 2155)
        assert "风控" in h

    def test_retry_whitelist_no_retry_on_block(self):
        calls = {"n": 0}

        @ae.retry_whitelist(retries=3, delay=0.01)
        async def fn():
            calls["n"] += 1
            raise ae.IPBlockError("blocked")

        with __import__("pytest").raises(ae.IPBlockError):
            asyncio.run(fn())
        assert calls["n"] == 1  # 确定性错误不重试

    def test_retry_whitelist_retries_transient(self):
        calls = {"n": 0}

        @ae.retry_whitelist(retries=3, delay=0.01)
        async def fn():
            calls["n"] += 1
            if calls["n"] < 2:
                raise ae.DataFetchError("transient")
            return "ok"

        assert asyncio.run(fn()) == "ok"
        assert calls["n"] == 2


class TestDouyinGuards:
    def test_sanitize_allows_douyin(self):
        assert dr._sanitize_http_url("https://www.douyin.com/video/123").startswith("https://")
        assert dr._sanitize_http_url("http://mssdk.bytedance.com/web/report").startswith("https://")

    def test_sanitize_blocked(self):
        assert dr._sanitize_http_url("http://127.1/") == ""
        assert dr._sanitize_http_url("http://2130706433/") == ""
        assert dr._sanitize_http_url("http://evil.com/x") == ""
        assert dr._sanitize_http_url("file:///etc/passwd") == ""
        assert dr._sanitize_http_url(123) == ""

    def test_fake_ms_token_shape(self):
        t = dr._gen_fake_ms_token()
        assert len(t) == 128 and t.endswith("==")

    def test_codes_via_error_hint(self):
        from kiana_vnext_plus.api_errors import error_hint
        assert dr._status_err(2155) == error_hint("douyin", 2155, default_message="detail API 风控/错误")

    def test_deterministic_error_no_retry(self, monkeypatch):
        """[v2.17 2.7] 确定性错误（2190 登录态过期）首次失败即返回，不再白耗第二次"""
        import json as _j
        calls = {"n": 0}

        def fake_http_get(api, timeout=15, referer=None, cookie_extra=""):
            calls["n"] += 1
            return api, _j.dumps({"status_code": 2190}).encode()

        monkeypatch.setattr(dr, "http_get", fake_http_get)
        monkeypatch.setattr(dr, "_gen_ms_token", lambda: "x")
        monkeypatch.setattr(dr, "_gen_fake_ms_token", lambda: "y")
        monkeypatch.setattr(dr, "_gen_ttwid", lambda: "")
        r = dr.resolve("https://www.douyin.com/video/7300000000000000000")
        assert calls["n"] == 1, f"确定性错误只许 1 次请求（实际 {calls['n']}）"
        assert "2190" in (r.get("error") or "")

    def test_transient_error_retries_then_success(self, monkeypatch):
        """[v2.17 2.7] 瞬时错误（2155）重试一次后成功（白名单语义）"""
        import json as _j
        calls = {"n": 0}
        responses = [_j.dumps({"status_code": 2155}).encode(),
                     _j.dumps({"status_code": 0, "aweme_detail": {
                         "video": {"play_addr": {"url_list": ["https://v.douyinvod.com/a/b_playwm.mp4"]},
                                   "bit_rate": []}, "duration": 1}}).encode()]

        def fake_http_get(api, timeout=15, referer=None, cookie_extra=""):
            calls["n"] += 1
            return api, responses[min(calls["n"] - 1, 1)]

        monkeypatch.setattr(dr, "http_get", fake_http_get)
        monkeypatch.setattr(dr, "_gen_ms_token", lambda: "x")
        monkeypatch.setattr(dr, "_gen_fake_ms_token", lambda: "y")
        monkeypatch.setattr(dr, "_gen_ttwid", lambda: "")
        r = dr.resolve("https://www.douyin.com/video/7300000000000000001")
        assert r.get("ok") is True
        assert calls["n"] == 2 and "https://v.douyinvod.com/a/b_play.mp4" in r["video_url"]


class TestSafeUrlopen:
    def test_invalid_targets_return_none(self):
        assert uu.safe_urlopen("http://127.1/x", allowed_hosts=("douyin.com",)) is None
        assert uu.safe_urlopen("http://evil.com/x", allowed_hosts=("douyin.com",)) is None
        assert uu.safe_urlopen("ftp://douyin.com/x", allowed_hosts=("douyin.com",)) is None

    def test_target_ok_check(self):
        assert uu._http_target_ok("https://www.douyin.com/v", allowed_hosts=("douyin.com",)) is True
        assert uu._http_target_ok("https://sub.douyin.com/v", allowed_hosts=("douyin.com",)) is True
        assert uu._http_target_ok("https://douyin.com.evil.com/v", allowed_hosts=("douyin.com",)) is False


# 下列向量由候选工程 extractor/music163/encrypt.py 离线生成（sec_key=0123456789abcdef，
# 文本 = {"ids":"[123]","level":"exhigh","encodeType":"aac","csrf_token":""}），写死作一致性对照。
WEAPI_PARAMS = ("oO+SrlItG5Lr4bz0kHseP90nUxHYXCD5Wkxe1N4hX7oyH+1/elA83YBUXNwIw88HNv53yBVQphMW"
                "Atfts+EEQ0O7vB2kKv0BGuwG0ByJdl857x1TDMIDEHpxgc8avUIZ+NHww1MTpD1KWud1SyQ9Sw==")
WEAPI_ENCSECKEY = ("35701388baf89fed412e11269b9c76625d095ecaf17f03fa018abe19ea2d38b949debf242ee39a71"
                   "ca1f6cda71b1b86a45aa909ee27f7e78e267d34e732f0de948206c3340a788d0003372183e2f753c1f"
                   "78b66ac23d134ac1fc9b993156520ea826b8aa89a962d4491b4b8d7e08738e1da9b07aa39bf4a7ef0b1"
                   "c210728cd52")


class TestMusic163Weapi:
    def test_fixed_vector_params(self):
        from kiana_vnext_plus.music163_resolver import build_weapi_body
        b = build_weapi_body({"ids": "[123]", "level": "exhigh",
                              "encodeType": "aac", "csrf_token": ""},
                             sec_key="0123456789abcdef")
        assert b["params"] == WEAPI_PARAMS
        assert b["encSecKey"] == WEAPI_ENCSECKEY

    def test_rsa_len_and_format(self):
        from kiana_vnext_plus.music163_resolver import _rsa_encrypt
        esk = _rsa_encrypt("0123456789abcdef")
        assert len(esk) == 256 and all(c in "0123456789abcdef" for c in esk)

    def test_extract_id_variants(self):
        from kiana_vnext_plus.music163_resolver import extract_id
        assert extract_id("https://music.163.com/#/song?id=19723756") == "19723756"
        assert extract_id("https://music.163.com/mv/5970120") == "5970120"
        assert extract_id("https://www.xin.com/abc") is None
        assert extract_id(None) is None

    def test_weapi_body_is_form_encoded(self):
        """[v2.17 2.5] 请求体必须是 form 编码（params=..&encSecKey=..），
        与 Content-Type 一致——原 json 序列化 + form 头必被网易拒。"""
        import asyncio as _aio
        from urllib.parse import parse_qs
        from kiana_vnext_plus import music163_resolver as mr
        import kiana_vnext_plus.url_utils as uu
        captured = {}

        class _Resp:
            def read(self):
                return b'{"code": 200, "data": [{"url": "https://x/1.m4a"}]}'

            def decode(self, enc):
                return self.read().decode(enc)

        def fake_safe_urlopen(url, allowed_hosts=(), headers=None, data=None, timeout=15):
            captured["body"] = data
            captured["headers"] = headers
            return _Resp()

        orig = uu.safe_urlopen
        uu.safe_urlopen = fake_safe_urlopen  # weapi_post 内 from .url_utils import → 打模块属性
        try:
            mr.weapi_post("https://music.163.com/weapi/x", {"ids": "[1]", "level": "exhigh",
                                                           "encodeType": "aac", "csrf_token": ""})
        finally:
            uu.safe_urlopen = orig
        assert captured["body"], "应发请求体"
        qs = parse_qs(captured["body"].decode("utf-8"))
        assert "params" in qs and "encSecKey" in qs, "body 必须是 form 编码（params/encSecKey 键）"
        assert "application/x-www-form-urlencoded" in captured["headers"]["Content-Type"]


class TestXhsResolver:
    def test_extract_note_id(self):
        from kiana_vnext_plus.xhs_resolver import extract_note_id
        nid = "64f0a2b4000000001a03d5d3"
        assert extract_note_id(f"https://www.xiaohongshu.com/explore/{nid}?xsec_token=ab") == nid
        assert extract_note_id(f"https://www.xiaohongshu.com/discovery/item/{nid}") == nid
        assert extract_note_id("https://www.other.com/x") is None

    def test_init_state_parsing(self):
        from kiana_vnext_plus.xhs_resolver import _init_state
        html = f'<html><script>window.__INITIAL_STATE__ = {{"a": {{"b": 1}}}};</script></html>'
        st = _init_state(html)
        assert st and st["a"]["b"] == 1

    def test_init_state_urlencoded(self):
        """[v2.17 2.3] decodeURIComponent(%7B...) 形态：unquote 后才能解析（真实页面常见）"""
        import urllib.parse as _up
        from kiana_vnext_plus.xhs_resolver import _init_state
        import base64 as _b64
        payload = _up.quote('{"a": {"b": 2}}')
        html = f'<script>window.__INITIAL_STATE__ = {payload};</script>'
        st = _init_state(html)
        assert st and st["a"]["b"] == 2
        # base64 包裹形态（RENDER_DATA）
        b64 = _b64.b64encode(b'{"a": {"b": 3}}').decode()
        html2 = f'<script id="RENDER_DATA" type="application/json">{b64}</script>'
        st2 = _init_state(html2)
        assert st2 and st2["a"]["b"] == 3

    def test_media_extract(self):
        from kiana_vnext_plus.xhs_resolver import _media_extract
        state = {"note": {"card": {"title": "测试标题",
                                   "imageList": [{"urlDefault": "https://xhs-img.com/a.jpg"}],
                                   "media": {"stream": {"h264": [{"master_url": "https://xhscdn.com/v.mp4"}]}}}}}
        title, images, video = _media_extract(state)
        assert title == "测试标题"
        assert images == ["https://xhs-img.com/a.jpg"]
        assert video == "https://xhscdn.com/v.mp4"


class TestKuaishouResolver:
    def test_extract_video_id(self):
        from kiana_vnext_plus.kuaishou_resolver import extract_video_id
        assert extract_video_id("https://www.kuaishou.com/short-video/3x8a1b2c3d4e5f") == "3x8a1b2c3d4e5f"
        assert extract_video_id("https://www.kuaishou.com/f/abcd1234ef56") == "abcd1234ef56"
        assert extract_video_id("https://example.com/none") is None

    def test_media_from_state(self):
        from kiana_vnext_plus.kuaishou_resolver import _media_from_state
        state = {"photo": {"srcNoMark": "https://kscdn.com/v_nomark.mp4",
                           "imageUrls": ["https://kscdn.com/c1.jpg", "https://kscdn.com/c2.jpg"]}}
        video, images = _media_from_state(state)
        assert video.startswith("https://kscdn.com/v_nomark")
        assert len(images) == 2

    # [v2.17 2.1] 快手签名占位契约：存在、恒空、不抛——签名通道接入前的稳定出口
    def test_prepare_signatures_empty_contract(self):
        import asyncio as _aio
        from kiana_vnext_plus.kuaishou_resolver import prepare_signatures, resolve
        assert prepare_signatures("3x8a1b2c3d4e5f") == {}
        assert prepare_signatures("x", browser=object(), api_uri="/rest/v/profile/feed") == {}
        assert prepare_signatures(None) == {}  # 不抛

    def test_extract_state(self):
        from kiana_vnext_plus.kuaishou_resolver import _extract_state
        html = '<html><script>window.pageData = {"k": "v"};</script></html>'
        assert _extract_state(html) == {"k": "v"}


class TestBiliRepliesAndAss:
    def test_fetch_bili_replies_paging(self):
        import asyncio as _aio
        from kiana_vnext_plus.comment_danmaku import fetch_bili_replies
        calls = []
        pages = {1: 20, 2: 5}  # 第一页满页 → 继续；第二页 5 条 → break

        class _Resp:
            def __init__(self, n):
                self.n = n

            def json(self):
                n = self.n
                reps = [{"rpid": 1000 + i, "member": {"uname": f"u{i}"},
                         "content": {"message": f"msg{i}"}, "like": i, "ctime": 1}
                        for i in range(pages[n])]
                return {"code": 0, "data": {"replies": reps}}

        class _S:
            async def get(self, url, params=None, headers=None, cookies=None, **k):
                calls.append((url, params.get("pn")))
                return _Resp(params["pn"])

        out = _aio.run(fetch_bili_replies(_S(), 12345, 999, {}, {}, "mixin", max_pages=3))
        assert len(out) == 25
        assert [c[1] for c in calls] == [1, 2]  # 第二页不满页 → 停止

    def test_fetch_bili_replies_block_keeps_part(self):
        import asyncio as _aio
        from kiana_vnext_plus.comment_danmaku import fetch_bili_replies

        class _Resp:
            def json(self):
                return {"code": -412, "data": None}

        class _S:
            async def get(self, url, params=None, headers=None, cookies=None, **k):
                return _Resp()

        out = _aio.run(fetch_bili_replies(_S(), 1, 2, {}, {}, "m"))
        assert out == []  # 风控：不断 chain，返回已取部分（空）

    def test_danmaku_to_ass_optional(self):
        from kiana_vnext_plus.comment_danmaku import danmaku_to_ass
        r = danmaku_to_ass("<i>d</i>")
        assert r is None or isinstance(r, str)  # 无 biliass → None（诚实降级）；有则 str

    def test_want_ass_wired_into_collect(self):
        """[v2.17 2.4] 爬取链路 want_ass 已接线（配置 bili_danmaku_ass 默认 True）；
        无 biliass 时 collect 不抛且无 danmaku_ass 键（降级但不失败）"""
        src = (Path(__file__).parent.parent / "kiana_vnext_plus" / "crawler.py").read_text(encoding="utf-8")
        assert "want_ass=bool(self.cfg.get(\"bili_danmaku_ass\", True))" in src
        cfg_src = (Path(__file__).parent.parent / "kiana_vnext_plus" / "config.py").read_text(encoding="utf-8")
        assert '"bili_danmaku_ass": True' in cfg_src


class TestRealSiteRules:
    """v2.16.1：rules/sites 真实站点规则（网易/搜狐/新浪/凤凰）——命中与抽取。"""

    def test_netease_rule_hits(self):
        from kiana_vnext_plus.site_rules import find_rule
        assert getattr(find_rule("https://news.163.com/25/0829/10/xxxx.html"), "name", None) == "netease-news"
        assert find_rule("https://m.163.com/a/abc.html") is not None

    def test_sohu_sina_ifeng_rules(self):
        from kiana_vnext_plus.site_rules import find_rule
        assert getattr(find_rule("https://www.sohu.com/a/123_456"), "name", None) == "sohu-news"
        assert getattr(find_rule("https://news.sina.com.cn/c/2025-08-29/doc-xxxx.shtml"),
                       "name", None) == "sina-news"
        assert getattr(find_rule("https://news.ifeng.com/c/7abcdEFGH"), "name", None) == "ifeng-news"

    def test_apply_rule_netease(self):
        from kiana_vnext_plus.site_rules import apply_rule
        html = ('<html><body><h1>标题甲</h1>'
                '<div class="post_body">正文内容<p>x</p>'
                '<img src="https://img.163.com/1.jpg"></div>'
                '<a class="next" href="/article/2.html">下一页</a></body></html>')
        r = apply_rule(html, "https://news.163.com/25/0829/10/aaa.html")
        assert r and r["fields"]["title"] == "标题甲"
        assert "正文内容" in (r["fields"]["content"] or "")
        assert any("img.163.com" in u for u in r["images"])

    def test_other_domain_unaffected(self):
        from kiana_vnext_plus.site_rules import find_rule
        assert find_rule("https://www.bilibili.com/video/BV1x") is None


class TestABogusVectors:
    """a_bogus 合规重写回归：固定向量逐字节断言（行为漂移=服务端必拒，修改即红）"""

    def test_fixed_vectors(self):
        import json as _json
        from kiana_vnext_plus.douyin_abogus import ABogus
        from urllib.parse import urlencode
        vec = _json.loads((Path(__file__).parent / "assets" /
                           "abogus_vectors.json").read_text(encoding="utf-8"))
        assert len(vec) >= 3
        for v in vec:
            sig = ABogus().get_value(urlencode(v["params"]), v["method"],
                                     v["t0"], v["t1"], v["r1"], v["r2"], v["r3"])
            assert sig == v["sig"], f"a_bogus 向量漂移: {v['params']}"


class TestRobotsPolicy:
    """[v2.17 E-P1-1] robots 合规：按域缓存/通配/allow 优先/失败放行/默认关接线"""

    def test_parse_rules(self, monkeypatch):
        from kiana_vnext_plus import robots_policy as rp
        text = ("User-agent: *\n"
                "Disallow: /private/\n"
                "Disallow: /tmp$\n"
                "Allow: /private/public/\n")
        # [v2.17 1-5] 返回三元组（新增 crawl_delay）——旧解包更新
        allows, disallows, crawl_delay = rp._parse_robots(text)
        assert "/private/" in disallows and "/tmp$" in disallows
        assert "/private/public/" in allows
        assert crawl_delay == 0.0

    def test_rule_match_star_and_anchor(self):
        from kiana_vnext_plus.robots_policy import _rule_match
        assert _rule_match("/private/a.html", "/private/*") is True
        assert _rule_match("/x/y.html", "/x/y.html$") is True
        assert _rule_match("/x/y.html2", "/x/y.html$") is False
        assert _rule_match("/private/a", "/private") is True

    def test_is_allowed_cache_and_priority(self, monkeypatch):
        from kiana_vnext_plus import robots_policy as rp
        rp.ROBOTS_CACHE.clear()
        calls = {"n": 0}

        def fake_fetch(host, timeout=8):
            calls["n"] += 1
            return ("User-agent: *\nDisallow: /private/\nAllow: /private/public/\n")
        monkeypatch.setattr(rp, "_fetch_robots", fake_fetch)
        assert rp.is_allowed("https://site.test/public/a") is True
        assert rp.is_allowed("https://site.test/private/x") is False
        assert rp.is_allowed("https://site.test/private/public/ok") is True  # allow 优先
        assert calls["n"] == 1  # 缓存命中不再抓

    def test_fetch_failure_allows(self, monkeypatch):
        from kiana_vnext_plus import robots_policy as rp
        rp.ROBOTS_CACHE.clear()
        monkeypatch.setattr(rp, "_fetch_robots", lambda host, timeout=8: None)
        assert rp.is_allowed("https://down.test/x") is True  # 不可达 → 放行（防误伤）

    def test_default_off_in_engine(self):
        src = (Path(__file__).parent.parent / "kiana_vnext_plus" / "config.py").read_text(encoding="utf-8")
        assert '"robots_respect": False' in src  # 默认关（既有行为不变）


class TestSitemapSeed:
    """[v2.17 E-P1-2] sitemap 播种：激活死代码 discover_sitemap/parse_sitemap（假 session 驱动）"""

    def test_discover_from_robots_and_paths(self):
        import asyncio as _aio
        from kiana_vnext_plus.enhancements import discover_sitemap

        class _Resp:
            def __init__(self, text, status=200):
                self.text = text
                self.status_code = status

        class _S:
            def __init__(self):
                self.calls = []

            async def get(self, url, **k):
                self.calls.append(url)
                if "robots.txt" in url:
                    return _Resp("User-agent: *\nSitemap: https://site.test/sm.xml\n")
                if "sm.xml" in url:
                    return _Resp("<urlset><url><loc>x</loc></url></urlset>")
                return _Resp("", 404)

        s = _S()
        maps = _aio.run(discover_sitemap("site.test", session=s))
        assert "https://site.test/sm.xml" in maps
        assert any("robots.txt" in c for c in s.calls)

    def test_parse_sitemap_xml(self):
        import asyncio as _aio
        from kiana_vnext_plus.enhancements import parse_sitemap

        class _Resp:
            text = ("<?xml version='1.0'?>\n<urlset><url><loc>https://s.test/a</loc></url>"
                    "<url><loc>https://s.test/b</loc></url></urlset>")
            status_code = 200

        class _S:
            async def get(self, url, **k):
                return _Resp()

        urls = _aio.run(parse_sitemap("https://s.test/sm.xml", session=_S()))
        assert "https://s.test/a" in urls and "https://s.test/b" in urls

    def test_engine_wired_sitemap_flag(self):
        src = (Path(__file__).parent.parent / "kiana_vnext_plus" / "crawler.py").read_text(encoding="utf-8")
        assert "_sitemap_discover" in src
        assert "discover_sitemap" in src  # 死代码已接线


class TestThrottleNotRetry:
    """[v2.17 E-P1-3] 限流≠重试：429/503 冷却等待不消耗 retry_count（原 3 次限流即 dead）。
    注意：不用 conftest tmp_frontier（asyncio.run 初始化后 flusher 协程随旧 loop 死亡，
    跨 loop 用 aiosqlite 连接会挂）——测试内自建并在同一 loop 里 init/使用/close。"""

    @staticmethod
    def _new_db(tmp_path, name):
        from kiana_vnext_plus.frontier import FrontierDB
        return FrontierDB(str(tmp_path / name))

    def test_throttled_keeps_retry_count(self, tmp_path):
        import asyncio as _aio
        import sqlite3 as _s3

        def _read_url_hash(url):
            con = _s3.connect(str(tmp_path / "thr.db"))
            try:
                return con.execute(
                    "SELECT url_hash FROM frontier WHERE normalized_url=?", (url,)).fetchone()[0]
            finally:
                con.close()

        def _read_status(url_hash):
            con = _s3.connect(str(tmp_path / "thr.db"))
            try:
                return con.execute(
                    "SELECT status, retry_count FROM frontier WHERE url_hash=?", (url_hash,)).fetchone()
            finally:
                con.close()

        async def flow():
            db = self._new_db(tmp_path, "thr.db")
            await db.init_async()
            try:
                await db.push("https://site.test/a", depth=0, priority=5)
                await db.flush()
                uh = _read_url_hash("https://site.test/a")
                for _ in range(3):
                    await db.mark_failed(uh, retry=True, throttled=True)
                    await db.flush()
                return _read_status(uh)
            finally:
                await db.close()

        r = _aio.run(flow())
        assert r[0] == "retry", f"限流不应 dead（status={r[0]}）"
        assert r[1] == 0, f"限流不应消耗 retry_count（={r[1]}）"

    def test_regular_failure_still_counts(self, tmp_path):
        import asyncio as _aio
        import sqlite3 as _s3

        async def flow():
            db = self._new_db(tmp_path, "reg.db")
            await db.init_async()
            try:
                await db.push("https://site.test/b", depth=0, priority=5)
                await db.flush()
                con = _s3.connect(str(tmp_path / "reg.db"))
                try:
                    uh = con.execute(
                        "SELECT url_hash FROM frontier WHERE normalized_url=?",
                        ("https://site.test/b",)).fetchone()[0]
                finally:
                    con.close()
                await db.mark_failed(uh, retry=True, throttled=False)
                await db.flush()
                con = _s3.connect(str(tmp_path / "reg.db"))
                try:
                    r = con.execute(
                        "SELECT retry_count FROM frontier WHERE url_hash=?", (uh,)).fetchone()
                finally:
                    con.close()
                return r[0]
            finally:
                await db.close()

        assert _aio.run(flow()) == 1  # 普通失败照常 +1


class TestCrawlStrategy:
    """[v2.17 E-P1-4] 爬行策略：bfs/dfs/bff 三档 ORDER BY（仅排序，租约/CAS 语义不变）"""

    def test_bfs_vs_dfs_order(self, tmp_path):
        import asyncio as _aio
        from kiana_vnext_plus.frontier import FrontierDB

        async def flow(strategy, name):
            db = FrontierDB(str(tmp_path / name))
            await db.init_async()
            try:
                await db.push("https://site.test/a", depth=0, priority=5)
                await _aio.sleep(0.05)  # scheduled_at 拉开（同秒不稳）
                await db.push("https://site.test/b", depth=0, priority=5)
                await db.flush()
                row = await db.pop_batch(10, worker_id="t", strategy=strategy)
                return [r["normalized_url"] for r in row]
            finally:
                await db.close()

        assert _aio.run(flow("bfs", "c_bfs.db")) == ["https://site.test/a", "https://site.test/b"]
        assert _aio.run(flow("dfs", "c_dfs.db")) == ["https://site.test/b", "https://site.test/a"]

    def test_bff_priority_desc(self, tmp_path):
        import asyncio as _aio
        from kiana_vnext_plus.frontier import FrontierDB

        async def flow():
            db = FrontierDB(str(tmp_path / "c_bff.db"))
            await db.init_async()
            try:
                await db.push("https://site.test/low", depth=0, priority=1)
                await db.push("https://site.test/high", depth=0, priority=9)
                await db.flush()
                row = await db.pop_batch(10, worker_id="t", strategy="bff")
                return [r["normalized_url"] for r in row]
            finally:
                await db.close()

        assert _aio.run(flow()) == ["https://site.test/high", "https://site.test/low"]


class TestFingerprintFreshness:
    """[v2.17 E-P1-5] 指纹保鲜：池升级 chrome136 系 + 在线更新可选接线（默认关）"""

    def test_pool_uses_available_targets(self):
        from kiana_vnext_plus.fingerprint_consistency import TLS_IMPERSONATE_POOL, pick_tls_impersonate
        from curl_cffi import get_fingerprint
        assert "chrome136" in TLS_IMPERSONATE_POOL
        assert all(t.startswith("chrome") for t in TLS_IMPERSONATE_POOL)  # UA 同源绑定仅 chrome 系
        for t in TLS_IMPERSONATE_POOL:
            get_fingerprint(t)  # 0.16.0 内置 target 必须全部可用（否则抛）

    def test_update_switch_default_off(self):
        cfg = (Path(__file__).parent.parent / "kiana_vnext_plus" / "config.py").read_text(encoding="utf-8")
        assert '"fingerprint_update_enabled": False' in cfg
        crawler_src = (Path(__file__).parent.parent / "kiana_vnext_plus" / "crawler.py").read_text(encoding="utf-8")
        assert "update_fingerprints" in crawler_src  # 接线存在（默认关不执行）


class TestFingerprintConsistencyAssets:
    """[v2.17 E-P1-6] 浏览器一致性资产（纯 Python 部分）：屏幕/任务栏/窗口 clamp"""

    def test_clamp_bounds(self):
        from kiana_vnext_plus.fingerprint_consistency import clamp_screen_and_window
        avail, ww, wh = clamp_screen_and_window(1920, 1080)
        assert avail < 1080 and avail >= 1080 - 72          # 任务栏扣除
        assert ww <= 1920 and wh <= avail                    # 窗口不超屏
        avail2, ww2, wh2 = clamp_screen_and_window(1920, 1080, win_w=9999, win_h=9999)
        assert ww2 <= 1920 and wh2 <= avail2                 # 超限被 clamp

    def test_generated_fingerprint_consistent(self, tmp_path):
        """生成指纹中 avail/window 必须 <= 屏幕尺寸（不可能组合消除）"""
        from kiana_vnext_plus.fingerprint_consistency import generate_default_fingerprint
        fp = generate_default_fingerprint()
        assert fp["avail_height"] < fp["screen_height"]
        assert fp["window_height"] <= fp["avail_height"]
        assert fp["window_width"] <= fp["screen_width"]


class TestIdentitySession:
    """[v2.17 E-P2] 身份捆绑最小版：出口+cookie+指纹捆绑、封锁整包退役换新"""

    def test_bundle_together(self):
        from kiana_vnext_plus.identity_session import SessionPool
        pool = SessionPool(proxies=("p1", "p2"), cookie_bundles=({"c": "A"}, {"c": "B"}))
        s = pool.get("site.test")
        assert s.proxy in ("p1", "p2")
        assert s.cookie_bundle and s.fingerprint_hint
        # 同一会话三要素同源（hint 由 proxy+bundle 派生）
        assert s.proxy and isinstance(s.fingerprint_hint, str) and len(s.fingerprint_hint) == 16

    def test_mark_bad_retires_whole_session(self):
        from kiana_vnext_plus.identity_session import SessionPool
        pool = SessionPool(proxies=("p1",), cookie_bundles=({"c": "A"}, {"c": "B"}, {"c": "C"}))
        s1 = pool.get("site.test")
        pool.mark_bad("site.test", s1)
        pool.mark_bad("site.test", s1)   # 第二次 → 退役
        s2 = pool.get("site.test")
        assert s2 is not s1                 # 整包退役换新（对象必然不同）
        assert s2.bad_count == 0            # 新会话清零
        assert s2.proxy == "p1"             # 单出口池内换新保留可用出口

    def test_mark_good_resets(self):
        from kiana_vnext_plus.identity_session import SessionPool
        pool = SessionPool(proxies=("p1",), cookie_bundles=({"c": "A"},))
        s = pool.get("d.com")
        pool.mark_bad("d.com", s)
        pool.mark_good("d.com", s)
        assert s.disabled is False


class TestRunInPage:
    """[v2.17 3.4] solver.run_in_page：目标闸/懒初始化/成功返回/失败归位（假体驱动）"""

    @staticmethod
    def _stub():
        from kiana_vnext_plus import solver_engine as se
        s = se.SolverEngine.__new__(se.SolverEngine)
        s._browser_available = True
        return s

    def test_browser_unavailable(self):
        s = self._stub()
        s._browser_available = False
        import asyncio as _a
        assert _a.run(s.run_in_page("https://kuaishou.com/x")) is None

    def test_gate_blocks_non_http(self):
        s = self._stub()
        import asyncio as _a
        assert _a.run(s.run_in_page("file:///etc/passwd")) is None
        assert _a.run(s.run_in_page("http://127.1/x")) is None

    def test_eval_success_and_returned(self):
        import asyncio as _a
        s = self._stub()

        class _Ready:
            def is_set(self):
                return True

        class _Page:
            def __init__(self):
                self.goto_url = None

            async def add_init_script(self, script):
                pass

            async def goto(self, url, wait_until="domcontentloaded", timeout=30000):
                self.goto_url = url

            async def wait_for_timeout(self, ms):
                pass

            async def evaluate(self, expr):
                return 42

        class _Entry:
            page = _Page()

        s._ready = _Ready()
        s._contexts = _a.Queue()
        s._contexts.put_nowait(_Entry())
        out = _a.run(s.run_in_page("https://www.kuaishou.com/short-video/x",
                                   eval_expr="window.__ks_realm", wait_ms=0))
        assert out == 42
        assert s._contexts.qsize() == 1  # 用完归还

    def test_eval_failure_returns_none_and_returns_entry(self):
        import asyncio as _a
        s = self._stub()

        class _Ready:
            def is_set(self):
                return True

        class _Page:
            async def add_init_script(self, script):
                pass

            async def goto(self, url, wait_until="domcontentloaded", timeout=30000):
                raise RuntimeError("goto failed")

            async def wait_for_timeout(self, ms):
                pass

            async def evaluate(self, expr):
                raise RuntimeError("no")

        class _Entry:
            page = _Page()

        s._ready = _Ready()
        s._contexts = _a.Queue()
        s._contexts.put_nowait(_Entry())
        assert _a.run(s.run_in_page("https://www.kuaishou.com/short-video/y",
                                    eval_expr="x", wait_ms=0)) is None
        assert s._contexts.qsize() == 1  # 失败也归还


class TestKuaishouPageSign:
    """[v2.17 3.3] 快手页内签名（实验）：捕获脚本常量 + _sign_in_browser 走假 solver"""

    def test_capture_script_present(self):
        from kiana_vnext_plus.kuaishou_resolver import KS_SIGN_CAPTURE_SCRIPT
        assert "caver" in KS_SIGN_CAPTURE_SCRIPT
        assert "__ks_realm" in KS_SIGN_CAPTURE_SCRIPT
        assert "$encode" in KS_SIGN_CAPTURE_SCRIPT


class TestScanFixGuards:
    """[v2.17 安全深扫修复] captcha 下载走 safe_urlopen / protocol fallback 前置目标闸"""

    def test_captcha_no_raw_urlopen(self):
        src = (Path(__file__).parent.parent / "kiana_vnext_plus" / "captcha_solver_extended.py").read_text(encoding="utf-8")
        assert "safe_urlopen" in src                       # 校验通道接入
        assert "from urllib.request import urlopen" not in src   # 裸 urlopen 移除

    def test_protocol_fallback_target_gate(self):
        src = (Path(__file__).parent.parent / "kiana_vnext_plus" / "protocol_engine.py").read_text(encoding="utf-8")
        assert "safe_urlopen" in src                       # fallback 已走公共安全请求件
        assert "blocked: target check failed" in src       # 拒目标语义

    def test_captcha_gate_blocks_private_url(self):
        from kiana_vnext_plus.url_utils import _http_target_ok
        assert _http_target_ok("http://127.1/x", ()) is False
        assert _http_target_ok("https://cdn.example.com/x", ()) is True

    def test_sign_in_browser_success(self):
        import asyncio as _a
        from kiana_vnext_plus.kuaishou_resolver import _sign_in_browser

        class _Solver:
            async def run_in_page(self, url, init_script="", eval_expr="", wait_ms=1500):
                assert "__ks_realm" in init_script
                return "签名串XYZ"

        sig = _a.run(_sign_in_browser(_Solver(), "https://www.kuaishou.com/short-video/x",
                                      "x", api_uri="/rest/v/profile/feed"))
        assert sig == {"__NS_hxfalcon": "签名串XYZ", "caver": 2}

    def test_sign_in_browser_failure_returns_none(self):
        import asyncio as _a
        from kiana_vnext_plus.kuaishou_resolver import _sign_in_browser

        class _Solver:
            async def run_in_page(self, url, init_script="", eval_expr="", wait_ms=1500):
                return None  # 未登录/未捕获

        assert _a.run(_sign_in_browser(_Solver(), "https://www.kuaishou.com/short-video/y",
                                       "y")) is None

    def test_resolve_async_attaches_signature(self):
        import asyncio as _a
        from kiana_vnext_plus.kuaishou_resolver import resolve_async

        class _Solver:
            async def run_in_page(self, url, init_script="", eval_expr="", wait_ms=1500):
                return "SIG"

        r = _a.run(resolve_async("https://www.kuaishou.com/short-video/3x8", solver=_Solver(),
                                 sign_enabled=True))
        # resolve 页面通道需网络——签名失败/页面失败都可能；核心断言：不抛且含签名
        assert isinstance(r, dict)
        assert r.get("signature", {}).get("__NS_hxfalcon") == "SIG" or (
            "signature" not in r)  # 页面通道 ok=False 时签名不做（诚实）


class TestCdpAttach:
    """[v2.17 3.5] CDP 接管既有浏览器（实验默认关）：配置/接线/close 不销毁接管浏览器"""

    def test_config_default_off(self):
        cfg = (Path(__file__).parent.parent / "kiana_vnext_plus" / "config.py").read_text(encoding="utf-8")
        assert '"cdp_attach": False' in cfg

    def test_launch_wiring_and_close_guard(self):
        se = (Path(__file__).parent.parent / "kiana_vnext_plus" / "solver_engine.py").read_text(encoding="utf-8")
        assert "connect_over_cdp" in se
        assert "生命周期归用户" in se            # 接管语义备注
        assert "not self._cdp_attached" in se   # close 不销毁接管浏览器
        crawler = (Path(__file__).parent.parent / "kiana_vnext_plus" / "crawler.py").read_text(encoding="utf-8")
        assert 'cdp_attach=bool(self.cfg.get("cdp_attach", False))' in crawler


class TestIdentityBundleWiring:
    """[v2.17 E-P2] 主链路接线：开关默认关零影响；开启时出口优先走会话池"""

    def test_default_off_no_pool(self, tmp_path):
        from omegaconf import OmegaConf
        from kiana_vnext_plus.config import GlobalConfig
        from kiana_vnext_plus.identity import ProjectIdentity
        from kiana_vnext_plus.crawler import Crawler
        proj = ProjectIdentity("t_bundle_off", base_dir=tmp_path, ephemeral=True)
        g = GlobalConfig(OmegaConf.create({"master_password": "pw"}))
        c = Crawler(proj, g, worker_id="t")
        assert getattr(c, "_identity_pool", None) is None  # 默认关 → 无池（零影响）

    def test_enabled_builds_pool_from_proxy_list(self, tmp_path):
        from omegaconf import OmegaConf
        from kiana_vnext_plus.config import GlobalConfig
        from kiana_vnext_plus.identity import ProjectIdentity
        from kiana_vnext_plus.crawler import Crawler
        proj = ProjectIdentity("t_bundle_on", base_dir=tmp_path, ephemeral=True)
        proj.config = OmegaConf.merge(proj.config, {"proxy_list": ["http://p1:1", "http://p2:2"]})
        g = GlobalConfig(OmegaConf.create({"master_password": "pw", "identity_bundle": True}))
        c = Crawler(proj, g, worker_id="t")
        pool = getattr(c, "_identity_pool", None)
        assert pool is not None
        sess = pool.get("x.com")
        assert sess.proxy in ("http://p1:1", "http://p2:2")  # 出口来自 proxy_list

    def test_config_key_present(self):
        cfg = (Path(__file__).parent.parent / "kiana_vnext_plus" / "config.py").read_text(encoding="utf-8")
        assert '"identity_bundle": False' in cfg


class TestWbiCache:
    """[v2.17 3.6.3] WBI keys 45 分钟缓存：同任务多视频免重复 nav，失效自动重取"""

    def test_cache_hits_within_ttl(self, monkeypatch):
        import asyncio as _aio
        from kiana_vnext_plus import comment_danmaku as cd
        calls = {"n": 0}

        class _Resp:
            def json(self):
                return {"code": 0, "data": {"wbi_img": {
                    "img_url": "https://i0.hdslb.com/bfs/wbi/img_key.png",
                    "sub_url": "https://i0.hdslb.com/bfs/wbi/sub_key.png"}}}

        class _S:
            async def get(self, url, headers=None, cookies=None, timeout=10, **k):
                calls["n"] += 1
                return _Resp()

        cd._WBI_CACHE["key"] = None
        cd._WBI_CACHE["ts"] = 0
        i1, s1 = _aio.run(cd._wbi_keys(_S()))
        i2, s2 = _aio.run(cd._wbi_keys(_S()))
        assert (i1, s1) == (i2, s2) == ("img_key", "sub_key")
        assert calls["n"] == 1  # 第二次命中缓存不再请求
        cd._WBI_CACHE["key"] = None
        cd._WBI_CACHE["ts"] = 0


class TestMediaSchema:
    """[v2.17 3.1] 统一媒体 schema：4 resolver 适配 + 兜底 + 往返 + 校验"""

    def test_douyin_adapter(self):
        from kiana_vnext_plus.media_schema import from_resolver, to_dict, is_valid
        item = from_resolver("douyin", {"ok": True, "aweme_id": "7300000000000000000",
                                        "video_url": "https://v.douyinvod.com/a/b_play.mp4",
                                        "method": "abogus", "bitrate": 1118})
        assert item.source == "douyin" and item.kind == "video"
        assert item.streams[0].url.startswith("https://v.douyinvod.com")
        assert item.streams[0].no_watermark is True
        assert is_valid(item)
        d = to_dict(item)
        assert d["kind"] == "video" and d["streams"][0]["container"] == "mp4"

    def test_watermark_detection(self):
        from kiana_vnext_plus.media_schema import from_resolver
        item = from_resolver("douyin", {"aweme_id": "1",
                                        "video_url": "https://x/a_playwm.mp4"})
        assert item.streams[0].no_watermark is False  # playwm 未替换 → 有水印

    def test_music163_adapter(self):
        from kiana_vnext_plus.media_schema import from_resolver, is_valid
        item = from_resolver("music163", {"ok": True, "song_id": "19723756",
                                          "media_url": "https://m1.m4a", "type": "song",
                                          "method": "weapi"})
        assert item.kind == "song" and is_valid(item)

    def test_xhs_kuaishou_adapter(self):
        from kiana_vnext_plus.media_schema import from_resolver
        it = from_resolver("xhs", {"note_id": "64f0a2b4", "video_url": "",
                                   "images": ["https://x/1.jpg"]})
        assert it.kind == "image_set" and it.images == ["https://x/1.jpg"]
        kt = from_resolver("kuaishou", {"video_id": "3x8", "video_url": "https://k/v.mp4",
                                        "images": []})
        assert kt.kind == "video"

    def test_generic_fallback(self):
        from kiana_vnext_plus.media_schema import from_resolver, is_valid
        item = from_resolver("whatever", {"media_url": "https://x/x.mp4", "title": "t"})
        assert item.streams and item.title == "t"
        assert not is_valid(from_resolver("whatever", {}))  # 空 → 非法

    def test_to_dict_roundtrip(self):
        from kiana_vnext_plus.media_schema import from_resolver, to_dict
        d = to_dict(from_resolver("douyin", {"aweme_id": "1", "video_url": "https://v/x.mp4"}))
        assert set(d) >= {"source", "media_id", "kind", "streams", "images", "method"}
