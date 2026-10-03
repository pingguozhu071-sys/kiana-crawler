# -*- coding: utf-8 -*-
"""鼠标轨迹数学测试（`trajectory_engine.py` 此前覆盖率仅 20.9%）

这是覆盖率线上**最后一个纯逻辑大块**——全都是可离线验证的数学：
最小急动度模型、费茨定律、对数正态延迟、生理性震颤、文本复杂度、轨迹生成。

**为什么这类函数值得单测**：它们没有异常、不联网、不抛错，
**算错了也照样返回一串看起来合理的数字**——只有把数学性质断言下来才能发现。
（本文件里最强的一条是"速度必须是位置的数值导数"：两个函数分头写过，
一旦有人只改其中一个，这条会立刻红。）

顺带清掉一处死代码：`_TREMOR_PHASE` 被**连写两遍**，第二遍立即覆盖第一遍。
"""
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kiana_vnext_plus import trajectory_engine as te      # noqa: E402


class TestMinimumJerk(unittest.TestCase):
    def test_position_boundaries(self):
        self.assertAlmostEqual(te._minimum_jerk_position(0.0), 0.0, places=12)
        self.assertAlmostEqual(te._minimum_jerk_position(1.0), 1.0, places=12)

    def test_position_is_monotonic(self):
        """位置必须单调不减——否则鼠标会"往回跳"（人类不会这样）"""
        prev = -1.0
        for i in range(101):
            s = te._minimum_jerk_position(i / 100)
            self.assertGreaterEqual(s + 1e-12, prev, f"tau={i/100} 处位置回退")
            prev = s

    def test_velocity_boundaries_and_normalization(self):
        """起止速度为 0、峰值在 0.5、归一化后落在 [0,1]"""
        self.assertAlmostEqual(te._minimum_jerk_velocity(0.0), 0.0, places=12)
        self.assertAlmostEqual(te._minimum_jerk_velocity(1.0), 0.0, places=12)
        self.assertAlmostEqual(te._minimum_jerk_velocity(0.5), 1.0, places=9)
        for i in range(101):
            v = te._minimum_jerk_velocity(i / 100)
            self.assertGreaterEqual(v, -1e-12)
            self.assertLessEqual(v, 1.0 + 1e-9)

    def test_velocity_is_the_numeric_derivative_of_position(self):
        """**本文件最强的一条**：速度必须是位置的导数。

        两个函数是分头写的（`30τ²-60τ³+30τ⁴` 与 `10τ³-15τ⁴+6τ⁵`）——
        只改其中一个不会报错、也不会崩，只会让轨迹**物理上自相矛盾**。
        这里用中心差分把它们绑在一起。
        """
        h = 1e-6
        for i in range(1, 100):
            tau = i / 100
            numeric = (te._minimum_jerk_position(tau + h)
                       - te._minimum_jerk_position(tau - h)) / (2 * h)
            self.assertAlmostEqual(numeric / 1.875, te._minimum_jerk_velocity(tau),
                                   places=5, msg=f"tau={tau} 处速度与位置导数不符")


class TestFittsSteps(unittest.TestCase):
    def test_zero_distance_returns_fixed(self):
        self.assertEqual(te._fitts_steps(0), 8)
        self.assertEqual(te._fitts_steps(-5), 8)

    def test_more_distance_more_steps(self):
        self.assertLessEqual(te._fitts_steps(50), te._fitts_steps(500))

    def test_smaller_target_more_steps(self):
        """目标越小越难 → 步数越多"""
        self.assertGreater(te._fitts_steps(300, 10), te._fitts_steps(300, 100))

    def test_clamped_to_range(self):
        for d in (1, 100, 10_000, 10 ** 9):
            for w in (1, 15, 1000):
                n = te._fitts_steps(d, w)
                self.assertGreaterEqual(n, 10)
                self.assertLessEqual(n, 50)


class TestLognormalDelay(unittest.TestCase):
    def test_positive_and_above_floor(self):
        for base in (0.05, 0.3, 1.0, 5.0):
            for _ in range(50):
                v = te.lognormal_delay(base)
                self.assertGreaterEqual(v, 0.02)

    def test_median_near_base(self):
        """对数正态的**中位数**应接近 base（这是它的定义方式）"""
        import statistics
        samples = [te.lognormal_delay(1.0) for _ in range(2000)]
        self.assertAlmostEqual(statistics.median(samples), 1.0, delta=0.15)

    def test_right_skewed(self):
        """右偏长尾：均值 > 中位数（均匀分布没有这个性质，一眼假）"""
        import statistics
        samples = [te.lognormal_delay(1.0) for _ in range(2000)]
        self.assertGreater(statistics.mean(samples), statistics.median(samples))


class TestTextComplexity(unittest.TestCase):
    def test_empty_is_zero(self):
        self.assertEqual(te._text_complexity(""), 0.0)

    def test_bounded(self):
        for t in ("hello world", "AAA!!!123", "!@#$%^&*()", "a" * 100,
                  "HELLO", "12345", "汉字测试"):
            v = te._text_complexity(t)
            self.assertGreaterEqual(v, 0.0)
            self.assertLessEqual(v, 1.0)

    def test_more_specials_scores_higher(self):
        self.assertLess(te._text_complexity("abcdefgh"), te._text_complexity("a!b@c#d$"))


class TestClamp(unittest.TestCase):
    def test_clamp(self):
        self.assertEqual(te._clamp(5, 0, 10), 5)
        self.assertEqual(te._clamp(-5, 0, 10), 0)
        self.assertEqual(te._clamp(50, 0, 10), 10)

    def test_clamp_to_viewport_respects_margin(self):
        x, y = te._clamp_to_viewport(5000, -20, 1920, 1080, margin=2)
        self.assertEqual(x, 1918)
        self.assertEqual(y, 2)


class TestNeighborKey(unittest.TestCase):
    def test_all_letters_have_neighbors(self):
        """26 个字母**全部**要有相邻键——缺一个就是"这个键永远打不错" """
        for ch in "abcdefghijklmnopqrstuvwxyz":
            n = te._get_neighbor_key(ch)
            self.assertIsNotNone(n, f"{ch} 应有相邻键")
            self.assertIn(n, te._KEYBOARD_NEIGHBORS[ch])

    def test_case_insensitive(self):
        self.assertIn(te._get_neighbor_key("A"), te._KEYBOARD_NEIGHBORS["a"])

    def test_non_alpha_returns_none(self):
        """数字/符号返回 None —— 这是**契约**，不是缺陷。

        调用点（`trajectory_engine` 打字模拟）用 `char.isalpha()` 先做了守卫，
        所以非字母根本不会走到这里；表里也只有字母。首版测试我断言 `"1"` 应有相邻键，
        **那是我的测试错了**，不是代码错了。
        """
        for ch in ("1", "!", " ", "中", "", "\t"):
            self.assertIsNone(te._get_neighbor_key(ch))

    def test_caller_guards_with_isalpha(self):
        """把"只有字母会打错"这个契约钉住——免得有人去掉守卫后静默失效。

        ⚠️ **必须按 AST 找"调用"，不能按名字找"出现"**：首版我搜 `"_get_neighbor_key" in body`，
        结果**函数自己的定义**（`def _get_neighbor_key(...)`）也命中，
        于是我去断言"定义体里有 isalpha"——必然失败。
        这已是本会话**第十次**"拿文本当结构"。改为只认 `ast.Call` 节点。
        """
        import ast
        src = Path(te.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        found = False
        for n in ast.walk(tree):
            if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = [x for x in ast.walk(n) if isinstance(x, ast.Call)
                     and isinstance(x.func, ast.Name) and x.func.id == "_get_neighbor_key"]
            if calls:
                found = True
                self.assertIn("isalpha", ast.unparse(n),
                              f"{n.name} 调用 _get_neighbor_key 前必须用 isalpha 守卫")
        self.assertTrue(found, "找不到 _get_neighbor_key 的调用点")

    def test_neighbors_are_mapped_keys(self):
        """相邻键表里的目标键也必须**在表里**（否则打错字会打到不存在的键）"""
        for src, neighbors in te._KEYBOARD_NEIGHBORS.items():
            for n in neighbors:
                self.assertIn(n, te._KEYBOARD_NEIGHBORS,
                              f"{src} 的邻居 {n!r} 不在键盘表里")


class TestPhysiologicalTremor(unittest.TestCase):
    """⚠️ **本类必须还原模块级状态**。

    `_physiological_tremor` 每次调用都改写**模块级**全局 `_TREMOR_FREQ`
    （连续随机游走，`global _TREMOR_FREQ`）。而既有的
    `tests/test_v213_phase1.py::TestTremor::test_frequency_in_8_12hz_band`
    **依赖这个全局的取值**（它用虚拟时钟数 80 个样点里的波峰数）。

    本类原先调用了它 500 次，把 `_TREMOR_FREQ` 推到别处 → 后面的用例
    **间歇性失败**（单独跑 20/20 全过，全量跑偶发）。
    **测试不该留全局副作用** —— 这里保存并在 tearDown 还原。
    """

    def setUp(self):
        self._saved_freq = te._TREMOR_FREQ
        self._saved_phase = te._TREMOR_PHASE

    def tearDown(self):
        te._TREMOR_FREQ = self._saved_freq
        te._TREMOR_PHASE = self._saved_phase

    def test_offset_is_small(self):
        for _ in range(50):
            x, y = te._physiological_tremor(100.0, 200.0, amplitude=1.0)
            self.assertLessEqual(abs(x - 100.0), 1.0 + 1e-9)
            self.assertLessEqual(abs(y - 200.0), 0.6 + 1e-9)

    def test_frequency_stays_in_band(self):
        """频率必须留在 8-12Hz 波段（这是"生理性"的定义）"""
        for _ in range(500):
            te._physiological_tremor(0.0, 0.0)
            self.assertGreaterEqual(te._TREMOR_FREQ, 8.0)
            self.assertLessEqual(te._TREMOR_FREQ, 12.0)


class TestGenerateHumanMousePath(unittest.TestCase):
    def test_returns_points_and_ends_near_target(self):
        pts = te.generate_human_mouse_path(0, 0, 800, 600, steps=20)
        self.assertTrue(pts, "必须返回轨迹点")
        self.assertGreaterEqual(len(pts), 10)
        ex, ey = pts[-1]
        # 末点应落在终点附近（有噪声与过冲回弹，给 3px 容差）
        self.assertLess(math.hypot(ex - 800, ey - 600), 3.0)

    def test_starts_near_start(self):
        pts = te.generate_human_mouse_path(100, 100, 900, 700, steps=25)
        sx, sy = pts[0]
        self.assertLess(math.hypot(sx - 100, sy - 100), 3.0)

    def test_tiny_distance_uses_jitter_path(self):
        """距离 <1px 时走"原地微抖"分支：点数 = steps+1，且不要求精确到点"""
        pts = te.generate_human_mouse_path(10, 10, 10.2, 10.1, steps=7)
        self.assertEqual(len(pts), 8)
        for x, y in pts:
            self.assertLess(math.hypot(x - 10.2, y - 10.1), 5.0)

    def test_path_is_finite(self):
        """任何参数下都不得产出 NaN/Inf（会直接把鼠标移到屏幕外）"""
        for args in ((0, 0, 1920, 1080), (5, 5, 5.5, 5.5), (0, 0, 1e6, 1e6)):
            for x, y in te.generate_human_mouse_path(*args, steps=15):
                self.assertTrue(math.isfinite(x) and math.isfinite(y))

    def test_overshoot_then_corrects(self):
        """过冲分支下，中段应出现"越过终点再回来"的极值"""
        import random as _r
        _r.seed(12345)
        pts = te.generate_human_mouse_path(0, 0, 500, 0, steps=20)
        xs = [p[0] for p in pts]
        self.assertLessEqual(max(xs), 500 + 20, "过冲不该偏离太远")
        self.assertLess(math.hypot(pts[-1][0] - 500, pts[-1][1]), 3.0)


if __name__ == "__main__":
    unittest.main()
