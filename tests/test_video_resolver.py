# -*- coding: utf-8 -*-
"""B站视频信息提取测试（`video_resolver.py` 此前覆盖率仅 20%）

该模块的 `resolve_bilibili_video` 需要联网，但 **`extract_bilibili_info` 是纯函数**
（正则 + 手写括号平衡扫描 + `json.loads`），完全可离线测——而"低覆盖的纯逻辑"
正是上一轮定下的目标。

本轮从中修掉一处**死代码**：`epid_match` 赋值后从未使用（ruff F841 一直挂着）。
它是"想过支持番剧、但没做完"的痕迹——番剧标准链接 `/bangumi/play/ep<数字>`
**不含 BV**，于是 bvid 为空、下游直接放弃；而 docstring 却声称"兼容 bangumi 页面"。
现改为把 epid 提取进返回值，并删掉那个不生效的变量。
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kiana_vnext_plus.video_resolver import (      # noqa: E402
    extract_bilibili_info, resolve_bilibili_video,
)


def _page(state) -> str:
    return f"<html><script>window.__INITIAL_STATE__ = {json.dumps(state)};</script></html>"


class TestBvidExtraction(unittest.TestCase):
    def test_from_video_url(self):
        got = extract_bilibili_info("<html></html>", "https://www.bilibili.com/video/BV1xx411c7mD")
        self.assertEqual(got["bvid"], "BV1xx411c7mD")

    def test_from_bangumi_url_with_bv(self):
        got = extract_bilibili_info("<html></html>",
                                    "https://www.bilibili.com/bangumi/play/BV1xx411c7mD")
        self.assertEqual(got["bvid"], "BV1xx411c7mD")

    def test_from_query_param(self):
        got = extract_bilibili_info("<html></html>",
                                    "https://www.bilibili.com/x?bvid=BV1xx411c7mD")
        self.assertEqual(got["bvid"], "BV1xx411c7mD")

    def test_no_bvid(self):
        got = extract_bilibili_info("<html></html>", "https://www.bilibili.com/")
        self.assertEqual(got["bvid"], "")


class TestEpidExtraction(unittest.TestCase):
    """本轮修复：番剧标准链接只带 epid"""

    def test_epid_is_surfaced(self):
        got = extract_bilibili_info("<html></html>",
                                    "https://www.bilibili.com/bangumi/play/ep123456")
        self.assertEqual(got.get("epid"), "123456",
                         "番剧标准链接的 epid 必须被提取出来（否则调用方无从下手）")

    def test_epid_only_url_has_no_bvid(self):
        """如实记录边界：epid 链接确实没有 bvid——**这是数据事实，不是提取失败**"""
        got = extract_bilibili_info("<html></html>",
                                    "https://www.bilibili.com/bangumi/play/ep123456")
        self.assertEqual(got["bvid"], "")

    def test_non_bangumi_url_has_no_epid(self):
        got = extract_bilibili_info("<html></html>", "https://www.bilibili.com/video/BV1xx411c7mD")
        self.assertNotIn("epid", got)


class TestInitialStateParsing(unittest.TestCase):
    def test_title_cid_pages(self):
        html = _page({"videoData": {"title": "标题", "cid": 111,
                                    "pages": [{"part": "P1", "cid": 111},
                                              {"part": "P2", "cid": 222}]}})
        got = extract_bilibili_info(html, "https://www.bilibili.com/video/BV1xx411c7mD")
        self.assertEqual(got["title"], "标题")
        self.assertEqual(got["cid"], 111)
        self.assertEqual(len(got["pages"]), 2)
        self.assertEqual(got["pages"][1], {"part": "P2", "cid": 222})

    def test_cid_falls_back_to_first_page(self):
        html = _page({"videoData": {"title": "T", "pages": [{"part": "P1", "cid": 777}]}})
        got = extract_bilibili_info(html, "https://www.bilibili.com/video/BV1xx411c7mD")
        self.assertEqual(got["cid"], 777, "主 cid 缺失时应回退到第一页的 cid")

    def test_empty_pages_does_not_lose_title(self):
        """空 pages 会让内部索引抛错——但**不能连 title 一起丢掉**"""
        html = _page({"videoData": {"title": "T", "pages": []}})
        got = extract_bilibili_info(html, "https://www.bilibili.com/video/BV1xx411c7mD")
        self.assertEqual(got["title"], "T")
        self.assertEqual(got["pages"], [])

    def test_json_with_brace_inside_string_still_parses(self):
        """**v2.18 P2-12 的回归钉**：手写括号扫描必须字符串/转义感知。

        原实现用 `(\\{.*\\});` 贪婪正则，会捕到全页最后一个 `};`
        （脚本拼接处）→ `json.loads` 必失败 → **B站流解析静默丢弃**。
        这里让 title 里同时含 `}` 与转义引号：
        """
        html = _page({"videoData": {"title": 'a}b"c', "cid": 5, "pages": []}})
        got = extract_bilibili_info(html, "https://www.bilibili.com/video/BV1xx411c7mD")
        self.assertEqual(got["title"], 'a}b"c')
        self.assertEqual(got["cid"], 5)

    def test_malformed_json_is_swallowed(self):
        html = "<html><script>window.__INITIAL_STATE__ = {not json};</script></html>"
        got = extract_bilibili_info(html, "https://www.bilibili.com/video/BV1xx411c7mD")
        self.assertEqual(got["title"], "", "解析失败应静默降级，不抛异常")
        self.assertEqual(got["bvid"], "BV1xx411c7mD", "bvid 来自 URL，不该受影响")

    def test_missing_initial_state(self):
        got = extract_bilibili_info("<html>nothing here</html>",
                                    "https://www.bilibili.com/video/BV1xx411c7mD")
        self.assertEqual(got["cid"], 0)
        self.assertEqual(got["pages"], [])


class TestResolveEarlyReturn(unittest.TestCase):
    """离线可测的部分：缺 bvid/cid 时必须**不发请求**直接返回空"""

    def test_empty_bvid_returns_immediately(self):
        self.assertEqual(__import__("asyncio").run(
            resolve_bilibili_video("", 123)), [])

    def test_empty_cid_returns_immediately(self):
        self.assertEqual(__import__("asyncio").run(
            resolve_bilibili_video("BV1xx411c7mD", 0)), [])


if __name__ == "__main__":
    unittest.main()
