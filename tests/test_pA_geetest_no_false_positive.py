# -*- coding: utf-8 -*-
"""GeeTest 检测**必须是结构判定，不能是文本判定**（真机实测的根因）

## 这个 bug 骗了我两轮

原来的检测是：
```python
if any(kw in lower for kw in ['geetest', 'gt.js', 'gt_challenge', ...]):
    return 'geetest'
```
**只匹配 HTML 里的裸字符串 `geetest`。**

真机实测（B站抓取）：
```
检测到挑战 [geetest]     17 次
页面上真实的挑战类名      []      ← 一个元素都没有
已点开验证入口            0 次    ← 没有元素可点
slider not found         17 次
```
⇒ **全是误报**。B站页面里必然含 `geetest` 字样（预加载脚本 URL / 风控 JS）。

**代价**：白起十几次浏览器；**并把排查引向错误的方向** ——
我先后怀疑"是不是 iframe"（改了跨 frame）和"是不是选择器不对"（改成了实测选择器）。
那两处**确实也是真 bug**，但**都不是主因**。要不是我加了"打印页面真实类名"的诊断
并且**自己跑了一次真机**，这个误报会一直藏着。

## 修法的两次收紧（第二次也是靠真机跑出来的）

· 第一版：加了"强证据"= 元素 / `window.initGeetest` / `script[src*=geetest]`
  → 真机复跑只从 17 降到 **2**，而那 2 次页面上**仍然 `[]`**。
· 第二版：**只认 DOM 元素**（脚本被加载 ≠ 有挑战在跑），
  脚本存在但没元素时**等 2s 复判**一次
  → 真机复跑 **0 次**，误报被正确记成"判定为误报"。

本文件守：**不许退回"文本里出现过就算有挑战"**。
"""
import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PKG = ROOT / "kiana_vnext_plus"
SRC = (PKG / "captcha_solver_extended.py").read_text(encoding="utf-8")


def _detect_body() -> str:
    tree = ast.parse(SRC)
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "detect_extended":
            return ast.unparse(n)
    raise AssertionError("找不到 detect_extended")


class TestGeetestDetectionIsStructural(unittest.TestCase):
    """检测必须有**结构证据**，不能只看文本"""

    def test_geetest_branch_queries_the_dom(self):
        """geetest 分支里必须真的去查 DOM"""
        body = _detect_body()
        i = body.find("geetest")
        self.assertGreater(i, 0, "找不到 geetest 分支")
        seg = body[max(0, i - 400): i + 3000]
        self.assertIn("querySelector", seg,
                      "geetest 检测没有查 DOM —— 又退回「文本里出现过就算有挑战」了")

    def test_does_not_accept_script_src_as_proof(self):
        """**不许把 `script[src*=geetest]` 当证据** —— 真机证明它太松

        B站**预加载**了极验 JS：脚本标签存在，但页面上没有任何控件。
        第一版修法就是栽在这里（17 → 2，那 2 次仍然 `[]`）。
        """
        body = _detect_body()
        i = body.find("geetest")
        seg = body[max(0, i - 400): i + 3000]
        self.assertNotIn("gt4.js", seg.replace("gt.js", "GTJS_MARK"),
                         "又把 script src 当证据了 —— 预加载会误报")
        # 也不许用 window.initGeetest 单独作证（预加载脚本同样会定义它）
        self.assertNotIn("window.initGeetest", seg,
                         "又把 window.initGeetest 当证据了 —— 预加载会误报")

    def test_false_positive_is_logged(self):
        """判定误报时要**说出来** —— 否则下一个人还得靠猜"""
        body = _detect_body()
        self.assertIn("误报", body,
                      "判定误报时没有日志 —— 排查时看不到「检测到了但其实是误报」")

    def test_waits_before_giving_up(self):
        """脚本在但元素还没渲染时，应**等一次再判**，别把真挑战漏掉"""
        body = _detect_body()
        self.assertIn("asyncio.sleep(2", body,
                      "没有等待复判 —— 挑战正在渲染时会被漏掉")


if __name__ == "__main__":
    unittest.main()
