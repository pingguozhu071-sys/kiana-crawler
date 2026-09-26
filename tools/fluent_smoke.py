"""FluentWindow 冒烟（第 0 步路线验证）：移屏外 show + grab，真实渲染不遮挡桌面"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout
from PySide6.QtCore import QTimer
from qfluentwidgets import FluentWindow, NavigationItemPosition, FluentIcon, BodyLabel

app = QApplication(sys.argv)

win = FluentWindow()
win.setWindowTitle("Kiana Fluent Smoke")
win.resize(1100, 720)

page = QWidget()
page.setObjectName("p-home")
lay = QVBoxLayout(page)
lay.addWidget(BodyLabel("页面一 · 冒烟测试"))
win.addSubInterface(page, FluentIcon.HOME, "首页")

page2 = QWidget()
page2.setObjectName("p-log")
win.addSubInterface(page2, FluentIcon.DOCUMENT, "日志")

# 移到屏幕外，真实渲染不遮挡桌面
win.move(-2600, -1600)
win.show()

def check():
    pm = win.grab()
    ok = pm.width() > 500 and win.windowHandle() is not None
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fluent_smoke.png")
    pm.save(out)
    print("SMOKE:", "PASS" if ok else "FAIL", "size:", pm.width(), "x", pm.height())
    print("SHOT:", out, os.path.getsize(out) if os.path.exists(out) else 0)
    app.quit()

QTimer.singleShot(1500, check)
QTimer.singleShot(8000, app.quit)
app.exec()
