# -*- coding: utf-8 -*-
"""P-A 复现：挑战在 iframe 里时，旧的 `page.locator()` 找不到，新的 `_loc()` 找得到

## 为什么写这个

真机日志里 GeeTest **每次都失败**：
```
检测到挑战 [geetest]: https://b23.tv/DgxnKc4
GeeTest: attempting local slider simulation
GeeTest slider not found          ← 每次都这句
```

**根因假设**：`solver_engine` 检测挑战时会**聚合所有 frame 的 HTML**
（`_frames_html` → `detect(extra_html=...)`），所以**看得见** iframe 里的 geetest；
但 `UnifiedChallengeSolver` 求解时只用 `self.page.locator(sel)`，
而 Playwright 的 **`page.locator()` 不跨 iframe** —— **够不着**。

于是形成"**检测看得见、求解够不着**"的自相矛盾。

## 这个文件做什么

不靠"看着像"下结论 —— 用**真浏览器**造一个 iframe 里放滑块的页面，然后断言：
  1. `page.locator(...)` **找不到**（证明旧实现在这种页面必然失败）；
  2. `solver._loc(...)` **找到了**（证明修复有效）。

两条都要成立，根因才算证实。**只测第 2 条是不够的** ——
那证明不了"旧代码真的会失败"。
"""
import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# iframe 里放一个 GeeTest 风格的滑块按钮；宿主页面**没有**任何 geetest 元素
_PAGE = """<!doctype html><html><body>
<h1>宿主页面</h1>
<iframe id="f" srcdoc="
  <html><body style='margin:0'>
    <div class='geetest_holder'>
      <div class='geetest_slider'>
        <div class='geetest_slider_button' style='width:40px;height:40px;
             background:red'>drag</div>
      </div>
    </div>
  </body></html>"></iframe>
</body></html>"""


def _run(coro):
    return asyncio.run(coro)


class TestIframeChallengeLocator(unittest.TestCase):
    """跨 frame 定位：复现 + 验证修复"""

    @classmethod
    def setUpClass(cls):
        try:
            from patchright.async_api import async_playwright  # noqa: F401
        except Exception as e:  # pragma: no cover
            raise unittest.SkipTest(f"没有可用浏览器内核: {e}")

    def _with_page(self, fn):
        """起浏览器 → 载入复现页面 → 把 (page, solver) 交给 fn

        ⚠️ 用 `ExtendedChallengeSolver`（`captcha_solver_extended.py`）——
        **GeeTest 的滑块逻辑在这个类里**。`UnifiedChallengeSolver`
        （`challenge_solver.py`）只是把它**委托**过去：
            self.extended_solver = ExtendedChallengeSolver(...)
        我第一版 import 了 `UnifiedChallengeSolver`，`ImportError` ——
        记在这里免得下次又找错文件。
        """

        async def _go():
            from patchright.async_api import async_playwright
            from kiana_vnext_plus.captcha_solver_extended import ExtendedChallengeSolver
            async with async_playwright() as p:
                b = await p.chromium.launch(headless=True)
                try:
                    pg = await b.new_page()
                    await pg.set_content(_PAGE)
                    await pg.wait_for_timeout(400)   # 等 iframe 渲染
                    solver = ExtendedChallengeSolver(pg, max_wait=2)
                    return await fn(pg, solver)
                finally:
                    await b.close()

        return _run(_go())

    def test_old_lookup_cannot_see_into_iframe(self):
        """**根因证实**：`page.locator()` 在不跨 iframe 的页面上必然找不到。

        这条如果意外通过了，说明我的根因假设是错的 —— 那时不该改代码，该重查。
        """
        async def check(pg, _solver):
            n = await pg.locator(".geetest_slider_button").count()
            return n
        n = self._with_page(check)
        self.assertEqual(n, 0,
                         "宿主页面竟然直接看得到 iframe 里的元素？"
                         "那说明 Playwright 版本行为变了，本修复的前提需重查")

    def test_new_lookup_finds_slider_in_iframe(self):
        """**修复有效**：`_loc()` 能跨 frame 找到滑块"""
        async def check(_pg, solver):
            loc = await solver._loc(".geetest_slider_button")
            return await loc.count()
        n = self._with_page(check)
        self.assertEqual(n, 1, "跨 frame 定位没生效 —— GeeTest 仍然够不着")

    def test_find_first_returns_the_matching_selector(self):
        """`_find_first` 要能报出**是哪个选择器命中的**（排查时有用）"""
        async def check(_pg, solver):
            loc, sel = await solver._find_first(
                [".definitely_not_here", ".geetest_slider_button", ".geetest_holder"])
            return (await loc.count() if loc is not None else 0), sel
        n, sel = self._with_page(check)
        self.assertEqual(n, 1)
        self.assertEqual(sel, ".geetest_slider_button",
                         "报错了命中的选择器（排查时会误导）")

    def test_no_match_returns_unmatchable_locator(self):
        """找不到时返回**必然匹配不到**的 locator（调用方不必加 None 判断）"""
        async def check(_pg, solver):
            loc = await solver._loc(".nothing_matches_this_anywhere")
            return await loc.count()
        self.assertEqual(self._with_page(check), 0)

    def test_no_raw_page_locator_left_in_solvers(self):
        """**不许再有裸的 `self.page.locator`** —— 留一个就留一个盲区

        只允许出现在 `_loc()` 自己内部（它就是那个跨 frame 的实现）。
        """
        import ast
        src = (ROOT / "kiana_vnext_plus" / "captcha_solver_extended.py").read_text(
            encoding="utf-8")
        tree = ast.parse(src)
        bad = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name == "_loc":        # 实现本身，允许
                continue
            body = ast.unparse(node)
            if "self.page.locator(" in body:
                bad.append(node.name)
        self.assertEqual(bad, [],
                         f"这些求解器还在用不跨 frame 的 page.locator: {bad}")


if __name__ == "__main__":
    unittest.main()
