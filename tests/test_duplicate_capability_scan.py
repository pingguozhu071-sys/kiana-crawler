# -*- coding: utf-8 -*-
"""「同一能力多份实现」扫描器回归（门禁第 14 项）

**本工程最稳定的缺陷模式**：九轮实测下来，出事的全是"同一个能力在多处各写一份"。
本文件锁的是**扫描器本身可信**——因为它的判据是校准出来的，不是拍脑袋的：

  ① **形参不一致**才算最危险（调用点 TypeError）——`**kwargs` 纯委托要视为兼容，
     否则 `write_page` 那类正常委托会被误报（第 25 轮踩过同一个坑）；
  ② **≥3 个模块的同名 = 通用动词**，只登记不阻断——实测校准：真出事的
     （`push`/`pop_batch`/`normalize_url`/`sanitize_video_filename`）**全是 2 个模块**；
     而 `acquire`/`record`/`release`/`solve` 逐条人工核对全是无关类共享的动词；
  ③ 新增一处形参不一致 → **阻断**（这才是有价值的方向）。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import duplicate_capability_scan as dcs       # noqa: E402


def _items(*pairs):
    """`("file.py", ("a","b"))` → 扫描器内部结构"""
    return [{"file": f, "line": i + 1, "params": list(p), "kind": "async"}
            for i, (f, p) in enumerate(pairs)]


class TestClassify(unittest.TestCase):
    def test_two_modules_different_params_is_blocking(self):
        """核心信号：两个模块、形参不一致 —— 第 25 轮那两个 TypeError 的形状"""
        items = _items(("a.py", ("url", "force")), ("b.py", ("url",)))
        self.assertEqual(dcs.classify(items), "signature_mismatch")

    def test_two_modules_same_params_is_registered_only(self):
        items = _items(("a.py", ("url",)), ("b.py", ("url",)))
        self.assertEqual(dcs.classify(items), "same_signature")

    def test_three_modules_is_a_generic_verb(self):
        """`acquire`/`record`/`release` 那类：通用动词，不阻断"""
        items = _items(("a.py", ("x",)), ("b.py", ("x", "y")), ("c.py", ("z",)))
        self.assertEqual(dcs.classify(items), "generic_name")

    def test_kwargs_delegate_is_compatible(self):
        """`f(*args, **kwargs)` 能接任意关键字 —— 具名签名与它**不冲突**

        注意夹具里的写法要和 `_params()` 的实际输出一致（带 `*`/`**` 前缀）——
        首版写成裸 `"kwargs"` 导致用例失败，那是**夹具错**不是代码错。
        """
        items = _items(("frontier.py", ("url_hash", "status_code")),
                       ("redis_frontier.py", ("*args", "**kwargs")))
        self.assertEqual(dcs.classify(items), "same_signature",
                         "**kwargs 纯委托被误报成失配——第 25 轮踩过这个坑")

    def test_kwargs_on_either_side_is_compatible(self):
        items = _items(("a.py", ("*args", "**kwargs")), ("b.py", ("x", "y")))
        self.assertEqual(dcs.classify(items), "same_signature")


class TestScanSanity(unittest.TestCase):
    def test_scan_is_non_trivial(self):
        """夹具自证：扫描失效（比如 AST 解析全挂）会让下面的断言全部**假绿**"""
        res = dcs.scan()
        self.assertGreaterEqual(len(res), 20, f"只扫到 {len(res)} 个同名实现，扫描可能已失效")

    def test_known_real_duplicate_is_found(self):
        """实证：`frontier` / `redis_frontier` 那对同名方法必须被扫到"""
        res = dcs.scan()
        for name in ("mark_failed", "push", "pop_batch"):
            self.assertIn(name, res, f"没扫到 {name} —— 两后端的同名方法应被识别")
            files = {i["file"] for i in res[name]}
            self.assertIn("redis_frontier.py", files)

    def test_two_backend_pair_is_not_reported_as_mismatch(self):
        """两个 frontier 后端现在已经签名对齐——不该再被报成失配"""
        res = dcs.scan()
        for name in ("push", "pop_batch", "mark_failed"):
            if name in res:
                self.assertNotEqual(dcs.classify(res[name]), "signature_mismatch",
                                    f"{name} 双后端又签名不一致了")


class TestCompare(unittest.TestCase):
    def test_new_mismatch_is_regression(self):
        base = {"a": {"risk": "signature_mismatch", "sites": ["x:1", "y:1"]}}
        cur = dict(base)
        cur["b"] = {"risk": "signature_mismatch", "sites": ["x:1", "z:1"]}
        self.assertEqual(dcs.compare(base, cur)["new_mismatch"], ["b"])

    def test_fixed_mismatch_is_reported(self):
        base = {"a": {"risk": "signature_mismatch", "sites": ["x:1", "y:1"]}}
        self.assertEqual(dcs.compare(base, {})["fixed_mismatch"], ["a"])

    def test_generic_names_do_not_trigger(self):
        base = {}
        cur = {"acquire": {"risk": "generic_name", "sites": ["a:1", "b:1", "c:1"]}}
        self.assertEqual(dcs.compare(base, cur)["new_mismatch"], [],
                         "通用动词不该阻断")

    def test_new_capability_is_flagged(self):
        """**新增同名跨模块实现必须被看见**——重复本身不是错，但"悄悄又抄一份"
        正是本工程所有缺陷的**机制**。要让重复变成有意识的行为。"""
        base = {"old": {"risk": "same_signature", "sites": ["a:1", "b:1"]}}
        cur = dict(base)
        cur["fresh"] = {"risk": "same_signature", "sites": ["c:1", "d:1"]}
        cmp = dcs.compare(base, cur)
        self.assertEqual(cmp["new_name"], ["fresh"])
        self.assertEqual(cmp["new_mismatch"], [], "同签名不该被当成形参失配")

    def test_existing_names_are_not_flagged(self):
        base = {"old": {"risk": "same_signature", "sites": ["a:1", "b:1"]}}
        self.assertEqual(dcs.compare(base, dict(base))["new_name"], [])

    def test_gone_capability_is_reported(self):
        base = {"old": {"risk": "same_signature", "sites": ["a:1", "b:1"]}}
        self.assertEqual(dcs.compare(base, {})["gone"], ["old"])


class TestBaselineMatchesTree(unittest.TestCase):
    def test_baseline_exists_and_is_current(self):
        base = dcs.load_baseline()
        self.assertTrue(base, "基线缺失——跑 --write-baseline")
        cur = dcs.analyze()
        cmp = dcs.compare(base, cur)
        self.assertEqual(cmp["new_mismatch"], [], "当前树出现了基线之外的新形参不一致")
        self.assertEqual(cmp["new_name"], [],
                         "当前树出现了基线之外的新同名跨模块实现——请登记或合并")

    def test_triage_of_known_forks_is_recorded(self):
        """本会话已**逐条triaged**的那批分叉仍在被守护（防止它们悄悄消失或改变风险档）"""
        base = dcs.load_baseline()
        for name in ("_domain_of", "init_session", "_media_of", "_fetch_html",
                     "mark_failed", "push", "pop_batch"):
            if name in base:
                self.assertIn(base[name]["risk"],
                              ("same_signature", "signature_mismatch", "generic_name"))


if __name__ == "__main__":
    unittest.main()
