# -*- coding: utf-8 -*-
"""短链种子：相对链接必须按**终到地址**解析（实测 v2.19.8 抓到的真 bug）

## 现场（机主真机跑短链种子）

种子 `https://b23.tv/n2vFgzi` → B 站短链服务 302 → `https://www.bilibili.com/video/BV1zJhr6bEVa`。
结果 **12 页成功 / 19 页失败**，失败全是同一个原因——**相对链接被拼到了短链主机上**：

    真实页面里的相关推荐是相对链接 `/video/BV1PSL96YEwp`
    程序拼出来的是        `https://b23.tv/video/BV1PSL96YEwp`   ← 这个网址根本不存在

短链服务只认它自己发出去的短码，`/video/...` 在 b23.tv 上必然 404。19 页失败就是这么来的。

## 根因

`safe_get` 为了逐跳复检重定向落点（SSRF 闸）用 `allow_redirects=False` 自己循环跟跳，
循环里维护着 `_cur`（当前/最终地址），**但返回 resp 时没把它带出来**；
下游 `parser.extract_metadata` 只能拿请求前的短链 url 当 `urljoin` 的基址。

## 本文件要钉死的四件事（对应验收项）

1. **发生 302 时按终到地址解析**（核心；`TestShortLinkBaseUrlEndToEnd` 真起**两台**回环
   服务——短链主机与真实页主机——端到端跑 `process_job`，断言出链落在真实页主机上）；
2. **没发生重定向时行为逐字不变**（`TestParserBaseUrl::test_default_unchanged`：
   不给 base_url 时 `extract_metadata` 的输出必须与改前完全一致；E2E 里再真跑一遍
   无重定向的抓取，确认出链没有被改动影响）；
3. **超跳数上限 / Location 缺失时不炸**（`TestSafeGetEdgeReturns`：仍返回响应对象、
   仍带终到地址，调用方不会 None 崩）；
4. **私网重定向仍被拦**（`TestSsrfGateUnchanged`：**回归钉子**，证明修复没有放松安全红线）。

## 为什么不 mock 掉网络

用 `http.server` 起本机回环服务做真 HTTP（`tests/test_m3_solver_service.py` 同款先例），
因为"302 跳转 + 逐跳"这件事本身就是 HTTP 语义，桩掉 socket 就只测了自己写的假实现。
回环请求超时一律给 **30 秒**（本工程刚因"5 秒超时"吃过 flaky 的亏——判据是状态码/URL，
不是延迟，不该顺带考核机器忙不忙）。

安全注意：环回地址对 `is_private_url` 是**私网**（这正是 SSRF 闸该拦的）。
本文件只在"测试自己起的那台服务器"上把它放行，且**屏蔽是按主机名精确匹配**的
（只放 127.0.0.1/localhost，`www.bilibili.com`、`b23.tv`、`169.254.169.254` 等
一律走真实判定）——放行面必须比真实闸更小，否则第 4 条钉子就是自欺。
"""
import asyncio
import contextlib
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("KIANA_CRYPTO_KEY", "kv_test")

from kiana_vnext_plus import url_utils as uu                      # noqa: E402
from kiana_vnext_plus.page_processor import PageProcessor          # noqa: E402
from kiana_vnext_plus.parser import extract_metadata               # noqa: E402

# 现场用的真实地址（只作字符串，不发任何真实请求）
SEED = "https://b23.tv/n2vFgzi"
FINAL = "https://www.bilibili.com/video/BV1zJhr6bEVa"
RELATED = "/video/BV1PSL96YEwp"
# 规则翻页专用路径：带 `?only=rule` 是为了让 `link_scoring` 的静态资源/垃圾过滤
# 不把它当普通出链入队——这样"它入队了"就只能是**规则层**干的，判据不串味。
RULE_NEXT = "/rulepage/9?only=rule"

LOOPBACK_TIMEOUT = 30          # 见模块 docstring：判据不是延迟，给宽


# ════════════════════════════════════════════════════════════════
# 桩：轻量响应 + 会话（只实现 safe_get 真正用到的面：is_redirect/headers/close）
# ════════════════════════════════════════════════════════════════
class _Resp:
    def __init__(self, status_code, headers=None, text=""):
        self.status_code = status_code
        self.headers = dict(headers or {})
        self._text = text
        self.is_redirect = 300 <= status_code < 400
        self.closed = False

    def close(self):
        self.closed = True


class _ChainSession:
    """按 Location 链表逐跳应答；没有下一跳就回 200。"""

    def __init__(self, chain, final_text="<html>ok</html>"):
        self.chain = dict(chain)      # url -> Location（或 None）
        self.final_text = final_text
        self.requested = []

    async def request(self, method, url, **kw):
        self.requested.append(url)
        nxt = self.chain.get(url)
        if nxt is None:
            return _Resp(200, text=self.final_text)
        return _Resp(302, {"Location": nxt})


# ════════════════════════════════════════════════════════════════
# 1. safe_get 必须把"实际落到的地址"带出来
# ════════════════════════════════════════════════════════════════
class TestSafeGetCarriesFinalUrl(unittest.TestCase):
    """核心机制：safe_get 逐跳后的 `_cur` 必须挂在响应对象上。

    改前的错误行为：循环里维护了 `_cur`，`return resp` 时**没带出来** →
    下游只能拿到请求前的短链 url（本 bug 的全部成因）。
    """

    def setUp(self):
        self._orig = uu.is_private_url

        def _gate(u, dns_check=None):
            # 只放行本测试自己造的主机名；其余（含真实公网/私网判定）走真实实现
            if _host_of(u) in ("b23.tv", "www.bilibili.com"):
                return False
            return self._orig(u, dns_check=dns_check)

        uu.is_private_url = _gate
        self.addCleanup(lambda: setattr(uu, "is_private_url", self._orig))

    def test_follows_302_and_exposes_final_url(self):
        s = _ChainSession({SEED: FINAL})
        resp = asyncio.run(uu.safe_get(s, SEED, max_hops=10))
        self.assertIsNotNone(resp, "合法公网短链不得被拦")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(getattr(resp, "_kiana_final_url", None), FINAL,
                         "302 跟到最后必须把终到地址带出来——不带就是本 bug 的根因")
        # 逐跳语义没变：先打短链，再打终到地址
        self.assertEqual(s.requested, [SEED, FINAL])

    def test_multi_hop_exposes_last_url(self):
        mid = "https://www.bilibili.com/s/redirect"
        s = _ChainSession({SEED: mid, mid: FINAL})
        resp = asyncio.run(uu.safe_get(s, SEED, max_hops=10))
        self.assertEqual(getattr(resp, "_kiana_final_url", None), FINAL)
        self.assertEqual(s.requested, [SEED, mid, FINAL])

    def test_no_redirect_exposes_original_url(self):
        """没重定向时终到地址 == 请求地址（下游据此退回 url，行为零变化）"""
        s = _ChainSession({})
        resp = asyncio.run(uu.safe_get(s, FINAL))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(getattr(resp, "_kiana_final_url", None), FINAL)
        self.assertEqual(s.requested, [FINAL])


class TestSafeGetEdgeReturns(unittest.TestCase):
    """边界两条 return 也必须带地址——它们**都已经发出了真实请求**，
    不带就等于把"当时真正请求的地址"丢掉（用户点名过这两条）。"""

    def setUp(self):
        self._orig = uu.is_private_url

        def _gate(u, dns_check=None):
            if _host_of(u) in ("b23.tv", "www.bilibili.com"):
                return False
            return self._orig(u, dns_check=dns_check)

        uu.is_private_url = _gate
        self.addCleanup(lambda: setattr(uu, "is_private_url", self._orig))

    def test_hop_limit_returns_response_not_none(self):
        """超跳数上限：返回最后一跳响应（不抛、不返回 None——调用方靠这个判"永久失败"）"""
        u1 = "https://b23.tv/a"
        u2 = "https://b23.tv/b"
        u3 = "https://b23.tv/c"
        s = _ChainSession({u1: u2, u2: u3, u3: "https://b23.tv/d"})
        resp = asyncio.run(uu.safe_get(s, u1, max_hops=2))
        self.assertIsNotNone(resp, "超跳上限不得返回 None（None 只表示被闸拦下）")
        self.assertEqual(getattr(resp, "_kiana_final_url", None), u3,
                         "超限时带出的必须是**当时真正请求的那个地址**")
        self.assertEqual(s.requested, [u1, u2, u3])

    def test_missing_location_returns_response(self):
        """302 但无 Location：返回该响应且带出当时的地址（不得 KeyError/None）"""
        class _NoLoc:
            async def request(self, method, url, **kw):
                return _Resp(302, {})

        resp = asyncio.run(uu.safe_get(_NoLoc(), "https://b23.tv/n2vFgzi"))
        self.assertIsNotNone(resp)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(getattr(resp, "_kiana_final_url", None), "https://b23.tv/n2vFgzi")


# ════════════════════════════════════════════════════════════════
# 2. SSRF 闸回归钉子：私网重定向**仍然被拦**
# ════════════════════════════════════════════════════════════════
class TestSsrfGateUnchanged(unittest.TestCase):
    """红线：本次修复**只加"带出终到地址"**，逐跳复检与拦截行为一个字都不能松。

    刻意**不放行任何主机**（连环回也不放）——闸该怎么判就怎么判。
    """

    def test_private_entry_blocked(self):
        """入口是私网/非法协议 → 在**发出任何请求之前**就返回 None。

        **真实闸**（`_real_gate()`）：本模块为跑回环服务放行了 127.0.0.1，
        这里必须把放行摘掉再验——否则"回环被拦"这条根本没在测。
        会话带记录：被拦时 `requested` 必须仍为空——"没打出去"才叫拦住了。
        """
        s = _ChainSession({})
        with _real_gate():
            for u in ("http://169.254.169.254/latest/meta-data/",
                      "http://127.0.0.1:8080/x",
                      "http://localhost/x",
                      "file:///C:/Windows/win.ini",
                      "ftp://x/y"):
                with self.subTest(url=u):
                    self.assertIsNone(asyncio.run(uu.safe_get(s, u)),
                                      f"{u} 必须被拦（None）")
        self.assertEqual(s.requested, [], "被拦的地址不得真正发出请求")

    def test_redirect_to_private_is_blocked(self):
        """外部合法地址 302 → 内网：必须拦下（本工程 v2.19.7 的 PoC 形状）"""
        class _S:
            async def request(self, method, url, **kw):
                return _Resp(302, {"Location": "http://169.254.169.254/latest/meta-data/"})

        # 入口只放行 www.bilibili.com；落点 169.254.169.254 走真实判定
        _orig = uu.is_private_url

        def _gate(u, dns_check=None):
            if _host_of(u) == "www.bilibili.com":
                return False
            return _orig(u, dns_check=dns_check)

        uu.is_private_url = _gate
        try:
            self.assertIsNone(
                asyncio.run(uu.safe_get(_S(), "https://www.bilibili.com/video/BV1")),
                "重定向落点为私网必须返回 None——修复不得放松这条")
        finally:
            uu.is_private_url = _orig

    def test_public_numeric_ip_still_allowed(self):
        """公网数字 IP 不受影响（证明上一条拦的是"私网"而不是"数字 IP"）"""
        class _S:
            async def request(self, method, url, **kw):
                return _Resp(200, text="ok")

        resp = asyncio.run(uu.safe_get(_S(), "http://93.184.216.34/x"))
        self.assertIsNotNone(resp, "公网数字 IP 不得被误拦")
        self.assertEqual(resp.status_code, 200)


# ════════════════════════════════════════════════════════════════
# 3. 解析层：base_url 生效，且缺省时行为逐字不变
# ════════════════════════════════════════════════════════════════
_BIZ_HTML = (
    '<html><head><title>某视频</title>'
    '<link rel="icon" href="/favicon.ico">'
    '<link rel="canonical" href="/video/BV1zJhr6bEVa">'
    '</head><body>'
    f'<a href="{RELATED}">相关推荐</a>'
    '<a href="/video/BV1AAA?t=1&amp;v=2">带实体参数</a>'
    '<img src="/img/cover.jpg">'
    '</body></html>'
)


class TestParserBaseUrl(unittest.TestCase):
    def test_default_unchanged(self):
        """不传 base_url → 与改前完全一致：仍以传入的 url 为基址"""
        d = extract_metadata(_BIZ_HTML, SEED)
        self.assertEqual(d["url"], SEED)
        self.assertIn("https://b23.tv/video/BV1PSL96YEwp", d["links"]["internal"],
                      "缺省时行为必须与改前一致（这是'零行为变化'的证据）")
        self.assertEqual(d["canonical"], "https://b23.tv/video/BV1zJhr6bEVa")
        self.assertEqual(d["favicon"], "https://b23.tv/favicon.ico")
        self.assertEqual(d["images"], ["https://b23.tv/img/cover.jpg"])

    def test_base_url_resolves_against_final_host(self):
        """传终到地址 → 出链落在真实主机上，b23.tv 一条都不许有"""
        d = extract_metadata(_BIZ_HTML, SEED, FINAL)
        self.assertEqual(d["url"], SEED, "data['url'] 仍是任务请求地址（落库/去重口径不变）")
        self.assertIn(f"https://www.bilibili.com{RELATED}", d["links"]["internal"])
        self.assertEqual(d["canonical"], "https://www.bilibili.com/video/BV1zJhr6bEVa")
        self.assertEqual(d["favicon"], "https://www.bilibili.com/favicon.ico")
        self.assertEqual(d["images"], ["https://www.bilibili.com/img/cover.jpg"])
        _all = d["links"]["internal"] + d["links"]["external"]
        self.assertNotIn(SEED, _all)
        self.assertFalse([u for u in _all if "b23.tv" in u],
                         f"终到地址生效后不得再出现短链主机：{_all}")

    def test_external_host_still_classified_external(self):
        html = '<html><body><a href="https://other.example/x">站外</a></body></html>'
        d = extract_metadata(html, SEED, FINAL)
        self.assertEqual(d["links"]["external"], ["https://other.example/x"])


class TestHtmlEntityDecoded(unittest.TestCase):
    """机主日志里的 `?amp%3Btrackid=` 形状：`<a href>` 没解 HTML 实体。

    实测依据（同一次探针）：lxml 会把 `<img src>` 的 `&amp;` 解成 `&`，
    **但 `<a href>` 不会**——于是 `&` 被当普通字符百分号编码成 `%26`，
    参数名变成 `amp;trackid`，服务端必然当成不存在的参数（链接必 404）。
    """

    def test_amp_entity_is_decoded_once(self):
        d = extract_metadata(_BIZ_HTML, SEED, FINAL)
        _hit = [u for u in d["links"]["internal"] if "BV1AAA" in u]
        self.assertEqual(len(_hit), 1, f"应解析出 BV1AAA 那条：{d['links']['internal']}")
        self.assertEqual(_hit[0], "https://www.bilibili.com/video/BV1AAA?t=1&v=2")
        for _bad in ("%26", "%3B", "amp;trackid", "&amp;"):
            self.assertNotIn(_bad, _hit[0], f"HTML 实体不得留给下游：{_hit[0]}")

    def test_svg_namespace_not_resurrected(self):
        """实体解码不得把 SVG 命名空间当成真链接（工程里专门过滤过 w3.org）"""
        html = ('<html><body><svg xmlns="http://www.w3.org/2000/svg"></svg>'
                '<a href="/x">ok</a></body></html>')
        d = extract_metadata(html, SEED, FINAL)
        self.assertNotIn("http://www.w3.org/2000/svg", d["links"]["internal"])
        self.assertIn("https://www.bilibili.com/x", d["links"]["internal"])


# ════════════════════════════════════════════════════════════════
# 4. 端到端：真起回环服务，真跑 process_job
# ════════════════════════════════════════════════════════════════
_SERVER_HITS = []


def _make_handler(redirect_to=""):
    """造 handler 类：`/seed` 302 → `redirect_to`；其余路径回一页含相对链接的 HTML。

    用工厂而不是常量类，是因为**两台服务要不同行为**（短链主机只发 302，
    真实页主机只发正文）——见 `TestShortLinkBaseUrlEndToEnd` 的类 docstring。
    """

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):                                   # noqa: N802
            _SERVER_HITS.append(self.path)
            if redirect_to and self.path.startswith("/seed"):
                self.send_response(302)
                self.send_header("Location", redirect_to)
                self.end_headers()
                return
            body = (
                '<html><head><title>真页面</title></head><body>'
                f'<a href="{RELATED}">相关推荐</a>'
                '<a href="/video/BV1AAA?t=1&amp;v=2">实体参数</a>'
                f'<a class="next" href="{RULE_NEXT}">下一页</a>'
                '</body></html>'
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    return _Handler


class _AsyncLoopbackSession:
    """真会话：打到本机回环服务，**绝不自作主张跟跳**。

    刻意用 `http.client` 而不是 `urllib.request`：实测（本轮踩过）urllib 的
    `HTTPRedirectHandler` 会在自定义 handler 之前就把 302 跟掉（探针里服务端
    收到 4 次多余请求才最终抛错），于是"302 原样交出来"这条前提被悄悄破坏——
    测试会变成在测 urllib 的跳转，而不是 `safe_get` 的逐跳。`http.client`
    给的就是原始状态行与响应头，语义与 curl_cffi 的 `allow_redirects=False` 一致。
    """

    def __init__(self):
        self.requested = []

    async def request(self, method, url, headers=None, timeout=None,
                      allow_redirects=False, **kw):
        import http.client
        from urllib.parse import urlsplit
        self.requested.append(url)
        parts = urlsplit(url)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query

        def _sync():
            conn = http.client.HTTPConnection(parts.hostname, parts.port or 80,
                                              timeout=LOOPBACK_TIMEOUT)
            try:
                conn.request(str(method).upper(), path, headers=dict(headers or {}))
                r = conn.getresponse()
                body = r.read().decode("utf-8", "replace")
                return _Resp(r.status, dict(r.getheaders()), body)
            finally:
                conn.close()

        return await asyncio.to_thread(_sync)


class _NoRedirect(urllib.request.BaseHandler):
    """强制把 3xx 当错误抛出 → 302 **原样送到 safe_get**。

    两个必须：① 继承 `BaseHandler`（urllib 的 `build_opener` 会校验 handler 类型）；
    ② 显式拦截 302——urllib 的 `HTTPRedirectHandler` 默认就在栈里会自己跟跳，
    用它会掩盖 safe_get 的逐跳语义（测试就变成在测 urllib 了）。
    """

    def http_error_302(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


_ORIG_GATE = None


@contextlib.contextmanager
def _real_gate():
    """临时**还原真实闸**——验证"回环/私网确实会被拦"时必须用它，
    否则测的是本模块为跑回环服务而开的那道小口子（= 自己骗自己）。
    退出时把本模块的回环放行装回去（E2E 用例还要用）。
    """
    _saved = uu.is_private_url
    uu.is_private_url = _ORIG_GATE if _ORIG_GATE is not None else _saved
    try:
        yield
    finally:
        _restore = _ORIG_GATE if _ORIG_GATE is not None else _saved

        def _gate(u, dns_check=None):
            if _host_of(u) in ("127.0.0.1", "localhost"):
                return False
            return _restore(u, dns_check=dns_check)

        uu.is_private_url = _gate


async def _fetch_via_safe_get(session, url):
    return await uu.safe_get(session, url, timeout=LOOPBACK_TIMEOUT, max_hops=10)


def _host_of(url: str) -> str:
    """取主机名（**不含端口**）——`extract_domain` 会带上 `:端口`，不能直接比。"""
    try:
        from urllib.parse import urlparse
        return (urlparse(str(url)).hostname or "").lower()
    except Exception:
        return ""


def setUpModule():
    """模块级放行**仅**回环（测试自己起的那台服务），其余主机一律走真实判定。

    回环在本工程是私网（SSRF 闸就该拦它）——不放行则测试自己的服务都打不到。
    放行面比真实闸更小是刻意的：这样 `TestSsrfGateUnchanged` 里那些
    "重定向到 169.254.169.254"的钉子仍然是**真实判定**在把关。

    注意比对的是 `_host_of`（去端口）而不是 `extract_domain`：后者按工程约定
    保留 `:端口`，拿它比 `"127.0.0.1"` 永远不相等（这里踩过一次）。
    """
    global _ORIG_GATE
    _ORIG_GATE = uu.is_private_url

    def _gate(u, dns_check=None):
        if _host_of(u) in ("127.0.0.1", "localhost"):
            return False
        return _ORIG_GATE(u, dns_check=dns_check)

    uu.is_private_url = _gate


def tearDownModule():
    if _ORIG_GATE is not None:
        uu.is_private_url = _ORIG_GATE


class _RouterOverSession:
    """`PageProcessor` 调的是 `router.fetch(url, domain, job)`，实际抓取走 session。

    这里忠实复刻主通道 `protocol_engine.fetch` 的**关键一步**：手动逐跳后把
    **终到地址**放进 `ResponseAdapter.url`（真实实现就是这样，见 protocol_engine
    的 `_cur, _hops` 循环与 `ResponseAdapter(resp.status_code, _cur, ...)`）。
    """

    def __init__(self, session):
        self.session = session
        self.fetch_calls = []

    async def fetch(self, url, domain=None, job=None):
        from kiana_vnext_plus.response_adapter import ResponseAdapter
        self.fetch_calls.append(url)
        resp = await _fetch_via_safe_get(self.session, url)
        if resp is None:
            return ResponseAdapter(400, url, {}, raw_text="blocked")
        return ResponseAdapter(resp.status_code, getattr(resp, "_kiana_final_url", url),
                               resp.headers, raw_text=resp._text)


def _make_fake_crawler(router):
    """最小假体：只提供 `process_job` 主路径真正读到的属性。

    刻意用 `SimpleNamespace`（工程既有 `tests/test_v216_stats.py` 同款）——
    它**没有** `link_extractor`/`http_cache`/`pipeline`/`exporter`/`solver`
    /`page_classifier`，正好把这些可选分支留在"关闭"位（getattr 守卫覆盖到）。
    """
    from types import SimpleNamespace

    class _Limits:
        max_pages_per_domain = 999
        max_pages = 999
        max_depth = 2

    class _Config:
        limits = _Limits()

        def get(self, k, d=None):
            return d

    class _Frontier:
        def __init__(self):
            self.pushed = []

        async def count_done_by_domain(self, d):
            return 0

        async def count_done_total(self):
            return 0

        async def mark_done(self, uh, leased_at=None):
            return True

        async def mark_failed(self, *a, **k):
            pass

        async def write_error(self, *a, **k):
            pass

        async def write_page(self, *a, **k):
            pass

        async def write_extracted(self, *a, **k):
            pass

        async def is_visited(self, h):
            return False

        async def push(self, url, depth=0, priority=0, parent_hash=None):
            self.pushed.append(url)

        async def adjust_priority(self, *a, **k):
            pass

    class _Concurrency:
        async def acquire_all(self, *a, **k):
            pass

        async def release_all(self, *a, **k):
            pass

    class _Adaptive:
        def record(self, *a, **k):
            pass

    class _ExitMgr:
        async def acquire_for_domain(self, *a, **k):
            return None

        async def release(self, *a, **k):
            pass

        async def report_result(self, *a, **k):
            pass

    frontier = _Frontier()
    crawler = SimpleNamespace(
        _fp_gen=None,
        _progress={"done": 0, "failed": 0, "pending": 0, "total": 0},
        _identifier=None,
        # 防御模式 'off'：既不进 sentinel(shield) 也不进 oracle(habakiri) 分支
        _defense_mode="off",
        project=SimpleNamespace(config=_Config()),
        cfg=_Config(),
        frontier=frontier,
        router=router,
        exit_mgr=_ExitMgr(),
        concurrency=_Concurrency(),
        adaptive=_Adaptive(),
    )
    crawler._update_progress = lambda k, n=1: crawler._progress.__setitem__(
        k, crawler._progress.get(k, 0) + n)
    # 身份反馈：无 armory 时真实实现也是空转，这里给出同名空方法即可
    crawler._identity_feedback = lambda *a, **k: None
    return crawler, frontier


class TestShortLinkBaseUrlEndToEnd(unittest.TestCase):
    """**本文件的核心用例**：真起回环 HTTP 服务，真跑 `process_job`。

    ## 为什么必须起**两台**服务（这里踩过一次"假绿"）

    第一版只起一台：短链与真实页同主机同端口，于是"按请求地址解析"与
    "按终到地址解析"**得出同一串 URL**——把修复整个改回去，测试照样全绿
    （变异反证当场抓到）。这跟本 bug 的形状正好相反：真实事故里
    b23.tv 与 www.bilibili.com 是**两个主机**，拼错才看得出。

    现在：服务 A = 短链主机（`/seed` 302 → 服务 B 的 `/real`），
    服务 B = 真实页主机（回一页带**相对链接**的 HTML）。
    相对链接只有按**服务 B** 解析才对；按请求地址（服务 A）解析会得到
    `http://127.0.0.1:<A>/video/…`——正是 `https://b23.tv/video/…` 的同构形状。

    ⚠ 两个端口由内核分配，**必须不同**——相同则本用例重新退化成假绿。
    """

    @classmethod
    def setUpClass(cls):
        cls.real_srv = HTTPServer(("127.0.0.1", 0), _make_handler())
        cls.real_port = cls.real_srv.server_address[1]
        cls.real_base = f"http://127.0.0.1:{cls.real_port}"

        cls.short_srv = HTTPServer(
            ("127.0.0.1", 0), _make_handler(redirect_to=f"{cls.real_base}/real"))
        cls.short_port = cls.short_srv.server_address[1]
        cls.short_base = f"http://127.0.0.1:{cls.short_port}"

        cls._threads = [
            threading.Thread(target=cls.real_srv.serve_forever, daemon=True),
            threading.Thread(target=cls.short_srv.serve_forever, daemon=True),
        ]
        for t in cls._threads:
            t.start()

    @classmethod
    def tearDownClass(cls):
        for srv in (cls.real_srv, cls.short_srv):
            srv.shutdown()
            srv.server_close()
        for t in cls._threads:
            t.join(timeout=LOOPBACK_TIMEOUT)

    def setUp(self):
        _SERVER_HITS.clear()
        # 端口必须真的不同，否则本用例区分不出两种解析基址（见类 docstring）
        self.assertNotEqual(self.short_port, self.real_port,
                            "两个回环端口撞了 → 本用例会退化成假绿")

    def _run_job(self, session, url):
        router = _RouterOverSession(session)
        crawler, frontier = _make_fake_crawler(router)
        proc = PageProcessor(crawler)
        proc._human_delay = lambda domain=None: asyncio.sleep(0)
        job = {"normalized_url": url, "domain": "127.0.0.1", "url_hash": "uh1", "depth": 0}
        asyncio.run(asyncio.wait_for(proc.process_job(job), timeout=LOOPBACK_TIMEOUT))
        return frontier

    def test_seed_302_relative_link_uses_final_host(self):
        """核心断言：相对链接落在**真实页主机**（服务 B），不得落在短链主机（服务 A）"""
        session = _AsyncLoopbackSession()
        seed = f"{self.short_base}/seed"
        frontier = self._run_job(session, seed)

        self.assertIn(f"{self.real_base}/real", session.requested,
                      f"safe_get 必须手动跟到终到地址：{session.requested}")

        want = f"{self.real_base}/video/BV1PSL96YEwp"
        wrong = f"{self.short_base}/video/BV1PSL96YEwp"
        self.assertIn(want, frontier.pushed,
                      f"相对链接必须按终到地址解析成 {want}；实际入队：{frontier.pushed}")
        self.assertNotIn(wrong, frontier.pushed,
                         f"**本 bug 的原形状**：相对链接被拼到短链主机上：{wrong}")

    def test_entity_decoded_link_is_queued_clean(self):
        """带 `&amp;` 的相对链接：入队的必须是解码后的干净 URL（且在真实页主机上）"""
        session = _AsyncLoopbackSession()
        frontier = self._run_job(session, f"{self.short_base}/seed")
        _hit = [u for u in frontier.pushed if "BV1AAA" in u]
        self.assertTrue(_hit, f"BV1AAA 那条应入队：{frontier.pushed}")
        self.assertEqual(_hit[0], f"{self.real_base}/video/BV1AAA?t=1&v=2")
        for _bad in ("%26", "%3B", "&amp;"):
            self.assertNotIn(_bad, _hit[0])

    def test_rule_pagination_uses_final_host(self):
        """**钉住 page_processor → site_rules 的传参**：规则翻页也要按终到地址补全。

        为什么单独一条 E2E：`TestSiteRulePaginationUsesFinalHost` 是直接调
        `apply_rule`，它验不了"页处理器有没有把终到地址**传进去**"——
        变异反证里 M5（把那行改回 `apply_rule(html, url)`）当时**照样全绿**。
        这里用真实 `process_job` 走完整链路：临时规则命中回环页，
        相对翻页 `/rulepage/9` 必须落到**真实页主机**上。
        """
        import tempfile
        from pathlib import Path as _P
        from kiana_vnext_plus import site_rules as sr

        yaml_text = (
            "name: loopback-demo\n"
            'match: ["127.0.0.1"]\n'
            "pagination:\n"
            '  next: {sel: "a.next", attr: href}\n'
        )
        _saved_loader = sr._loader            # 全局单例：必须还原，别污染其它用例
        with tempfile.TemporaryDirectory() as td:
            (_P(td) / "site.yaml").write_text(yaml_text, encoding="utf-8")
            sr._loader = sr.RuleLoader(_P(td))
            try:
                session = _AsyncLoopbackSession()
                frontier = self._run_job(session, f"{self.short_base}/seed")
            finally:
                sr._loader = _saved_loader

        want = f"{self.real_base}{RULE_NEXT}"
        wrong = f"{self.short_base}{RULE_NEXT}"
        self.assertIn(want, frontier.pushed,
                      f"规则翻页必须落在终到主机 {want}；实际入队：{frontier.pushed}")
        self.assertNotIn(wrong, frontier.pushed,
                         f"规则翻页被拼到短链主机上（本 bug 的同形）：{wrong}")

    def test_no_redirect_behavior_unchanged(self):
        """**没有 302** 时行为与改前一致：相对链接按请求地址解析（真跑一遍）

        直接打真实页主机（不经过短链），此时"请求地址 == 终到地址"，
        出链必须与短链场景**同一条**——证明修复没在无重定向时改变行为。
        """
        session = _AsyncLoopbackSession()
        frontier = self._run_job(session, f"{self.real_base}/real")
        self.assertEqual(session.requested, [f"{self.real_base}/real"],
                         "无重定向时不得多发任何一跳")
        self.assertIn(f"{self.real_base}/video/BV1PSL96YEwp", frontier.pushed)
        self.assertNotIn("/seed", _SERVER_HITS,
                         "没走短链种子，短链服务不该被访问")


class TestSiteRulePaginationUsesFinalHost(unittest.TestCase):
    """规则层里的相对链接（item_link / 翻页 next_url）也必须按终到地址补全。

    这条**不是顺手改的**：`next_url` 会被 `_parse_page` 直接入队（规则翻页），
    短链种子下若补成 `https://b23.tv/page/2`，等于每次翻页都往一个不存在的
    地址上撞——与主 bug 完全同形。用临时规则文件驱动真实 `apply_rule`。
    """

    _YAML = """name: shortlink-demo
match: ["b23.tv", "www.bilibili.com"]
list:
  selector: "div.post"
  fields:
    title: {sel: "h2 a", text: true}
    item_link: {sel: "h2 a", attr: href}
pagination:
  next: {sel: "a.next", attr: href}
"""

    _HTML = ('<html><body>'
             '<div class="post"><h2><a href="/video/BV1AAA">标题一</a></h2></div>'
             '<a class="next" href="/list?p=2">下一</a>'
             '</body></html>')

    def _apply(self, url, base_url=None):
        import tempfile
        from pathlib import Path as _P
        from unittest import mock
        from kiana_vnext_plus import site_rules as sr
        with tempfile.TemporaryDirectory() as td:
            (_P(td) / "site.yaml").write_text(self._YAML, encoding="utf-8")
            with mock.patch.object(sr, "_loader", sr.RuleLoader(_P(td))):
                return sr.apply_rule(self._HTML, url, base_url)

    def test_default_unchanged(self):
        """不传 base_url → 仍按请求地址补全（行为与改前一致）"""
        res = self._apply(SEED)
        self.assertIsNotNone(res, "临时规则应命中 b23.tv（match 列表里有它）")
        self.assertEqual(res["list_items"][0]["item_link"], "https://b23.tv/video/BV1AAA")
        self.assertEqual(res["next_url"], "https://b23.tv/list?p=2")

    def test_base_url_resolves_against_final_host(self):
        res = self._apply(SEED, FINAL)
        self.assertIsNotNone(res, "临时规则应命中 b23.tv（match 列表里有它）")
        self.assertEqual(res["list_items"][0]["item_link"],
                         "https://www.bilibili.com/video/BV1AAA",
                         "item_link 必须按终到地址补全")
        self.assertEqual(res["next_url"], "https://www.bilibili.com/list?p=2",
                         "翻页 next_url 必须按终到地址补全（否则每次翻页都 404）")


if __name__ == "__main__":
    unittest.main()
