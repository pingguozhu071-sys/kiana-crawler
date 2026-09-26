# -*- coding: utf-8 -*-
"""[v2.19.8] 门禁脚本自身两个缺陷的回归测试。

① 静态检查项：原实现只认 `Found N errors` 正则 → **真清零反而判 FAIL**（干净时
   ruff 输出 `All checks passed!`、mypy 输出 `Success: no issues found ...`）。
   现以退出码为准。这里直接喂给 `_static_count` 真实工具输出样本做行为验证。
② `--skip-network` 死参数：docstring 声称会跳过 pip-audit，但 main() 从未传参。
"""
import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 本机实测的真实输出样本（v2.19.8 采集）
_RUFF_DIRTY = "kiana_vnext_plus/x.py:1:1: F401 unused\nFound 82 errors.\n[*] 7 fixable ..."
_RUFF_CLEAN = "All checks passed!"
_MYPY_DIRTY = "kiana_vnext_plus/x.py:1: error: Incompatible types\nFound 103 errors in 61 files"
_MYPY_CLEAN = "Success: no issues found in 61 source files"


def _rc():
    sys.path.insert(0, str(ROOT / "tools"))
    import release_check
    return release_check


class TestStaticCount:
    def test_dirty_counts_parsed(self):
        rc = _rc()
        assert rc._static_count(1, _RUFF_DIRTY) == 82
        assert rc._static_count(1, _MYPY_DIRTY) == 103

    def test_clean_zero_no_longer_fails(self):
        """核心回归：干净（rc=0）必须算 0，而不是"抓不到数字 → ValueError → FAIL" """
        rc = _rc()
        assert rc._static_count(0, _RUFF_CLEAN) == 0
        assert rc._static_count(0, _MYPY_CLEAN) == 0

    def test_rc_zero_but_noise_still_zero(self):
        rc = _rc()
        assert rc._static_count(0, "") == 0

    def test_tool_crash_is_none(self):
        """非零退出且抓不到计数 = 工具真异常（崩溃/参数错）→ None（才该 FAIL）"""
        rc = _rc()
        assert rc._static_count(2, "error: unrecognized arguments: --bogus") is None
        assert rc._static_count(1, "Traceback (most recent call last): ...") is None


class TestStaticCheckAcceptsClean:
    def test_clean_scenario_passes(self, monkeypatch):
        """端到端：两个工具都干净时，check_static 必须 PASS（原实现这条会 FAIL）"""
        rc = _rc()
        monkeypatch.setattr(rc, "_run", lambda cmd, timeout=900: (
            0, _RUFF_CLEAN if "ruff" in " ".join(map(str, cmd)) else _MYPY_CLEAN))
        status, detail, _ = rc.check_static()
        assert status == rc.PASS, f"清零场景仍被判 FAIL: {detail}"
        assert "0" in detail

    def test_dirty_over_baseline_fails(self, monkeypatch):
        rc = _rc()
        monkeypatch.setattr(rc, "_run", lambda cmd, timeout=900: (
            1, "Found 85 errors" if "ruff" in " ".join(map(str, cmd))
            else "Found 104 errors in 61 files"))
        status, detail, _ = rc.check_static()
        assert status == rc.FAIL and "85" in detail

    def test_tool_crash_fails(self, monkeypatch):
        rc = _rc()
        monkeypatch.setattr(rc, "_run", lambda cmd, timeout=900: (2, "boom: no such option"))
        status, detail, _ = rc.check_static()
        assert status == rc.FAIL and "静态检查异常" in detail


class TestSkipNetworkWired:
    def test_skip_network_skips_pip_audit(self):
        """--skip-network 必须真的跳过在线 pip-audit（原为死参数，照旧联网）"""
        rc = _rc()
        status, detail, _ = rc.check_deps(skip_network=True)
        assert status == rc.SKIP and "skip-network" in detail

    def test_main_passes_flag(self):
        """结构：main() 必须把 args.skip_network 传进 check_deps（防止再退化成死参数）"""
        src = (ROOT / "tools" / "release_check.py").read_text(encoding="utf-8")
        code = ast.unparse(ast.parse(src)).replace("'", "").replace('"', "")
        assert "check_deps(args.skip_network)" in code
