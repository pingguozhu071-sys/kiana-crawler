# -*- coding: utf-8 -*-
"""媒体流分片**不许当页面抓、也不许当独立视频下** —— 真机日志驱动的回归

## 真机病象（用户 2026-10-03 那趟 2 条 b23.tv 种子 `depth=10 max=30` / 288 秒）

结果本身是好的（cookies 生效、146MB/78.9MB 的视频都拿到了），但日志里反复刷：

```
yt-dlp 未产出文件: https://xy61x164x142x12xy.mcdn.bilivideo.cn:8082/v1/resource/
                   upgcxcode/45/35/36062563545/...
ERROR: [generic] 36062563545-1-30032: Unable to download webpage: HTTP Error 403
ERROR: [generic] 36062563545-1-30016: Unable to download webpage: HTTP Error 403
Video download failed: Direct download failed
```

## 追出来的完整链路（不是猜的）

```
page_processor._enqueue_and_render          ← B站视频页被处理
  └ video_resolver.resolve_bilibili_video   ← 返回 dash.video[].baseUrl
      └ frontier.add_video_download         ← **每一条轨道分片各入一次队**
          └ video_downloads 表（status=pending）
              └ crawler._video_download_worker → _download_one_video
                  └ universal_downloader.download_video（yt-dlp，generic extractor）
                      → HTTP 403（签名短时效 + 没带主站 Referer）
                  └ 直连兜底 media_downloader.download_direct → 也 403
                      → "Video download failed: Direct download failed"
```

**为什么必然 403**：这些是**时间签名**的一次性地址（`deadline=`），而下载 worker 是
5 秒轮询 + 排队后才取；且 yt-dlp 走 generic extractor，工程给它的 `http_headers`
只有 UA、**没有主站 Referer**（B站 CDN 校验 Referer）。

**为什么不下载也没损失**：同一个视频的**页面 URL 早就在上面几行入队了**，
yt-dlp 自带 B站 extractor 会自己挑轨、自己带 Referer/cookies ——
逐条入队等于把同一视频按轨道数重复下 N 遍。

## 本文件守的三道防线

| 防线 | 实现 | 挡什么 |
|---|---|---|
| ① 判据 | `url_utils.is_media_stream_url` | 定义"什么算媒体流分片"（**形态判定，不是域名黑名单**） |
| ② 页面队列 | `frontier.push` / `redis_frontier.push` | 分片**结构上不可能**变成页面任务 |
| ③ 下载队列 | `page_processor._enqueue_and_render` / `crawler._seed` | 分片不会变成一条 yt-dlp 任务 |

⚠️ **反向断言和正向断言一样重要**：真实视频页、图片 CDN、`.mp4`/`.ts`/`.m3u8`
直链（"给一个直链就直接下"是本工程明确支持的能力）**必须全部放行** ——
误杀一个真视频的代价（东西永远下不到）远大于多试一次。
"""
import ast
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PKG = ROOT / "kiana_vnext_plus"

# ── 真机日志里逐字抄下来的 URL（`[真机]` 标注）────────────────────────────
# 只截断尾部（日志本身也是 `...` 截断的），主机、端口、路径形态保持原样。
LOG_CDN_SEGMENTS = (
    # [真机] xy61x…mcdn.bilivideo.cn:8082 —— 注意是 **.cn**，不是 .com
    "https://xy61x164x142x12xy.mcdn.bilivideo.cn:8082/v1/resource/upgcxcode/45/35/"
    "36062563545/36062563545-1-30032.m4s?deadline=1759500000&uipk=5",
    # [真机] upos-sz-mirrorcoso1.bilivideo.com
    "https://upos-sz-mirrorcoso1.bilivideo.com/upgcxcode/45/35/36062563545/"
    "36062563545-1-30016.m4s?deadline=1759500000&uipk=5",
    "https://upos-sz-mirrorcoso1.bilivideo.com/upgcxcode/45/35/36062563545/"
    "36062563545-1-30011.m4s?deadline=1759500000&uipk=5",
)
# 真机里真实拿到了的东西（必须继续能用）
REAL_VIDEO_PAGE = "https://www.bilibili.com/video/BV1obZjBSEpT"
REAL_SHORTLINK = "https://b23.tv/ybyASFu"


def _fn_body(mod_name: str, fn_name: str) -> str:
    """取某个（异步）函数的源码文本。`FunctionDef`/`AsyncFunctionDef` 都要认。"""
    src = (PKG / mod_name).read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == fn_name), None)
    if fn is None:
        raise AssertionError(f"找不到函数 {fn_name}（{mod_name}）")
    return ast.unparse(fn)


def _parse_fn(mod_name: str, fn_name: str) -> ast.AST:
    """取函数的 **AST 节点** —— 结构断言必须走 AST。

    ⚠️ 本文件第一版用的是"`ast.unparse` 出来的文本里有没有这个名字"，
    对抗性验证当场抓到**两处假绿**：
      · `redis_frontier.push` 的 **docstring** 里提到了判据名字；
      · `crawler.run` 里 `from .url_utils import … as _is_stream_seg` 这行 **import**
        本身就是函数体内的真实语句，拆掉守卫它还在。
    这正是本工程点过名的"**拿文本当结构**"：数出现次数 ≠ 检查它长在哪。
    """
    src = (PKG / mod_name).read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == fn_name), None)
    if fn is None:
        raise AssertionError(f"找不到函数 {fn_name}（{mod_name}）")
    return fn


def _calls_predicate(node) -> bool:
    """`node` 里有没有对媒体分片判据的**调用**（只认调用 —— 注释/字符串/import 都不算）。"""
    suffixes = ("is_media_stream_url", "_is_stream_seg")
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            if any(n.func.id.endswith(s) for s in suffixes):
                return True
    return False


def _stmt_lists(node):
    """产出 `node` 内部所有的**语句列表**（含嵌套块）——用于找"同一个块里的兄弟语句"。"""
    for _field, value in ast.iter_fields(node):
        if isinstance(value, list) and value and all(isinstance(v, ast.stmt) for v in value):
            yield value
        if isinstance(value, ast.AST):
            yield from _stmt_lists(value)
        elif isinstance(value, list):
            for v in value:
                if isinstance(v, ast.AST):
                    yield from _stmt_lists(v)


# ══════════════════════════════════════════════════════════════════════════
# 防线①：判据本身
# ══════════════════════════════════════════════════════════════════════════
class TestPredicate(unittest.TestCase):
    def setUp(self):
        from kiana_vnext_plus.url_utils import is_media_stream_url
        self.f = is_media_stream_url

    def test_real_log_fragments_are_recognised(self):
        for u in LOG_CDN_SEGMENTS:
            self.assertTrue(self.f(u), f"真机分片没被识别: {u}")

    def test_fragment_shapes_without_known_hosts(self):
        """判据是**形态**，不是域名 —— 换任何主机都得成立（黑名单会漂）"""
        for u in ("https://unknown-cdn-2030.example/seg/a.m4s",
                  "https://a.b.c/x/y-1-2.m4s?token=zzz",
                  "http://1.2.3.4:8080/v/init.cmfv",
                  "https://x/y.cmfa", "https://x/y.cmft"):
            self.assertTrue(self.f(u), f"分片形态没被识别: {u}")

    # ── 反向断言：防误伤（这一组和上面一样重要）──────────────────────────
    def test_real_video_page_survives(self):
        """真实视频页**必须放行** —— 误杀它等于这个视频永远下不到"""
        for u in (REAL_VIDEO_PAGE, REAL_SHORTLINK,
                  "https://www.bilibili.com/bangumi/play/ep5127650",
                  "https://www.youtube.com/watch?v=aqz-KE-bpKQ",
                  "https://v.douyin.com/iABCdef/",
                  "https://mp.weixin.qq.com/s/abc"):
            self.assertFalse(self.f(u), f"真实页面被误杀: {u}")

    def test_image_cdn_survives(self):
        """该抓的图片必须放行（图片走的是图片通道，本来也不该被这条判据碰到）"""
        for u in ("https://i0.hdslb.com/bfs/archive/abc123.jpg",
                  "https://p3-sign.douyinpic.com/tos-cn-i-0813/xyz~tplv.jpeg",
                  "https://mmbiz.qpic.cn/mmbiz_png/abc/640?wx_fmt=png",
                  "https://i0.hdslb.com/bfs/face/abc.png@100w_100h.webp"):
            self.assertFalse(self.f(u), f"图片 CDN 被误杀: {u}")

    def test_direct_media_links_survive(self):
        """**"给一个直链就直接下"是本工程明确支持的能力**（crawler._seed 媒体直链分支、
        m3u8_downloader）—— `.mp4`/`.ts`/`.m3u8`/无后缀 一律不许判成分片。"""
        for u in ("https://cdn.example.com/movie.mp4",
                  "https://cdn.example.com/a/b/c.mkv",
                  "https://cdn.example.com/hls/index.m3u8",
                  "https://cdn.example.com/hls/seg-1.ts",
                  "https://cdn.example.com/audio/song.flac",
                  # 无后缀的 CDN 资源路径：缺证据，**宁可放过**
                  "https://b-edge.mountaintoys.cn:4483/x/y",
                  "https://xy61x164x142x12xy.mcdn.bilivideo.cn:8082/v1/resource/upgcxcode/45/35/36062563545/init"):
            self.assertFalse(self.f(u), f"合法直链被误杀: {u}")

    def test_degenerate_input_is_safe(self):
        """退化输入不许抛，一律当"不是"（宁可放过）"""
        for u in ("", None, "not a url", "://", "b23.tv", "ftp://x/a.m4s",
                  "file:///c:/a.m4s", 123):
            self.assertFalse(self.f(u), f"退化输入处理不对: {u!r}")

    def test_query_string_does_not_confuse_it(self):
        """带 query 的分片照样认；`.m4s` 只出现在 query 里的**页面**不该被误判"""
        self.assertTrue(self.f("https://x/y.m4s?a=1&b=2"))
        self.assertFalse(self.f("https://www.bilibili.com/video/BV1x?from=m4s"))


# ══════════════════════════════════════════════════════════════════════════
# 防线②：页面队列**结构上**拒收（真机那条 `skip` 计数就是这么被挡住的）
# ══════════════════════════════════════════════════════════════════════════
class TestPageQueueRejects(unittest.TestCase):
    def test_frontier_push_drops_fragments(self):
        """SQLite 后端：分片进不了 frontier 表 —— 于是**永远不会被 pop 出来**，
        也就永远不会出现在 `skip`（页面配额跳过）这类页面计数里。"""
        import shutil
        from kiana_vnext_plus.frontier import FrontierDB

        async def flow():
            td = tempfile.mkdtemp(prefix="kiana_seg_")
            try:
                f = FrontierDB(str(Path(td) / "f.db"))
                await f.init_async()
                try:
                    for u in LOG_CDN_SEGMENTS:
                        await f.push(u)
                    await f.push(REAL_VIDEO_PAGE)
                    await f.flush()
                    return await f.get_counts()
                finally:
                    await f.close()
            finally:
                # Windows 下 aiosqlite 句柄释放略滞后于 close() 返回（既有用例同此处理）
                shutil.rmtree(td, ignore_errors=True)

        counts = asyncio.run(flow())
        self.assertEqual(counts.get("pending", 0), 1,
                         f"页面队列里只该有那条真视频页，实际 {counts}")
        self.assertNotIn("dead", counts)

    def test_both_backends_have_the_gate(self):
        """两个 frontier 后端都要有闸：只在 SQLite 里加，Redis 直连路径照样漏。

        **必须是真的 `if <判据>(): return`** —— 文档里提一句判据名字不算数
        （本文件第一版就是这样假绿的：`redis_frontier.push` 的 docstring 里写着
        `is_media_stream_url`，于是拆掉守卫测试照样通过）。
        """
        for mod in ("frontier.py", "redis_frontier.py"):
            fn = _parse_fn(mod, "push")
            ok = any(isinstance(n, ast.If) and _calls_predicate(n.test)
                     and any(isinstance(b, ast.Return) for b in n.body)
                     for n in ast.walk(fn))
            self.assertTrue(
                ok, f"{mod}.push 上没有**真的**媒体分片闸"
                    f"（必须是 `if 判据(...): return`，注释里提一句不算）")


# ══════════════════════════════════════════════════════════════════════════
# 防线③：下载队列也不收分片（真机那些 403 就是从这条路来的）
# ══════════════════════════════════════════════════════════════════════════
class _FakeFrontier:
    """只记 `add_video_download` 的调用（页面队列在本用例里不参与）。"""

    def __init__(self):
        self.video_enqueues = []

    async def add_video_download(self, video_url, domain):
        self.video_enqueues.append((video_url, domain))


class _FakeCrawler:
    """`PageProcessor.__init__` 会读这几个属性 —— 给全，才能真走它的构造。"""

    def __init__(self, frontier):
        self.frontier = frontier
        self._dl_video = True
        self._dl_image = False          # 不装 downloader，图片分支自然跳过
        self.project = None
        self.cfg = None
        self.exit_mgr = None
        self.router = None
        self.adaptive = None


def _processor(frontier):
    """构造一个**真的** PageProcessor（不绕过 __init__，接线才作数）。"""
    from kiana_vnext_plus.page_processor import PageProcessor
    return PageProcessor(_FakeCrawler(frontier))


class TestVideoEnqueueRejects(unittest.TestCase):
    def test_page_embedded_fragments_are_not_enqueued(self):
        """页面里的 `<video src>` / `og:video` 指向分片时不许入下载队列；
        **同一批里的完整 `.mp4` 直链必须照旧入队**（反向断言）。"""
        fr = _FakeFrontier()
        pp = _processor(fr)
        data = {"videos": [LOG_CDN_SEGMENTS[0], LOG_CDN_SEGMENTS[1],
                           "https://cdn.example.com/full.mp4"]}
        asyncio.run(pp._enqueue_and_render(data, "<html></html>",
                                           "https://news.example.com/article/1",
                                           "news.example.com"))
        got = [u for u, _ in fr.video_enqueues]
        self.assertEqual(got, ["https://cdn.example.com/full.mp4"],
                         f"分片混进了下载队列 / 或完整直链被误杀: {got}")

    def test_bilibili_resolver_fragments_are_not_enqueued(self):
        """**真机那条主链路**：B站视频页 → resolver 返回 8 条 DASH 轨道 →
        改前会逐条入队（每条必然 403）。现在一条都不许进。"""
        import json
        from kiana_vnext_plus import video_resolver as vr

        # resolver 的**真实**返回形态：support_formats（无 url）+ dash.video（带 url）+ dash.audio
        fake_streams = (
            [{"quality": 127, "desc": "8K 超高清", "format": "flv"}]
            + [{"url": u, "quality": q, "type": "video", "codecs": "avc1"}
               for q, u in ((116, LOG_CDN_SEGMENTS[0]), (80, LOG_CDN_SEGMENTS[1]),
                            (64, LOG_CDN_SEGMENTS[2]))]
            + [{"url": "https://upos-sz-mirrorcoso1.bilivideo.com/x/a.m4s",
                "quality": 30280, "type": "audio"}]
        )
        _orig = vr.resolve_bilibili_video
        vr.resolve_bilibili_video = lambda *a, **k: _async(fake_streams)
        try:
            fr = _FakeFrontier()
            pp = _processor(fr)
            html = ("<html><script>window.__INITIAL_STATE__ = "
                    + json.dumps({"videoData": {"title": "T", "cid": 111}})
                    + ";</script></html>")
            asyncio.run(pp._enqueue_and_render({"videos": []}, html,
                                               REAL_VIDEO_PAGE, "www.bilibili.com"))
        finally:
            vr.resolve_bilibili_video = _orig

        got = [u for u, _ in fr.video_enqueues]
        self.assertEqual(got, [REAL_VIDEO_PAGE],
                         f"B站轨道分片被当成了独立下载任务（页面 URL 应恰好入队一次）: {got}")

    def test_seed_path_rejects_fragments(self):
        """种子路径：分片种子**不入页面队列也不入下载队列**（否则它会被当页面抓一轮）。

        种子分流写在 `Crawler.run` 里（不是独立函数），所以要在 `run` 的 AST 里定位：
        **紧跟在 `if is_m3u8 or is_media:` 之后的同级语句**里必须有一个分片守卫，
        且守卫体内是 `continue`（= 哪个队列都不入）。

        ⚠️ 第一版写的是"`run` 的解码文本里有没有 `is_media_stream_url`"——
        对抗性验证当场抓到假绿：`from .url_utils import … as _is_stream_seg` 这行
        **import 本身就是 `run` 里的真实语句**，拆掉守卫它还在。
        """
        fn = _parse_fn("crawler.py", "run")
        found = False
        for lst in _stmt_lists(fn):
            for i, st in enumerate(lst):
                if not (isinstance(st, ast.If) and "is_m3u8 or is_media" in ast.unparse(st.test)):
                    continue
                for nxt in lst[i + 1:]:
                    if (isinstance(nxt, ast.If) and _calls_predicate(nxt.test)
                            and any(isinstance(b, ast.Continue) for b in nxt.body)):
                        found = True
                break
        self.assertTrue(
            found,
            "种子路径没有分片闸——分片种子会掉进最后那条 frontier.push 变成页面任务")


class TestRealFrontierEndState(unittest.TestCase):
    """**真表**结局：分片在两张表里都不留痕。

    这一条直接对着验收口径写：**那类 URL 不该出现在页面抓取的计数里**。
    页面计数（`done` / `fail` / `skip`）全部来自 `frontier` 表被 pop 出来的任务 ——
    分片既然进不了 `frontier`，就永远不会被 pop，也就永远不会让 `skip` 涨一下。
    """

    def test_fragments_reach_neither_table(self):
        import json
        import shutil
        import sqlite3
        from kiana_vnext_plus import video_resolver as vr
        from kiana_vnext_plus.frontier import FrontierDB

        fake_streams = (
            [{"quality": 127, "desc": "8K 超高清", "format": "flv"}]
            + [{"url": u, "quality": q, "type": "video", "codecs": "avc1"}
               for q, u in ((116, LOG_CDN_SEGMENTS[0]), (80, LOG_CDN_SEGMENTS[1]))]
        )
        td = tempfile.mkdtemp(prefix="kiana_segend_")
        db_path = str(Path(td) / "f.db")

        async def flow():
            f = FrontierDB(db_path)
            await f.init_async()
            _orig = vr.resolve_bilibili_video
            vr.resolve_bilibili_video = lambda *a, **k: _async(fake_streams)
            try:
                pp = _processor(f)                      # 真 PageProcessor + 真 FrontierDB
                html = ("<html><script>window.__INITIAL_STATE__ = "
                        + json.dumps({"videoData": {"title": "T", "cid": 111}})
                        + ";</script></html>")
                await pp._enqueue_and_render({"videos": []}, html,
                                             REAL_VIDEO_PAGE, "www.bilibili.com")
                # 顺带把分片也往页面队列里塞一次（模拟"别的路径漏了闸"），
                # 再塞一条真页面做对照（真实运行里它由种子路径入队）
                for u in LOG_CDN_SEGMENTS:
                    await f.push(u)
                await f.push(REAL_VIDEO_PAGE)
                await f.flush()
            finally:
                vr.resolve_bilibili_video = _orig
                await f.close()

        asyncio.run(flow())
        conn = sqlite3.connect(db_path)
        try:
            vids = [r[0] for r in conn.execute(
                "SELECT video_url FROM video_downloads").fetchall()]
            n_frontier = conn.execute("SELECT COUNT(*) FROM frontier").fetchone()[0]
        finally:
            conn.close()
        shutil.rmtree(td, ignore_errors=True)

        self.assertEqual(vids, [REAL_VIDEO_PAGE],
                         f"下载表里应只有页面 URL（真机那些分片一条都不许进）: {vids}")
        self.assertFalse([v for v in vids if v.endswith(".m4s")],
                         "分片进了 video_downloads —— 那正是 403 空转的来源")
        self.assertEqual(n_frontier, 1,
                         f"页面队列表里只该有真页面（分片进得去就会让 skip 计数涨）: {n_frontier}")


def _async(value):
    async def _c():
        return value
    return _c()


if __name__ == "__main__":
    unittest.main()
