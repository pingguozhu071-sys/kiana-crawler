"""GUI 离屏视觉冒烟（v2.11）：offscreen 渲染主页/任务页/数据页 → PNG 自查（不弹窗）

⚠️ [v2.19.8 标注・已过期] 本脚本针对 **v8 旧壳**（`launcher_v8.KianaV8`），而 v8 自 v2.12 起
只是遗留壳（现行 GUI 入口是 `launcher_v9.py`）→ 它检查的不是现行界面，**不要拿它做验收**。
现行替代（按用途选）：
  · 五页截图 + 控件断言：`python tools/v9_smoke.py`（移屏外，产出 tests/assets/gui_shots/v9_*.png）
  · 离屏性能探针（稳态停顿 <200ms）：`python tools/gui_perf_probe.py`
保留原因：仅作 v8 壳的历史对照。改 GUI 时请勿参考本文件。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer

app = QApplication(sys.argv)
import launcher_v8

win = launcher_v8.KianaV8() if hasattr(launcher_v8, "KianaV8") else None
assert win is not None, "未找到主窗口类 KianaV8"
win.show()

out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "assets", "gui_shots")
os.makedirs(out_dir, exist_ok=True)


def snap(page_key, idx, fname):
    win._nav_to(page_key) if page_key != "home" else win._nav_to("home")
    QTimer.singleShot(300, lambda: (
        win.grab().save(os.path.join(out_dir, fname)),
        _next(idx + 1),
    ))


pages = [("home", "gui_home.png"), ("data", "gui_data.png"), ("tasks", "gui_tasks.png")]
_i = [0]


def _next(_):
    if _i[0] >= len(pages):
        app.quit()
        return
    key, fname = pages[_i[0]]
    _i[0] += 1
    win._nav_to(key)
    QTimer.singleShot(400, lambda: (win.grab().save(os.path.join(out_dir, fname)), _next(0)))


QTimer.singleShot(500, lambda: _next(0))
QTimer.singleShot(8000, app.quit)  # 兜底退出
app.exec()
print("SNAP_DONE:", os.listdir(out_dir))
