# -*- coding: utf-8 -*-
"""P2：5 个「有默认值、却从来没人能改」的配置键 —— 六跳接线

## 这 5 个键的共同病史

审计测试 `tests/test_config_wiring_audit.py` 曾把它们标成 **`GAP`**：
「看起来是用户会想控的功能，但**当前无入口**」。

具体病象：`config.DEFAULT_GLOBAL` 里有、引擎里也在读，
**但 gcfg / EngineBridge 的翻译表里没有它们** ——
于是用户在界面上怎么设，引擎永远读到默认值。**典型的"能填不生效"。**

| 键 | 默认 | 本轮给的入口 |
|---|---|---|
| `headless` | `True` | 首页开关「显示浏览器窗口」（**反向**） |
| `export_markdown` | `True` | 首页开关「导出 Markdown」 |
| `proxy_fetcher_enabled` | `False` | 设置页「自动抓取代理」 |
| `proxy_source` | `''` | 设置页「代理源」输入框（与上者配套） |
| `fingerprint_update_enabled` | `False` | 设置页「指纹库自动更新」 |

## 本文件守什么

1. **六跳齐**（缺任何一跳 = 引擎读不到用户在界面上设的值）；
2. **`headless` 的反转只允许出现一次** —— 界面是「显示窗口」、引擎要 `headless`，
   两处都翻 = 翻回去了（用户会发现开关"反着来"）；
3. 每个键都在 `ENTRY_FILES` 里出现（审计测试据此判定"不再是 GAP"）。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PKG = ROOT / "kiana_vnext_plus"

# 键 → (首页控件名 或 None, 设置页控件名 或 None)
GAPS = {
    "headless": ("sw_headful", None),
    "export_markdown": ("sw_md", None),
    "proxy_fetcher_enabled": (None, "sw_proxy_fetch"),
    "proxy_source": (None, "proxy_src_edit"),
    "fingerprint_update_enabled": (None, "sw_fp_update"),
}


class TestGapKeysAreWired(unittest.TestCase):
    """六跳：缺一跳就是"能填不生效"（这几个键原来的病）"""

    def _text(self, rel):
        return (ROOT / rel).read_text(encoding="utf-8")

    def test_each_key_reaches_engine_gcfg(self):
        """第⑤跳：`run_crawler` 的 gcfg 键表里必须有"""
        run = self._text("run_crawler.py")
        missing = [k for k in GAPS if f'"{k}"' not in run]
        self.assertEqual(missing, [], f"这些键没进 run_crawler 的 gcfg: {missing}")

    def test_each_key_reaches_engine_bridge(self):
        """第④跳：EngineBridge 翻译表里必须有"""
        bridge = self._text("launcher_v8.py")
        missing = [k for k in GAPS if f'"{k}"' not in bridge]
        self.assertEqual(missing, [], f"这些键没进 EngineBridge 翻译表: {missing}")

    def test_each_key_has_a_gui_control(self):
        """第①跳：界面上必须有控件（否则又回到"无入口"）"""
        gui = self._text("launcher_v9.py")
        missing = []
        for key, (home_w, set_w) in GAPS.items():
            for w in (home_w, set_w):
                if w and f"self.{w}" not in gui:
                    missing.append(f"{key} → {w}")
        self.assertEqual(missing, [], f"这些键界面控件缺失: {missing}")

    def test_each_key_is_passed_in_start(self):
        """第③跳：`_start()` 组装 cfg 时必须带上"""
        gui = self._text("launcher_v9.py")
        missing = [k for k in GAPS if f'"{k}"' not in gui]
        self.assertEqual(missing, [], f"这些键没进 _start 的 cfg: {missing}")

    def test_key_defaults_match_config(self):
        """⑥ `DEFAULT_GLOBAL` 仍是唯一默认源：两边默认值必须一致"""
        import re
        cfg = (PKG / "config.py").read_text(encoding="utf-8")
        want = {"headless": "True", "export_markdown": "True",
                "proxy_fetcher_enabled": "False", "proxy_source": '""',
                "fingerprint_update_enabled": "False"}
        for k, v in want.items():
            self.assertRegex(cfg, rf'"{k}"\s*:\s*{re.escape(v)}',
                             f"DEFAULT_GLOBAL 的 {k} 默认值不是 {v}")


class TestHeadlessInversionHappensOnce(unittest.TestCase):
    """`headless` 是**反向**表达（界面「显示浏览器窗口」），只能翻一次"""

    def test_inversion_is_in_start_only(self):
        """反转应发生在 `_start()`（界面→cfg 的那一跳），第④⑤跳原样传递"""
        gui = (ROOT / "launcher_v9.py").read_text(encoding="utf-8")
        self.assertIn("not bool(h.sw_headful.isChecked())", gui,
                      "_start 里没做 headless 的反转")
        # 反转只此一处：不许在别处再 not 一次
        self.assertEqual(gui.count("not bool(h.sw_headful.isChecked())"), 1,
                         "headless 的反转出现了多次 —— 翻两次等于没翻")
        # 第④⑤跳必须是原样 `bool(cfg.get("headless", True))`，
        # **不许**写 `not cfg.get(...)`（那会翻第二次）
        for rel in ("run_crawler.py", "launcher_v8.py"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertNotIn('not cfg.get("headless"', text,
                             f"{rel} 里又翻了一次 headless —— 开关会反着来")


if __name__ == "__main__":
    unittest.main()
