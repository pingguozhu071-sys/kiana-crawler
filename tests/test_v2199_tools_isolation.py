# -*- coding: utf-8 -*-
"""[v2.19.9] `tools/` 脚本的**默认安全**隔离：不弹窗、不写机主真实数据根。

## 为什么必须是"默认"

`tests/conftest.py` 的 autouse 护栏**只在 pytest 里生效**。`tools/` 下的脚本是
`python tools/xxx.py` 直接执行的 —— 护栏不在场。而其中几个（GUI 探针/冒烟）
**光构造 `KianaV9()` 就有写路径**：`launcher_v9.py` 的启动迁移钩子会写
`secrets/*.bin` 并改写真 `launcher_config.json`；`_start()` / `_wp_update()`
更是直接 `save_config()`；`EngineBridge.start()` 还会写真 `http_cache/`。

⇒ 所以本文件钉三件事：
  ① 那 5 个脚本**都**在 `import launcher_*` **之前**调用了 `activate()`；
  ② `activate()` 的**默认**行为真的把数据根搬进临时目录（含 `CONFIG_FILE` 与
     `data_root()` 两个钩子），并且**不碰**机主的真实值；
  ③ 逃生舱 `--real` 存在且在帮助文本里写明了风险。

⚠️ 本文件**不运行任何 `tools/` 脚本**（它们会弹窗），只做 AST 断言与纯逻辑调用。
"""
import ast
import os
import pathlib
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

# 所有需要"默认隔离"的脚本（= 会构造 launcher 窗口 / 会走 save_config / 会真起引擎的）
ISOLATED_SCRIPTS = (
    "gui_perf_probe.py",
    "v9_smoke.py",
    "v9_mini_smoke.py",
    "v9_engine_smoke.py",
    "gui_smoke_shot.py",
)


@pytest.fixture()
def env_guard():
    """`activate()` 是**直接写 `os.environ`** 的（它跑在 argparse 之前、也在测试框架
    的 monkeypatch 之外），monkeypatch 追不到那些写入 —— 所以这里自己前后快照整个环境。

    少了这道保护，被重定向的 `LOCALAPPDATA` 会**漏给同一进程里的其它测试**，
    那正是"隔离设施自己变成污染源"的经典翻车方式。
    """
    before = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(before)


@pytest.fixture()
def iso(env_guard, monkeypatch):
    import _tools_isolation as ti
    monkeypatch.setattr(ti, "_activated_root", None)   # 让每次 activate() 都从头来
    return ti


# ══════════════════════════════════════════════════════════════════════
# 1. 逃生舱：--real 的判定是纯函数
# ══════════════════════════════════════════════════════════════════════
class TestWantReal:
    def test_default_is_isolated(self, iso, monkeypatch):
        monkeypatch.delenv(iso.REAL_ENV, raising=False)
        assert iso.want_real([]) is False, "默认竟然不是隔离模式"
        assert iso.want_real(["--depth", "2"]) is False

    def test_flag_opts_in(self, iso, monkeypatch):
        monkeypatch.delenv(iso.REAL_ENV, raising=False)
        assert iso.want_real(["--real"]) is True
        assert iso.want_real(["--pages", "5", "--real"]) is True

    def test_env_var_opts_in(self, iso, monkeypatch):
        monkeypatch.setenv(iso.REAL_ENV, "1")
        assert iso.want_real([]) is True

    def test_help_text_states_the_risk(self, iso):
        """**帮助里必须写明风险** —— 一个藏起来的逃生舱等于没有逃生舱。"""
        import argparse
        ap = argparse.ArgumentParser()
        iso.add_real_flag(ap)
        helptext = ap.format_help()
        assert "--real" in helptext
        for word in ("危险", "真实数据根", "隔离"):
            assert word in helptext, f"帮助文本没写明 {word!r}"


# ══════════════════════════════════════════════════════════════════════
# 2. 默认模式：真的把数据根搬走，而且不碰机主的真实值
# ══════════════════════════════════════════════════════════════════════
class TestDefaultRedirectsTheDataRoot:
    def test_returns_a_temp_root_and_leaves_the_real_one_untouched(self, iso, monkeypatch):
        real = str(ROOT / "_fake_real_localappdata")
        monkeypatch.setenv("LOCALAPPDATA", real)
        got = iso.activate("t_script", argv=[])
        assert got is not None, "默认模式竟然返回 None（= 没隔离）"
        assert str(got).startswith(tempfile.gettempdir()), "隔离根不在临时目录里"
        assert os.environ["LOCALAPPDATA"] != real, "LOCALAPPDATA 没被重定向"
        assert pathlib.Path(os.environ["LOCALAPPDATA"]).is_dir()

    def test_both_hooks_land_inside_the_temp_root(self, iso, monkeypatch):
        """**这条是本次修复的核心**：`CONFIG_FILE`（导入期求值）与 `data_root()`
        （调用期求值）必须**同时**能落进临时根。

        只重定向其中一个是不够的 —— 这正是 `tests/conftest.py` 记的那个坑：
        `setenv("LOCALAPPDATA", …)` 对已经固化的模块常量无效。

        ⚠️ 这里**不能**直接断言 `launcher_v8.CONFIG_FILE`：conftest 的 autouse 护栏
        已经把那个模块常量钉到 pytest 的 tmp 上了（且它在本测试之前就完成了导入）。
        要验的是"**导入期那一步**会算到哪里"，所以直接调那个唯一的路径函数。
        """
        monkeypatch.setenv("LOCALAPPDATA", str(ROOT / "_fake_real_localappdata"))
        root = iso.activate("t_script", argv=[])

        from kiana_vnext_plus.config import data_root
        assert pathlib.Path(data_root()).is_relative_to(root), \
            f"data_root() 没落在隔离根里：{data_root()}"

        import launcher_v8
        cfg_after_redirect = launcher_v8._launcher_config_path()
        assert cfg_after_redirect.is_relative_to(root), (
            f"导入期算出的 CONFIG_FILE 路径没落在隔离根里：{cfg_after_redirect} "
            f"—— 说明 activate() 没能在导入之前生效")
        # 而且它确实**不再**指向那个假真实根（否则等于没隔离）
        assert not cfg_after_redirect.is_relative_to(ROOT / "_fake_real_localappdata")

    def test_is_idempotent(self, iso, monkeypatch):
        """重复调用不许又造一个新临时目录（否则"最后一个赢"，隔离根会漂）。"""
        monkeypatch.setenv("LOCALAPPDATA", str(ROOT / "_fake_real_localappdata"))
        a = iso.activate("t_script", argv=[])
        b = iso.activate("t_script", argv=[])
        assert a == b, "第二次 activate() 又造了一个新的隔离根"

    def test_clears_inherited_cookie_and_portable_vars(self, iso, monkeypatch):
        """不许继承机主的 cookies 来源（否则脚本会拿真登录态去真爬），
        也不许继承 `KIANA_PORTABLE`（非冻结时它把数据根指到**仓库**里的 KianaData/，
        那是污染仓库而不是隔离）。"""
        monkeypatch.setenv("LOCALAPPDATA", str(ROOT / "_fake_real_localappdata"))
        monkeypatch.setenv("KIANA_PORTABLE", "1")
        monkeypatch.setenv("KIANA_COOKIE_FILES", "C:/real/cookies.txt")
        monkeypatch.setenv("KIANA_COOKIE_FILE", "C:/real/cookies2.txt")
        root = iso.activate("t_script", argv=[])
        assert "KIANA_PORTABLE" not in os.environ
        assert "KIANA_COOKIE_FILES" not in os.environ
        assert "KIANA_COOKIE_FILE" not in os.environ
        from kiana_vnext_plus.config import data_root
        assert pathlib.Path(data_root()).is_relative_to(root)

    def test_real_mode_changes_nothing(self, iso, monkeypatch):
        real = str(ROOT / "_fake_real_localappdata")
        monkeypatch.setenv("LOCALAPPDATA", real)
        monkeypatch.setenv("KIANA_PORTABLE", "1")
        assert iso.activate("t_script", argv=["--real"]) is None
        assert os.environ["LOCALAPPDATA"] == real, "--real 竟然还是改了环境变量"
        assert os.environ["KIANA_PORTABLE"] == "1"


# ══════════════════════════════════════════════════════════════════════
# 3. 副作用的取舍：隔离数据根**不许**把浏览器也弄丢
# ══════════════════════════════════════════════════════════════════════
class TestBrowserPathIsPreserved:
    def test_real_chromium_dir_is_pinned_before_redirect(self, iso, monkeypatch, tmp_path):
        """`run_crawler._detect_browser_path()` 是从 `%LOCALAPPDATA%\\ms-playwright`
        找 Chromium 的 —— 重定向数据根会把它指丢。所以必须先把它固化进
        `PLAYWRIGHT_BROWSERS_PATH`（`run_crawler.py:92` 是 `setdefault`，先设的赢）。"""
        fake_local = tmp_path / "LocalAppData"
        chromium = fake_local / "ms-playwright" / "chromium-1234"
        chromium.mkdir(parents=True)
        monkeypatch.setenv("LOCALAPPDATA", str(fake_local))
        monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
        monkeypatch.delenv("PATCHRIGHT_BROWSERS_PATH", raising=False)

        iso.activate("t_script", argv=[])
        assert os.environ.get("PLAYWRIGHT_BROWSERS_PATH") == str(fake_local / "ms-playwright")
        assert os.environ.get("PATCHRIGHT_BROWSERS_PATH") == str(fake_local / "ms-playwright")

    def test_user_supplied_path_wins(self, iso, monkeypatch, tmp_path):
        """用户自己设过就**不许覆盖**（他可能指向打包内 browsers/ 或别的安装）。"""
        fake_local = tmp_path / "LocalAppData"
        (fake_local / "ms-playwright" / "chromium-1234").mkdir(parents=True)
        monkeypatch.setenv("LOCALAPPDATA", str(fake_local))
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "D:/my/browsers")
        iso.activate("t_script", argv=[])
        assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == "D:/my/browsers"

    def test_no_browser_dir_is_not_an_error(self, iso, monkeypatch, tmp_path):
        fake_local = tmp_path / "LocalAppData"
        fake_local.mkdir(parents=True)
        monkeypatch.setenv("LOCALAPPDATA", str(fake_local))
        monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
        monkeypatch.delenv("PATCHRIGHT_BROWSERS_PATH", raising=False)
        assert iso.activate("t_script", argv=[]) is not None
        assert "PLAYWRIGHT_BROWSERS_PATH" not in os.environ


# ══════════════════════════════════════════════════════════════════════
# 4. 接线：**全仓排查**固化成一条会持续生效的断言
# ══════════════════════════════════════════════════════════════════════
def _parse(name: str) -> ast.Module:
    return ast.parse((ROOT / "tools" / name).read_text(encoding="utf-8"))


def _activate_line(tree) -> int:
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "activate"):
            return n.lineno
    raise AssertionError("这个脚本没有调用 activate()")


def _launcher_import_lines(tree) -> list:
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name in ("launcher_v8", "launcher_v9"):
                    out.append(n.lineno)
        elif isinstance(n, ast.ImportFrom) and n.module in ("launcher_v8", "launcher_v9"):
            out.append(n.lineno)
    return out


def _window_construction_lines(tree) -> list:
    return [n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr in ("KianaV9", "KianaV8")]


def _dynamic_launcher_load_lines(tree) -> list:
    """`importlib.util.spec_from_file_location("launcher_v9", ...)` 这种**动态导入**
    也是"导入 launcher"，一样必须排在 activate() 之后。"""
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr in ("spec_from_file_location", "exec_module"):
            if "launcher" in ast.unparse(n):
                out.append(n.lineno)
    return out


class TestScriptsActivateBeforeTouchingLauncher:
    @pytest.mark.parametrize("name", ISOLATED_SCRIPTS)
    def test_activate_precedes_every_launcher_touch(self, name):
        """**顺序就是全部**：`launcher_v8.CONFIG_FILE` 是**导入期**求值的模块常量 ——
        导入之后再改环境变量，它早就固化到机主真路径上了，隔离等于白做。"""
        tree = _parse(name)
        line = _activate_line(tree)
        later = _launcher_import_lines(tree) + _window_construction_lines(tree) \
            + _dynamic_launcher_load_lines(tree)
        assert later, f"{name} 看起来根本没碰 launcher，那就没必要隔离（请从清单里去掉）"
        assert line < min(later), (
            f"{name}: activate() 在第 {line} 行，但第一处 launcher 接触在第 {min(later)} 行 "
            f"—— 顺序反了，隔离无效")

    @pytest.mark.parametrize("name", ISOLATED_SCRIPTS)
    def test_docstring_tells_the_operator_about_real(self, name):
        """脚本自己的说明里要点出 `--real` —— 这些脚本大多没有 argparse，
        `--help` 指望不上，所以说明文字就是它的"帮助"。"""
        src = (ROOT / "tools" / name).read_text(encoding="utf-8")
        assert "--real" in src, f"{name} 的说明里没提 --real（逃生舱不可发现 = 没有逃生舱）"

    def test_no_tools_script_constructs_a_launcher_window_unisolated(self):
        """**全仓排查的固化**：将来新加的 `tools/` 脚本只要构造 launcher 窗口，
        就**必须**先 activate() —— 光构造 `KianaV9()` 就有写真配置的路径
        （启动迁移钩子），这条断言让下一个作者不可能漏掉。"""
        offenders = []
        for p in sorted((ROOT / "tools").glob("*.py")):
            if p.name.startswith("_"):
                continue
            tree = ast.parse(p.read_text(encoding="utf-8"))
            touches = (_window_construction_lines(tree)
                       + _launcher_import_lines(tree)
                       + _dynamic_launcher_load_lines(tree))
            if not touches:
                continue
            try:
                line = _activate_line(tree)
            except AssertionError:
                offenders.append(p.name)
                continue
            if line > min(touches):
                offenders.append(f"{p.name}(顺序反了)")
        assert not offenders, (
            "这些 tools/ 脚本会碰 launcher 却没有先做默认隔离："
            f"{offenders} —— 它们会读写机主真实的 launcher_config.json / secrets/ / "
            "http_cache/。照 tools/_tools_isolation.py 的用法在最顶上加 activate()。")

    def test_the_helper_stays_importable_without_qt(self):
        """隔离器必须在**任何** GUI 之前可用：它自己不许 import Qt / launcher。"""
        src = (ROOT / "tools" / "_tools_isolation.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods.update(a.name.split(".")[0] for a in n.names)
            elif isinstance(n, ast.ImportFrom) and n.module:
                mods.add(n.module.split(".")[0])
        assert "PySide6" not in mods, "隔离器 import 了 Qt（那它就跑不到 GUI 之前了）"
        assert not any(m.startswith("launcher") for m in mods), \
            "隔离器 import 了 launcher —— 那正好是它要抢在之前的东西"


# ══════════════════════════════════════════════════════════════════════
# 5. 端到端：**另起一个真进程**，看它到底往哪儿写（含反证）
#
#    前面全是 AST 与纯逻辑。这一组回答的是本轮唯一真正的问题：
#    「把 `activate()` 那一行加上，机主的真实数据根**是不是真的**一个字节都不动？」
#
#    ⚠️ 用**子进程**而不是进程内，是因为"导入期求值的模块常量"这件事只有在
#    全新解释器里才复现得出来（进程内 `launcher_v8` 早被 conftest 打过桩了）。
#    ⚠️ 子进程不建 QApplication、不 show()、不联网 —— 不会弹任何窗口。
# ══════════════════════════════════════════════════════════════════════

# 子进程要跑的代码：假装自己是 `tools/` 下的一个探针脚本。
# FAKE_REAL 扮演"机主的真实 LOCALAPPDATA"；WITH_ISOLATION 决定加不加那一行。
_E2E_PROBE = r'''
import json, os, sys
REPO, FAKE_REAL, WITH_ISO = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
sys.path.insert(0, os.path.join(REPO, "tools"))
sys.path.insert(0, REPO)
os.environ["LOCALAPPDATA"] = FAKE_REAL          # 扮演机主的真实数据根

isolated_root = None
if WITH_ISO:
    from _tools_isolation import activate
    isolated_root = activate("e2e_probe")       # 顺序关键：必须在 import launcher 之前

import launcher_v8                              # noqa: E402  （导入期固化 CONFIG_FILE）
from kiana_vnext_plus.config import data_root   # noqa: E402

# 真走一次"保存配置"的路径（这正是 tools 脚本会碰到的那条）
launcher_v8.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
launcher_v8.CONFIG_FILE.write_text(json.dumps({"probe": True}), encoding="utf-8")
os.makedirs(os.path.join(data_root(), "http_cache"), exist_ok=True)

print(json.dumps({
    "config_file": str(launcher_v8.CONFIG_FILE),
    "data_root": str(data_root()),
    "isolated_root": str(isolated_root) if isolated_root else "",
}))
'''


def _run_e2e_probe(fake_real: pathlib.Path, with_isolation: bool) -> dict:
    """在**子进程**里跑一次探针，返回它报告的落点。

    `KIANA_TOOLS_KEEP_DATA=1` 是**必须**的：隔离根默认由 `atexit` 删掉
    （那是设计行为——跑完不留垃圾），子进程一退，父进程就再也看不到里面写了什么。
    留着它，父进程才能验证"确实写进了隔离根"而不是"什么都没做"。
    （这条也是被自己的测试逼出来的：第一版没设它，断言 ③ 直接红——
      顺带**证明了 atexit 清理真的在跑**。）
    """
    import json
    import subprocess as sp
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["KIANA_TOOLS_KEEP_DATA"] = "1"
    env.pop("KIANA_PORTABLE", None)
    r = sp.run([sys.executable, "-c", _E2E_PROBE, str(ROOT), str(fake_real),
                "1" if with_isolation else "0"],
               capture_output=True, text=True, encoding="utf-8", errors="replace",
               timeout=180, env=env)
    assert r.returncode == 0, f"探针子进程失败：\n{r.stdout}\n{r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


class TestEndToEndIsolation:
    """**本轮最强的一条判据**：隔离开与不开，落点必须一个在临时根、一个在假真实根。

    只做"开了隔离之后假真实根没变"是不够的 —— 那样无法排除
    "这段代码本来就什么都不写"（那这条测试就是安慰剂）。所以先跑**反证**：
    不加隔离时它**必须真的**写进假真实根。
    """

    def test_counterfactual_without_isolation_it_really_writes(self, tmp_path):
        """反证（**没有它，下面那条测试毫无意义**）：不加隔离时，
        `launcher_v8.CONFIG_FILE` 就是机主真路径，保存配置真的落在那儿。"""
        fake_real = tmp_path / "FakeRealLocalAppData"
        fake_real.mkdir()
        info = _run_e2e_probe(fake_real, with_isolation=False)
        assert info["isolated_root"] == "", "没加隔离却报告了隔离根"
        assert pathlib.Path(info["config_file"]).is_relative_to(fake_real), \
            f"不加隔离时配置竟然没落在'机主'目录里：{info['config_file']}"
        assert (fake_real / "KianaVnextPlus" / "launcher_config.json").exists(), \
            "反证失败：这段代码根本没写 —— 那下面的隔离测试就是安慰剂"
        assert (fake_real / "KianaVnextPlus" / "http_cache").is_dir(), \
            "反证失败：http_cache 没建出来（说明写入面不止配置一个）"

    def test_with_isolation_the_real_root_is_untouched(self, tmp_path):
        """正题：加一行 `activate()` 之后，那个"机主目录"**一个条目都不许多**。

        比文件指纹更严：连**目录结构**都不能变（`mkdir` 不改任何文件的 mtime，
        只比文件指纹会漏掉"用默认 root 建了个目录"这种情况）。
        """
        fake_real = tmp_path / "FakeRealLocalAppData"
        fake_real.mkdir()
        before = sorted(p.name for p in fake_real.iterdir())

        info = _run_e2e_probe(fake_real, with_isolation=True)
        root = pathlib.Path(info["isolated_root"])
        try:
            assert info["isolated_root"], "activate() 没返回隔离根"
            # ① 两个钩子都落进隔离根
            assert pathlib.Path(info["config_file"]).is_relative_to(root), \
                f"CONFIG_FILE（导入期常量）没落在隔离根里：{info['config_file']}"
            assert pathlib.Path(info["data_root"]).is_relative_to(root), \
                f"data_root()（调用期）没落在隔离根里：{info['data_root']}"
            assert pathlib.Path(info["config_file"]).is_relative_to(
                root / "LocalAppData" / "KianaVnextPlus")
            # ② 假"机主目录"的结构**一个条目都没多**
            after = sorted(p.name for p in fake_real.iterdir())
            assert after == before, (
                f"隔离模式下机主目录还是被动了：{before} → {after}。"
                f"（只比文件指纹会漏掉 mkdir —— 所以这里比的是**条目集合**）")
            # ③ 隔离根里确实写进去了（否则"没动"可能只是因为什么都没做）
            assert (root / "LocalAppData" / "KianaVnextPlus"
                    / "launcher_config.json").exists(), \
                "隔离根里没有配置文件 —— 那这次探测根本没走到写入路径"
            assert (root / "LocalAppData" / "KianaVnextPlus" / "http_cache").is_dir(), \
                "隔离根里没有 http_cache —— 派生数据的落点也没被隔离住"
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)     # 临时根不留给别人

    def test_real_flag_writes_to_the_real_root_on_purpose(self, tmp_path):
        """逃生舱：加 `--real` 时**故意**写真实根 —— 但必须打印风险提示，
        不能悄悄就用真根（"默认安全"不等于"没有出口"）。"""
        import subprocess as sp
        fake_real = tmp_path / "FakeRealLocalAppData"
        fake_real.mkdir()
        env = dict(os.environ)
        env["QT_QPA_PLATFORM"] = "offscreen"
        env.pop("KIANA_PORTABLE", None)
        probe = (
            "import os, sys;"
            f"sys.path.insert(0, {str(ROOT / 'tools')!r});"
            f"os.environ['LOCALAPPDATA'] = {str(fake_real)!r};"
            "import _tools_isolation as t;"
            "print('ROOT=', t.activate('probe', argv=['--real']))"
        )
        r = sp.run([sys.executable, "-c", probe], capture_output=True, text=True,
                   encoding="utf-8", errors="replace", timeout=120, env=env)
        assert r.returncode == 0, r.stderr
        assert "ROOT= None" in r.stdout, "--real 竟然还做了隔离"
        assert "⚠️" in r.stderr and "真实数据根" in r.stderr, \
            "--real 用了真根却没打印风险提示（逃生舱必须是显眼的）"
