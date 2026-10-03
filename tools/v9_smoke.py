"""launcher_v9 冒烟：移屏外 show + 四页切换截图 + 关键控件断言（不遮挡桌面）

[v2.19.9] 加**默认隔离**：`KianaV9()` 构造期就有写机主真实配置的路径（启动迁移钩子），
本脚本原先靠 `move(-2600,-1600)` 只躲开了**视线**、没躲开**数据**。
现在默认把数据根重定向到临时目录，跑完即删；要对真机跑请显式加 `--real`。
"""
from pathlib import Path
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 显式把 tools/ 也放进来：直接跑时 Python 会自动加脚本目录，但 `python -m tools.v9_smoke`
# 那种跑法不会 —— 显式写一行，两种跑法都能 import 到隔离器。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ⚠️ 必须在 import launcher_v9 **之前**激活（见 _tools_isolation 的模块说明）
from _tools_isolation import activate  # noqa: E402

activate("v9_smoke")

from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtCore import QTimer  # noqa: E402

app = QApplication(sys.argv)
import launcher_v9  # noqa: E402

win = launcher_v9.KianaV9()
# ── 底图集成冒烟（阶段 B）──
win.config["wp_mode"] = "single"
win.config["wp_path"] = str(Path(__file__).resolve().parent.parent / "tests" / "assets" / "sample_anime_2.jpg")
win.config["wp_blur"] = 6
win.config["wp_auto_dim"] = True
win.config["wp_accent_lock"] = False
win.move(-2600, -1600)
win.resize(1180, 760)
win.show()

out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tests", "assets", "gui_shots")
os.makedirs(out_dir, exist_ok=True)

pages = [(win.home, "v9_home.png"), (win.logp, "v9_log.png"),
         (win.datap, "v9_data.png"), (win.taskp, "v9_tasks.png"),
         (win.settings, "v9_settings.png")]
_i = [0]
_results = []


def _next():
    if _i[0] >= len(pages):
        _finish()
        return
    page, fname = pages[_i[0]]
    _i[0] += 1
    win.switchTo(page)
    QTimer.singleShot(350, lambda: (
        _results.append((fname, win.grab().save(os.path.join(out_dir, fname)))),
        _next()))


def _finish():
    # accent 取色是异步线程——轮询等待最多 10s
    if not win.config.get("accent_auto") and _finish.tries < 5:
        _finish.tries += 1
        QTimer.singleShot(2000, _finish)
        return
    ok = all(r[1] for r in _results)
    # 关键控件断言（坑4 防线：connect 目标存在性）
    checks = {
        "start_btn": win.home.start_btn is not None,
        "stat_labels": len(win._stat_labels) == 7,
        "pause_disabled": not win.home.pause_btn.isEnabled(),
        "cookie_sync": win.settings.cookie_edit_d.text() == win.home.cookie_edit.text(),
        # ── 底图断言（阶段 B）──
        "wallpaper_set": win._wp_pix is not None,
        "accent_extracted": bool(win.config.get("accent_auto")),
    }
    print("SHOTS:", [(f, s) for f, s in _results])
    print("CHECKS:", checks)
    print("SMOKE:", "PASS" if (ok and all(checks.values())) else "FAIL")
    try:
        with open(str(Path(__file__).resolve().parent.parent / "tests" / "assets" / "v9_smoke_result.txt"),
                  "w", encoding="utf-8") as f:
            f.write(f"{checks}\nSMOKE: {'PASS' if (ok and all(checks.values())) else 'FAIL'}\n")
    except Exception:
        pass
    app.quit()


_finish.tries = 0
QTimer.singleShot(600, _next)
QTimer.singleShot(15000, app.quit)
app.exec()
