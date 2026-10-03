# -*- coding: utf-8 -*-
"""「定义了但引擎内无人调用」的守卫（死代码 / 没接线）

**为什么做这一条**：本轮换了个角度查"死代码"，发现引擎里有 **35 个函数在引擎内零引用**。
前几轮已经在别的形态上反复撞到同一个病：

| 轮 | 形态 |
|---|---|
| 19 | `impersonate` 三份拷贝只修两份 |
| 25 | `RedisFrontier` 缺 6 个方法 |
| 36 | `cookie_armory_enabled` **功能建好了但没入口** |
| 38 | 6 个配置键**定义了却没人读** |
| **40** | **35 个函数定义了却没人调** |

**本轮最扎眼的一条**：`trace_forensics.archive_trace` —— 那是第 13 轮做的"trace 取证"
（脱敏归档，13 个测试 + 2 组变异反证）。**它建好了，但没有任何地方调用它。**
与第 36 轮那个"身份池从没被启用过"是**完全同一类**。

**本文件只登记、不阻断**——引擎本质是库，部分函数是对外 API。
但**新增**一个没人调用的函数必须被看见：要么接线，要么登记。
基线**只许降不许升**。
"""
import ast
import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PKG = ROOT / "kiana_vnext_plus"
BASELINE = ROOT / "tests" / "assets" / "dead_function_baseline.json"

# 框架回调 / 鸭子类型接口：由基类或协议调用，静态扫不到 —— 显式豁免
FRAMEWORK = frozenset({"do_POST", "do_GET", "log_message", "handle", "emit", "recv", "send"})


def scan() -> dict:
    """→ `{函数名: {"sites": [...], "test_only": bool}}`（引擎内零引用者）"""
    defs = {}
    for f in sorted(os.listdir(PKG)):
        if not f.endswith(".py"):
            continue
        try:
            tree = ast.parse((PKG / f).read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith("__"):
                defs.setdefault(n.name, []).append((f, n.lineno))

    texts = {}
    for d in (PKG, ROOT / "tools", ROOT / "tests", ROOT):
        for f in os.listdir(d):
            p = Path(d) / f
            if p.suffix == ".py":
                texts[p] = p.read_text(encoding="utf-8", errors="ignore")

    dead = {}
    for name, sites in defs.items():
        if name in FRAMEWORK:
            continue
        eng = test = 0
        for p, t in texts.items():
            c = t.count(name)
            if not c:
                continue
            if "tests" in p.parts:
                test += c
            elif p.parent == PKG:
                eng += max(0, c - sum(1 for ff, _ in sites if ff == p.name))
            else:
                eng += c
        if eng == 0:
            dead[name] = {"sites": [f"{f}:{n}" for f, n in sites], "test_only": test > 0}
    return dead


def _baseline() -> dict:
    try:
        return json.loads(BASELINE.read_text(encoding="utf-8")).get("dead_functions", {})
    except Exception:
        return {}


CURRENT = scan()
BASE = _baseline()


class TestNoNewDeadFunction(unittest.TestCase):
    def test_scan_is_non_trivial(self):
        """夹具自证：扫描失效的话下面的断言会**假绿**"""
        self.assertGreaterEqual(len(CURRENT), 10,
                                f"只扫到 {len(CURRENT)} 个零引用函数，扫描可能失效")

    def test_no_new_unreferenced_function(self):
        """**新增**一个引擎内没人调用的函数 → 要么接线，要么登记"""
        fresh = sorted(set(CURRENT) - set(BASE))
        self.assertEqual(
            fresh, [],
            f"新增了引擎内**零引用**的函数: {fresh}——"
            f"请接上调用，或更新 tests/assets/dead_function_baseline.json 并写明理由")

    def test_baseline_only_shrinks(self):
        """**只许降不许升**：接上或删掉一个就更新基线让它变小"""
        gone = sorted(set(BASE) - set(CURRENT))
        if gone:
            self.fail(f"这些函数已被接上/删除，请把它们从基线移除（只许降）: {gone}")

    def test_known_unwired_features_are_still_registered(self):
        """把本轮最扎眼的那几个"建好了没接线"的钉住——它们**不该悄悄消失**"""
        for name in ("archive_trace", "sync_cookies_from_browser", "fetch_stealth"):
            self.assertIn(name, CURRENT,
                          f"{name} 已不在零引用集合里——若已接线，请同步本断言与基线")


if __name__ == "__main__":
    unittest.main()
