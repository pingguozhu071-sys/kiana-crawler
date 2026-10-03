# -*- coding: utf-8 -*-
"""格式降级的退避判据测试（真机实测发现的"白等 2.5 秒"）

**真机现象**（2026-10-01 首次公网运行，B 站短链）：
```
格式 best/best[ext=mp4]/best 失败（DownloadError），2.5s 后降级重试
[K-DIAG] fmt=bestvideo+bestaudio[acodec=flac]/best/bestvideo+bestaudio/best result='...mp4'
```
**每个视频**都要先失败一次第一档格式，**无条件退避 2.5 秒**，然后第二档就成功了。

问题不在"降级"（降级是对的），在**退避**：
"这个格式不存在"是**确定性**错误——重试多久结果都一样，等 2.5 秒纯属白等。

而退避对**限流/网络**类失败是有意义的（那些等一下真的会好）。

所以判据必须是"**是否确定性**"，而不是"是否失败"。本文件把这条判据钉住。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kiana_vnext_plus.universal_downloader import (      # noqa: E402
    is_deterministic_format_error,
)


class _Err(Exception):
    pass


class TestDeterministicFormatError(unittest.TestCase):
    def test_bilibili_format_error_is_deterministic(self):
        """**真机原样报错**——必须判为确定性，否则又白等 2.5 秒"""
        real = ("ERROR: [BiliBili] BV1zJhr6bEVa_p1: Requested format is not available. "
                "Use --list-formats for a list of available formats")
        self.assertTrue(is_deterministic_format_error(_Err(real)))

    def test_no_formats_found_is_deterministic(self):
        self.assertTrue(is_deterministic_format_error(_Err("No video formats found")))

    def test_case_insensitive(self):
        self.assertTrue(is_deterministic_format_error(_Err("REQUESTED FORMAT IS NOT AVAILABLE")))

    # ── 下面这些**必须仍然退避**：它们等一下真的会好 ──────────────────
    def test_rate_limit_still_backs_off(self):
        self.assertFalse(is_deterministic_format_error(_Err("HTTP Error 429: Too Many Requests")))

    def test_forbidden_still_backs_off(self):
        """403 可能是风控，等一下可能就好——不能当确定性错误跳过退避"""
        self.assertFalse(is_deterministic_format_error(
            _Err("Unable to download webpage: HTTP Error 403: Forbidden")))

    def test_timeout_still_backs_off(self):
        self.assertFalse(is_deterministic_format_error(_Err("Connection timed out")))

    def test_network_error_still_backs_off(self):
        self.assertFalse(is_deterministic_format_error(_Err("Unable to resolve host")))

    def test_empty_message_is_not_deterministic(self):
        """空消息无从判断 → **保守地按需退避**（宁可多等，不要误判成确定性）"""
        self.assertFalse(is_deterministic_format_error(_Err("")))


class TestCallSiteUsesTheHelper(unittest.TestCase):
    """结构性钉子：重试循环必须**用这个判据**，而不是退回无条件退避"""

    def _src(self) -> str:
        import ast
        p = ROOT / "kiana_vnext_plus" / "universal_downloader.py"
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "download_video":
                return ast.unparse(n)
        return ""

    def test_download_video_consults_the_helper(self):
        body = self._src()
        self.assertIn("is_deterministic_format_error", body,
                      "download_video 必须用该判据决定是否退避")

    def test_sleep_is_guarded_not_unconditional(self):
        """退避必须**只在"非确定性"那一支**——无条件 sleep 就是本 bug 的原形态。

        ⚠️ **必须按 AST 判，不能按文本**：首版我"往上回看 6 行找不含 else"，
        结果 `ast.unparse` 的换行/缩进跟源码不同，直接误报。
        这已是本会话第 N 次"拿文本当结构"——结构的事就该用结构判。

        判据：找到 test 里引用 `is_deterministic_format_error` 的那个 `If`，
        **`orelse`（else 支）里必须有 `asyncio.sleep`，`body`（if 支）里必须没有**。
        """
        import ast
        p = ROOT / "kiana_vnext_plus" / "universal_downloader.py"
        tree = ast.parse(p.read_text(encoding="utf-8"))

        def _calls_sleep(node) -> bool:
            # 注意：`If.body` / `If.orelse` 是 **list**，`ast.walk` 只吃单个节点
            items = node if isinstance(node, list) else [node]
            return any(isinstance(x, ast.Call)
                       and isinstance(x.func, ast.Attribute)
                       and x.func.attr == "sleep"
                       for it in items for x in ast.walk(it))

        def _tests_helper(node) -> bool:
            return any(isinstance(x, ast.Name) and x.id == "is_deterministic_format_error"
                       for x in ast.walk(node.test))

        found = []
        for n in ast.walk(tree):
            if isinstance(n, ast.If) and _tests_helper(n):
                found.append(n)
        self.assertTrue(found, "找不到以 is_deterministic_format_error 为判据的分支")

        for n in found:
            self.assertTrue(_calls_sleep(n.orelse),
                            "else 支（非确定性失败）里必须有退避")
            self.assertFalse(_calls_sleep(n.body),
                             "if 支（确定性失败）里**不该有退避**——那就是本 bug 的原形态")


if __name__ == "__main__":
    unittest.main()
