# -*- coding: utf-8 -*-
"""[v2.19.8] `page_timeout` 接线回归测试。

修的是一个"声明与行为不符"的缺陷：`--page-timeout` 的帮助文本承诺 `0=关闭`，但
① argparse 默认值也是 0（"未指定"与"显式 0"不可区分），② `crawl()` 的 gcfg 显式
键表里根本没有这个键 → 显式传 0 时引擎读到的仍是 `DEFAULT_GLOBAL` 的 300s，
**看门狗关不掉**；GUI 侧经 `launcher_config.json` 手写的同键也到不了引擎。

本文件锁住三个层次：参数解析 → 覆盖项生成（纯函数行为）→ 两条入口的键表接线。

⚠️ 断言写法：源码检查一律走 `_code_flat()`（剥 docstring + **去引号**）。
`ast.unparse` 会把字符串统一成单引号，带引号的模式串会假红。
"""
import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class _StripProse(ast.NodeTransformer):
    """剥掉 docstring 与裸字符串语句（只留可执行代码）——避免断言打到注释上"""

    def generic_visit(self, node):
        super().generic_visit(node)
        for field in ("body", "orelse", "finalbody"):
            lst = getattr(node, field, None)
            if isinstance(lst, list):
                kept = [s for s in lst if not (
                    isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)
                    and isinstance(s.value.value, str))]
                if field == "body" and not kept:
                    kept = [ast.Pass()]
                setattr(node, field, kept)
        return node


def _code_flat(rel_path: str) -> str:
    """可执行代码 + 去掉所有引号字符（引号风格无关的匹配）"""
    src = (ROOT / rel_path).read_text(encoding="utf-8")
    code = ast.unparse(_StripProse().visit(ast.parse(src)))
    return code.replace("'", "").replace('"', "")


# ════════════════════════════════════════════════════════════
# 1. argparse：未指定 ≠ 显式 0
# ════════════════════════════════════════════════════════════
class TestArgparseSemantics:
    def test_default_is_none_in_source(self):
        """默认值必须是 None：否则"未指定"与"显式 0"无法区分（原缺陷根因）"""
        seg = _code_flat("run_crawler.py").split("--page-timeout", 1)[1].split(")", 1)[0]
        assert "default=None" in seg, "argparse 默认仍是 0 → 显式 0 无法与未指定区分"

    def test_explicit_value_passthrough_includes_zero(self):
        """显式传值（含 0）必须无条件透传，不能再有 `> 0` 门槛"""
        code = _code_flat("run_crawler.py")
        assert "if args.page_timeout is not None:" in code, "缺少 `is not None` 判断"
        assert "args.page_timeout > 0" not in code, "旧的 `> 0` 门槛又回来了"
        assert "cfg[page_timeout] = max(0, int(args.page_timeout))" in code


# ════════════════════════════════════════════════════════════
# 2. 覆盖项纯函数（行为）
# ════════════════════════════════════════════════════════════
class TestPageTimeoutOverride:
    def _fn(self):
        import run_crawler
        return run_crawler.page_timeout_override

    def test_absent_key_yields_no_override(self):
        """未指定 → 不产生覆盖项（保持 DEFAULT_GLOBAL 是唯一默认源）"""
        f = self._fn()
        assert f({}) == {}
        assert f({"depth": 1}) == {}
        assert f(None) == {}

    def test_explicit_zero_disables(self):
        """显式 0 → 覆盖为 0（=关闭看门狗）：本次修复的核心承诺"""
        assert self._fn()({"page_timeout": 0}) == {"page_timeout": 0}

    def test_positive_passthrough(self):
        assert self._fn()({"page_timeout": 120}) == {"page_timeout": 120}

    def test_string_number_coerced(self):
        """手写配置里写成字符串也要能用"""
        assert self._fn()({"page_timeout": "45"}) == {"page_timeout": 45}

    def test_negative_clamped_to_zero(self):
        """负数按 0（等价"关闭"），不产生荒谬的超时值"""
        assert self._fn()({"page_timeout": -5}) == {"page_timeout": 0}

    def test_illegal_value_falls_back_safely(self, capsys):
        """非法值 → 不覆盖（沿用默认 300s）+ 告警；不能因手滑把兜底关掉"""
        assert self._fn()({"page_timeout": "abc"}) == {}
        assert "非法值" in capsys.readouterr().out


# ════════════════════════════════════════════════════════════
# 3. 两条入口的键表接线（结构）
# ════════════════════════════════════════════════════════════
class TestWiringBothEntries:
    def test_cli_gcfg_table_uses_override(self):
        """CLI：crawl() 的 gcfg 键表必须合并覆盖项（原表里没这个键 → 传了也不生效）"""
        seg = _code_flat("run_crawler.py").split("gcfg = GlobalConfig(OmegaConf.create(", 1)
        assert len(seg) == 2
        assert "page_timeout_override(cfg)" in seg[1][:3000], "gcfg 键表未接覆盖项"

    def test_gui_engine_cfg_translates_key(self):
        """GUI：EngineBridge 翻译表必须透传 page_timeout（同 captcha_api_keys 的历史缺口）"""
        seg = _code_flat("launcher_v8.py").split("engine_cfg = {", 1)
        assert len(seg) == 2
        body = seg[1][:4000]
        assert "cfg.get(page_timeout" in body, "GUI 翻译表未读取 page_timeout"
        assert "engine_cfg[page_timeout] = max(0, int(_pt))" in body, "GUI 翻译表未写入引擎键"

    def test_config_remains_single_default_source(self):
        """DEFAULT_GLOBAL 仍是唯一默认源：两处入口都不许硬编码第二个 300"""
        assert "page_timeout: 300" in _code_flat("kiana_vnext_plus/config.py")
        for rel in ("run_crawler.py", "launcher_v8.py"):
            assert "page_timeout: 300" not in _code_flat(rel), f"{rel} 里硬编码了第二份默认值"


# ════════════════════════════════════════════════════════════
# 4. 引擎侧消费语义
# ════════════════════════════════════════════════════════════
class TestEngineConsumer:
    def test_zero_means_disabled_and_default_is_300(self):
        from kiana_vnext_plus.config import DEFAULT_GLOBAL
        assert int(DEFAULT_GLOBAL["page_timeout"]) == 300
        seg = _code_flat("kiana_vnext_plus/crawler.py").split(
            "_safe_int_cfg(self.cfg.get(page_timeout", 1)
        assert len(seg) == 2, "引擎侧读取点丢失"
        assert "_pt > 0" in seg[1][:200], "引擎侧启用条件不再是 >0（0=关闭语义被破坏）"
