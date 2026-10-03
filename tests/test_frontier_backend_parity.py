# -*- coding: utf-8 -*-
"""frontier 两个后端**接口平价**守卫（SQLite vs Redis）

**发现的真问题**：`frontier_backend="redis"` 时爬取**根本跑不起来**——

  ① `crawler` 入口播种调用 `push(..., force=True, parent_hash=...)`，
     而 Redis 的 `push` 只有 `(url, depth, priority)` → **TypeError**；
  ② `crawler` 取任务调用 `pop_batch(..., strategy=...)`，
     而 Redis 的 `pop_batch` 没有该形参 → **TypeError**
     （且 crawler 对它有"重试 3 次后 re-raise"的包装 → **整场爬取终止**）；
  ③ Redis 后端**缺 6 个引擎实际调用的方法**（`flush` / `mark_duplicate` /
     `count_done_total` / 三个冷却方法）→ **AttributeError**；
     其中 `flush()` 在 `crawler` 种子入队后**第一轮就调**。

**为什么一直没被发现**：`test_v2196_review_fixes.py` **已经**有一条"Redis 后端签名须与
FrontierDB 对齐"的用例——但它检查的是**手写的 4 个方法**（`mark_done` / `mark_failed` /
`mark_done_checked` / `heartbeat_lease`）。**手写清单本身就是这个洞的来源。**

所以本文件的清单**自动从真实调用点推导**（AST 扫 `self.frontier.X(` / `frontier.X(`），
不手写——这样新加一处调用点也会被自动纳入检查。
"""
import ast
import inspect
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kiana_vnext_plus.frontier import FrontierDB           # noqa: E402
from kiana_vnext_plus.redis_frontier import RedisFrontier  # noqa: E402

PKG = ROOT / "kiana_vnext_plus"

# 引擎在 frontier 上调用时的接收者名（`self.frontier` 与传参进来的 `frontier`）
_RECEIVERS = ("self.frontier", "frontier")


def engine_call_surface() -> dict:
    """AST 扫出**引擎实际调用的** frontier 方法 → `{name: [调用点]}`。

    **不手写清单**——手写清单正是本文件要修的那个洞。
    """
    surface = {}
    for p in sorted(PKG.glob("*.py")):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call) or not isinstance(n.func, ast.Attribute):
                continue
            if ast.unparse(n.func.value) in _RECEIVERS:
                surface.setdefault(n.func.attr, []).append(f"{p.name}:{n.lineno}")
    return surface


SURFACE = engine_call_surface()


class TestCallSurfaceSanity(unittest.TestCase):
    def test_surface_is_non_trivial(self):
        """夹具自证：扫描一旦失效（比如接收者写法变了），下面的用例会变成**假绿**"""
        self.assertGreaterEqual(len(SURFACE), 20,
                                f"只扫到 {len(SURFACE)} 个调用点，扫描可能已失效: {sorted(SURFACE)}")

    def test_key_calls_are_present(self):
        for must in ("push", "pop_batch", "flush", "mark_failed", "mark_done"):
            self.assertIn(must, SURFACE, f"扫描没扫到 {must} —— 扫描逻辑有问题")


class TestBothBackendsImplementTheSurface(unittest.TestCase):
    def test_redis_implements_every_method_the_engine_calls(self):
        """③ 引擎调用到的每个方法，Redis 后端都必须有（否则 AttributeError）"""
        missing = {name: SURFACE[name] for name in sorted(SURFACE)
                   if not hasattr(RedisFrontier, name)}
        self.assertEqual(missing, {},
                         f"RedisFrontier 缺少引擎会调用的方法: "
                         f"{ {k: v[:3] for k, v in missing.items()} }——"
                         f"`frontier_backend=\"redis\"` 会在这些调用点抛 AttributeError")

    def test_sqlite_implements_every_method_the_engine_calls(self):
        missing = [n for n in sorted(SURFACE) if not hasattr(FrontierDB, n)]
        self.assertEqual(missing, [], f"FrontierDB 缺少: {missing}")


class TestSignatureParity(unittest.TestCase):
    """① ② 签名必须兼容——否则是 TypeError（比 AttributeError 更晚、更难查）"""

    def test_redis_accepts_at_least_the_sqlite_params(self):
        """对**两边都有**的方法：Redis 必须接受 SQLite 的全部形参名。

        这就是 `push(force=)` / `pop_batch(strategy=)` 两个 TypeError 的通用形式。

        注意 `(*args, **kwargs)` 形式的纯委托（`write_page` / `write_extracted` /
        `write_error`）**是兼容的**——`**kwargs` 能接住任意关键字实参。
        首版没考虑这点，把它们误报成失配；这里显式处理。
        """
        problems = {}
        for name in sorted(SURFACE):
            if not (hasattr(FrontierDB, name) and hasattr(RedisFrontier, name)):
                continue
            try:
                db_sig = inspect.signature(getattr(FrontierDB, name))
                rd_sig = inspect.signature(getattr(RedisFrontier, name))
            except (TypeError, ValueError):
                continue
            kinds = {p.kind for p in rd_sig.parameters.values()}
            if inspect.Parameter.VAR_KEYWORD in kinds:
                continue          # **kwargs：任意关键字都能接，天然兼容
            missing = sorted(set(db_sig.parameters) - set(rd_sig.parameters))
            if missing:
                problems[name] = missing
        self.assertEqual(problems, {},
                         f"Redis 后端形参不全（调用点会 TypeError）: {problems}")

    def test_push_and_pop_batch_regression_pins(self):
        """把本轮修掉的两个 TypeError 钉死（直接对着调用点签名断言）"""
        for cls in (FrontierDB, RedisFrontier):
            push = inspect.signature(cls.push).parameters
            self.assertIn("force", push)
            self.assertIn("parent_hash", push)
            pop = inspect.signature(cls.pop_batch).parameters
            self.assertIn("strategy", pop)

    def test_flush_and_cooldown_pins(self):
        """把 AttributeError 那批钉死"""
        for name in ("flush", "mark_duplicate", "count_done_total",
                     "set_domain_cooldown", "clear_domain_cooldown", "load_active_cooldowns"):
            self.assertTrue(hasattr(RedisFrontier, name), f"RedisFrontier 缺 {name}")


if __name__ == "__main__":
    unittest.main()
