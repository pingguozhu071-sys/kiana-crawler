# -*- coding: utf-8 -*-
"""R6 短链基址：**修复前会红的**两条回归测试（前人测试结构上的盲区）

## 为什么现有测试没抓到 R6

`tests/test_shortlink_base_url.py` 很扎实，但它**恰好绕开了两条真实根因**：

1. 它的 `_RouterOverSession.fetch` **只复刻 protocol 层**，从不构造
   **Tier-4 浏览器求解层**返回的那种适配器 —— 而 B站页面**必然**走 Tier-4；
2. 它的 `_make_fake_crawler` **刻意没有 `http_cache` / `solver`**（源码注释自陈）
   ⇒ 缓存路径（根因 B）在测试里**根本不存在**。

所以两条真根因在测试里都是"不存在的地形"。本文件把它们补上。

## 根因（都由子代理人真机插桩实测确认）

- **A**：`engine_router._try_solver` 用**请求 URL** 构造 `ResponseAdapter`，
  浏览器真实落点只放在 `headers["_final_url"]` 里**没人读**。
- **B**：`HttpCache.store` 的 meta 是**固定键白名单**，`final_url` **每次写盘都被丢**。
  （前人以为"旧条目才有这问题"，实测**新写入的也永远没有**。）
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SHORT = "https://b23.tv/ybyASFu"
FINAL = "https://www.bilibili.com/video/BV1obZjBSEpT"


class TestTier4KeepsBrowserLandingUrl(unittest.TestCase):
    """根因 A：Tier-4 必须把**浏览器落点**当 `ResponseAdapter.url`

    修复前：`ResponseAdapter(status, url, ...)` 用的是**请求 URL**（短链），
    于是 `page_processor._final_url_of` 先读 `response.url` 就拿到 `b23.tv`，
    整页相对链接全被拼成 `https://b23.tv/video/BV...`。
    """

    def test_solver_return_url_is_the_landing_url(self):
        from kiana_vnext_plus.engine_router import EngineRouter

        class _Solver:
            """桩求解器：像真的一样把落点放在 headers 里（这正是生产者的契约）"""
            def __init__(self):
                self.calls = []

            async def solve(self, url, proxy=None, challenge_wait=None, **kw):
                self.calls.append(url)
                return ("<html><body>ok</body></html>", 200,
                        {"_final_url": FINAL, "content-type": "text/html"})

        class _Exit:
            async def acquire_for_domain(self, domain):
                return None

            async def release(self, proxy):     # router 在 finally 里会调
                return None

        class _R(EngineRouter):
            def __init__(self, solver):      # 只装这一个依赖
                self.solver = solver
                self.exit_mgr = _Exit()
                self.challenge_wait = 5
                self.FALLBACK_SOLVER = "solver"

        r = _R(_Solver())
        import asyncio
        resp = asyncio.run(r._try_solver(SHORT, "b23.tv"))
        self.assertIsNotNone(resp, "Tier-4 没返回响应")
        self.assertEqual(
            resp.url, FINAL,
            f"Tier-4 的 url 应是**浏览器落点**，实际是 {resp.url!r} —— "
            "根因 A 回归了：基址会退回短链主机")

    def test_missing_final_url_header_falls_back_to_request_url(self):
        """没有 `_final_url` 时**退回请求 URL**（不猜、不炸）—— 兼容旧桩求解器"""
        from kiana_vnext_plus.engine_router import EngineRouter

        class _Solver:
            async def solve(self, url, proxy=None, challenge_wait=None, **kw):
                return ("<html/>", 200, {})     # 无 _final_url

        class _Exit:
            async def acquire_for_domain(self, domain):
                return None

            async def release(self, proxy):     # router 在 finally 里会调
                return None

        class _R(EngineRouter):
            def __init__(self, solver):
                self.solver, self.exit_mgr = solver, _Exit()
                self.challenge_wait, self.FALLBACK_SOLVER = 5, "solver"

        import asyncio
        resp = asyncio.run(_R(_Solver())._try_solver(SHORT, "b23.tv"))
        self.assertEqual(resp.url, SHORT, "无落点时应退回请求 URL")


class TestHttpCacheKeepsFinalUrl(unittest.TestCase):
    """根因 B：`HttpCache` 的 meta 必须保存 `final_url`

    缓存命中时，meta 里的 `final_url` 是**唯一**能还原相对链接基址的东西
    （HTML 里没有它）。丢了就只能退回请求 URL —— 短链场景下就是错的。
    """

    def test_final_url_survives_roundtrip(self):
        from kiana_vnext_plus.p1_enhancements import HttpCache
        d = tempfile.mkdtemp()
        c = HttpCache(d)
        c.store(SHORT, "<html/>", {"final_url": FINAL, "etag": "abc"})
        got = c.get(SHORT)
        self.assertIsNotNone(got, "缓存没写进去？")
        meta = (got or {}).get("meta") or {}
        self.assertEqual(
            meta.get("final_url"), FINAL,
            f"meta 里的 final_url 是 {meta.get('final_url')!r} —— 根因 B 回归了："
            "写盘时又把它丢了，缓存命中必然把基址退回短链主机")

    def test_other_meta_fields_still_saved(self):
        """别把原有的 etag / last_modified 丢了（它们是条件请求的依据）"""
        from kiana_vnext_plus.p1_enhancements import HttpCache
        d = tempfile.mkdtemp()
        c = HttpCache(d)
        c.store(SHORT, "<html/>",
                {"final_url": FINAL, "etag": "abc", "last-modified": "Wed, 01"})
        meta = (c.get(SHORT) or {}).get("meta") or {}
        self.assertEqual(meta.get("etag"), "abc", "etag 丢了 —— 条件请求会失效")
        self.assertEqual(meta.get("last_modified"), "Wed, 01",
                         "last_modified 丢了 —— 条件请求会失效")


class TestStaleCacheEntriesSelfHeal(unittest.TestCase):
    """**修复前写入的旧条目必须自愈**，否则会继续污染到 TTL 过期为止

    机主机器上实测有 **169 个**修复前的条目（写盘时 `final_url` 被丢弃）。
    只修写入端的话，它们仍会让短链种子的基址退回 `b23.tv`。

    判据**刻意收窄**：只有"**请求地址本身是短链**且缺 `final_url`"才算未命中 ——
    非短链站点不需要该字段也能正确解析，误当 miss 只会白白降低命中率。
    """

    def _seed_legacy(self, d, url):
        """伪造一个"修复前"的条目：有 meta/html，但**没有** final_url"""
        import hashlib
        import json
        k = hashlib.sha256(url.encode()).hexdigest()[:16]
        (Path(d) / f"{k}.html").write_text("<html>legacy</html>", encoding="utf-8")
        (Path(d) / f"{k}.meta").write_text(
            json.dumps({"url": url, "fetched_at": __import__("time").time()}),
            encoding="utf-8")

    def test_legacy_shortener_entry_is_treated_as_miss(self):
        from kiana_vnext_plus.p1_enhancements import HttpCache
        d = tempfile.mkdtemp()
        c = HttpCache(d)
        self._seed_legacy(d, SHORT)
        self.assertIsNone(
            c.get(SHORT),
            "旧的短链条目仍然命中 —— 它的基址必然是错的，会继续污染数据面")

    def test_legacy_plain_entry_still_hits(self):
        """**不许误伤**：非短链站点不需要 `final_url`，缓存该照常命中"""
        from kiana_vnext_plus.p1_enhancements import HttpCache
        PLAIN = "https://www.bilibili.com/video/BV1xx/"
        d = tempfile.mkdtemp()
        c = HttpCache(d)
        self._seed_legacy(d, PLAIN)
        self.assertIsNotNone(
            c.get(PLAIN),
            "普通条目被误当未命中 —— 缓存命中率会白白下降")

    def test_new_shortener_entry_hits_normally(self):
        """修复**之后**写入的短链条目带 `final_url`，应正常命中"""
        from kiana_vnext_plus.p1_enhancements import HttpCache
        d = tempfile.mkdtemp()
        c = HttpCache(d)
        c.store(SHORT, "<html/>", {"final_url": FINAL})
        self.assertIsNotNone(c.get(SHORT), "新条目不该被自愈逻辑误伤")


class TestNeedsSolverNoLongerFalsePositivesOnBilibili(unittest.TestCase):
    """根因 A 的**触发面**：B站页面不该被判成"需要求解器"

    B站每个视频页骨架都内联
    `<script src=".../risk-captcha-sdk/CaptchaLoader.js">`，
    旧的裸子串判据必然命中 ⇒ **每个 B站页面白起一次 Chromium**，
    而 Tier-4 正是基址丢失的入口（所以这个误报**必然**引出 R6）。
    """

    def _needs(self, html):
        from kiana_vnext_plus.engine_router import EngineRouter
        from kiana_vnext_plus.response_adapter import ResponseAdapter

        class _R(EngineRouter):
            def __init__(self):
                pass

        return _R()._needs_solver(
            ResponseAdapter(200, "https://x/", {}, raw_text=html))

    def test_bilibili_skeleton_is_not_a_challenge(self):
        html = ('<html><head><script defer src="https://s1.hdslb.com/bfs/seed/'
                'jinkela/risk-captcha-sdk/CaptchaLoader.js"></script></head>'
                '<body>视频</body></html>')
        self.assertFalse(self._needs(html),
                         "B站骨架又被判成挑战了 —— 会白起浏览器，且必然引出 R6")

    def test_real_challenges_still_detected(self):
        for name, html in (
            ("recaptcha", '<div class="g-recaptcha"></div>'),
            ("cloudflare", '<div id="cf-challenge-running">x</div>'),
            ("滑块表单", '<form action="/captcha"><img id="captcha"></form>'),
            ("turnstile", '<div class="cf-turnstile"></div>'),
        ):
            self.assertTrue(self._needs(html), f"{name} 没被检出 —— 会漏掉真挑战")

    def test_clean_page_is_not_a_challenge(self):
        self.assertFalse(self._needs("<html><body><h1>hello</h1></body></html>"))


if __name__ == "__main__":
    unittest.main()
