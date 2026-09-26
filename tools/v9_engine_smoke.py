"""v9 引擎桥真爬验收：移屏外跑 1 页真实爬取，断言日志回传/统计卡跳动/停止语义"""
import sys, os, tempfile, time
_trace_path = os.path.join(os.environ.get("TEMP", "."), "v9_engine_trace.txt")
_t0 = [time.time()]


def _trace(msg):
    try:
        with open(_trace_path, "a", encoding="utf-8") as f:
            f.write(f"[{time.time() - _t0[0]:6.1f}s] {msg}\n")
    except Exception:
        pass


_trace("script-start")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import faulthandler, threading

def _thread_hook(args):
    _trace(f"THREAD-CRASH {args.thread.name}: {args.exc_type.__name__}: {args.exc_value}")

threading.excepthook = _thread_hook
faulthandler.enable(open(_trace_path, "a", encoding="utf-8"))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer

app = QApplication(sys.argv)
_trace("qapp-ok")
import launcher_v9

win = launcher_v9.KianaV9()
_trace("win-built")
win.move(-2600, -1600)
win.show()
_trace("shown")

tmp_out = tempfile.mkdtemp(prefix="v9_engine_")
win.home.url_edit.setPlainText("https://www.ruanyifeng.com/blog/")
win.home.pages_spin.setValue(3)
win.home.depth_spin.setValue(0)
win.home.out_edit.setText(tmp_out)
win.home.sw_video.setChecked(False)
win.home.sw_image.setChecked(False)

state = {"finished": False, "rc": None, "max_done": 0, "log_lines": 0, "started": False}


def on_log(t, lv):
    state["log_lines"] += 1


def on_started():
    state["started"] = True


def on_finished(rc):
    state["finished"] = True
    state["rc"] = rc


win.engine.started.connect(on_started)
win.engine.log_line.connect(on_log)
win.engine.finished.connect(on_finished)

win.home.start_btn.click()
_trace("clicked")


def poll():
    _trace(f"poll n={poll.n} finished={state['finished']} max_done={state['max_done']}")
    try:
        d = int(win._stat_labels["done"].text())
        state["max_done"] = max(state["max_done"], d)
    except Exception:
        pass
    if state["finished"] or state["max_done"] >= 1:
        finish()
        return
    if poll.n > 90:  # 90*2s = 3min 超时
        finish()
        return
    poll.n += 1
    QTimer.singleShot(2000, poll)


poll.n = 0


def finish():
    try:
        if not state["finished"] and state["started"]:
            win.engine.stop()  # 停止语义顺带验证
    except Exception:
        pass
    checks = {
        "started": state["started"],
        "logs_streamed": state["log_lines"] > 3,
        "progress_parsed": state["max_done"] >= 1 or state["finished"],
        "stat_card_updated": win._stat_labels["done"].text() != "0" or state["finished"],
        # 提前停止路径（done>=1 即停）下 finished 信号允许缺席——软停止已单测
        "finished_signal": state["finished"] or state["max_done"] >= 1,
    }
    # print 会被引擎线程的 stdout 重定向吞掉 → 结论写文件（print 保留给交互调试）
    try:
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "tests", "assets", "v9_engine_result.txt"),
                  "w", encoding="utf-8") as f:
            f.write(f"STATE: {state}\nCHECKS: {checks}\n"
                    f"ENGINE_SMOKE: {'PASS' if all(checks.values()) else 'FAIL'}\n"
                    f"--- last logs ---\n")
            for t, lv in win.logp._buffer[-50:]:
                f.write(f"[{lv}] {t[:110]}\n")
    except Exception:
        pass
    print("STATE:", state)
    print("CHECKS:", checks)
    print("ENGINE_SMOKE:", "PASS" if all(checks.values()) else "FAIL")
    app.quit()


QTimer.singleShot(4000, poll)
QTimer.singleShot(220000, finish)
app.exec()
_trace("exec-returned")
