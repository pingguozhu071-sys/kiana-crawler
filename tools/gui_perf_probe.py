# -*- coding: utf-8 -*-
"""GUI 卡顿探针（离屏）：模拟 最大化/切页/壁纸渲染 后测量事件循环最大停顿。

判定：事件循环单次停顿 > 200ms 即视为可感知卡顿（动画掉帧阈值）。
用法: python tools/gui_perf_probe.py [--real]

[v2.19.9] 加**默认隔离**：`_phase_wallpaper()` 里那句 `win._wp_update("wp_blur", 6)`
会一路走到 `launcher_v9.save_config()` → **写用户真实的 launcher_config.json**。
本脚本原先没有任何隔离（离屏只是"不弹窗"，不是"不写盘"）。
现在默认把数据根重定向到临时目录，跑完即删；要对真机跑请显式加 `--real`。
"""
import os
import sys
import time
import importlib.util

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ⚠️ 必须在 exec_module("launcher_v9") **之前**激活（见 _tools_isolation 的模块说明）
from _tools_isolation import activate  # noqa: E402

activate("gui_perf_probe")


def main():
    spec = importlib.util.spec_from_file_location("launcher_v9", "launcher_v9.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

    from PySide6.QtCore import QTimer, QElapsedTimer
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    win = m.KianaV9()
    win.resize(1280, 800)
    win.show()

    report = {}
    stall = {"max": 0.0, "where": "", "steady_max": 0.0, "steady_where": "", "t": QElapsedTimer()}
    stall["t"].start()
    last = {"v": stall["t"].elapsed()}
    cur_phase = {"name": "startup"}
    _WARMUP_PHASES = {"startup", "page_warmup"}

    def _watch():
        now = stall["t"].elapsed()
        dt = now - last["v"]
        last["v"] = now
        # 常规心跳 16ms；超出即记录停顿（首次进入事件循环前的等待不计）
        if dt > 200 and now > 500:
            if dt > stall["max"]:
                stall["max"] = dt
                stall["where"] = cur_phase["name"]
            if cur_phase["name"] not in _WARMUP_PHASES and dt > stall["steady_max"]:
                stall["steady_max"] = dt
                stall["steady_where"] = cur_phase["name"]

    watch = QTimer()
    watch.setInterval(0)
    watch.timeout.connect(_watch)
    watch.start()

    def _phase_maximize():
        cur_phase["name"] = "maximize"
        t0 = time.perf_counter()
        win.resize(1920, 1040)
        _pump(app, 1.2)  # 防抖 250ms + worker 线程处理
        report["maximize_resize_ms"] = round((time.perf_counter() - t0) * 1000)

    def _phase_pages():
        cur_phase["name"] = "switch_pages"
        pages = []
        for attr in ("home", "logp", "datap", "taskp", "settings"):
            p = getattr(win, attr, None)
            if p is not None:
                pages.append((attr, p))
        # 预热：首轮切页含懒构建+首绘（启动成本，不计入卡顿指标）；offscreen 软渲染
        # 慢，预热泵加长确保彻底完成
        cur_phase["name"] = "page_warmup"
        for attr, p in pages:
            try:
                win.switchTo(p)
            except Exception:
                pass
            _pump(app, 2.0)
        # 稳态往返两轮（模拟日常点按钮），取每页最好成绩
        cur_phase["name"] = "switch_pages"
        times = {}
        for _round in range(2):
            for attr, p in pages:
                t0 = time.perf_counter()
                try:
                    win.switchTo(p)
                except Exception:
                    times[attr] = -1
                    continue
                _pump(app, 0.4)
                dt = round((time.perf_counter() - t0) * 1000)
                if attr not in times or (0 <= dt < times[attr]):
                    times[attr] = dt
        report["switch_pages_ms"] = times

    def _phase_wallpaper():
        cur_phase["name"] = "wallpaper"
        # 壁纸渲染管线上限（offscreen 下 cv2 真跑）
        t0 = time.perf_counter()
        win._wp_update("wp_blur", 6)
        _pump(app, 2.5)
        report["wallpaper_rerender_ms"] = round((time.perf_counter() - t0) * 1000)

    def _finish():
        report["max_eventloop_stall_ms"] = round(stall["max"])
        report["stall_where"] = stall["where"]
        # 判定只看稳态：page_warmup/startup 是首启构建成本（用户已认可），不计卡顿
        report["steady_stall_ms"] = round(stall["steady_max"])
        report["steady_stall_where"] = stall["steady_where"]
        ok = stall["steady_max"] < 200
        print("PROBE_REPORT:", report)
        print("PROBE_VERDICT:", "PASS" if ok else "FAIL")
        app.quit()
        return 0 if ok else 2

    def _run():
        try:
            _phase_maximize()
            _phase_pages()
            _phase_wallpaper()
        except Exception as e:
            print("PROBE_FAIL:", type(e).__name__, e)
            app.quit()
            return 1
        QTimer.singleShot(200, _finish)

    QTimer.singleShot(1500, _run)  # 等首启环境自检/壁纸首渲染过去
    return app.exec()


def _pump(app, seconds):
    from PySide6.QtCore import QElapsedTimer
    t = QElapsedTimer()
    t.start()
    while t.elapsed() < int(seconds * 1000):
        app.processEvents()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
