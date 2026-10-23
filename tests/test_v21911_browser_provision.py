"""v2.19.11 回归：安装包不再随包浏览器，且「装浏览器」这条路必须真的存在。

**为什么有这条**（2026-10-23 实测，不是设计口味）：

    NSIS 单次输入数据上限 = 2^31 字节 = 2,147,483,648
    实测崩溃点          = 2,147,173,959 字节（差 309,688 就撞线）
    报错                = Internal compiler error #12345: error mmapping datablock to 148126.
    32 位 NSIS 3.12 与 64 位 makensis（PE machine=0x8664）**同一个样**

只把两棵 onedir 去重（45.5% 重复）后载荷 2.125 GiB 仍超；把 chromium +
headless_shell 移出包后 1.453 GiB（余量 0.547 GiB）才编得出来 —— 实测
`KianaVnextPlus-Setup-2.19.11.0.exe` 433.70 MB、849 秒、exit 0。

于是"浏览器从哪来"必须有人回答，否则装完不能登录、不能渲染。这里钉四件事：
  ① 两个 spec 的浏览器随包块**默认关**，只有 KIANA_BUNDLE_BROWSERS=1 才开；
  ② `tools/install_browsers.py` 能认出打包版里的 driver，并拼出正确的下载命令；
  ③ 下载目标 = `%LOCALAPPDATA%\\ms-playwright` —— 与 `run_crawler._detect_browser_path()`
     的候选链**同源**（否则装完不被发现，等于没装）；
  ④ 载荷工具在超 2^31 时**直接拒绝写盘**，不产出注定编不出来的东西。

判据查结构（条件表达式、候选链、argv、退出码），零处断言日志文案。
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, rel: str):
    path = ROOT / rel
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"载入失败：{path}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ib = _load("install_browsers", "tools/install_browsers.py")
pp = _load("pack_installer_payload", "tools/pack_installer_payload.py")


# ══════════════════════════════════════════════════════════════════════
# 1. driver 识别与候选链
# ══════════════════════════════════════════════════════════════════════
@pytest.fixture()
def fake_driver(tmp_path: Path) -> Path:
    d = tmp_path / "_internal" / "playwright" / "driver"
    (d / "package").mkdir(parents=True)
    (d / "node.exe").write_bytes(b"MZ")
    (d / "package" / "cli.js").write_text("// cli", encoding="utf-8")
    return d


class TestDriver:
    def test_is_driver_needs_both_pieces(self, fake_driver: Path, tmp_path: Path):
        assert ib.is_driver(fake_driver)
        half = tmp_path / "half"
        half.mkdir()
        (half / "node.exe").write_bytes(b"MZ")
        assert not ib.is_driver(half)

    def test_explicit_wins(self, fake_driver: Path):
        cands = ib.driver_candidates(str(fake_driver))
        assert cands[0] == fake_driver

    def test_find_driver_skips_missing(self, fake_driver: Path, tmp_path: Path, monkeypatch):
        """候选链里前面的不存在，必须继续往后找，而不是直接放弃。"""
        monkeypatch.setattr(
            ib, "driver_candidates",
            lambda explicit=None: [tmp_path / "nope", fake_driver],
        )
        assert ib.find_driver("whatever") == fake_driver

    def test_packaged_driver_is_in_candidates(self, monkeypatch, tmp_path: Path):
        """打包版的 driver 在 _MEIPASS\\playwright\\driver —— 必须出现在候选里。"""
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
        cands = [str(c).replace("\\", "/") for c in ib.driver_candidates()]
        assert any(c.endswith("/playwright/driver") for c in cands)
        assert any(c.endswith("/patchright/driver") for c in cands)


class TestCommand:
    def test_build_command_argv(self, fake_driver: Path):
        cmd = ib.build_command(fake_driver, ["chromium", "chromium-headless-shell"])
        assert cmd[0].endswith("node.exe")
        assert cmd[1].endswith("cli.js")
        assert cmd[2] == "install"
        assert cmd[3:] == ["chromium", "chromium-headless-shell"]

    def test_install_sets_browsers_path(self, fake_driver: Path, tmp_path: Path, monkeypatch):
        dest = tmp_path / "ms-playwright"
        seen = {}

        def fake_call(cmd, env=None, timeout=None):  # noqa: ANN001
            seen["cmd"] = cmd
            seen["env"] = env
            return 0

        monkeypatch.setattr(ib.subprocess, "call", fake_call)
        assert ib.install(fake_driver, dest, ["chromium"]) == 0
        assert seen["env"]["PLAYWRIGHT_BROWSERS_PATH"] == str(dest)
        assert seen["env"]["PATCHRIGHT_BROWSERS_PATH"] == str(dest)
        assert dest.is_dir()

    def test_install_reports_missing_node(self, fake_driver: Path, tmp_path: Path, monkeypatch):
        def boom(*_a, **_kw):
            raise FileNotFoundError("node.exe")

        monkeypatch.setattr(ib.subprocess, "call", boom)
        assert ib.install(fake_driver, tmp_path / "d", ["chromium"]) == 2


# ══════════════════════════════════════════════════════════════════════
# 2. 默认安装位置必须与 run_crawler 的候选链同源
# ══════════════════════════════════════════════════════════════════════
class TestDestConsistency:
    def test_default_dest_matches_runner_candidate(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        assert ib.default_dest() == tmp_path / "ms-playwright"
        src = (ROOT / "run_crawler.py").read_text(encoding="utf-8", errors="ignore")
        # _detect_browser_path 的候选链里必须有同一个位置
        assert 'ms-playwright' in src

    def test_check_exit_codes(self, fake_driver: Path, tmp_path: Path, monkeypatch):
        dest = tmp_path / "ms-playwright"
        # 只认显式参数：本机 pip 环境里真有 playwright driver，不能让它把用例带跑
        monkeypatch.setattr(
            ib, "driver_candidates",
            lambda explicit=None: [Path(explicit)] if explicit else [],
        )
        # 没有 driver → 2
        assert ib.main(["--check", "--driver", str(tmp_path / "nope"), "--dest", str(dest)]) == 2
        # 有 driver 但没装浏览器 → 0（探测本身成功，装没装由打印说明）
        assert ib.main(["--check", "--driver", str(fake_driver), "--dest", str(dest)]) == 0


# ══════════════════════════════════════════════════════════════════════
# 3. 两个 spec：浏览器随包必须默认关
# ══════════════════════════════════════════════════════════════════════
class TestSpecGating:
    @pytest.mark.parametrize("spec_name", ["KianaLauncher.spec", "KianaCrawler.spec"])
    def test_bundling_is_opt_in(self, spec_name: str):
        spec = (ROOT / spec_name).read_text(encoding="utf-8", errors="ignore")
        assert "KIANA_BUNDLE_BROWSERS" in spec
        assert '_BUNDLE_BROWSERS = _os.environ.get("KIANA_BUNDLE_BROWSERS", "") == "1"' in spec
        # 关键：浏览器那块必须**受这个开关控制**，不能裸跑
        assert "if _BUNDLE_BROWSERS and _mpw.is_dir():" in spec

    @pytest.mark.parametrize("spec_name", ["KianaLauncher.spec", "KianaCrawler.spec"])
    def test_spec_explains_the_2gib_reason(self, spec_name: str):
        """留下理由，别让下一个人以为是随手删的。"""
        spec = (ROOT / spec_name).read_text(encoding="utf-8", errors="ignore")
        assert "2^31" in spec or "2 GiB" in spec or "2.125 GiB" in spec


# ══════════════════════════════════════════════════════════════════════
# 4. 载荷工具必须拒绝超限
# ══════════════════════════════════════════════════════════════════════
class TestOversizeGuard:
    def test_refuses_when_over_limit(self, tmp_path: Path, monkeypatch):
        a = tmp_path / "dist" / "KianaLauncher"
        b = tmp_path / "dist" / "KianaCrawler"
        a.mkdir(parents=True)
        b.mkdir(parents=True)
        (a / "x.bin").write_bytes(b"1")
        (b / "x.bin").write_bytes(b"1")
        monkeypatch.setattr(pp, "total_bytes", lambda _plan: 2**31)
        assert pp.main(["--dist", str(tmp_path / "dist")]) == 1
        assert not (tmp_path / "dist" / "_pack").exists()

    def test_allow_oversize_writes(self, tmp_path: Path, monkeypatch):
        a = tmp_path / "dist" / "KianaLauncher"
        b = tmp_path / "dist" / "KianaCrawler"
        a.mkdir(parents=True)
        b.mkdir(parents=True)
        (a / "x.bin").write_bytes(b"1")
        monkeypatch.setattr(pp, "total_bytes", lambda _plan: 2**31)
        # 落盘后的字节校验也走 naive_bytes —— 一并钉住，否则会被"写出的包很小"判失败
        monkeypatch.setattr(pp, "naive_bytes", lambda _roots: 2**31)
        assert pp.main(["--dist", str(tmp_path / "dist"), "--allow-oversize"]) == 0
        assert (tmp_path / "dist" / "_pack" / "x.bin").exists()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
