"""v2.19 测试补强：反检测主干结构回归（批次 6.1）

背景：评估指出反检测六个模块（evasion_engine / evasion_v2 / ultimate_evasion /
stealth_v3 / adaptive_v2 / smart_adaptive）**零测试覆盖**——而这正是工程的核心卖点。
本文件不启动真实浏览器，只验证"生成逻辑的结构正确性"（清单完整性、脚本非空、
维度表合法、覆盖关系、纯函数行为）。

全部离线。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 最小指纹（各 build_* 均按 .get 容错取值）
_FP = {
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/136.0.0.0 Safari/537.36",
    "platform": "Win32",
    "language": "zh-CN",
    "languages": ["zh-CN", "zh", "en"],
    "hardware_concurrency": 16,
    "device_memory": 8,
    "screen_width": 1920,
    "screen_height": 1080,
    "color_depth": 24,
    "timezone": "Asia/Shanghai",
    "geo_code": "CN",
    "webgl_vendor": "Google Inc. (NVIDIA)",
    "webgl_renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0)",
    "canvas_noise": 0.01,
}


class TestEvasionScriptGeneration(unittest.TestCase):
    """脚本生成：返回非空 JS 且含关键 API 覆写"""

    def test_evasion_engine_scripts(self):
        from kiana_vnext_plus import evasion_engine as m
        js = m.build_evasion_scripts(dict(_FP))
        self.assertIsInstance(js, str)
        self.assertGreater(len(js), 200, "应生成实质脚本（非空壳）")
        # 关键反检测面：navigator 属性覆写
        self.assertIn("navigator", js.lower())

    def test_combined_stealth_scripts(self):
        from kiana_vnext_plus import evasion_engine as m
        js = m.build_combined_stealth(dict(_FP))
        self.assertIsInstance(js, str)
        self.assertGreater(len(js), 200)

    def test_ultimate_scripts(self):
        from kiana_vnext_plus import ultimate_evasion as m
        js = m.build_ultimate_evasion_scripts(dict(_FP))
        self.assertIsInstance(js, str)
        self.assertGreater(len(js), 200)

    def test_ultimate_combined(self):
        from kiana_vnext_plus import ultimate_evasion as m
        js = m.build_ultimate_combined(dict(_FP))
        self.assertIsInstance(js, str)
        self.assertGreater(len(js), 200)

    def test_injection_scripts(self):
        from kiana_vnext_plus import injection_scripts as m
        js = m.build_stealth_scripts(dict(_FP))
        self.assertIsInstance(js, str)
        self.assertGreater(len(js), 100)

    def test_biometrics_script(self):
        from kiana_vnext_plus import behavioral_biometrics as m
        js = m.build_biometrics_script(dict(_FP))
        self.assertIsInstance(js, str)
        self.assertGreater(len(js), 100)

    def test_empty_fingerprint_does_not_crash(self):
        """空指纹不得抛异常（各 build_* 应容错降级）"""
        from kiana_vnext_plus import (evasion_engine, ultimate_evasion,
                                      injection_scripts, behavioral_biometrics)
        for fn in (evasion_engine.build_evasion_scripts,
                   evasion_engine.build_combined_stealth,
                   ultimate_evasion.build_ultimate_evasion_scripts,
                   ultimate_evasion.build_ultimate_combined,
                   injection_scripts.build_stealth_scripts,
                   behavioral_biometrics.build_biometrics_script):
            out = fn({})
            self.assertIsInstance(out, str, f"{fn.__name__}({{}}) 应返回字符串")


class TestDimensionTables(unittest.TestCase):
    """维度表：结构合法（三元组/编号唯一/非空说明）"""

    def _check(self, dims, name):
        self.assertIsInstance(dims, list)
        self.assertGreater(len(dims), 0, f"{name} 维度表不得为空")
        ids = []
        for it in dims:
            self.assertEqual(len(it), 3, f"{name} 每项应为 (编号, 名称, 说明) 三元组")
            _no, label, desc = it
            self.assertTrue(str(label).strip(), f"{name} 维度名不得为空")
            self.assertTrue(str(desc).strip(), f"{name} 维度说明不得为空")
            ids.append(str(_no))
        self.assertEqual(len(ids), len(set(ids)), f"{name} 维度编号必须唯一")

    def test_evasion_dimensions(self):
        from kiana_vnext_plus import evasion_engine as m
        self._check(m.get_evasion_dimensions(), "evasion")

    def test_ultimate_dimensions(self):
        from kiana_vnext_plus import ultimate_evasion as m
        self._check(m.get_ultimate_dimensions(), "ultimate")

    def test_biometrics_dimensions(self):
        from kiana_vnext_plus import behavioral_biometrics as m
        self._check(m.get_biometrics_dimensions(), "biometrics")


class TestTrajectoryPureFunctions(unittest.TestCase):
    """人类运动模型纯函数（离线可精确断言）"""

    def test_minimum_jerk_boundaries(self):
        from kiana_vnext_plus.trajectory_engine import _minimum_jerk_position
        self.assertAlmostEqual(_minimum_jerk_position(0.0), 0.0, places=6)
        self.assertAlmostEqual(_minimum_jerk_position(1.0), 1.0, places=6)

    def test_minimum_jerk_monotonic(self):
        """位置应单调递增（加速-匀速-减速，不得回退）"""
        from kiana_vnext_plus.trajectory_engine import _minimum_jerk_position
        prev = -1.0
        for i in range(0, 21):
            v = _minimum_jerk_position(i / 20.0)
            self.assertGreaterEqual(v + 1e-9, prev, f"tau={i/20} 处位置回退")
            prev = v

    def test_minimum_jerk_velocity_bounds(self):
        from kiana_vnext_plus.trajectory_engine import _minimum_jerk_velocity
        self.assertAlmostEqual(_minimum_jerk_velocity(0.0), 0.0, places=6)
        self.assertAlmostEqual(_minimum_jerk_velocity(1.0), 0.0, places=6)
        # 峰值在 tau=0.5 附近且归一化后 <= 1
        self.assertAlmostEqual(_minimum_jerk_velocity(0.5), 1.0, places=2)

    def test_fitts_steps_bounds(self):
        from kiana_vnext_plus.trajectory_engine import _fitts_steps
        self.assertEqual(_fitts_steps(0), 8, "零距离应返回默认步数")
        s_far = _fitts_steps(2000, 10)
        s_near = _fitts_steps(50, 10)
        self.assertGreater(s_far, s_near, "距离越远步数应越多（费茨定律）")
        for d, w in ((1, 1), (5000, 1), (100, 400)):
            self.assertGreaterEqual(_fitts_steps(d, w), 10)
            self.assertLessEqual(_fitts_steps(d, w), 50)

    def test_lognormal_delay_distribution(self):
        """对数正态延迟：非负、右偏长尾（中位数接近 base、存在明显大于 base 的样本）"""
        from kiana_vnext_plus.trajectory_engine import lognormal_delay
        samples = [lognormal_delay(0.5, sigma=0.7, min_v=0.02) for _ in range(400)]
        self.assertTrue(all(s >= 0.02 for s in samples), "延迟不得低于下限")
        m = sorted(samples)[len(samples) // 2]  # 中位数
        mx = max(samples)
        self.assertLess(m * 3, mx, "分布应右偏长尾（最大值显著大于中位数）")

    def test_lognormal_bad_input_safe(self):
        from kiana_vnext_plus.trajectory_engine import lognormal_delay
        self.assertGreaterEqual(lognormal_delay(0), 0)
        self.assertGreaterEqual(lognormal_delay(-5), 0)


class TestDomainHealthStateMachine(unittest.TestCase):
    """smart_adaptive.DomainHealth 状态机（此前零覆盖）

    注：block_rate / success_rate / avg_latency / risk_score 均为 **property**。
    """

    def test_record_and_metrics(self):
        from kiana_vnext_plus.smart_adaptive import DomainHealth
        h = DomainHealth("example.com")
        for _ in range(3):
            h.record(True, latency=0.5, status_code=200)
        self.assertGreater(h.success_rate, 0)
        self.assertAlmostEqual(h.block_rate, 0.0, places=6)
        self.assertLess(h.risk_score, 5, "全成功时风险分应较低")

    def test_blocks_raise_risk(self):
        from kiana_vnext_plus.smart_adaptive import DomainHealth
        h = DomainHealth("blocked.example")
        for _ in range(6):
            h.record(False, latency=1.0, status_code=403)
        self.assertGreater(h.block_rate, 0.3, "连续 403 应体现封禁率")
        self.assertGreater(h.risk_score, 0, "被封后风险分应上升")
        self.assertEqual(h.recovery_state, "cooling", "封禁后应进入冷却态")

    def test_success_recovers_state(self):
        from kiana_vnext_plus.smart_adaptive import DomainHealth
        h = DomainHealth("recover.example")
        h.record(False, latency=1.0, status_code=429)
        self.assertEqual(h.recovery_state, "cooling")
        h.record(True, latency=0.3, status_code=200)
        self.assertEqual(h.recovery_state, "recovering", "成功后应转入恢复态")
        self.assertEqual(h.consecutive_blocks, 0, "成功后连续封禁计数应清零")

    def test_metrics_safe_when_empty(self):
        from kiana_vnext_plus.smart_adaptive import DomainHealth
        h = DomainHealth("empty.example")
        # 空样本：property 访问不得抛异常（且应有合理默认）
        self.assertEqual(h.block_rate, 0.0)
        self.assertEqual(h.success_rate, 1.0)
        h.avg_latency; h.risk_score  # noqa: B018 - 仅验证可访问


class TestDataCleanerAndAdaptive(unittest.TestCase):
    """adaptive_v2 的纯工具与控制器基本行为"""

    def test_url_normalization(self):
        from kiana_vnext_plus.adaptive_v2 import DataCleaner
        out = DataCleaner.normalize_url("HTTPS://Example.COM/Path?b=2&a=1#frag")
        self.assertIn("example.com", out, "host 应小写")
        self.assertNotIn("frag", out, "fragment 应移除")
        self.assertLess(out.index("a=1"), out.index("b=2"), "query 参数应排序")

    def test_clean_html_removes_scripts_and_comments(self):
        from kiana_vnext_plus.adaptive_v2 import DataCleaner
        out = DataCleaner.clean_html(
            "<html><body><p>你好</p><script>var x=1;</script><!-- c --></body></html>")
        self.assertIn("你好", out, "正文应保留")
        self.assertNotIn("var x=1", out, "script 内容应移除")
        self.assertNotIn("<!--", out, "注释应移除")

    def test_junk_url_filter(self):
        from kiana_vnext_plus.adaptive_v2 import DataCleaner
        self.assertTrue(DataCleaner.filter_junk_url("https://x.com/pixel.gif?utm_source=a"))
        self.assertTrue(DataCleaner.filter_junk_url("https://doubleclick.net/ad"))
        self.assertFalse(DataCleaner.filter_junk_url("https://example.com/article/1"))

    def test_structured_extraction_reuses_linear_regex(self):
        from kiana_vnext_plus.adaptive_v2 import DataCleaner
        got = DataCleaner.extract_structured("联系 a.b@x.com 电话 13812345678")
        self.assertIn("a.b@x.com", got["emails"])
        self.assertIn("13812345678", got["phones"])

    def test_controller_record_does_not_crash(self):
        from kiana_vnext_plus.adaptive_v2 import AdaptiveControllerV2
        try:
            c = AdaptiveControllerV2()
        except TypeError:
            self.skipTest("AdaptiveControllerV2 构造需外部依赖，跳过")
        for ok, code in ((True, 200), (False, 403), (True, 200), (False, 429)):
            c.record("example.com", ok, latency=0.8, ban_detected=(code in (403, 429)))
        try:
            c.stop()
        except Exception:
            pass


if __name__ == "__main__":
    unittest.main()
