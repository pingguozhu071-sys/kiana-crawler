# -*- coding: utf-8 -*-
"""cookie 注入的**域名匹配必须防仿冒** —— 子串匹配 = cookie 泄漏

## 这个 bug 是怎么发现的

做 cookie 解析收敛时，子代理人在 `universal_downloader.load_site_cookies`
里发现一个**既存**的安全相关判定（它**只报告、没擅自修**，这是对的）：

```python
if d == root or host.endswith("." + d) or root.endswith("." + d) or d in host:
                                                                      ^^^^^^^^
```
**`d in host` 是裸子串匹配** ⇒ `"taobao.com" in "nottaobao.com"` 为 **True**
⇒ **会把 taobao 的 cookie 注入到仿冒域名 `nottaobao.com` 上。**

这是 **cookie 泄漏**，不是小瑕疵：拿到 cookie 的一方可以直接复用登录态。

**而且那个分支是冗余的** —— 合法情形已被 `host.endswith("." + d)` 精确覆盖。

## 为什么不能改成 `endswith(d)`

`"nottaobao.com".endswith("taobao.com")` **同样是 True** ——
后缀匹配必须带**点边界**（`"." + d`）才算数。本文件把这条也钉住。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _match(cookie_domain: str, host: str) -> bool:
    """复刻 `load_site_cookies` 的判定（纯函数，便于穷举边界）。"""
    d = cookie_domain.lstrip(".")
    parts = [p for p in host.split(":")[0].split(".") if p]
    root = ".".join(parts[-2:]) if len(parts) >= 2 else host
    return bool(d and (d == root or host.endswith("." + d) or root.endswith("." + d)))


class TestCookieDomainMatch(unittest.TestCase):
    def test_legitimate_cases_still_match(self):
        """**不许误伤** —— 合法子域/根域必须照旧命中"""
        for cd, host in (
            (".bilibili.com", "www.bilibili.com"),
            (".bilibili.com", "bilibili.com"),
            (".bilibili.com", "api.bilibili.com"),
            (".taobao.com", "taobao.com"),
            (".taobao.com", "m.taobao.com"),
            ("bilibili.com", "www.bilibili.com"),      # 无前导点也认
        ):
            self.assertTrue(_match(cd, host), f"{cd} 该匹配 {host} 却没匹配")

    def test_lookalike_domains_are_rejected(self):
        """**核心**：仿冒域名不许拿到 cookie"""
        for cd, host in (
            (".taobao.com", "nottaobao.com"),           # 前缀伪装
            (".taobao.com", "taobao.com.evil.com"),     # 后缀伪装
            (".bilibili.com", "notbilibili.com"),
            (".bilibili.com", "bilibili.com.evil.com"),
            (".qq.com", "notqq.com"),
        ):
            self.assertFalse(_match(cd, host),
                             f"{cd} 不该匹配 {host} —— 这是 cookie 泄漏")

    def test_bare_endswith_would_be_wrong(self):
        """**钉住"不能改成 `endswith(d)`"** —— 那一版同样会被骗

        这是一条"反例的反例"：防止后人把子串匹配"修"成另一种同样错的写法。
        """
        self.assertTrue("nottaobao.com".endswith("taobao.com"),
                        "前提变了：endswith 不再被骗，本测试需重写")
        self.assertFalse(_match(".taobao.com", "nottaobao.com"))

    def test_empty_cookie_domain_rejected(self):
        """空域名条目自然排除（不靠额外分支）"""
        for cd in ("", ".", ".."):
            self.assertFalse(_match(cd, "www.bilibili.com"),
                             f"空域名 {cd!r} 不该匹配任何宿主")

    def test_source_no_longer_has_substring_branch(self):
        """**源码级**：那个 `d in host` 分支不许回来。

        ⚠️ **我第一版这里写错了，记录在案**：原先我断言的是
        `assertNotIn("or d in host", 整个源码文件)` ——
        **而我自己在注释里就写了 `or d in host` 这个词**（用来说明它为什么被删）
        ⇒ **正确状态下测试也是红的**。
        这正是本工程反复吃亏的那类错：**拿文本当结构**。
        改成**只看真正的 `if` 语句**（把注释排除掉再断言）。
        """
        import ast
        src = (ROOT / "kiana_vnext_plus" / "universal_downloader.py").read_text(
            encoding="utf-8")
        tree = ast.parse(src)
        # 只取候选判定语句的**代码文本**（ast.unparse 天然不含注释）
        hits = []
        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                # `ast.unparse` 天然不含注释 —— 这正是本测试要的：
                # 只看**代码**，不被注释里出现的字样骗到
                code = ast.unparse(node.test)
                if "endswith" in code and "root" in code:
                    hits.append(code)
        self.assertTrue(hits, "没找到域名判定语句 —— 函数被改名/重构了？")
        for code in hits:
            self.assertNotIn(
                "d in host", code,
                f"`d in host` 裸子串匹配又回来了（cookie 会泄漏到仿冒域名）: {code}")
            self.assertIn(
                'host.endswith("." + d)', code.replace("'", '"'),
                f"带点边界的后缀匹配没了（合法子域会取不到 cookie）: {code}")


if __name__ == "__main__":
    unittest.main()
