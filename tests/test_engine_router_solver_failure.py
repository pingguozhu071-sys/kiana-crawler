# -*- coding: utf-8 -*-
"""回退链上的求解异常**不许被静默吞掉**（engine_router._try_solver）

**背景**：`engine_router._try_solver` 原来是

```python
except Exception:
    pass
```

求解引擎抛异常（浏览器崩 / CDP 失联 / 超时）会被**无声吞掉**，调用方只看到 `None`，
于是"**求解器崩了**"与"**求解器没给出可用响应**"变得**无法区分**。
而整条回退链存在的意义就是回答"这个站为什么失败"——M2 的结构探针与 trace 取证
都是为这个问题服务的；这里是回退链上**最该留下线索**的一处。

本文件锁三件事：
  ① 求解抛异常时：**不向外抛**（回退链要继续走）、**计数 +1**、**代理一定归还**；
  ② 计数从 0 开始且可读（可观测性，不是内部状态）；
  ③ 结构上：该文件不再有"只 pass 且无理由"的处理器。
"""
import ast
import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from kiana_vnext_plus.engine_router import EngineRouter      # noqa: E402
import silent_failure_scan as sfs                            # noqa: E402


class _ExitMgr:
    def __init__(self):
        self.released = []

    async def acquire_for_domain(self, domain):
        return "http://127.0.0.1:9/proxy"

    async def release(self, proxy):
        self.released.append(proxy)


class _BoomSolver:
    async def solve(self, *a, **k):
        raise RuntimeError("浏览器崩了")


class _OkSolver:
    async def solve(self, *a, **k):
        return "<html>ok</html>", 200, {}


def _router(solver):
    return EngineRouter(protocol=None, solver=solver, session_pool=None,
                        exit_mgr=_ExitMgr())


PUBLIC_URL = "https://example.com/a"


class TestSolverExceptionIsVisible(unittest.TestCase):
    def test_counter_starts_at_zero(self):
        self.assertEqual(_router(_BoomSolver()).solver_exceptions, 0)

    def test_exception_is_counted_and_not_raised(self):
        """① 不向外抛（回退链要继续）+ 计数 +1（线索要留下）"""
        r = _router(_BoomSolver())
        out = asyncio.run(r._try_solver(PUBLIC_URL, "example.com"))
        self.assertIsNone(out, "求解失败应返回 None，让回退链继续")
        self.assertEqual(r.solver_exceptions, 1,
                         "求解异常必须被计数——否则'崩了'与'没响应'无法区分")

    def test_counter_accumulates(self):
        r = _router(_BoomSolver())
        for _ in range(3):
            asyncio.run(r._try_solver(PUBLIC_URL, "example.com"))
        self.assertEqual(r.solver_exceptions, 3)

    def test_proxy_is_released_even_when_solver_raises(self):
        """finally 必须归还代理——否则异常路径会漏掉一个出口"""
        r = _router(_BoomSolver())
        asyncio.run(r._try_solver(PUBLIC_URL, "example.com"))
        self.assertEqual(r.exit_mgr.released, ["http://127.0.0.1:9/proxy"])

    def test_success_path_unchanged(self):
        """修的是异常路径，成功路径的行为不许被带坏"""
        r = _router(_OkSolver())
        resp = asyncio.run(r._try_solver(PUBLIC_URL, "example.com"))
        self.assertIsNotNone(resp)
        self.assertEqual(resp.status_code, 200)     # ResponseAdapter 暴露的是 status_code
        self.assertEqual(r.solver_exceptions, 0, "成功不该计数")

    def test_private_url_never_reaches_solver(self):
        """SSRF 闸仍在（修复不得绕过它）"""
        class _Spy:
            called = False

            async def solve(self, *a, **k):
                _Spy.called = True
                return "", 200, {}

        r = _router(_Spy())
        self.assertIsNone(asyncio.run(r._try_solver("http://127.0.0.1/x", "localhost")))
        self.assertFalse(_Spy.called, "私网 URL 不得触达求解器")


class TestNoSilentHandlerLeft(unittest.TestCase):
    def test_file_has_no_unexplained_pass_only_handler(self):
        """③ 结构上确认那处 `except: pass` 真的没了（不是靠读注释相信）"""
        hits = sfs.scan_file(ROOT / "kiana_vnext_plus" / "engine_router.py")
        self.assertEqual(hits, [], f"engine_router.py 仍有静默处理器: {hits}")

    def test_solver_except_logs_the_exception(self):
        """必须**记日志**而不只是计数：类型与原因要能在日志里看到"""
        src = (ROOT / "kiana_vnext_plus" / "engine_router.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        found = False
        for n in ast.walk(tree):
            if isinstance(n, ast.ExceptHandler) and n.type is not None:
                body = ast.unparse(n.body)
                if "solver_exceptions" in body:
                    found = True
                    self.assertIn("type(e).__name__", body, "日志要带上异常类型")
                    self.assertIn("logger.warning", body, "至少是 warning 级别")
        self.assertTrue(found, "找不到求解异常的处理分支——它被改回静默了吗？")


if __name__ == "__main__":
    unittest.main()
