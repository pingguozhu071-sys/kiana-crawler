# -*- coding: utf-8 -*-
"""域名提取**唯一实现**守卫

**发现的真问题**：同一能力有**三份**，且在真实输入上**结论不一致**：

| 输入 | `url_utils.extract_domain`（旧） | `rate_limiter._domain_of` | `session_pool._domain_of` |
|---|---|---|---|
| `example.com`（裸域名） | **`''`（空串）** | `example.com` | `example.com` |
| `https://[`（畸形） | **抛 ValueError** | `unknown` | **抛 ValueError** |
| `not a url` | `''` | `not a url` | `not a url` |

两个后果都真实可达：

① **裸域名种子**会让 `frontier.domain` 存成**空串**——而 `extract_domain` 的调用点
   恰恰只有 `frontier.push` / `redis_frontier.push` 两处，都是**写 `domain` 列**。
   于是所有裸域名种子挤进同一个空桶，而限流器与会话池却按 `example.com` 分桶：
   **域名限额、域名级冷却、`count_done_by_domain` 全部错位**。
② **畸形 URL 会抛异常**，而该函数在 `frontier.push` 里被调用、`push` 又在爬取主循环上。

现三处统一委托 `url_utils.extract_domain`（缺 scheme 就补、绝不抛异常）。
"""
import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kiana_vnext_plus.rate_limiter import RateLimiter              # noqa: E402
from kiana_vnext_plus.session_pool import SessionPool              # noqa: E402
from kiana_vnext_plus.url_utils import extract_domain              # noqa: E402

PKG = ROOT / "kiana_vnext_plus"

# 三处实现（现在应完全一致）
IMPLS = {
    "url_utils.extract_domain": staticmethod(extract_domain),
    "rate_limiter._domain_of": staticmethod(RateLimiter._domain_of),
    "session_pool._domain_of": staticmethod(SessionPool._domain_of),
}

# 曾经让三者分道扬镳的输入
CASES = [
    "https://www.Example.com/a/b?x=1",
    "http://example.com:8080/p",
    "example.com",                 # ← 裸域名：旧 extract_domain 给空串
    "example.com:8080",
    "not a url",                   # ← rate_limiter 旧版给 'not a url'
    "https://[",                   # ← 畸形：旧版直接抛 ValueError
    "", None, "/a/b", "https://a.b.c.d/e",
]


class TestThreeImplementationsAgree(unittest.TestCase):
    def test_all_agree_on_every_case(self):
        """**本文件最重要的一条**：三处对每个输入必须给出同一结果"""
        problems = {}
        for case in CASES:
            vals = {}
            for name, fn in IMPLS.items():
                try:
                    vals[name] = fn(case)
                except Exception as e:
                    vals[name] = f"RAISES:{type(e).__name__}"
            if len(set(vals.values())) > 1:
                problems[repr(case)] = vals
        self.assertEqual(problems, {}, f"域名提取仍不一致: {problems}")

    def test_bare_domain_is_not_empty(self):
        """① 裸域名必须能提取出域名（旧版给空串，导致 frontier 域名桶错位）"""
        for case in ("example.com", "example.com:8080"):
            got = extract_domain(case)
            self.assertTrue(got, f"{case} 提取结果为空 —— 域名桶会全部挤进空串")
            self.assertIn("example.com", got)

    def test_never_raises(self):
        """② 畸形输入不得抛异常（该函数在爬取主循环的 push 里被调用）"""
        for case in CASES + ["https://[:::", "http://", "://x", "a" * 500]:
            try:
                extract_domain(case)
            except Exception as e:      # pragma: no cover
                self.fail(f"extract_domain({case!r}) 抛了 {type(e).__name__}")

    def test_junk_input_is_unchanged(self):
        """垃圾输入行为不变（只补"像主机名"的输入，别把垃圾当域名）"""
        for junk in ("not a url", "", None, "/a/b"):
            self.assertEqual(extract_domain(junk), "")


class TestSingleSourceStructure(unittest.TestCase):
    def _src(self, name):
        return (PKG / name).read_text(encoding="utf-8")

    def test_the_two_helpers_delegate(self):
        """结构上确认两处 `_domain_of` 真的**在代码里**委托唯一实现。

        ⚠️ **必须剥掉 docstring 再判**：本文件的 docstring 里就写着
        "改为委托 `url_utils.extract_domain`"——整函数 unparse 会把注释文字也算进去，
        于是"把实现改回本地版本"的变异**测不出来**（首版就是这样，变异只被另一条用例
        抓到，这条形同虚设）。这与本会话反复踩的"靠源码文本判"是同一个坑。
        """
        for name in ("rate_limiter.py", "session_pool.py"):
            tree = ast.parse(self._src(name))
            fn = None
            for n in ast.walk(tree):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_domain_of":
                    fn = n
            self.assertIsNotNone(fn, f"{name} 找不到 _domain_of")
            body = fn.body
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                body = body[1:]                      # 剥掉 docstring
            code = "\n".join(ast.unparse(s) for s in body)
            self.assertIn("extract_domain", code,
                          f"{name}._domain_of 的**代码**里没有委托唯一实现 —— 又会分叉")

    def test_no_second_domain_extractor_function(self):
        """反向扫描：**具名的域名提取函数**不许再出现第二份。

        首版这里扫的是"所有 `urlparse(...).netloc`"，结果报了 14 处——
        其中绝大多数是**正当的直接用法**（拼 Referer、取目录名、判断主站），
        not "域名提取实现"。过严的反向扫描会逼人绕过它，故收窄到**函数名**：
        凡名字像域名提取器的，都必须委托 `extract_domain`。
        """
        import re as _re
        name_pat = _re.compile(r"(domain_of|extract_domain|get_domain|host_of)$")
        offenders = []
        for p in sorted(PKG.glob("*.py")):
            if p.name == "url_utils.py":       # 唯一实现所在处，豁免
                continue
            try:
                tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
            except Exception:
                continue
            for n in ast.walk(tree):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and name_pat.search(n.name):
                    if "extract_domain" not in ast.unparse(n):
                        offenders.append(f"{p.name}:{n.lineno}:{n.name}")
        self.assertEqual(offenders, [],
                         f"又出现了第二份域名提取实现（应委托 extract_domain）: {offenders}")


if __name__ == "__main__":
    unittest.main()
