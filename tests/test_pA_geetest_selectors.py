# -*- coding: utf-8 -*-
"""P-A 实测结论的钉子：GeeTest 的选择器必须**前缀容忍**，且要先点入口

## 这些数字是**量出来的**，不是猜的

用 GeeTest **官方 demo**（`geetest.com/en/demo`）抓真实 DOM 实测：

| 选择器 | 真实 DOM 命中数 |
|---|---|
| `[class*="geetest_box_btn"]` | **1**（「点击验证」入口） |
| `[class*="geetest_btn_svg"]` | **1**（入口） |
| `[class*="geetest_holder"]` | **1**（容器） |
| `[class*="geetest_slider_btn"]` | **0**（点开入口前不存在） |
| `.geetest_slider_button` | **0** |
| `.geetest_btn_slide` | **0** |
| `.geetest-item-wrap` | **0** |
| `.geetest_radar_tip` | **0** |

两条结论：

1. **真实类名带实例 ID 后缀** —— 实测是 `geetest_box_btn_38052eff`。
   所以 `.geetest_slider_button` 这种**普通类选择器永远匹配不上**。
   引擎原来那 10 个选择器里 **8 个命中数为 0**（全是编出来的名字）。
2. **GeeTest 不是一上来就有滑块** —— 初始 DOM 里只有「点击验证」入口，
   滑块要点开之后才出现。**引擎从来没点过那个入口。**

本文件守这两条，防止以后被改回"猜的选择器"。
"""
import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PKG = ROOT / "kiana_vnext_plus"
SRC = (PKG / "captcha_solver_extended.py").read_text(encoding="utf-8")


def _geetest_solve_body() -> str:
    """取 GeeTest 求解那个函数的源码文本（`_solve_geetest`）。"""
    tree = ast.parse(SRC)
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and "geetest" in n.name.lower():
            return ast.unparse(n)
    raise AssertionError("找不到 GeeTest 求解函数")


class TestGeetestSelectorsAreMeasured(unittest.TestCase):
    """选择器必须来自实测，而不是"看着像" """

    def test_geetest_selectors_use_prefix_matching(self):
        """GeeTest 的选择器必须是**属性前缀**（容忍 `_xxxxxxxx` 实例后缀）"""
        body = _geetest_solve_body()
        self.assertIn('[class*="geetest_', body,
                      "GeeTest 选择器没有用属性前缀 —— 真实类名带实例后缀，普通类选择器匹配不上")

    def test_no_bare_geetest_class_selector(self):
        """不许再用**裸的** `.geetest_xxx` 类选择器（实测命中数为 0）"""
        body = _geetest_solve_body()
        import re
        bare = re.findall(r"['\"]\.geetest_[a-z_]+['\"]", body)
        self.assertEqual(bare, [],
                         f"又用回裸的类选择器了（实测匹配不上）: {bare}")

    def test_entry_is_clicked_before_looking_for_slider(self):
        """**必须先点入口** —— 滑块要点开才出现（实测初始 DOM 里滑块命中 0）"""
        body = _geetest_solve_body()
        self.assertIn("geetest_box_btn", body, "没点「点击验证」入口")
        # 点击要发生在"找滑块"之前
        i_click = body.find("geetest_box_btn")
        i_slider = body.find("slider_selectors")
        self.assertGreater(i_slider, i_click,
                           "顺序不对：应该**先点入口再找滑块**")

    def test_real_entry_button_is_geetest_btn_click(self):
        """**真正的入口按钮是 `geetest_btn_click`**（实测 300x50，文案 'Click to verify'）

        我第一版只写了 `box_btn` / `btn_svg` / `holder` —— **恰恰漏了真正那个**。
        这四个在官方 demo 上实测**全部命中**，但 `geetest_btn_click` 是最准的，
        必须排在第一个。
        """
        body = _geetest_solve_body()
        self.assertIn("geetest_btn_click", body,
                      "漏了实测出来的真按钮 —— 只点容器（holder）可能点不动")
        # 它应该是**第一个**尝试的
        i_real = body.find("geetest_btn_click")
        i_holder = body.find("geetest_holder")
        self.assertLess(i_real, i_holder,
                        "真按钮应排在兜底容器之前")

    def test_other_vendors_kept(self):
        """**别家**的选择器不许被一起删掉（阿里/京东/网易各有各的类名）

        ⚠️ 我第一版把阿里的写成 `.nc_1_n1z`，**测试红了** ——
        源码里其实是 **`#nc_1_n1z`（井号，按 id 选）**。
        是**测试的断言写错**，不是代码错。照实改过来。
        """
        body = _geetest_solve_body()
        for sel in ("#nc_1_n1z", ".JDJRV-slide-btn", ".yidun_slider", ".JCap-slide-btn"):
            self.assertIn(sel, body, f"把别家的选择器 {sel} 删了 —— 那些站点会退化")

    def test_not_found_dumps_real_class_names(self):
        """找不到时必须**把页面真实类名打出来** —— 下次真机就知道真实名字是什么"""
        body = _geetest_solve_body()
        self.assertIn("真实的挑战类名", body,
                      "找不到时只报一句 'slider not found' —— 下次还得靠猜")
        self.assertIn("querySelectorAll", body, "没有真的去页面上采集类名")


if __name__ == "__main__":
    unittest.main()
