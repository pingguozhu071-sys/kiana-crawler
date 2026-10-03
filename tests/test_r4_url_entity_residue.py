# -*- coding: utf-8 -*-
"""R4：URL 里的 HTML 实体 / 编码残渣必须清掉 —— **否则同一个视频会被当成两个键**

## 真机证据（用户 2026-10-03 GUI 抓取）

```
B站视频入队: https://www.bilibili.com/video/BV1PSL96YEwp?amp%3Btrackid=we
                                                      ↑ amp%3B
```
`amp%3B` 是 `&amp;` 被砍掉首字符、`;` 又被百分号编码成 `%3B` 的产物。
这类 URL **能下**（多个无用参数），但会被当成**独立的下载键** ——
同一个视频可能因此入队两次、或与规范 URL 各下一份
（本工程已经吃过"同视频下两遍、白耗 82MB"的亏）。

## 收口位置

入队点有**三处**（`crawler` 种子路径 + `page_processor` 两条），
按"**单一实现**"纪律清在**唯一的写库入口** `FrontierDB.add_video_download`，
而不是三个调用点各写一遍。清洗函数本体在 `url_utils.clean_url_entity_residue`。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class TestResidueCleaner(unittest.TestCase):
    """清洗函数本身（纯函数，快）"""

    def _f(self, u):
        from kiana_vnext_plus.url_utils import clean_url_entity_residue
        return clean_url_entity_residue(u)

    def test_real_machine_case(self):
        """真机日志里那条，必须清成干净 URL"""
        self.assertEqual(
            self._f("https://www.bilibili.com/video/BV1PSL96YEwp?amp%3Btrackid=we"),
            "https://www.bilibili.com/video/BV1PSL96YEwp?trackid=we")

    def test_entity_forms(self):
        for src, want in (
            ("https://x.com/a?b=1&amp;c=2", "https://x.com/a?b=1&c=2"),
            ("https://x.com/a?amp;c=2", "https://x.com/a?c=2"),
            ("https://x.com/a?b=1&#38;c=2", "https://x.com/a?b=1&c=2"),
            ("https://x.com/a?b=1&#x26;c=2", "https://x.com/a?b=1&c=2"),
        ):
            self.assertEqual(self._f(src), want, f"{src} 没被清对")

    def test_does_not_touch_legitimate_urls(self):
        """**不许误伤** —— 这条比"能清对"更重要"""
        for u in (
            "https://x.com/a?b=1&c=2",                       # 正常
            "https://x.com/amp;weird/path",                  # 路径里的 amp;（不在查询串）
            "https://x.com/a?q=%E4%B8%AD%20x&z=1",           # 其它百分号编码
            "https://cdn.com/v?sign=abc%3D&t=123",           # 签名参数（下载钥匙）
        ):
            self.assertEqual(self._f(u), u, f"{u} 被误改了")

    def test_empty_and_none_safe(self):
        self.assertEqual(self._f(""), "")
        self.assertIsNone(self._f(None))


class TestFrontierCleansOnEnqueue(unittest.TestCase):
    """写库入口真的会清 —— 端到端"""

    def test_enqueued_key_is_cleaned(self):
        import asyncio
        import os
        import tempfile
        from kiana_vnext_plus.frontier import FrontierDB

        async def go():
            d = tempfile.mkdtemp()
            db = FrontierDB(os.path.join(d, "f.db"))
            await db.init_async()
            try:
                await db.add_video_download(
                    "https://www.bilibili.com/video/BV1PSL96YEwp?amp%3Btrackid=we",
                    "www.bilibili.com")
                await db.flush()
                rows = await db.get_pending_videos(10)
                return [r.get("video_url") if isinstance(r, dict) else r
                        for r in rows]
            finally:
                await db.close()

        keys = asyncio.run(go())
        self.assertTrue(keys, "没入队成功")
        for k in keys:
            self.assertNotIn("amp%3B", str(k),
                             f"入队键里还有残渣: {k}")
            self.assertNotIn("amp;", str(k),
                             f"入队键里还有残渣: {k}")


if __name__ == "__main__":
    unittest.main()
