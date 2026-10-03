# -*- coding: utf-8 -*-
"""`navigator.webdriver` 的**取值语义**测试 —— 必须是 `false`，不能是 `undefined`

## 为什么这条值得单独钉

真实 Chrome 的 `navigator.webdriver` 是 **`false`**（布尔）；
只有被自动化工具控制时才是 **`true`**。

而本工程的隐身链**一直在把它改成 `undefined`**：

```javascript
Object.defineProperty(navigator, 'webdriver', { get: () => undefined, configurable: true });
```

`undefined` **不是**真浏览器的任何取值 —— 它等于在说"这个属性被人动过"。
**一个诚实的 `false` 比一个可疑的 `undefined` 安全得多。**

顺带发现的第二个问题（同一个块里）：

```javascript
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });  // 定义
delete navigator.webdriver;                                                // 又删掉
```

第 2 行把第 1 行刚定义的属性删了，**自相矛盾** —— 净效果只剩第 3 步的原型改写。

## 最后写者问题

`evasion_v2` 的脚本注册在整条链**之后**，它原来也写 `undefined` —— 所以
**只改 `injection_scripts` 是没用的，会被它盖回去**。本文件把"最终取值"钉住。
"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PKG = ROOT / "kiana_vnext_plus"
# 参与 webdriver 改写的文件（按注入顺序；evasion_v2 最后）
FILES = ("injection_scripts.py", "evasion_engine.py", "evasion_v2.py")


class TestWebdriverValueIsBooleanFalse(unittest.TestCase):
    def test_no_undefined_webdriver_getter(self):
        """**核心**：任何 `webdriver` 的 getter 都不许返回 `undefined`"""
        offenders = []
        for f in FILES:
            text = (PKG / f).read_text(encoding="utf-8")
            for m in re.finditer(r"webdriver'[^\n]*\n[^\n]*get:\s*\(\)\s*=>\s*(\w+)", text):
                if m.group(1) == "undefined":
                    offenders.append(f"{f}: {m.group(0)[:70]}")
        self.assertEqual(offenders, [],
                         "这些地方把 webdriver 改成了 undefined（应为 false）:\n" + "\n".join(offenders))

    def test_last_writer_uses_false(self):
        """**最后写者**（evasion_v2，注册在链之后）必须用 `false` —— 否则前面的改动被盖掉"""
        text = (PKG / "evasion_v2.py").read_text(encoding="utf-8")
        hits = re.findall(r"Object\.defineProperty\(\s*navigator\s*,\s*'webdriver'[^\n]*", text)
        self.assertTrue(hits, "evasion_v2 里找不到 webdriver 改写")
        for h in hits:
            self.assertIn("false", h, f"最后写者没用 false: {h[:90]}")

    def test_no_self_defeating_delete(self):
        """不许"先定义再 delete"——那是自相矛盾的写法"""
        text = (PKG / "injection_scripts.py").read_text(encoding="utf-8")
        # 找出 defineProperty(navigator,'webdriver' 之后紧跟 delete navigator.webdriver 的形态
        self.assertNotRegex(
            text, r"defineProperty\(\s*navigator\s*,\s*'webdriver'[\s\S]{0,200}?delete\s+navigator\.webdriver",
            "又出现「先定义 webdriver 再把它 delete 掉」的自相矛盾写法")

    def test_all_webdriver_getters_return_false(self):
        """所有 webdriver getter 一律 false（真实 Chrome 的取值）"""
        bad = []
        for f in FILES:
            text = (PKG / f).read_text(encoding="utf-8")
            for m in re.finditer(r"webdriver'[^\n]*\n[^\n]*get:\s*\(\)\s*=>\s*(\w+)", text):
                if m.group(1) != "false":
                    bad.append(f"{f}: 返回 {m.group(1)}")
        self.assertEqual(bad, [], f"这些地方不是 false: {bad}")


if __name__ == "__main__":
    unittest.main()
