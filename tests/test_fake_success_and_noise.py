# -*- coding: utf-8 -*-
"""B 组修复：假成功、日志噪音、可选依赖 API 漂移

三件事都来自**真机日志**（用户那次 8 个 B站种子的抓取）。

## ① 「视频下载完成」刷同一个旧文件名 —— **假成功**

日志里反复出现同一行：
```
视频下载完成: 021edaa56c4c.mp4 (146.0MB)
视频下载完成: 021edaa56c4c.mp4 (146.0MB)   ← 又是它
```
而每个"完成"其实**都没下成**。根因是两处兜底：
```python
for _f in _vdir.rglob("*"):
    if _is_real_video_file(_f):
        _real = _f; break      # ← 抓的是**目录里第一个**真视频
```
当**本次**产物判定失败时（真机就是那个 46 字节错误页），
它拿**之前下过的别的视频**当本次成果 → 记 completed、file_size 也是别人的。
**比失败更坏**：它让用户以为东西下好了。

修法：下载前拍快照，兜底只认**本次新出现**的文件。

## ② `PoTokenProvider BgUtilHTTP already registered` 刷屏

实测定位（不是"建两次"，是**竞态**）：
    顺序建 2 个 YoutubeDL        → 0 次
    **并发 12 个**               → **42 次**
    先单线程预热再并发           → **0 次**
修法：`prewarm_ytdlp_plugins()` 在单线程里先把插件注册跑一遍（幂等、失败不抛）。

## ③ biliass 装上反而更糟 —— API 漂移被静默吞掉

引擎只写死了旧版调用 `biliass.Danmaku2ASS(...)`，而 biliass 2.x 只有
`convert_to_ass(...) -> str`。装上 2.x 后：`import` **成功**、调用**必抛**，
异常又被 `except Exception` + `logger.debug` 吞掉
⇒ **连"biliass 未安装"那句提示都没了**，变成纯静默失败
（用户看到"弹幕 ASS 莫名其妙没了"且毫无线索）。

修法：兼容两代 API；失败**升到 warning**，上游改 API 时要看得见。
"""
import inspect
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PKG = ROOT / "kiana_vnext_plus"


class TestNoFakeVideoSuccess(unittest.TestCase):
    """① 兜底不许把"上一次的成果"认成本次的"""

    def _body(self, name):
        import ast
        src = (PKG / "crawler.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name == name), None)
        self.assertIsNotNone(fn, f"找不到 {name}")
        return ast.unparse(fn)

    def test_download_takes_a_snapshot_first(self):
        """下载前必须拍快照 —— 否则兜底无从判断"哪些是新出现的" """
        body = self._body("_download_one_video")
        self.assertIn("_pristine", body, "没有下载前快照")
        self.assertIn("_vdir.rglob", body, "快照没覆盖视频目录")

    def test_fallbacks_exclude_preexisting_files(self):
        """**两处**兜底都要排除下载前就在的文件"""
        body = self._body("_download_one_video")
        self.assertGreaterEqual(
            body.count("if _f in _pristine"), 2,
            "兜底没都排除旧文件 —— 会把上一次下好的视频认成本次成果（假成功）")

    def test_no_unfiltered_rglob_fallback_left(self):
        """不许再有"抓目录里第一个真视频"这种写法"""
        import re
        src = (PKG / "crawler.py").read_text(encoding="utf-8")
        # 紧跟 rglob 之后直接判 _is_real_video_file、中间没有 _pristine 检查的
        bad = re.findall(
            r"for _f in _vdir\.rglob\(\"\*\"\):\s*\n\s*if _is_real_video_file", src)
        self.assertEqual(bad, [], f"还有 {len(bad)} 处无过滤的兜底")


class TestYtdlpPrewarm(unittest.TestCase):
    """② 并发建 YoutubeDL 引发插件注册竞态 → 预热修掉"""

    def test_prewarm_function_exists_and_is_idempotent(self):
        from kiana_vnext_plus.universal_downloader import prewarm_ytdlp_plugins
        prewarm_ytdlp_plugins()
        prewarm_ytdlp_plugins()          # 幂等：第二次应直接返回
        self.assertTrue(callable(prewarm_ytdlp_plugins))
        # 文档里必须写清"为什么"（这条修复不写理由，后人会当无用代码删掉）
        doc = inspect.getsource(prewarm_ytdlp_plugins)
        self.assertIn("竞态", doc)

    def test_concurrent_creation_is_quiet_after_prewarm(self):
        """**实测**：预热后并发建实例，不再刷 'already registered'"""
        import contextlib
        import io
        import threading
        import yt_dlp
        from kiana_vnext_plus.universal_downloader import prewarm_ytdlp_plugins
        prewarm_ytdlp_plugins()
        buf = io.StringIO()

        def mk():
            with contextlib.redirect_stderr(buf):
                with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True,
                                       "ignoreconfig": True}):
                    pass
        ts = [threading.Thread(target=mk) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(buf.getvalue().count("already registered"), 0,
                         "预热没起作用，插件注册仍在竞态")

    def test_prewarm_is_called_before_each_construction(self):
        """两个 YoutubeDL 构造点前面都要有预热调用"""
        src = (PKG / "universal_downloader.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(src.count("prewarm_ytdlp_plugins()"), 3,
                                "预热调用点少于 2 处（定义 1 + 调用 2）")


class TestBiliassApiDrift(unittest.TestCase):
    """③ 可选依赖的 API 漂移不许被静默吞掉"""

    def test_supports_new_api(self):
        """装的是 2.x 时，真的能转出 ASS（不是"import 成功但调用必抛"）"""
        import importlib.util
        if importlib.util.find_spec("biliass") is None:
            self.skipTest("biliass 未安装（降级路径由 test_missing_dep_is_honest 覆盖）")
        from kiana_vnext_plus.comment_danmaku import danmaku_to_ass
        xml = ('<?xml version="1.0" encoding="UTF-8"?><i>'
               '<d p="1.5,1,25,16777215,1700000000,0,abc,1">测试</d></i>')
        out = danmaku_to_ass(xml, width=1920, height=1080)
        self.assertIsNotNone(out, "装上了却转不出来 —— API 又漂了")
        self.assertIn("[Script Info]", out)

    def test_handles_both_generations(self):
        """源码必须同时认 `convert_to_ass`（2.x）与 `Danmaku2ASS`（1.x）"""
        src = (PKG / "comment_danmaku.py").read_text(encoding="utf-8")
        self.assertIn("convert_to_ass", src, "没适配 2.x API")
        self.assertIn("Danmaku2ASS", src, "把 1.x 的兼容路径删了（老环境会挂）")
        self.assertIn("hasattr(biliass", src, "没有按能力探测，而是硬写死某一代")

    def test_failure_is_not_silent(self):
        """转换失败必须**看得见** —— 原来 `logger.debug` 在默认级别下等于没有"""
        import ast
        src = (PKG / "comment_danmaku.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name == "danmaku_to_ass"), None)
        self.assertIsNotNone(fn, "找不到 danmaku_to_ass")
        body = ast.unparse(fn)
        self.assertIn("logger.warning", body,
                      "转换失败还是 debug 级 —— 上游改 API 时用户毫无线索")

    def test_missing_dep_is_honest(self):
        """没装时那句提示必须保留（它是有用信息，不是噪音）"""
        src = (PKG / "comment_danmaku.py").read_text(encoding="utf-8")
        self.assertIn("biliass 未安装", src)


if __name__ == "__main__":
    unittest.main()
