# -*- coding: utf-8 -*-
"""反爬量化靶场回归（M3-c）

要锁死的三件事：
  ① **`unknown` 不进分母**，且"一个都没测出来"必须是 `score=None`——
     **不能记成 0 分**："没测"与"很差"是两回事，混了就会把 SKIP 说成 FAIL；
  ② **只许升不许降**：分数线下降即判退步；且要**逐站**报出掉了哪些站
     （只看总分会被"一升一降"抵消掉）；
  ③ 单站异常**不中断整轮**（记为 unknown），否则一个站点抽风会让整轮没有结果。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import stealth_bench as sb          # noqa: E402


class TestSiteList(unittest.TestCase):
    def test_borrowed_list_is_intact(self):
        """清单是**只借清单与方法**借来的——键要稳定，否则基线对不上号"""
        keys = [s["key"] for s in sb.SITES]
        self.assertEqual(keys, ["sannysoft", "incolumitas", "rebrowser",
                                "browserscan", "deviceinfo", "creepjs"])
        for s in sb.SITES:
            self.assertTrue(s["url"].startswith("https://"))


class TestSummarize(unittest.TestCase):
    def test_unknown_is_excluded_from_denominator(self):
        s = sb.summarize({"a": sb.PASS, "b": sb.FAIL, "c": sb.UNKNOWN})
        self.assertEqual(s["measured"], 2)
        self.assertEqual(s["score"], 0.5, "unknown 不得进分母")
        self.assertEqual(s["unknown"], 1)
        self.assertEqual(s["total"], 3)

    def test_all_unknown_scores_none_not_zero(self):
        """**本文件最重要的一条**：全没测出来 → score=None，不是 0"""
        s = sb.summarize({"a": sb.UNKNOWN, "b": sb.UNKNOWN})
        self.assertIsNone(s["score"], "全 unknown 必须给 None——0 分会把'没测'说成'很差'")
        self.assertEqual(s["measured"], 0)

    def test_all_pass_scores_one(self):
        s = sb.summarize({"a": sb.PASS, "b": sb.PASS})
        self.assertEqual(s["score"], 1.0)


class TestCompare(unittest.TestCase):
    def _base(self, **kw):
        return {"sites": kw, "score": sum(1 for v in kw.values() if v == sb.PASS) / max(len(kw), 1)}

    def test_regression_is_flagged(self):
        base = self._base(a=sb.PASS, b=sb.PASS)
        cmp = sb.compare(base, {"a": sb.PASS, "b": sb.FAIL})
        self.assertFalse(cmp["ok"], "掉分必须判退步")
        self.assertEqual(cmp["regressed"], ["b"])

    def test_improvement_is_ok(self):
        base = self._base(a=sb.PASS, b=sb.FAIL)
        cmp = sb.compare(base, {"a": sb.PASS, "b": sb.PASS})
        self.assertTrue(cmp["ok"])
        self.assertEqual(cmp["improved"], ["b"])

    def test_offsetting_change_is_not_hidden(self):
        """一升一降：总分不变，但**必须**逐站报出退步的那一个"""
        base = self._base(a=sb.PASS, b=sb.FAIL)
        cmp = sb.compare(base, {"a": sb.FAIL, "b": sb.PASS})
        self.assertEqual(cmp["score_delta"], 0.0)
        self.assertEqual(cmp["regressed"], ["a"])
        self.assertFalse(cmp["ok"], "总分不变不等于没问题——逐站必须能看出退步")

    def test_unknown_new_site_is_not_a_regression(self):
        base = self._base(a=sb.PASS)
        cmp = sb.compare(base, {"a": sb.PASS, "b": sb.UNKNOWN})
        self.assertTrue(cmp["ok"], "新站没测出来不算退步")

    def test_empty_baseline_is_handled(self):
        cmp = sb.compare({}, {"a": sb.PASS})
        self.assertTrue(cmp["ok"])
        self.assertIsNone(cmp["score_delta"])


class TestRunBench(unittest.TestCase):
    def test_collects_verdicts(self):
        res = sb.run_bench(lambda url: sb.PASS)
        self.assertEqual(len(res), len(sb.SITES))
        self.assertTrue(all(v == sb.PASS for v in res.values()))

    def test_probe_exception_does_not_abort_round(self):
        """单站异常记 unknown，整轮继续——否则一个站点抽风整轮就没结果了"""
        calls = []

        def flaky(url):
            calls.append(url)
            if len(calls) == 2:
                raise RuntimeError("boom")
            return sb.FAIL

        res = sb.run_bench(flaky)
        self.assertEqual(len(calls), len(sb.SITES), "异常后必须继续跑完")
        self.assertEqual(list(res.values()).count(sb.UNKNOWN), 1)
        self.assertEqual(list(res.values()).count(sb.FAIL), len(sb.SITES) - 1)

    def test_garbage_verdict_becomes_unknown(self):
        res = sb.run_bench(lambda url: "banana")
        self.assertTrue(all(v == sb.UNKNOWN for v in res.values()),
                        "非约定取值一律归 unknown，不许当 fail 或 pass")


class TestBaselineRoundTrip(unittest.TestCase):
    def test_save_and_load(self):
        import os
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp(prefix="kiana_bench_")
        try:
            p = os.path.join(tmp, "sub", "b.json")
            data = {"sites": {"a": sb.PASS}, "score": 1.0}
            self.assertTrue(sb._save(p, data), "应自动建目录")
            self.assertEqual(sb._load(p), data)
            self.assertEqual(sb._load(os.path.join(tmp, "missing.json")), {},
                             "基线缺失 → 空字典，不抛")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_baseline_default_path_is_outside_repo(self):
        p = Path(sb.baseline_path()).resolve()
        self.assertFalse(str(p).startswith(str(ROOT.resolve())),
                         f"基线落在仓库内: {p}")


if __name__ == "__main__":
    unittest.main()
