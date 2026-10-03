# -*- coding: utf-8 -*-
"""GUI 六跳接线「到达断言」（M1-f）

工程纪律：GUI 新增的键必须**逐跳**接上，缺任何一跳就是"能填不生效"——而且**不报错**，
只在用户那边表现为"界面能开、功能没反应"。所以这里用 AST 逐跳证明键真的写在代码里，
**不靠肉眼在界面上确认**，也**不启动 Qt**（无窗口、无 GPU、无浏览器的纯静态检查）。

六跳：
  ① 控件 create   `launcher_v9.HomePage.build`          → self.sw_cdp / self.cdp_port_spin
  ② `_wire` 接线  `launcher_v9.KianaV9._wire`           → checkedChanged / valueChanged
  ③ `_start()` cfg `launcher_v9.KianaV9._start`         → cfg 字典含两键
  ④ EngineBridge  `launcher_v8.EngineBridge._run_engine` → engine_cfg 含两键
  ⑤ `crawl()` gcfg `run_crawler.crawl`                  → gcfg 含两键
  ⑥ DEFAULT_GLOBAL `kiana_vnext_plus/config.py`         → 含两键（唯一默认源）

另锁两条行为约束：
  · 默认**关**（与 DEFAULT_GLOBAL 的 False 一致）；
  · 开启时**先探测端口，不通就不起任务**（"不能乱用"）。
"""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEYS = ("cdp_attach", "cdp_port")


def _tree(rel):
    return ast.parse((ROOT / rel).read_text(encoding="utf-8"))


def _func(tree, name, cls=None):
    if cls is not None:
        for n in ast.walk(tree):
            if isinstance(n, ast.ClassDef) and n.name == cls:
                for m in n.body:
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name == name:
                        return m
        raise AssertionError(f"找不到 {cls}.{name}")
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    raise AssertionError(f"找不到函数 {name}")


def _dict_keys(node):
    """收集子树里所有 Dict 的字面量字符串键"""
    keys = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Dict):
            for k in n.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.add(k.value)
    return keys


class TestSixHops(unittest.TestCase):
    def test_hop1_widgets_created_on_home(self):
        build = _func(_tree("launcher_v9.py"), "build", cls="HomePage")
        attrs = {n.attr for n in ast.walk(build) if isinstance(n, ast.Attribute)}
        self.assertIn("sw_cdp", attrs, "第①跳：首页没有创建接管开关")
        self.assertIn("cdp_port_spin", attrs, "第①跳：首页没有创建调试端口控件")
        self.assertIn("cdp_status", attrs, "第①跳：没有状态行（用户看不到连没连上）")

    def test_hop2_wired_in_wire(self):
        wire = _func(_tree("launcher_v9.py"), "_wire", cls="KianaV9")
        src = ast.unparse(wire)
        self.assertIn("sw_cdp", src, "第②跳：_wire 没有接线开关")
        self.assertIn("checkedChanged", src, "第②跳：开关信号没接")
        self.assertIn("cdp_port_spin", src, "第②跳：端口控件没接线")
        self.assertIn("valueChanged", src, "第②跳：端口信号没接")

    def test_hop3_start_cfg_carries_keys(self):
        start = _func(_tree("launcher_v9.py"), "_start", cls="KianaV9")
        keys = _dict_keys(start)
        for k in KEYS:
            self.assertIn(k, keys, f"第③跳：_start() 的 cfg 缺 {k}")

    def test_hop4_engine_bridge_translation_table(self):
        run = _func(_tree("launcher_v8.py"), "_run_engine", cls="EngineBridge")
        keys = _dict_keys(run)
        for k in KEYS:
            self.assertIn(k, keys, f"第④跳：EngineBridge 翻译表缺 {k}（键到此断掉）")

    def test_hop5_crawl_gcfg(self):
        crawl = _func(_tree("run_crawler.py"), "crawl")
        keys = _dict_keys(crawl)
        for k in KEYS:
            self.assertIn(k, keys, f"第⑤跳：crawl() 的 gcfg 缺 {k}（键到此断掉）")

    def test_hop6_default_global_is_single_source(self):
        tree = _tree("kiana_vnext_plus/config.py")
        assign = None
        for n in tree.body:
            if isinstance(n, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "DEFAULT_GLOBAL" for t in n.targets):
                assign = n
        self.assertIsNotNone(assign, "第⑥跳：找不到 DEFAULT_GLOBAL（唯一默认源）")
        keys = _dict_keys(assign)
        for k in KEYS:
            self.assertIn(k, keys, f"第⑥跳：DEFAULT_GLOBAL 缺 {k}")


class TestSafetyConstraints(unittest.TestCase):
    def test_default_is_off(self):
        """默认必须关——照 config.DEFAULT_GLOBAL 的取值，不另写一份默认。"""
        tree = _tree("kiana_vnext_plus/config.py")
        for n in tree.body:
            if isinstance(n, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "DEFAULT_GLOBAL" for t in n.targets):
                d = n.value
                for kw in ast.walk(d):
                    if isinstance(kw, ast.Dict):
                        pairs = dict(zip(
                            [k.value for k in kw.keys if isinstance(k, ast.Constant)],
                            kw.values))
                        if "cdp_attach" in pairs:
                            self.assertIsInstance(pairs["cdp_attach"], ast.Constant)
                            self.assertIs(pairs["cdp_attach"].value, False,
                                          "接管开关默认必须是 False")
                            return
        self.fail("DEFAULT_GLOBAL 里找不到 cdp_attach")

    def test_start_probes_before_running(self):
        """'不能乱用'：开启接管时先探测端口；不通就**不起任务**（绝不静默回退）"""
        start = _func(_tree("launcher_v9.py"), "_start", cls="KianaV9")
        src = ast.unparse(start)
        self.assertIn("probe_cdp_endpoint", src, "起任务前必须探测 CDP 端口")
        self.assertIn("任务未启动", src, "探测不通过必须明确告知并未启动任务")

    def test_engine_side_reports_fallback(self):
        """引擎侧接管失败必须可观测（本轮 M1-d 的修复不许回退）"""
        src = (ROOT / "kiana_vnext_plus" / "solver_engine.py").read_text(encoding="utf-8")
        self.assertIn("cdp_fallback_reason", src)
        self.assertIn("cdp_attach_failures", src)
        self.assertIn("def probe_cdp_endpoint", src)


if __name__ == "__main__":
    unittest.main()
