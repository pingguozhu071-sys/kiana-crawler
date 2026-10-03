# -*- coding: utf-8 -*-
"""R6：短链误解析造出的假 URL —— 两道防线

## 真机病象（用户那次 8 个 B站种子的抓取）

日志里反复出现：

```
discarding data: https://b23.tv/video/BV1phhHz8EPF?amp%3Btrackid=web_related_...
静态解析为空/占位页 → 通用浏览器渲染兜底（JS 渲染站点: https://b23.tv/video/BV1phhHz8EPF?...）
B站视频入队: https://b23.tv/video/BV1phhHz8EPF?amp%3Btrackid=web_related_
[download] 100.0% of 46.00B
Video downloaded: ...2aa15d2c30bc.unknown_video (46 bytes)
```

**`b23.tv` 是短链服务，它的路径只能是一段短码**（`b23.tv/ybyASFu`）——
`b23.tv/video/BVxxx` 这种"短链主机 + 站点路径"的组合**在真实世界里不存在**。

根因是**解析基址**：页面真实落点是 `www.bilibili.com/video/BVxxx`，
但短链种子进来时基址退回了 `b23.tv/xxx`，于是页面里的相对链接
（`/video/BVyyy`）被拼成了 `b23.tv/video/BVyyy`。

**代价**（每条假 URL 都要付）：
1. 当成**页面任务**去抓一次 → 失败 → 再**白起一次浏览器**渲染兜底；
2. 当成**视频任务**交给 yt-dlp → 它**不报错**，存下 **46 字节的 HTML 错误页** →
   被记成 `Video downloaded` → 误标 completed → 用户目录里堆垃圾文件。

## 本文件守的两道防线

| 防线 | 实现 | 挡什么 |
|---|---|---|
| ① 地址本身是错的 | `url_utils.is_shortener_misresolution` | 压根不让它进队列 |
| ② 产物不是媒体 | `universal_downloader.video_artifact_problem` | 就算漏过去也**删掉垃圾**、继续换档 |

防线 ① 在**页面入队**与**视频入队**两个入口都要装上 —— 少装一个，
另一个入口照样把假 URL 放进去。
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PKG = ROOT / "kiana_vnext_plus"


class TestShortenerMisresolution(unittest.TestCase):
    """防线①：短链主机 + 站点路径 = 必然不存在的 URL"""

    def setUp(self):
        from kiana_vnext_plus.url_utils import is_shortener_misresolution
        self.bad = is_shortener_misresolution

    def test_real_bad_urls_are_caught(self):
        """真机抓到的那些必须被拦（`[真机]` 标注的是日志里逐字抄下来的）"""
        for u in ("https://b23.tv/video/BV1hJGL6ZE31?amp%3Btrackid=web_related_0.router-related",
                  "https://b23.tv/video/BV1phhHz8EPF?amp%3Btrackid=web_related_",
                  "https://b23.tv/space/12345"):
            self.assertTrue(self.bad(u), f"真机坏地址没被拦: {u}")

    def test_normal_short_links_survive(self):
        """**不许误杀真短链** —— 误杀一个真视频比多试一次代价大得多"""
        for u in ("https://b23.tv/ybyASFu", "https://b23.tv/izsrwgd",
                  "https://b23.tv/n2vFgzi", "https://v.douyin.com/iABCdef/"):
            self.assertFalse(self.bad(u), f"真短链被误杀: {u}")

    def test_normal_sites_survive(self):
        """正常站点（含"看着像短链"的合法主机）不许误杀"""
        for u in ("https://www.bilibili.com/video/BV1obZjBSEpT",
                  "https://space.bilibili.com/177291194",
                  # ← 真机日志里有这条，它是**合法主机**，绝不能拦
                  "https://player.bilibili.com/player.html?bvid=BV1as411f7Y3",
                  "https://mp.weixin.qq.com/s/abc",
                  "https://www.bilibili.com/blackboard/era/xxx.html"):
            self.assertFalse(self.bad(u), f"正常站点被误杀: {u}")

    def test_degenerate_inputs_are_safe(self):
        """空串/怪输入不许抛，一律当"不是"（宁可放过）"""
        for u in ("", "not a url", "://", "b23.tv"):
            self.assertFalse(self.bad(u), f"退化输入处理不对: {u!r}")

    def test_dotted_single_segment_is_caught(self):
        """构造用例：短码不含点号，`player.html` 这种像文件名的必是站点路径"""
        self.assertTrue(self.bad("https://b23.tv/player.html?bvid=BV1as411f7Y3"))


class TestVideoArtifactCheck(unittest.TestCase):
    """防线②：产物得像视频（挡 46 字节错误页）"""

    def setUp(self):
        from kiana_vnext_plus.universal_downloader import video_artifact_problem
        self.chk = video_artifact_problem
        self.dir = Path(tempfile.mkdtemp())

    def _mk(self, name, size):
        p = self.dir / name
        p.write_bytes(b"x" * size)
        return p

    def test_46_byte_error_page_is_rejected(self):
        """**真机的 46 字节**是这条防线的由来"""
        r = self.chk(self._mk("junk.unknown_video", 46))
        self.assertTrue(r, "46 字节的错误页没被拦")
        self.assertIn("46", r, "报错里没写出实际大小，排查时看不出问题")

    def test_real_video_passes(self):
        self.assertEqual(self.chk(self._mk("ok.mp4", 200_000)), "",
                         "正常视频被误拦")

    def test_non_media_suffix_rejected_even_if_large(self):
        """大文件但扩展名说"没认出格式" → 也不收（它可能是别的东西）"""
        self.assertIn("unknown_video", self.chk(self._mk("big.unknown_video", 200_000)))

    def test_missing_file_rejected(self):
        self.assertTrue(self.chk(self.dir / "nope.mp4"))

    def test_tiny_media_file_rejected(self):
        self.assertTrue(self.chk(self._mk("tiny.mp4", 100)))


class TestBothGuardsAreWired(unittest.TestCase):
    """两道防线必须装进**真实的入队路径** —— 只写函数不接线等于没做。

    ⚠️ 本类**第一版是假绿的**：只用 `"is_shortener_misresolution" in src` 判"接线了"，
    但那个字符串在文件里**别处也有**（视频入队分支、import 行）——
    于是**拆掉页面入队那道防线，测试照样全绿**（反向验证当场抓到）。
    这正是本工程反复吃亏的"**拿文本当结构**"：数出现次数 ≠ 检查它长在哪。
    现改为 **AST 结构化断言**：必须出现在 `_extract_links` 的**过滤条件**里。
    """

    def test_page_link_filter_uses_guard(self):
        """页面入队：`_extract_links` 的**过滤条件**里必须有它（不是"文件里出现过"）"""
        import ast
        src = (PKG / "page_processor.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "_extract_links"), None)
        self.assertIsNotNone(fn, "找不到 _extract_links")
        body = ast.unparse(fn)
        # 必须出现在那条 `links = [l for l in raw_links if ...]` 里
        self.assertIn("is_shortener_misresolution", body,
                      "页面入队的过滤条件里没有防线①（假 URL 会变成页面任务）")
        # 再收紧一层：它得是 ListComp 的某个 if 的一部分
        ok = False
        for node in ast.walk(fn):
            if isinstance(node, ast.ListComp):
                conds = [ast.unparse(g) for g in node.generators for g in g.ifs]
                if any("is_shortener_misresolution" in c for c in conds):
                    ok = True
        self.assertTrue(ok, "防线① 不在 links 列表推导的 if 条件里 —— 接了也不算数")

    def test_video_enqueue_uses_guard(self):
        """视频入队：B站分支里必须有它（否则假 URL 照样进 yt-dlp）"""
        src = (PKG / "page_processor.py").read_text(encoding="utf-8")
        idx = src.find("add_video_download(url, domain)")
        self.assertGreater(idx, 0, "找不到视频入队点")
        window = src[max(0, idx - 1200):idx]
        self.assertIn("is_shortener_misresolution", window,
                      "视频入队没装防线①（假 URL 会直达 yt-dlp）")

    @staticmethod
    def _fn_body(src, name):
        """取某个函数的源码文本（`ast.unparse`）。

        ⚠️ 必须**同时**认 `FunctionDef` 与 `AsyncFunctionDef` ——
        本文件第一版只找前者，而 `download_video` 是 **async** 的，
        于是 `next()` 返回 None、测试直接失败（不是假绿，但同样是我漏了节点类型）。
        """
        import ast
        tree = ast.parse(src)
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name == name), None)
        if fn is None:
            raise AssertionError(f"找不到函数 {name}（FunctionDef/AsyncFunctionDef 都找过了）")
        return ast.unparse(fn)

    def test_downloader_uses_artifact_check(self):
        """下载完成处：两个 return 点都要过防线②"""
        body = self._fn_body((PKG / "universal_downloader.py").read_text(encoding="utf-8"),
                             "download_video")
        self.assertGreaterEqual(body.count("video_artifact_problem(p)"), 2,
                                "download_video 的两个成功返回点没都装防线②")

    def test_garbage_is_deleted_not_just_skipped(self):
        """拦下之后要**删掉文件** —— 只 skip 不删，垃圾照样留在用户目录里"""
        body = self._fn_body((PKG / "universal_downloader.py").read_text(encoding="utf-8"),
                             "download_video")
        self.assertGreaterEqual(body.count("p.unlink(missing_ok=True)"), 2,
                                "拦下垃圾后没有删除（两处都要删）——用户目录还是会被污染")


if __name__ == "__main__":
    unittest.main()
