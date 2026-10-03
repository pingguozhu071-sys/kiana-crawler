"""v9 最小引擎触发：example.com 快速失败——判定死因与 crawl 内容是否相关

[v2.19.9] 加**默认隔离**：本脚本原先零隔离，直接 `KianaV9()` 构造 + `engine.start()`，
会真写真读用户数据根（`launcher_config.json` / `secrets/` / `http_cache/`）。
现在默认把数据根重定向到临时目录，跑完即删；要对真机跑请显式加 `--real`。
"""
from pathlib import Path
import sys, os, time, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# ⚠️ 必须在 import launcher_v9 **之前**激活（见 _tools_isolation 的模块说明）
from _tools_isolation import activate  # noqa: E402

activate("v9_mini_smoke")

_trace_path = str(Path(__file__).resolve().parent.parent / "tests" / "assets" / "v9mini_trace.txt")
_t0 = [time.time()]


def _trace(msg):
    try:
        with open(_trace_path, "a", encoding="utf-8") as f:
            f.write(f"[{time.time() - _t0[0]:6.1f}s] {msg}\n")
    except Exception:
        pass


_trace("start")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
app = QApplication(sys.argv)
import launcher_v9
win = launcher_v9.KianaV9()
win.move(-2600, -1600)
win.show()
_trace("shown")
win.engine.started.connect(lambda: _trace("engine-started"))
win.engine.finished.connect(lambda rc: (_trace(f"engine-finished rc={rc}")))
_tmp_dir = tempfile.mkdtemp(prefix="v9mini_")
win.engine.start(["https://example.com/"], {
    "crawl_depth": 0, "max_pages": 1, "download_path": _tmp_dir,
    "log_level": "INFO", "dl_video": False, "dl_image": False, "dl_audio": False,
})
_trace("engine-start-called")


def finish():
    _trace("finish")
    with open(_trace_path, "a", encoding="utf-8") as f:
        f.write("result-written\n")
    app.quit()


QTimer.singleShot(45000, finish)
app.exec()
_trace("exec-returned")
