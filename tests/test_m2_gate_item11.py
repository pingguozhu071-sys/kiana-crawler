# -*- coding: utf-8 -*-
"""门禁第 11 项（结构指纹）回归（M2-c）

要锁死的三件事：
  ① **离线基准不符必须 FAIL**——样本被替换/截断，或指纹算法悄悄改了，都要红；
  ② **数不出来必须 SKIP**（退出码 2），不许冒充 PASS；
  ③ `check_verify_all` 的**假 PASS 口子**必须保持关闭：原判定
     `rc == 0 and "PASS" in out` 会让"总体 FAIL、某单项 PASS"被判成 PASS。
"""
import json
import sys
import tempfile
import shutil
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import fingerprint_baseline as fb          # noqa: E402
import ast                                 # noqa: E402


class TestOfflineBaseline(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="kiana_fp11_"))
        self._orig = fb.BASELINE

    def tearDown(self):
        fb.BASELINE = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, samples):
        p = self.tmp / "baseline.json"
        p.write_text(json.dumps({"_说明": ["keep"], "samples": samples},
                                ensure_ascii=False), encoding="utf-8")
        fb.BASELINE = p
        return p

    def test_real_baseline_matches(self):
        self.assertEqual(fb.check_offline(), fb.EXIT_OK, "入库基准必须与样本一致")

    def test_drift_is_detected(self):
        cur = fb._compute()
        bad = dict(cur)
        first = sorted(bad)[0]
        bad[first] = int(bad[first]) ^ 0xFF          # 指纹被改
        self._write(bad)
        self.assertEqual(fb.check_offline(), fb.EXIT_MISMATCH,
                         "指纹漂移必须 FAIL（否则算法改了也没人知道）")

    def test_missing_sample_in_baseline_is_detected(self):
        cur = fb._compute()
        partial = dict(cur)
        partial.pop(sorted(partial)[0])              # 基准里少一个样本
        self._write(partial)
        self.assertEqual(fb.check_offline(), fb.EXIT_MISMATCH)

    def test_extra_sample_not_in_baseline_is_detected(self):
        cur = dict(fb._compute())
        cur["brand_new_sample.html"] = 12345         # 新样本未入基准
        self._write(cur)
        self.assertEqual(fb.check_offline(), fb.EXIT_MISMATCH,
                         "新增样本必须重建基准（强制显式动作 + 写明原因）")

    def test_missing_or_corrupt_baseline_is_detected(self):
        fb.BASELINE = self.tmp / "nope.json"
        self.assertEqual(fb.check_offline(), fb.EXIT_MISMATCH)
        bad = self.tmp / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        fb.BASELINE = bad
        self.assertEqual(fb.check_offline(), fb.EXIT_MISMATCH)

    def test_write_baseline_refuses_to_drop_explanation(self):
        """重建时不许把 `_说明` 段弄丢——那是给后来者看的"""
        p = self.tmp / "no_explain.json"
        p.write_text(json.dumps({"samples": {}}, ensure_ascii=False), encoding="utf-8")
        fb.BASELINE = p
        self.assertEqual(fb.write_baseline(), fb.EXIT_MISMATCH)


class TestCannotMeasureIsSkip(unittest.TestCase):
    def test_online_without_targets_returns_cannot_measure(self):
        """数不出来 → 退出码 2（门禁映射为 SKIP），**不是** 0（PASS）"""
        import kiana_vnext_plus.config as cfg
        orig = cfg.data_root
        try:
            cfg.data_root = lambda: tempfile.mkdtemp(prefix="kiana_notargets_")
            self.assertEqual(fb.run_online(), fb.EXIT_CANNOT_MEASURE)
        finally:
            cfg.data_root = orig


class TestFalsePassHoleStaysClosed(unittest.TestCase):
    def _fn(self, fn_name):
        tree = ast.parse((ROOT / "tools" / "release_check.py").read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name == fn_name:
                return n
        self.fail(f"找不到 {fn_name}")

    def test_verify_all_uses_overall_line_only(self):
        """必须只认"总体: ✅ PASS"，不许再出现宽松合取。

        **用 AST 判而非字符串匹配**：首版这里写成 `assertNotIn('and "PASS" in out', src)`，
        而 `ast.unparse` 会把字符串**规范化成单引号**，于是变异注入（把宽松合取加回去）
        根本测不出来 —— 一条自己就漏检的测试。改为直接看那个 `if` 的条件节点：
        它必须是单条比较，不得是 `and`/`or` 组合。
        """
        fn = self._fn("check_verify_all")
        # 纯 AST 判定：先按**节点结构**找"返回 PASS"的分支，再取其所在 if 的条件。
        # 不用 unparse 出来的字符串去 match —— 元组会被加括号、字符串引号会被规范化，
        # 这类"靠源码文本判"的写法在本轮已经骗过我一次（见 docstring）。
        pass_returns = [n for n in ast.walk(fn)
                        if isinstance(n, ast.Return)
                        and isinstance(n.value, ast.Tuple)
                        and n.value.elts
                        and isinstance(n.value.elts[0], ast.Name)
                        and n.value.elts[0].id == "PASS"]
        self.assertTrue(pass_returns, "找不到返回 PASS 的分支")
        ids = {id(r) for r in pass_returns}
        pass_ifs = [n for n in ast.walk(fn)
                    if isinstance(n, ast.If) and any(id(s) in ids for s in n.body)]
        self.assertTrue(pass_ifs, "返回 PASS 的分支不在任何 if 里（判定逻辑变了？）")
        for n in pass_ifs:
            self.assertNotIsInstance(
                n.test, ast.BoolOp,
                "判定条件不得是 and/or 组合——那会让'总体 FAIL、某单项 PASS'被判成 PASS")
            self.assertIn("总体", ast.unparse(n.test), "必须以总体行为准")

    def test_structure_probe_maps_exit2_to_skip(self):
        """第 11 项：退出码 2 = 数不出来 = SKIP（不是 PASS、也不是 FAIL）"""
        src = ast.unparse(self._fn("check_structure_probe"))
        self.assertIn("SKIP", src)
        self.assertIn("rc == 2", src)
        self.assertIn("--online", src, "在线部分必须显式开启")

    def test_gate_registers_item_11(self):
        tree = ast.parse((ROOT / "tools" / "release_check.py").read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name == "main":
                src = ast.unparse(n)
                self.assertIn("check_structure_probe", src, "第 11 项必须被注册进门禁")
                return
        self.fail("找不到 main")


if __name__ == "__main__":
    unittest.main()
