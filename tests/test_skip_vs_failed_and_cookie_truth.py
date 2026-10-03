# -*- coding: utf-8 -*-
"""两个「真机实测」发现的修复的回归钉

## 一、`fail` 里混进了「因为达到页数上限而跳过」的任务

`page_processor` 里两处「达到上限」的分支原来都调 `_update_progress('failed')`：

```python
if await self.frontier.count_done_total() >= self.project.config.limits.max_pages:
    await self.frontier.mark_failed(uh, retry=False)
    self._update_progress('failed')      # ← 不是失败，是主动跳过
```

真机后果：跑 2 个 B站视频，进度条显示 **`done=2 fail=19`** ——
而**日志里一条错误都没有**。排查者（我）被这个假失败数带着查了半天。

修后：`done=2 fail=0 skip=16`。

> `frontier` 那边**仍必须** `mark_failed(..., retry=False)`：主循环的退出条件是
> 「pending + retry == 0」，不把任务从 pending 移走就会**反复取到 → 反复跳过 → 死循环**。
> 也就是**移除动作是对的、标签是错的** —— 修在报告层。

## 二、`cookies` 探测**用无关的检查冒充成功**

`detect_bilibili_cookie_browser` 只检查「cookies 文件里有没有 `bilibili` 这个词」，
日志却宣称「检测到 B站**会员** cookies → **强制最高画质**」。

真机后果：机主的 cookies 字段齐全（SESSDATA/bili_jct/DedeUserID）、文件声明 2027 才过期，
但服务端回 **`-101 账号未登录`** → 实际只拿到 **480P**，日志却在报"最高画质"。

修后日志如实说：
`⚠️ B站 cookies 已失效（code=-101 账号未登录）…本次只能拿到 480P 及以下`
"""
import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PKG = ROOT / "kiana_vnext_plus"


class TestSkippedIsNotFailed(unittest.TestCase):
    """① 达到上限 = 跳过，不是失败"""

    def _tree(self, name):
        return ast.parse((PKG / name).read_text(encoding="utf-8"))

    def test_cap_branches_use_skipped_not_failed(self):
        """两处「达到上限」的分支必须记 `skipped`"""
        src = (PKG / "page_processor.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        bad = []
        for node in ast.walk(tree):
            # 找 `if ... count_done... >= ...limits.max_pages...:` 这种上限判断
            if not isinstance(node, ast.If):
                continue
            cond = ast.unparse(node.test)
            if "max_pages" not in cond:
                continue
            body = ast.unparse(node)
            if "_update_progress('failed')" in body or '_update_progress("failed")' in body:
                bad.append(cond[:70])
            if "_update_progress('skipped')" not in body and "_update_progress(\"skipped\")" not in body:
                bad.append(f"（{cond[:50]}）未记 skipped")
        self.assertEqual(bad, [], f"上限分支仍在记 failed 或没记 skipped: {bad}")

    def test_progress_dict_declares_skipped(self):
        """`_progress` 初始化要有 `skipped` 键（否则读的人不知道有这个口径）"""
        src = (PKG / "crawler.py").read_text(encoding="utf-8")
        self.assertIn("'skipped': 0", src, "_progress 初始化缺 skipped 键")

    def test_cli_shows_skip_separately(self):
        """进度条必须把 skip 与 fail **分开显示**——合并就是本 bug 的原形态"""
        src = (ROOT / "run_crawler.py").read_text(encoding="utf-8")
        self.assertIn("skip={sk}", src, "进度条没单独显示 skip")
        # fail 不许再包含 skipped
        self.assertNotRegex(src, r"failed\"?,?\s*0\)\s*\+\s*p\.get\(\"skipped\"",
                            "skip 又被并回 fail 了")


    def test_video_domain_cap_moves_row_out_of_pending(self):
        """**行为**（不是查文本）：视频版「每域上限」必须把行移出 pending 并记 `skipped`

        这是本文件第一节那条教训在**视频路径**上的同一形状：原实现只写一个 `return`，
        行仍是 `pending` ⇒ `crawl()` 收尾那个「等 pending 视频清空（上限 150 秒）」的循环
        **永远不 break** ⇒ 每次撞上限都白等满 150 秒；同一 project 目录续爬时
        又会被重新捞出来再空转一遍。
        """
        import asyncio
        import sqlite3
        import tempfile
        from types import SimpleNamespace

        from kiana_vnext_plus.crawler import Crawler
        from kiana_vnext_plus.frontier import FrontierDB

        capped = "http://cap.test/3.mp4"
        with tempfile.TemporaryDirectory() as td:
            db_path = str(Path(td) / "cap.db")
            db = FrontierDB(db_path)

            async def flow():
                await db.init_async()
                for i in (1, 2, 3):
                    await db.add_video_download(f"http://cap.test/{i}.mp4", "cap.test")
                await db.flush()
                # 上限设 2，而该域已有 3 行 ⇒ 必然落到「达到上限」那一支
                fake = SimpleNamespace(
                    frontier=db,
                    project=SimpleNamespace(config=SimpleNamespace(
                        video_settings=SimpleNamespace(max_downloads_per_domain=2))),
                    _video_cap_warned=set(),
                )
                await Crawler._download_one_video(
                    fake, {"domain": "cap.test", "video_url": capped})
                await db.flush()
                pending = [v["video_url"] for v in await db.get_pending_videos(10)]
                stats = await db.get_video_stats()
                await db.close()
                return pending, stats

            pending, stats = asyncio.run(flow())
            con = sqlite3.connect(db_path)
            try:
                row = con.execute(
                    "SELECT status FROM video_downloads WHERE video_url=?",
                    (capped,)).fetchone()
            finally:
                con.close()

        self.assertNotIn(
            capped, pending,
            "达到每域上限后该行仍留在 pending ⇒ 收尾的排空循环永远不 break（白烧 150 秒），"
            "续爬时还会被重新捞出来再空转一遍")
        self.assertEqual(
            row, ("skipped",),
            "上限分支必须记 skipped —— 既不是 failed（那不是失败），更不是 completed"
            "（那是本工程最忌的『假成功』）")
        self.assertEqual(stats, {"completed": 0, "bytes": 0},
                         "被跳过的视频不许进完成统计")


    def test_batch_size_is_bounded_by_max_pages(self):
        """**行为**：单批取任务数必须受 `max_pages` 约束 —— 否则一个批就能冲过上限

        真机实测（v2.19.9）：`-m 3`（max_pages=3）跑了 **10 页**。根因是 `pop_batch`
        一次最多租出 50 个任务并**并发启动**，而 `max_pages` 是在每个任务开始前才查的
        ⇒ 那 50 个查到同一个"还没超"的计数 ⇒ 全部照跑。
        """
        from kiana_vnext_plus.crawler import _batch_cap

        self.assertEqual(_batch_cap(3, 1), 2, "还剩 2 页额度，就该只租 2 个")
        self.assertEqual(_batch_cap(30, 0), 30)
        self.assertEqual(_batch_cap(100, 90), 10)
        self.assertEqual(_batch_cap(5000, 0), 50, "额度充足时必须保持原批上限 50")
        # 额度用完**不许收窄到 0**：pop 恒空 ⇒ pending 永不清零 ⇒ 空转（同一形状的坑）
        self.assertEqual(_batch_cap(3, 3), 50)
        self.assertEqual(_batch_cap(3, 999), 50)
        self.assertGreaterEqual(_batch_cap(1, 1), 1)

    def test_pop_batch_call_uses_the_cap(self):
        """接线（**AST**，不是数出现次数）：`pop_batch` 的第一个实参必须是 `_batch_cap(...)`"""
        tree = self._tree("crawler.py")
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and n.func.attr == "pop_batch"]
        self.assertTrue(calls, "crawler.py 里找不到 pop_batch 调用")
        for c in calls:
            self.assertTrue(c.args, "pop_batch 没传批量上限")
            arg = ast.unparse(c.args[0])
            self.assertIn("_batch_cap", arg,
                          f"pop_batch 的批量上限不是 _batch_cap(...)，而是 `{arg}`"
                          " —— 又硬编码了一个与 max_pages 无关的数")


class TestCookieLoginIsActuallyVerified(unittest.TestCase):
    """② cookies 探测必须**真验证登录态**，不能只看文件名/内容里有没有关键字"""

    def test_verify_function_exists(self):
        from kiana_vnext_plus import universal_downloader as ud
        self.assertTrue(hasattr(ud, "verify_bilibili_login"),
                        "缺 verify_bilibili_login —— 又退回「只看有没有 bilibili 这个词」了")

    def test_detection_alone_no_longer_claims_premium(self):
        """**核心**：日志里不许再出现「检测到 B站会员 cookies … 强制最高画质」这种断言"""
        src = (PKG / "universal_downloader.py").read_text(encoding="utf-8")
        self.assertNotIn("检测到 B站会员 cookies", src,
                         "又出现「只凭探测就宣称是会员」的日志")

    def test_reports_invalid_cookies_honestly(self):
        """失效时必须**明说只能拿 480P**，而不是含糊过去"""
        src = (PKG / "universal_downloader.py").read_text(encoding="utf-8")
        self.assertIn("已失效", src)
        self.assertIn("480P", src)

    def test_network_failure_does_not_change_behaviour(self):
        """**探测失败不许影响下载** —— 返回 None 表示"没测出来"，不是"没登录"。

        真机踩过：实现里漏了 `import json` → NameError → 如果没兜住就会中断下载。
        """
        src = (PKG / "universal_downloader.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "verify_bilibili_login"), None)
        self.assertIsNotNone(fn, "找不到 verify_bilibili_login")
        body = ast.unparse(fn)
        self.assertIn("except Exception", body, "没有异常兜底——探测失败会连累下载")
        self.assertIn("None", body, "没有「没测出来」这一态")

    def test_uses_the_ssrf_gate(self):
        """nav 请求也要过工程的安全闸（红线：动态 URL 请求必须过闸）"""
        src = (PKG / "universal_downloader.py").read_text(encoding="utf-8")
        self.assertIn("safe_urlopen", src, "nav 探测没走 safe_urlopen")

    def test_never_logs_cookie_values(self):
        """**红线**：日志里绝不能出现 cookie 的值"""
        src = (PKG / "universal_downloader.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "verify_bilibili_login"), None)
        body = ast.unparse(fn)
        # 不许把 pairs / cookie 串进 logger
        self.assertNotRegex(body, r"logger\.\w+\([^)]*(pairs|cookie\b)",
                            "日志里出现了 cookie 内容")


if __name__ == "__main__":
    unittest.main()
