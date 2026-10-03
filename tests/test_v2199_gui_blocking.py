# -*- coding: utf-8 -*-
"""[v2.19.9] GUI 主线程阻塞收口：设置页「隐私扫描」不再冻界面。

## 为什么单独一个文件

`_run_priv_scan()` 原来在**槽函数里同步** `subprocess.run(timeout=30)`：
子进程要读 %TEMP%、扫用户配置、抽查日志 —— 最坏 30 秒里 Qt 事件循环**完全不转**，
窗口一片白、点什么都没反应。这与上一轮修掉的 `_start()`（最长 10 秒）是同一类，
只是更久；上一轮为 `_refresh_privacy_status_async` / `_check_cookie_sources_async`
立的那套模式（daemon 线程 + 自己的事件循环 + `QApplication.postEvent` 回主线程）
这次要照搬到第三条路径上。

## 判据（缺一不可）

1. **不许阻塞**：桩住扫描子进程让它"挂着"，`_run_priv_scan()` 必须**秒回**；
2. **不许丢结果**：异步回来之后，结论照样要落到**原来看得见的地方**（toast）。
   只把它丢进线程却没人显示 = "异步了，结论没了"，比同步假死更隐蔽；
3. **不许跨线程 Signal / `asyncio.run()`**（CLAUDE.md 陷阱表 + 本文件的 AST 断言）；
4. **不许弹第二个窗口**：打包版里 `sys.executable` 就是 GUI 自身、`tools/` 又没随包
   分发 —— 拿它去跑脚本等于**再弹一个 Kiana 窗口**。这条纯逻辑判据在这里单测。

⚠️ 本文件**不启动任何子进程**（`subprocess.run` 被桩住），也不 `show()` 窗口：
离屏构造（`QT_QPA_PLATFORM=offscreen`）只为验证接线与耗时。
"""
import ast
import copy
import json
import os
import sys
import threading
import time
import unittest
from functools import lru_cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


# ══════════════════════════════════════════════════════════════════════
# 源码级工具（照 tests/test_cookie_login_gui.py 的样式）
# ══════════════════════════════════════════════════════════════════════
class _StripProse(ast.NodeTransformer):
    """删掉裸字符串语句（docstring / 伪注释）——它们不是可执行代码。"""

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


def _tree() -> ast.Module:
    """读一次缓存住（本文件多处要用同一份 AST）。

    刻意**不用 `setUpClass`**：本文件的类是普通类、不是 `unittest.TestCase`，
    pytest 对普通类只认小写 `setup_class` —— 写成 `setUpClass` 会**静默不执行**，
    然后每个测试都挂在 `AttributeError: no attribute 'tree'` 上（我第一版就是这么翻车的）。
    """
    return _cached_tree()


@lru_cache(maxsize=1)
def _cached_tree() -> ast.Module:
    return ast.parse((ROOT / "launcher_v9.py").read_text(encoding="utf-8"))


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


def _nested(tree, outer, inner, cls=None):
    """取 `outer` 里嵌套定义的 `inner`（本文件用它区分"主线程那层"与"线程里那层"）。"""
    for n in ast.walk(_func(tree, outer, cls=cls)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == inner:
            return n
    raise AssertionError(f"{outer} 里没有嵌套函数 {inner}")


def _code_only(node) -> str:
    """AST 节点 → **只含可执行代码**的文本（剥掉 docstring 与裸字符串）。

    本文件的断言大多形如"主线程那层不该再出现 X"，而**说明性 docstring 本身就会引用 X**
    （例如解释"原实现是在本槽里同步 `subprocess.run(..., timeout=30)`"）。
    不剥掉就会被自己的说明骗成假红 —— 我第一版正是这么翻车的。
    """
    return ast.unparse(_StripProse().visit(copy.deepcopy(node)))


def _outer_only(fn) -> str:
    """函数体文本：**剥掉注释** + 把**嵌套函数整体替换成 pass**。

    用途：断言"主线程这一层没有 X"。直接 `ast.unparse(整个函数)` 会把线程体 `_work`
    一并算进来，于是"主线程没直接调 subprocess"这条断言永远是假绿。
    """
    fn = copy.deepcopy(fn)
    fn.body = [ast.Pass() if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)) else s
               for s in fn.body]
    return _code_only(fn)


# ══════════════════════════════════════════════════════════════════════
# 1. 纯逻辑：打包版守卫（不碰 Qt、不碰子进程）
# ══════════════════════════════════════════════════════════════════════
class TestPrivScanGuardIsPureLogic:
    def _gui(self):
        try:
            import launcher_v9
            return launcher_v9
        except Exception as e:                      # 无 Qt/qfluentwidgets 环境
            pytest.skip(f"launcher_v9 不可导入：{e}")

    def test_source_mode_is_available(self):
        """源码模式下没有"不可用"的理由 —— 否则这个功能就被守卫误杀了。"""
        gui = self._gui()
        assert gui._priv_scan_unavailable_reason() == ""

    def test_frozen_mode_refuses_instead_of_spawning_a_second_gui(self, monkeypatch):
        """**这条是本轮防"弹第二个窗口"的核心判据。**

        打包版里 `sys.executable` 就是 KianaLauncher.exe，而 spec **没有**把 `tools/`
        打进包（已核对 KianaLauncher.spec 的 datas）。于是原实现那行
        `subprocess.run([sys.executable, <脚本>])` 会：
          ① 找不到脚本（子进程白跑一趟），② 更糟 —— 那个 exe **不看 argv**，
             直接又起一个完整 GUI（`main()` 里是 `QApplication(sys.argv)`）。
        在"不许弹窗"的纪律下，正确做法是**如实拒绝**，而不是把窗口弹到用户脸上。
        """
        gui = self._gui()
        monkeypatch.setattr(gui.sys, "frozen", True, raising=False)
        reason = gui._priv_scan_unavailable_reason()
        assert reason, "冻结版竟然认为可以跑扫描 —— 会弹出第二个 GUI 窗口"
        assert "窗口" in reason, "理由要说到点子上（会弹窗），否则下一个人又会去'修好'它"

    def test_missing_script_reports_honestly(self, monkeypatch):
        """脚本不在（比如被挪走）→ 也得给理由，不许静默当"扫过了、没问题"。"""
        gui = self._gui()
        monkeypatch.setattr(gui, "_priv_scan_script_path",
                            lambda: Path("Z:/definitely/not/here.py"))
        assert gui._priv_scan_unavailable_reason() != ""


# ══════════════════════════════════════════════════════════════════════
# 2. AST 接线断言（不需要导入 Qt）
# ══════════════════════════════════════════════════════════════════════
class TestPrivScanWiring(unittest.TestCase):
    """源码级接线断言。继承 `unittest.TestCase` 是为了能用 `assertIn` 这类断言
    —— 普通类上它们不存在（`AttributeError`），而那看起来像"测试挂了"，
    实际是"断言根本没跑"。"""

    @property
    def tree(self):
        return _tree()

    def test_subprocess_moved_off_the_main_thread(self):
        """主线程那一层**不许**再有 subprocess 调用；它必须在线程体 `_work` 里。

        只断言"函数里有 Thread"是不够的 —— 完全可以"起了线程，然后自己在主线程
        又同步跑一遍"（本工程真出现过"起了线程但仍同步等"的写法）。
        """
        fn = _func(self.tree, "_run_priv_scan", cls="KianaV9")
        outer = _outer_only(fn)
        self.assertNotIn("subprocess", outer,
                         "_run_priv_scan 的主线程那一层还在碰 subprocess（照旧会冻界面）")
        work = _nested(self.tree, "_run_priv_scan", "_work", cls="KianaV9")
        self.assertIn("subprocess", ast.unparse(work), "子进程没被搬进线程体")

    def test_uses_the_projects_thread_pattern(self):
        """照 `_refresh_privacy_status_async` / `_check_cookie_sources_async` 同款：
        daemon 线程 + `_deliver_async`（= `QApplication.postEvent`）回主线程。"""
        src = _code_only(_func(self.tree, "_run_priv_scan", cls="KianaV9"))
        self.assertIn("Thread", src, "没起后台线程（界面照旧假死最长 30 秒）")
        self.assertIn("daemon", src, "必须是 daemon 线程（否则关窗口时会被挂住）")
        self.assertIn("_deliver_async", src, "结果没经 postEvent 投递回主线程")
        self.assertNotIn("asyncio.run(", src)
        self.assertNotIn("Signal", src, "CLAUDE.md 陷阱表：worker 线程 emit 信号偶发崩")

    def test_result_is_delivered_back_and_dispatched(self):
        """投递出去还得有人接：`event()` 必须分发 `priv_scan` 这一路，否则结果掉进黑洞。"""
        ev = _code_only(_func(self.tree, "event", cls="KianaV9"))
        self.assertIn("priv_scan", ev, "postEvent 投递的 priv_scan 没人接")
        self.assertIn("_on_priv_scan_done", ev, "没接到回填槽上")

    def test_handler_restores_the_button(self):
        """回填那一槽必须**还原按钮**，否则一次扫描之后按钮永久禁用。"""
        src = _code_only(_func(self.tree, "_on_priv_scan_done", cls="KianaV9"))
        self.assertIn("setEnabled(True)", src, "按钮没被还原 —— 扫一次就永久灰掉")
        self.assertIn("_toast", src, "结论没落到原来看得见的地方（toast）")

    def test_the_button_is_reachable_from_the_window(self):
        """按钮必须挂在设置页上（`win.settings.priv_scan_btn`），否则槽里拿不到它、
        就没法做禁用/还原。原实现里它只是个局部变量 `priv_scan`。"""
        build = _code_only(_func(self.tree, "build", cls="SettingsPage"))
        self.assertIn("priv_scan_btn", build, "按钮没被挂到设置页上（槽里够不着）")

    def test_busy_flag_guards_double_click(self):
        """连点不许起 N 个子进程。"""
        src = _code_only(_func(self.tree, "_run_priv_scan", cls="KianaV9"))
        self.assertIn("_priv_scan_busy", src, "没有防重入标志")


# ══════════════════════════════════════════════════════════════════════
# 3. 离屏端到端：真起线程 + 真 postEvent 回主线程（不弹窗、不起子进程）
# ══════════════════════════════════════════════════════════════════════
try:
    import launcher_v9 as gui
except Exception:                                   # pragma: no cover
    gui = None


@pytest.mark.skipif(gui is None, reason="launcher_v9 不可导入")
class TestPrivScanOffscreen:
    """AST 只能证明"代码里是这么写的"，证明不了"真的不卡、结果真的回得来"。"""

    @pytest.fixture
    def win(self, tmp_path, monkeypatch):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        # 数据根挪到 tmp：离屏构造会读 launcher_config / cookies 来源，
        # 不许碰到用户真实的 %LOCALAPPDATA%\KianaVnextPlus
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        monkeypatch.delenv("KIANA_COOKIE_FILES", raising=False)
        monkeypatch.delenv("KIANA_COOKIE_FILE", raising=False)
        try:
            from PySide6.QtWidgets import QApplication
        except Exception as e:                              # pragma: no cover
            pytest.skip(f"PySide6 不可用：{e}")
        # ⚠️ 必须在构造**之前**把路径钉住：`launcher_v8.CONFIG_FILE` 是导入期常量，
        # `setenv("LOCALAPPDATA", …)` 对它无效 → 任何 save_config 都会写用户真配置。
        # （理由详见 tests/conftest.py 的 autouse 护栏与 test_cookie_login_gui.py。）
        fake_cfg = tmp_path / "launcher_config.json"
        for _mod in (sys.modules.get("launcher_v8"), gui):
            if _mod is not None:
                monkeypatch.setattr(_mod, "CONFIG_FILE", fake_cfg, raising=False)
        assert gui.CONFIG_FILE == fake_cfg, f"launcher_v9.CONFIG_FILE 没被打桩：{gui.CONFIG_FILE}"
        app = QApplication.instance() or QApplication([])
        w = gui.KianaV9()
        # toast 换成记录器：既验证"结果回来了"，又不弹 InfoBar（离屏也不该弹）
        w._toasts = []
        monkeypatch.setattr(w, "_toast",
                            lambda msg, kind="info": w._toasts.append((msg, kind)))
        yield w
        w.close()
        app.processEvents()

    def _hang_subprocess(self, monkeypatch, stdout: str):
        """把 `subprocess.run` 换成"挂住直到放行"的桩 —— 真实 timeout 是 30 秒，
        桩用 Event 等待，测试更快也更确定。**不真的起任何子进程。**"""
        release = threading.Event()
        seen = {}

        class _R:
            returncode = 1

            def __init__(self, out):
                self.stdout = out
                self.stderr = ""

        def _fake(cmd, **kw):
            seen["cmd"] = list(cmd)
            seen["timeout"] = kw.get("timeout")
            release.wait(15.0)
            return _R(stdout)

        monkeypatch.setattr(gui.subprocess, "run", _fake)
        return release, seen

    def test_scan_returns_immediately_even_when_the_subprocess_hangs(self, win, monkeypatch):
        """**本轮的核心回归**：扫描子进程再慢，点按钮也必须秒回，
        而且结论照样要落到原来看得见的地方（toast）。

        两半判据缺一不可：
          · 时间：同步版会一直等到放行（≥ 桩的等待时间）→ 必红；
          · 结果：只把子进程丢进线程却没人显示 = "异步了，结论丢了"。
        """
        from PySide6.QtWidgets import QApplication

        release, seen = self._hang_subprocess(
            monkeypatch, "  打码密钥存档方式   ⚠️ 检测到明文打码 Key\n\nsummary: ⚠️ 1 项风险")

        t0 = time.perf_counter()
        win._run_priv_scan()
        elapsed = time.perf_counter() - t0

        assert elapsed < 1.0, (
            f"_run_priv_scan() 花了 {elapsed:.2f} 秒 —— 它又在主线程等子进程了"
            "（真实 timeout 是 30 秒，界面会冻满 30 秒）")
        assert not release.is_set(), "桩在调用期间就被放行了，这条测试量不到阻塞"
        # 扫描期间：按钮禁用 + 文字有反馈
        btn = win.settings.priv_scan_btn
        assert btn.isEnabled() is False, "扫描期间按钮没禁用 —— 连点会起 N 个子进程"
        assert btn.text() != "隐私扫描", "扫描期间按钮文字没有任何反馈"

        release.set()                                   # 放子进程收工
        app = QApplication.instance()
        deadline = time.time() + 10
        while time.time() < deadline and not win._toasts:
            app.processEvents()
            time.sleep(0.02)

        assert win._toasts, "扫描结果没回来 —— 异步了但结论被丢掉了"
        msg, kind = win._toasts[-1]
        assert "⚠️" in msg, f"风险行没进 toast：{msg!r}"
        assert kind == "warning", f"有风险却报成 {kind}（谎报干净）"
        assert btn.isEnabled() is True, "扫描回来了按钮没还原 —— 扫一次就永久灰掉"
        assert btn.text() == "隐私扫描", "按钮文字没还原"
        assert seen.get("timeout") == 30, (
            "子进程的 timeout 被改了 —— 它不是卡界面的原因（线程里等 30 秒无所谓），"
            "改小只会让扫描更容易被误判失败")

    def test_clean_scan_reports_success(self, win, monkeypatch):
        """干净时要报 success —— 反之亦然，别把两个方向都写成 warning。"""
        from PySide6.QtWidgets import QApplication

        release, _ = self._hang_subprocess(monkeypatch, "  打码密钥存档方式   ✓ 无明文打码 Key\n\nsummary: ✅ 干净")
        win._run_priv_scan()
        release.set()
        app = QApplication.instance()
        deadline = time.time() + 10
        while time.time() < deadline and not win._toasts:
            app.processEvents()
            time.sleep(0.02)
        assert win._toasts, "干净扫描的结论也没回来"
        assert win._toasts[-1][1] == "success"

    def test_failure_is_reported_not_swallowed(self, win, monkeypatch):
        """子进程炸了也必须出结论（否则用户以为"没提示 = 没问题"）。"""
        from PySide6.QtWidgets import QApplication

        def _boom(cmd, **kw):
            raise OSError("桩：子进程起不来")

        monkeypatch.setattr(gui.subprocess, "run", _boom)
        win._run_priv_scan()
        app = QApplication.instance()
        deadline = time.time() + 10
        while time.time() < deadline and not win._toasts:
            app.processEvents()
            time.sleep(0.02)
        assert win._toasts, "子进程失败被静默吞掉了"
        assert win._toasts[-1][1] == "error"
        assert win.settings.priv_scan_btn.isEnabled() is True, "失败后按钮没还原"

    def test_frozen_mode_does_not_start_a_thread_at_all(self, win, monkeypatch):
        """冻结版走守卫分支：**连线程都不起**（也就绝不会起第二个 GUI）。"""
        calls = []
        monkeypatch.setattr(gui, "_priv_scan_unavailable_reason", lambda: "桩：打包版不可用")
        monkeypatch.setattr(gui.subprocess, "run",
                            lambda *a, **k: calls.append(a))
        started = []
        monkeypatch.setattr(gui.threading, "Thread",
                            lambda *a, **k: started.append(a) or _NoopThread())
        win._run_priv_scan()
        assert not calls, "守卫分支竟然还是起了子进程"
        assert not started, "守卫分支竟然还是起了线程"
        assert win._toasts and win._toasts[-1][1] == "warning", \
            "守卫分支必须如实告知，不能静默什么都不做"


class _NoopThread:
    def start(self):
        raise AssertionError("守卫分支不该起线程")


# ══════════════════════════════════════════════════════════════════════
# 4. 同批收口的另外三处主线程阻塞（v2.19.9 第二轮）
#
#    这三处第一轮只"报告"没修，因为改法都是"搬进后台线程"（会动行为）。
#    本轮补齐：判据一个字没改，只是不再占主线程。
# ══════════════════════════════════════════════════════════════════════
class TestTailReadIsPureLogic:
    """`_read_last_json_line` —— `_parse_progress` 的热路径性能修复。

    `stats.jsonl` 是**追加式**的、随爬取无限增长，而这个读取会被**每条进度日志**
    触发一次。整份 `read_text` 等于每几秒重读一遍全历史，还在主线程上。
    尾部读把代价压成常数 —— 但**必须仍然拿到最后一行**，这是正确性底线。

    ⚠️ 刻意**不继承** `unittest.TestCase`：`TestCase` 的方法拿不到 pytest 的
    `tmp_path` fixture（会变成 "missing 1 required positional argument"）。
    这里用普通类 + 裸 `assert`，与下面的离屏测试同一写法。
    """

    def _f(self):
        if gui is None:
            pytest.skip("launcher_v9 不可导入")
        return gui._read_last_json_line

    def test_returns_the_last_line(self, tmp_path):
        p = tmp_path / "stats.jsonl"
        p.write_text('{"done": 1}\n{"done": 2}\n{"done": 3, "total": 10}\n', encoding="utf-8")
        assert self._f()(p)["done"] == 3

    def test_ignores_trailing_blank_lines(self, tmp_path):
        p = tmp_path / "stats.jsonl"
        p.write_text('{"done": 7}\n\n\n', encoding="utf-8")
        assert self._f()(p)["done"] == 7

    def test_survives_seeking_into_the_middle_of_a_line(self, tmp_path):
        """尾部窗口比文件小 ⇒ seek 必然落在某行**中间**，但**最后一行照样要拿到**。

        （[对抗性验证] 记录：这条测试**不能**证明"先 readline() 丢掉半行"是必需的
        —— 从后往前扫本来就走不到那个残行。我原先那句 readline() 被验证为
        **一行行为都不影响**的死代码，已删。真正的判据是下面那条"写到一半的尾行"。）
        """
        p = tmp_path / "stats.jsonl"
        lines = [json.dumps({"done": i, "pad": "x" * 50}) for i in range(200)]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        got = self._f()(p, tail_bytes=300)          # 300 字节必然切在中间某行
        assert got is not None, "尾部读落在行中间时整条读失败了"
        assert got["done"] == 199, f"拿到的不是最后一行：{got['done']}"

    def test_a_half_written_tail_line_falls_back_to_the_previous_one(self, tmp_path):
        """**引擎一边追加、GUI 一边读**是常态：尾行完全可能正被写到一半。

        只取"最后一个元素"的实现撞上它就返回 None ⇒ 悄悄掉进 regex 兜底。
        正确行为是**继续往回找**第一条完整的记录。
        （把 `continue` 改回 `return None`，这条会红 —— 这是"往回扫"的真正判据。）
        """
        p = tmp_path / "stats.jsonl"
        p.write_text('{"done": 10, "total": 100}\n{"done": 11, "tot', encoding="utf-8")
        got = self._f()(p)
        assert got is not None, "尾行写到一半时整条读失败了（会掉进 regex 兜底）"
        assert got["done"] == 10, f"没有回退到上一条完整记录：{got!r}"

    def test_empty_file_is_none(self, tmp_path):
        p = tmp_path / "stats.jsonl"
        p.write_text("", encoding="utf-8")
        assert self._f()(p) is None

    def test_corrupt_tail_gives_up_cleanly(self, tmp_path):
        """整段尾部全是垃圾 → 老实返回 None（让调用方走 regex 兜底），不抛异常。"""
        p = tmp_path / "stats.jsonl"
        p.write_text("{this is not json\nneither is this\n", encoding="utf-8")
        assert self._f()(p) is None

    def test_missing_file_is_none(self, tmp_path):
        assert self._f()(tmp_path / "nope.jsonl") is None

    def test_empty_json_object_is_still_a_hit(self, tmp_path):
        """合法的 `{}` 必须返回 `{}` 而不是 None —— 调用方若用真值判断，
        就会把"读到了空快照"误当成"没读到"而掉进 regex 兜底，
        统计卡显示的数字于是与 stats.jsonl 说的不一致。"""
        p = tmp_path / "stats.jsonl"
        p.write_text('{"done": 1}\n{}\n', encoding="utf-8")
        got = self._f()(p)
        assert got == {}, f"空的 JSON 对象被当成了『读不到』：{got!r}"


class TestOtherMainThreadBlockersAreFixed(unittest.TestCase):
    """三处**只报告没修**的主线程阻塞 —— 现在都搬进后台了。

    判据沿用上一条同类测试：**主线程那一层不许再出现那个阻塞调用**，
    而且它必须真的在线程体 `_work` 里（只断言"函数里有 Thread"会漏掉
    "起了线程、主线程又同步跑一遍"这种写法）。
    """

    @property
    def tree(self):
        return _tree()

    def _assert_moved(self, fn_name, blocking_token):
        fn = _func(self.tree, fn_name, cls="KianaV9")
        outer = _outer_only(fn)
        self.assertNotIn(blocking_token, outer,
                         f"{fn_name} 的主线程那一层还在用 {blocking_token}（照旧会冻界面）")
        work = _nested(self.tree, fn_name, "_work", cls="KianaV9")
        src = ast.unparse(work)
        self.assertIn(blocking_token, src, f"{fn_name} 的耗时调用没被搬进线程体")
        self.assertIn("daemon", ast.unparse(fn), "必须是 daemon 线程")
        self.assertIn("_deliver_async", src, "结果没经 postEvent 投递回主线程")

    def test_refresh_tasks_moved_off_the_main_thread(self):
        """原实现在主线程对最多 60 个任务目录 `rglob` + 逐文件 `stat()`
        —— 那棵树里是下载下来的视频/图片，动辄上万文件。"""
        self._assert_moved("_refresh_tasks", "rglob")

    def test_retry_dead_moved_off_the_main_thread(self):
        """`sqlite3.connect` 默认 `timeout=5.0`：引擎持写锁时主线程最多干等 5 秒。"""
        self._assert_moved("_retry_dead", "sqlite3")

    def test_import_url_file_read_moved_off_the_main_thread(self):
        """用户可能选一个几万行的 URL 清单，整份 `read_text` 不该占主线程。"""
        self._assert_moved("_import_url_file", "read_text")

    def test_progress_stats_read_is_bounded(self):
        """`_parse_progress` 不许再整份读 `stats.jsonl`（它在**每条**进度日志上跑）。"""
        src = ast.unparse(_func(self.tree, "_parse_progress", cls="KianaV9"))
        self.assertNotIn("read_text", src,
                         "还在整份读 stats.jsonl —— 它会随爬取无限增长，而本函数被"
                         "每条进度日志调用一次")
        self.assertIn("_read_last_json_line", src, "没走尾部读的唯一实现")

    def test_progress_does_not_use_truthiness_on_the_snapshot(self):
        """[对抗性验证 补] 调用方必须用 `is not None` 判"读到了"，**不许用真值判断**。

        合法的 `{}` 快照是真值假（falsy）—— 用 `if last:` 会把它当成"没读到"
        而掉进 regex 兜底，统计卡显示的数字于是与 `stats.jsonl` 说的不一致。
        改这一处**不会有任何测试变红**（我实测过），所以显式钉一条。
        """
        f = _func(self.tree, "_parse_progress", cls="KianaV9")
        src = _code_only(f)
        self.assertIn("is not None", src,
                      "调用方用真值判断接收快照 —— 合法的 `{}` 会被误判成『没读到』")

    def test_priv_scan_decodes_the_child_as_utf8(self):
        """[对抗性验证 补] 子进程的解码必须**显式 utf-8**，且与扫描器那侧配套。

        `tools/privacy_scanner.py` 内部把自己的 stdout 改成了 UTF-8
        （否则它 print ⚠️/✅ 时在 GBK 控制台上抛 UnicodeEncodeError ——
        而 GUI 这条路是**管道**，它必崩，这个按钮以前从来没成功过）。
        父进程若还用 `text=True` 走 locale(cp936)，就变成"那边写 UTF-8、
        这边按 GBK 解" → 满屏乱码。**改回 text=True 不会有任何测试变红**，故显式钉住。
        """
        work = _nested(self.tree, "_run_priv_scan", "_work", cls="KianaV9")
        src = ast.unparse(work)
        # 注意：`ast.unparse` 会把字符串字面量规范化成**单引号**（本文件里已踩过两次）
        self.assertIn("encoding='utf-8'", src,
                      "父进程没按 utf-8 解码子进程输出（与扫描器那侧不配套）")
        self.assertNotIn("text=True", src,
                         "用了 text=True → 走 locale(cp936)，与子进程的 UTF-8 不配套")
        # 另一侧：扫描器自己必须把 stdout 调成 utf-8（同一份契约的两半）
        scanner = (ROOT / "tools" / "privacy_scanner.py").read_text(encoding="utf-8")
        self.assertIn('reconfigure(encoding="utf-8"', scanner,
                      "扫描器没把自己的 stdout 改成 utf-8（在管道下 print ⚠️ 会崩）")

    def test_new_kinds_are_dispatched(self):
        """投递出去还得有人接，否则结果掉进黑洞、按钮永久禁用。"""
        ev = _code_only(_func(self.tree, "event", cls="KianaV9"))
        for kind, handler in (("tasks", "_on_tasks_done"),
                              ("retry_dead", "_on_retry_dead_done"),
                              ("url_file", "_on_url_file_loaded")):
            self.assertIn(kind, ev, f"postEvent 投递的 {kind} 没人接")
            self.assertIn(handler, ev, f"{kind} 没接到回填槽 {handler} 上")

    def test_tasks_refresh_restores_the_button(self):
        """刷新是异步的 ⇒ 必须还原按钮，否则点一次「刷新」就永久灰掉。"""
        src = _code_only(_func(self.tree, "_on_tasks_done", cls="KianaV9"))
        self.assertIn("setEnabled(True)", src, "刷新按钮没被还原")
        self.assertIn("tasks_list", src, "结论没落回原来那个控件")
        build = _code_only(_func(self.tree, "build", cls="TasksPage"))
        self.assertIn("refresh_btn", build, "按钮没挂到页面上（槽里够不着）")

    def test_busy_guard_and_feedback(self):
        """连点不许排队起 N 个遍历线程；而且遍历期间要有文字反馈。"""
        src = _code_only(_func(self.tree, "_refresh_tasks", cls="KianaV9"))
        self.assertIn("_tasks_busy", src, "没有防重入标志")
        self.assertIn("setEnabled(False)", src, "遍历期间按钮没禁用")
        self.assertIn("tasks_list", src, "遍历期间没有任何文字反馈")


@pytest.mark.skipif(gui is None, reason="launcher_v9 不可导入")
class TestTasksRefreshOffscreen:
    """离屏端到端：统计再慢，点「刷新」也必须秒回，且结果照样填进列表。"""

    @pytest.fixture
    def win(self, tmp_path, monkeypatch):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        try:
            from PySide6.QtWidgets import QApplication
        except Exception as e:                              # pragma: no cover
            pytest.skip(f"PySide6 不可用：{e}")
        fake_cfg = tmp_path / "launcher_config.json"
        for _mod in (sys.modules.get("launcher_v8"), gui):
            if _mod is not None:
                monkeypatch.setattr(_mod, "CONFIG_FILE", fake_cfg, raising=False)
        app = QApplication.instance() or QApplication([])
        w = gui.KianaV9()
        monkeypatch.setattr(w, "_toast", lambda *a, **k: None)
        yield w
        w.close()
        app.processEvents()

    def test_returns_immediately_even_when_the_walk_is_slow(self, win, monkeypatch):
        """把 `rglob` 换成"挂住直到放行"的桩 —— 真实场景是遍历上万文件。"""
        from PySide6.QtWidgets import QApplication

        release = threading.Event()
        walked = {}

        class _SlowDir:
            name = "cli_20260101_000000"

            def stat(self):
                class _S:
                    st_mtime = 1700000000.0
                return _S()

            def rglob(self, _pat):
                walked["yes"] = True
                release.wait(15.0)
                return []

        class _FakeBase:
            def glob(self, _pat):
                return [_SlowDir()]

        monkeypatch.setattr(win, "_tasks_base", lambda: _FakeBase())

        t0 = time.perf_counter()
        win._refresh_tasks()
        elapsed = time.perf_counter() - t0
        assert elapsed < 1.0, (
            f"_refresh_tasks() 花了 {elapsed:.2f} 秒 —— 它又在主线程遍历产物目录了")
        assert win.taskp.refresh_btn.isEnabled() is False, "遍历期间按钮没禁用"
        assert "⏳" in win.taskp.tasks_list.toPlainText(), "遍历期间没有任何文字反馈"

        release.set()
        app = QApplication.instance()
        deadline = time.time() + 10
        while time.time() < deadline:
            app.processEvents()
            if win.taskp.refresh_btn.isEnabled():
                break
            time.sleep(0.02)
        assert walked.get("yes"), "后台线程没真的去遍历"
        assert win.taskp.refresh_btn.isEnabled() is True, "回来了按钮没还原"
        assert "cli_20260101_000000" in win.taskp.tasks_list.toPlainText(), \
            "统计结果没填进列表 —— 异步了但结论被丢掉了"

    def test_walk_failure_is_reported_not_swallowed(self, win, monkeypatch):
        """遍历炸了也要出结论（不许静默留个空列表，那看起来像"没有历史任务"）。"""
        from PySide6.QtWidgets import QApplication

        class _BoomBase:
            def glob(self, _pat):
                raise OSError("桩：目录读不了")

        monkeypatch.setattr(win, "_tasks_base", lambda: _BoomBase())
        win._refresh_tasks()
        app = QApplication.instance()
        deadline = time.time() + 10
        while time.time() < deadline:
            app.processEvents()
            if "读取失败" in win.taskp.tasks_list.toPlainText():
                break
            time.sleep(0.02)
        assert "读取失败" in win.taskp.tasks_list.toPlainText(), \
            "遍历失败被静默吞掉了（用户会以为'没有历史任务'）"

    def test_retry_dead_toasts_from_the_worker_path(self, win, monkeypatch):
        """`_on_retry_dead_done` 是 postEvent 的落点：必须按原措辞弹结论。"""
        seen = []
        monkeypatch.setattr(win, "_toast", lambda msg, kind="info": seen.append((msg, kind)))
        win._on_retry_dead_done(("已重置 3 个失败页 → 下次启动同 URL 自动续爬", "success"))
        assert seen == [("已重置 3 个失败页 → 下次启动同 URL 自动续爬", "success")]

    def test_url_file_result_is_appended_on_the_main_thread(self, win):
        """`_on_url_file_loaded` 负责把后台读到的文本追加进 URL 框。"""
        win.home.url_edit.setPlainText("https://a.example")
        win._on_url_file_loaded(("https://b.example\nhttps://c.example", "x.txt", ""))
        text = win.home.url_edit.toPlainText()
        assert "https://a.example" in text and "https://b.example" in text
        assert "https://c.example" in text

    def test_url_file_error_is_toasted(self, win, monkeypatch):
        seen = []
        monkeypatch.setattr(win, "_toast", lambda msg, kind="info": seen.append((msg, kind)))
        win._on_url_file_loaded((None, "x.txt", "编码坏掉了"))
        assert seen and seen[-1][1] == "error"
        assert "编码坏掉了" in seen[-1][0]


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
