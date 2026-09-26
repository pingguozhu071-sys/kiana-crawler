"""Kiana Vnext Plus — GUI v9（Fluent 壳，v2.12 主入口）

v2.12 起本文件为打包主入口（spec 指向本文件）；launcher_v8.py 为 legacy 旧壳仅保留对照。
并行新壳：qfluentwidgets FluentWindow + 四页平移 + EngineBridge 复用自 launcher_v8（零改动）。
施工红线（见 v2.12 计划坑表）：
  - 桥接层（EngineBridge/_LineStream/_QtLogHandler）直接 import 复用，逻辑零改动
  - 统计卡走显式注册（_stat_labels），废弃 findChildren 扫描（坑3：静默不更新）
  - cookie 双入口同步保留（首页 + 设置页）
  - 退出码语义 / URL 清洗 / 1 页爬验证 全部照搬 v8
"""
import sys
import os
import re
import json
import time
import subprocess
import threading
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(PROJECT_DIR))

from PySide6.QtCore import Qt, QTimer, Signal, QEvent, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QIcon, QColor, QPainter, QPixmap
from PySide6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
                               QPlainTextEdit, QLabel, QLineEdit, QSpinBox, QFileDialog,
                               QGridLayout, QFrame)

from kiana_vnext_plus import wallpaper, config as _kcfg
DATA_ROOT = _kcfg.data_root

# [v2.18.1] worker→主线程投递改用 QApplication.postEvent（文档明确的线程安全 API）。
# 原 SignalInstance.emit 跨 threading.Thread 在部分环境下偶发
# "TypeError: only accepts 0 argument(s)"——heisenbug 不纠缠，事件机制一劳永逸。
_WP_EVT = QEvent.registerEventType()


class _WpReadyEvent(QEvent):
    def __init__(self, kind: str, value):
        super().__init__(QEvent.Type(_WP_EVT))
        self.kind = kind    # "wp"=壁纸渲染完成(value=seq) / "accent"=取色完成(value=hex)
        self.value = value

from qfluentwidgets import (FluentWindow, FluentIcon as FIF, NavigationItemPosition,
                            PrimaryPushButton, PushButton, ToolButton, BodyLabel, TitleLabel,
                            SubtitleLabel, CardWidget, SwitchButton, ComboBox,
                            ProgressBar, InfoBar, InfoBarPosition, setTheme, setThemeColor,
                            Theme, Slider, ScrollArea)

from launcher_v8 import CONFIG_FILE, EngineBridge, load_captcha_keys, save_captcha_keys

APP_ICON = PROJECT_DIR / "assets" / "icon.ico"
IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp")


# ════════════════════════════════════════════════════════════
# 配置（与 v8 同一文件；wp_* 为 v2.12 外观系统预留键）
# ════════════════════════════════════════════════════════════
def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg: dict):
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


THEMES = {"深色": Theme.DARK, "亮色": Theme.LIGHT}
ACCENTS = {"蓝": "#4FA3E8", "紫": "#9B7EDE", "青": "#2FC6C6", "粉": "#F0708A"}


# ════════════════════════════════════════════════════════════
# 页面骨架：Fluent 内容页（透明背景，让 FluentWindow 底色透出）
# ════════════════════════════════════════════════════════════
class _Page(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName(self._object_name())
        # [v2.18.2 根因A] 页面内容自然高度可能超过视口（设置页 8 卡 ≈1550px vs
        # 1080p 视口 ≈950px），QVBoxLayout 空间不足时把卡片压到最小高度 →
        # 卡内输入框/按钮堆叠重叠。统一改为 ScrollArea 包裹：卡片保持自然高度，
        # 超出滚动查看；所有页面 build 代码零改动（self.lay 语义不变，挂在内容层上）。
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = ScrollArea()   # qfluentwidgets ScrollArea，内建 SmoothScrollDelegate 平滑滚动
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        # QScrollArea 自身画背景（含 viewport 区域）→ 透明；内容层用 objectName
        # 限定透明（无选择器 QSS 会波及卡内 QLineEdit 等原生控件背景）
        self.scroll.setStyleSheet(
            "QScrollArea{border: none; background: transparent}"
            "#pageContent{background: transparent}")
        content = QWidget()
        content.setObjectName("pageContent")
        content.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        # [v2.18.2] 实测 setWidget()/resize 后 autoFillBackground 会被内部路径
        # 置回 True 并以 palette window 色（浅灰）填充整页——setWidget 之后再关一次
        content.setAutoFillBackground(False)
        self.lay = QVBoxLayout(content)   # 现有 self.lay 用法全部不变
        self.lay.setContentsMargins(28, 28, 28, 20)
        self.lay.setSpacing(14)
        self.scroll.setWidget(content)
        # setWidget 可能把 autoFill 置回 True（离屏实测），以最终状态为准
        content.setAutoFillBackground(False)
        outer.addWidget(self.scroll)

    def _object_name(self) -> str:
        raise NotImplementedError


def _stat_card(title: str) -> tuple:
    """统计卡：CardWidget + 数字(StatNum 契约) + 标题。返回 (card, num_label)"""
    card = CardWidget()
    v = QVBoxLayout(card)
    v.setContentsMargins(18, 14, 18, 14)
    v.setSpacing(4)
    num = QLabel("0")
    num.setObjectName("StatNum")
    num.setStyleSheet("font-size: 26px; font-weight: 700;")
    t = BodyLabel(title)
    t.setStyleSheet("color: gray;")
    v.addWidget(num)
    v.addWidget(t)
    return card, num


# ════════════════════════════════════════════════════════════
# 首页：任务控制（v8 平移 + Fluent 控件）
# ════════════════════════════════════════════════════════════
class HomePage(_Page):
    def _object_name(self):
        return "p-home"

    def build(self, cfg: dict):
        # 标题
        self.lay.addWidget(TitleLabel("任务控制"))

        # URL 多行
        self.lay.addWidget(BodyLabel("目标网址（每行一个）"))
        self.url_edit = QPlainTextEdit()
        self.url_edit.setPlaceholderText("https://example.com\nhttps://example.org/page1")
        self.url_edit.setFixedHeight(64)
        self.lay.addWidget(self.url_edit)
        # [v2.17 1-4] 导入文件 / 剪贴板（url_edit 追加行，复用 _clean_urls 语义）
        row_imp = QHBoxLayout()
        imp_file = PushButton("导入文件")
        imp_file.clicked.connect(lambda: self.window()._import_url_file())
        imp_clip = PushButton("从剪贴板粘贴")
        imp_clip.clicked.connect(lambda: self.window()._import_url_clipboard())
        row_imp.addWidget(imp_file)
        row_imp.addWidget(imp_clip)
        row_imp.addStretch(1)
        self.lay.addLayout(row_imp)

        # 参数行
        row = QHBoxLayout()
        row.setSpacing(12)
        row.addWidget(BodyLabel("深度"))
        self.depth_spin = QSpinBox(); self.depth_spin.setRange(1, 50)
        self.depth_spin.setValue(int(cfg.get("depth", 5)))
        row.addWidget(self.depth_spin)
        row.addWidget(BodyLabel("页数"))
        self.pages_spin = QSpinBox(); self.pages_spin.setRange(1, 100000)
        self.pages_spin.setValue(int(cfg.get("pages", 500)))
        row.addWidget(self.pages_spin)
        row.addWidget(BodyLabel("输出目录"))
        self.out_edit = QLineEdit(str(cfg.get("outdir", "")))
        self.out_edit.setPlaceholderText("留空=默认 Downloads\\KianaVnextPlus")
        row.addWidget(self.out_edit, 1)
        b = PushButton("浏览"); b.clicked.connect(self._browse)
        row.addWidget(b)
        self.lay.addLayout(row)

        # Cookie 行（与设置页双向同步：信号由窗口层接线）
        crow = QHBoxLayout()
        crow.addWidget(BodyLabel("Cookies"))
        self.cookie_edit = QLineEdit(str(cfg.get("cookie_file", "")))
        self.cookie_edit.setPlaceholderText("留空=默认位置；多文件分号分隔，可同时爬多站")
        crow.addWidget(self.cookie_edit, 1)
        cp = PushButton("选择文件…"); cp.clicked.connect(self._pick_cookie)
        crow.addWidget(cp)
        cc = PushButton("清空"); cc.clicked.connect(lambda: self.cookie_edit.clear())
        crow.addWidget(cc)
        self.lay.addLayout(crow)

        # 过滤行
        frow = QHBoxLayout()
        frow.addWidget(BodyLabel("过滤白名单"))
        self.filter_edit = QLineEdit(str(cfg.get("filter_text", "")))
        self.filter_edit.setPlaceholderText("逗号分隔关键词，只爬含关键词的页面（留空=不过滤）")
        frow.addWidget(self.filter_edit, 1)
        self.lay.addLayout(frow)

        # 勾选组（Fluent SwitchButton）
        checks = QHBoxLayout()
        checks.setSpacing(18)
        self.sw_video = self._switch("下载视频", checks, bool(cfg.get("dl_video", True)))
        self.sw_image = self._switch("下载图片", checks, bool(cfg.get("dl_image", True)))
        self.sw_audio = self._switch("下载音频", checks, bool(cfg.get("dl_audio", False)))
        self.sw_sanitize = self._switch("内容脱敏", checks, bool(cfg.get("sanitize", True)))
        self.sw_robots = self._switch("协议合规", checks, bool(cfg.get("robots_respect", False)))
        checks.addStretch(1)
        self.lay.addLayout(checks)

        # 画质 + 视频副产物行（[v2.16.1] preferred_resolution/字幕/封面/详情 GUI 化——
        # 原只是配置文件死键，GUI 无入口）
        qrow = QHBoxLayout()
        qrow.setSpacing(18)
        qrow.addWidget(BodyLabel("画质"))
        self.res_combo = ComboBox()
        _res_items = ["自动(最高)", "4K", "2K", "1080P", "720P", "480P"]
        self.res_combo.addItems(_res_items)
        _res = str(cfg.get("resolution", "highest"))
        self.res_combo.setCurrentIndex({"highest": 0, "2160": 1, "1440": 2,
                                        "1080": 3, "720": 4, "480": 5}.get(_res, 0))
        qrow.addWidget(self.res_combo)
        self.sw_subs = self._switch("字幕", qrow, bool(cfg.get("subs", True)))
        self.sw_thumb = self._switch("封面", qrow, bool(cfg.get("thumb", True)))
        self.sw_info = self._switch("详情", qrow, bool(cfg.get("info", True)))
        qrow.addStretch(1)
        self.lay.addLayout(qrow)

        # 按钮组
        btns = QHBoxLayout()
        btns.setSpacing(10)
        self.start_btn = PrimaryPushButton(FIF.PLAY, "开始爬取")
        self.pause_btn = PushButton(FIF.PAUSE, "暂停")
        self.pause_btn.setEnabled(False)
        self.stop_btn = PushButton(FIF.CLOSE, "停止")
        self.stop_btn.setEnabled(False)
        self.retry_btn = PushButton(FIF.SYNC, "重试失败页")
        self.open_btn = PushButton(FIF.FOLDER, "打开输出目录")
        for w in (self.start_btn, self.pause_btn, self.stop_btn, self.retry_btn, self.open_btn):
            w.setFixedHeight(34)
            btns.addWidget(w)
        btns.addStretch(1)
        self.lay.addLayout(btns)

        # 状态 + 进度
        srow = QHBoxLayout()
        self.badge = BodyLabel("空闲")
        srow.addWidget(self.badge)
        self.progress = ProgressBar()
        self.progress.setRange(0, 100); self.progress.setValue(0)
        srow.addWidget(self.progress, 1)
        self.lay.addLayout(srow)

        # 隐私状态行（v2.11 语义保留）
        self.privacy_status = BodyLabel("隐私状态: 自检待运行…")
        self.lay.addWidget(self.privacy_status)

        # 统计卡（显式注册 StatNum——不走 findChildren，坑3 防线）
        stats = QHBoxLayout()
        self.card_done, self.lbl_done = _stat_card("已抓取")
        self.card_fail, self.lbl_fail = _stat_card("失败")
        self.card_speed, self.lbl_speed = _stat_card("页面/秒")
        for c in (self.card_done, self.card_fail, self.card_speed):
            stats.addWidget(c)
        self.lay.addLayout(stats)
        self.lay.addStretch(1)

        # v8 兼容别名（迁移期逻辑零改动：勾选组读取方式不变）
        self.chk_video, self.chk_image = self.sw_video, self.sw_image
        self.chk_audio, self.chk_sanitize = self.sw_audio, self.sw_sanitize

    def _switch(self, text, lay, checked):
        w = QWidget()
        h = QHBoxLayout(w); h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(BodyLabel(text))
        sw = SwitchButton()
        sw.setChecked(checked)
        h.addWidget(sw)
        lay.addWidget(w)
        return sw

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if d:
            self.out_edit.setText(d)

    def _pick_cookie(self):
        f, _ = QFileDialog.getOpenFileName(self, "选择 cookies.txt", "", "All Files (*)")
        if f:
            self.cookie_edit.setText(f)

    def refresh_from_config(self, cfg: dict):
        """设置页改动后回写首页 cookie 行（双入口同步）"""
        self.cookie_edit.setText(str(cfg.get("cookie_file", "")))


# ════════════════════════════════════════════════════════════
# 日志页
# ════════════════════════════════════════════════════════════
class LogPage(_Page):
    def _object_name(self):
        return "p-log"

    def build(self):
        self.lay.addWidget(TitleLabel("运行日志"))
        tool = QHBoxLayout()
        self.filter_combo = ComboBox()
        self.filter_combo.addItems(["全部", "INFO", "成功", "警告", "错误"])
        tool.addWidget(self.filter_combo)
        tool.addStretch(1)
        clear_btn = PushButton(FIF.DELETE, "清空")
        clear_btn.clicked.connect(lambda: self.log_view.clear())
        tool.addWidget(clear_btn)
        self.lay.addLayout(tool)

        card = CardWidget()
        v = QVBoxLayout(card); v.setContentsMargins(4, 4, 4, 4)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setStyleSheet("background: transparent; border: none;")
        v.addWidget(self.log_view)
        self.lay.addWidget(card, 1)
        self._log_filter = {"info": True, "ok": True, "warn": True, "err": True}
        self.filter_combo.currentIndexChanged.connect(self._apply_filter)
        self._buffer = []

    def _apply_filter(self, idx):
        self._log_filter = {"info": idx in (0, 1), "ok": idx in (0, 2),
                            "warn": idx in (0, 3), "err": idx in (0, 4)}
        self.log_view.clear()
        for t, lv in self._buffer:
            if self._log_filter.get(lv, True):
                self._append(t, lv)

    def append(self, text, level="info"):
        self._buffer.append((text, level))
        if len(self._buffer) > 5000:
            self._buffer = self._buffer[-5000:]
        if self._log_filter.get(level, True):
            self._append(text, level)

    def _append(self, text, level):
        from PySide6.QtGui import QTextCharFormat, QTextCursor
        cur = self.log_view.textCursor()
        cur.movePosition(QTextCursor.End)
        fmt = QTextCharFormat()
        colors = {"ok": "#5FBF77", "warn": "#E2B93D", "err": "#E5534B",
                  "title": "#4FA3E8", "info": "#B0BEC5"}
        fmt.setForeground(QColor(colors.get(level, "#B0BEC5")))
        cur.insertText(text + "\n", fmt)
        self.log_view.setTextCursor(cur)
        self.log_view.ensureCursorVisible()


# ════════════════════════════════════════════════════════════
# 数据页（4 统计卡激活）
# ════════════════════════════════════════════════════════════
class DataPage(_Page):
    def _object_name(self):
        return "p-data"

    def build(self):
        self.lay.addWidget(TitleLabel("数据统计"))
        grid = QHBoxLayout()
        self.card_d_done, self.lbl_d_done = _stat_card("已抓取页面")
        self.card_d_fail, self.lbl_d_fail = _stat_card("失败页面")
        self.card_d_pend, self.lbl_d_pend = _stat_card("待处理")
        self.card_d_total, self.lbl_d_total = _stat_card("总计")
        for c in (self.card_d_done, self.card_d_fail, self.card_d_pend, self.card_d_total):
            grid.addWidget(c)
        self.lay.addLayout(grid)
        self.lay.addStretch(1)


# ════════════════════════════════════════════════════════════
# 任务页（历史任务：刷新/打开/删除）
# ════════════════════════════════════════════════════════════
class TasksPage(_Page):
    def _object_name(self):
        return "p-tasks"

    def build(self):
        self.lay.addWidget(TitleLabel("历史任务"))
        self.lay.addWidget(BodyLabel("保留 7 天 / 50GB 自动清理 · 点击行选中后操作"))
        tool = QHBoxLayout()
        refresh_btn = PushButton(FIF.SYNC, "刷新")
        refresh_btn.clicked.connect(lambda: self.window()._refresh_tasks())
        tool.addWidget(refresh_btn)
        tool.addStretch(1)
        self.lay.addLayout(tool)

        card = CardWidget()
        v = QVBoxLayout(card); v.setContentsMargins(4, 4, 4, 4)
        self.tasks_list = QPlainTextEdit()
        self.tasks_list.setReadOnly(True)
        self.tasks_list.setStyleSheet("background: transparent; border: none;")
        v.addWidget(self.tasks_list)
        self.lay.addWidget(card, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        open_btn = PrimaryPushButton(FIF.FOLDER, "打开选中任务产物")
        open_btn.clicked.connect(lambda: self.window()._open_selected_task())
        row.addWidget(open_btn)
        del_btn = PushButton(FIF.DELETE, "删除选中任务")
        del_btn.clicked.connect(lambda: self.window()._delete_selected_task())
        row.addWidget(del_btn)
        self.lay.addLayout(row)


# ════════════════════════════════════════════════════════════
# 设置页（主题/强调色/cookie 同步；外观面板阶段 C 扩展）
# ════════════════════════════════════════════════════════════
class SettingsPage(_Page):
    def _object_name(self):
        return "p-settings"

    def build(self, win):
        self.lay.addWidget(TitleLabel("设置"))
        _sub = BodyLabel("所有设置即存即生效 · 玻璃透明度即时预览")
        _sub.setStyleSheet("font-size: 12px; color: gray;")
        self.lay.addWidget(_sub)

        card = CardWidget()
        v = QVBoxLayout(card); v.setContentsMargins(20, 16, 20, 16); v.setSpacing(12)
        row1 = QHBoxLayout()
        row1.addWidget(BodyLabel("主题"))
        self.theme_combo = ComboBox()
        self.theme_combo.addItems(list(THEMES.keys()))
        self.theme_combo.setCurrentText("深色" if win.theme != "light" else "亮色")
        self.theme_combo.currentTextChanged.connect(win._set_theme)
        row1.addWidget(self.theme_combo); row1.addStretch(1)
        v.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(BodyLabel("强调色"))
        self.accent_combo = ComboBox()
        self.accent_combo.addItems(list(ACCENTS.keys()))
        self.accent_combo.setCurrentText(win.accent_name)
        self.accent_combo.currentTextChanged.connect(win._set_accent)
        row2.addWidget(self.accent_combo); row2.addStretch(1)
        v.addLayout(row2)
        self.lay.addWidget(card)

        card2 = CardWidget()
        v2 = QVBoxLayout(card2); v2.setContentsMargins(20, 16, 20, 16); v2.setSpacing(12)
        v2.addWidget(SubtitleLabel("Cookies（与首页同步）"))
        self.cookie_edit_d = QLineEdit(str(win.config.get("cookie_file", "")))
        self.cookie_edit_d.setPlaceholderText("留空=默认位置；多文件分号分隔，可同时爬多站")
        v2.addWidget(self.cookie_edit_d)
        row3 = QHBoxLayout()
        cp = PushButton("选择文件…"); cp.clicked.connect(self._pick_cookie)
        cc = PushButton("清空"); cc.clicked.connect(lambda: self.cookie_edit_d.clear())
        row3.addWidget(cp); row3.addWidget(cc); row3.addStretch(1)
        v2.addLayout(row3)
        self.lay.addWidget(card2)

        # ── 打码 API 密钥卡（v2.16 M3 自主集成：三平台 GUI 入口；v2.19.7 起 DPAPI 密文）──
        card4 = CardWidget()
        v4 = QVBoxLayout(card4); v4.setContentsMargins(20, 16, 20, 16); v4.setSpacing(12)
        v4.addWidget(SubtitleLabel("打码 API 密钥（验证码自动识别，选填）"))
        # [v2.19.7 安全·扫描发现] 原为 `win.config.get("captcha_api_keys")`（明文 JSON）；
        # 现走 DPAPI 密文文件，读到旧明文会自动迁移加密并抹掉配置里的明文副本。
        _existing_keys = load_captcha_keys(win.config)
        self.cap_twocaptcha = QLineEdit(str(_existing_keys.get("twocaptcha", "")))
        self.cap_twocaptcha.setPlaceholderText("2Captcha Key（选填）")
        self.cap_twocaptcha.setEchoMode(QLineEdit.EchoMode.Password)
        self.cap_capsolver = QLineEdit(str(_existing_keys.get("capsolver", "")))
        self.cap_capsolver.setPlaceholderText("CapSolver Key（选填）")
        self.cap_capsolver.setEchoMode(QLineEdit.EchoMode.Password)
        self.cap_anticaptcha = QLineEdit(str(_existing_keys.get("anticaptcha", "")))
        self.cap_anticaptcha.setPlaceholderText("AntiCaptcha Key（选填）")
        self.cap_anticaptcha.setEchoMode(QLineEdit.EchoMode.Password)
        v4.addWidget(self.cap_twocaptcha)
        v4.addWidget(self.cap_capsolver)
        v4.addWidget(self.cap_anticaptcha)
        row4 = QHBoxLayout()
        cap_save = PushButton("保存密钥")
        cap_save.clicked.connect(lambda: win._save_captcha_keys(
            self.cap_twocaptcha.text().strip(), self.cap_capsolver.text().strip(),
            self.cap_anticaptcha.text().strip()))
        cap_show = PushButton("显示一次")
        cap_show.clicked.connect(self._toggle_captcha_echo)
        row4.addWidget(cap_save); row4.addWidget(cap_show); row4.addStretch(1)
        v4.addLayout(row4)
        self.lay.addWidget(card4)

        # ── LLM 智能增强卡（v2.16.1 阶段5：默认关/API Key 本机保存；任务完成后对采集
        # 数据做摘要/分类——不改 UI 美术，仅复用打码卡同款控件模板）──
        card4b = CardWidget()
        v4b = QVBoxLayout(card4b); v4b.setContentsMargins(20, 16, 20, 16); v4b.setSpacing(12)
        v4b.addWidget(SubtitleLabel("LLM 智能增强（选填，默认关闭）"))
        self.sw_llm = SwitchButton("启用（任务完成后对采集数据做摘要/分类）")
        self.sw_llm.setChecked(bool(win.config.get("llm_enabled", False)))
        v4b.addWidget(self.sw_llm)
        self.llm_base = QLineEdit(str(win.config.get("llm_api_base", "https://api.deepseek.com/v1")))
        self.llm_base.setPlaceholderText("API 地址（OpenAI 兼容，如 https://api.deepseek.com/v1）")
        v4b.addWidget(self.llm_base)
        self.llm_model = QLineEdit(str(win.config.get("llm_model", "deepseek-v4-flash")))
        self.llm_model.setPlaceholderText("模型名（deepseek-v4-flash / gpt-4o-mini 等）")
        v4b.addWidget(self.llm_model)
        self.llm_key = QLineEdit(str(win.config.get("llm_key", "")))
        self.llm_key.setPlaceholderText("API Key（仅本机保存）")
        self.llm_key.setEchoMode(QLineEdit.EchoMode.Password)
        v4b.addWidget(self.llm_key)
        # [v2.17 2.8] 月度预算 GUI 化（原仅 config 键 llm_budget_month，无入口）
        rowb = QHBoxLayout()
        rowb.addWidget(BodyLabel("月度预算"))
        self.llm_budget = QSpinBox()
        self.llm_budget.setRange(1, 100000)
        self.llm_budget.setValue(int(win.config.get("llm_budget_month", 500)))
        rowb.addWidget(self.llm_budget)
        rowb.addStretch(1)
        v4b.addLayout(rowb)
        row4b = QHBoxLayout()
        llm_save = PushButton("保存 LLM 设置")
        llm_save.clicked.connect(lambda: win._save_llm_settings(
            self.sw_llm.isChecked(), self.llm_key.text().strip(),
            self.llm_base.text().strip(), self.llm_model.text().strip(),
            self.llm_budget.value()))
        row4b.addWidget(llm_save); row4b.addStretch(1)
        v4b.addLayout(row4b)
        self.lay.addWidget(card4b)

        # ── 身份捆绑轮换卡（v2.17 E-P2：默认关；出口代理+cookie 打包虚拟用户，封锁整包退役
        # ——复用 LLM 卡同款控件模板，不涉及美术）──
        card4c = CardWidget()
        v4c = QVBoxLayout(card4c)
        v4c.setContentsMargins(20, 16, 20, 16)
        v4c.setSpacing(12)
        v4c.addWidget(SubtitleLabel("身份捆绑轮换（实验，默认关闭）"))
        self.sw_ident = SwitchButton(
            "启用（出口代理与 cookies 打包为虚拟用户，被封整包退役换新）")
        self.sw_ident.setChecked(bool(win.config.get("identity_bundle", False)))
        v4c.addWidget(self.sw_ident)
        v4c.addWidget(BodyLabel(
            "开启后需在 cookies 文件/代理列表配置多组出口；"
            "cookie 组与代理同源（KIANA_COOKIE_FILES / KIANA_PROXY_LIST）"))
        row4c = QHBoxLayout()
        ident_save = PushButton("保存身份捆绑设置")
        ident_save.clicked.connect(lambda: win._save_ident_settings(self.sw_ident.isChecked()))
        row4c.addWidget(ident_save); row4c.addStretch(1)
        v4c.addLayout(row4c)
        self.lay.addWidget(card4c)

        # ── 代理状态卡（v2.16 M3 自主集成：节点/健康分/失败直方图，脱敏展示）──
        card5 = CardWidget()
        v5 = QVBoxLayout(card5); v5.setContentsMargins(20, 16, 20, 16); v5.setSpacing(8)
        v5.addWidget(SubtitleLabel("出口代理状态"))
        self.proxy_status = BodyLabel("（未启用代理时显示直连状态）")
        v5.addWidget(self.proxy_status)
        row5 = QHBoxLayout()
        proxy_refresh = PushButton("刷新")
        proxy_refresh.clicked.connect(win._refresh_proxy_status)
        priv_scan = PushButton("隐私扫描")
        priv_scan.clicked.connect(win._run_priv_scan)
        row5.addWidget(proxy_refresh); row5.addWidget(priv_scan); row5.addStretch(1)
        v5.addLayout(row5)
        self.lay.addWidget(card5)

        # ── 外观卡（v2.12 阶段 C：二次元底图 + 毛玻璃）──
        card3 = CardWidget()
        v3 = QVBoxLayout(card3); v3.setContentsMargins(20, 16, 20, 16); v3.setSpacing(10)
        v3.addWidget(SubtitleLabel("外观 · 底图"))

        rowm = QHBoxLayout()
        rowm.addWidget(BodyLabel("底图模式"))
        self.wp_mode = ComboBox()
        self.wp_mode.addItems(["关", "单张", "文件夹"])
        _mode_map = {"off": "关", "single": "单张", "folder": "文件夹"}
        self.wp_mode.setCurrentText(_mode_map.get(str(win.config.get("wp_mode", "off")), "关"))
        self.wp_mode.currentTextChanged.connect(
            lambda t: win._wp_update("wp_mode", {"关": "off", "单张": "single", "文件夹": "folder"}[t]))
        rowm.addWidget(self.wp_mode); rowm.addStretch(1)
        self.wp_change_btn = PushButton(FIF.SYNC, "换一张")
        self.wp_change_btn.clicked.connect(win._wp_render)
        rowm.addWidget(self.wp_change_btn)
        self.wp_reset_btn = PushButton("恢复纯色")
        self.wp_reset_btn.clicked.connect(win._wp_set_mode_off)
        rowm.addWidget(self.wp_reset_btn)
        v3.addLayout(rowm)

        rowp = QHBoxLayout()
        self.wp_pick_img = PushButton("选择图片…")
        self.wp_pick_img.clicked.connect(win._wp_pick_image)
        self.wp_pick_dir = PushButton("选择文件夹…")
        self.wp_pick_dir.clicked.connect(win._wp_pick_folder)
        rowp.addWidget(self.wp_pick_img); rowp.addWidget(self.wp_pick_dir); rowp.addStretch(1)
        v3.addLayout(rowp)

        # [v2.18.2 作者新要求] 面板玻璃透明度：滑杆 40-95 + 预设组，即调即生效。
        # 独立 QTimer 防抖（不用 _wp_debounce——它属于底图系统，会连带重渲染整张底图）
        rowg = QHBoxLayout()
        rowg.addWidget(BodyLabel("面板玻璃"))
        self.panel_alpha_slider = Slider(Qt.Horizontal)
        self.panel_alpha_slider.setRange(40, 95)
        self.panel_alpha_slider.setValue(int(win.config.get("panel_alpha", 65)))
        self.panel_alpha_val = BodyLabel(f"{self.panel_alpha_slider.value()}%")
        self.panel_alpha_slider.valueChanged.connect(
            lambda v: (self.panel_alpha_val.setText(f"{v}%"),
                       win._panel_alpha_changed(v, self.panel_alpha_slider)))
        rowg.addWidget(self.panel_alpha_slider, 1); rowg.addWidget(self.panel_alpha_val)
        v3.addLayout(rowg)

        rowpreset = QHBoxLayout()
        rowpreset.addWidget(BodyLabel("预设"))
        for name, val in (("通透 40", 40), ("标准 65", 65), ("柔和 78", 78), ("实心 95", 95)):
            b = PushButton(name)
            b.clicked.connect(lambda _=False, v=val: self._set_panel_alpha(v, win))
            rowpreset.addWidget(b)
        rowpreset.addStretch(1)
        v3.addLayout(rowpreset)

        # 模糊滑杆
        rowb = QHBoxLayout()
        rowb.addWidget(BodyLabel("毛玻璃"))
        self.wp_blur_slider = Slider(Qt.Horizontal)
        self.wp_blur_slider.setRange(0, 30)
        self.wp_blur_slider.setValue(int(win.config.get("wp_blur", 0)))
        self.wp_blur_val = BodyLabel(str(self.wp_blur_slider.value()))
        self.wp_blur_slider.valueChanged.connect(
            lambda v: (self.wp_blur_val.setText(str(v)), win._wp_update("wp_blur", v)))
        rowb.addWidget(self.wp_blur_slider, 1); rowb.addWidget(self.wp_blur_val)
        v3.addLayout(rowb)

        # 暗化滑杆
        rowd = QHBoxLayout()
        rowd.addWidget(BodyLabel("暗化"))
        self.wp_dim_slider = Slider(Qt.Horizontal)
        self.wp_dim_slider.setRange(0, 60)
        self.wp_dim_slider.setValue(int(win.config.get("wp_dim", 0)))
        self.wp_dim_val = BodyLabel(str(self.wp_dim_slider.value()))
        self.wp_dim_slider.valueChanged.connect(
            lambda v: (self.wp_dim_val.setText(str(v)), win._wp_update("wp_dim", v)))
        rowd.addWidget(self.wp_dim_slider, 1); rowd.addWidget(self.wp_dim_val)
        v3.addLayout(rowd)

        # 九宫格焦点 + 开关组
        rowf = QHBoxLayout()
        rowf.addWidget(BodyLabel("焦点"))
        self.focus_btns = []
        fgrid = QGridLayout()
        fgrid.setSpacing(2)
        self._focus = int(win.config.get("wp_focus", 4))
        for i in range(9):
            b = ToolButton()
            b.setFixedSize(26, 26)
            b.setText("■" if i == self._focus else "·")
            b.clicked.connect(lambda _=False, idx=i: self._set_focus(idx, win))
            fgrid.addWidget(b, i // 3, i % 3)
            self.focus_btns.append(b)
        fwrap = QWidget(); fwrap.setLayout(fgrid)
        rowf.addWidget(fwrap)
        rowf.addSpacing(24)
        self.sw_autodim = SwitchButton("智能蒙层")
        self.sw_autodim.setChecked(bool(win.config.get("wp_auto_dim", True)))
        self.sw_autodim.checkedChanged.connect(lambda c: win._wp_update("wp_auto_dim", c))
        rowf.addWidget(self.sw_autodim)
        self.sw_accent = SwitchButton("取色联动")
        self.sw_accent.setChecked(not bool(win.config.get("wp_accent_lock", False)))
        self.sw_accent.checkedChanged.connect(lambda c: win._wp_set_accent_lock(not c))
        rowf.addWidget(self.sw_accent)
        rowf.addStretch(1)
        v3.addLayout(rowf)
        # [v2.18.2] 外观卡分两组：底图区（上）与签名区（下），中间加分隔线
        _sep = QFrame()
        _sep.setFrameShape(QFrame.Shape.HLine)
        _sep.setStyleSheet("color: rgba(255,255,255,18);")
        _sep.setFixedHeight(1)
        v3.addWidget(_sep)
        # [v2.19.9] 电子签名开关（默认**关**；颜色随强调色联动）
        # 默认关闭的理由：签名文本是个人标识，公开仓库不应预置任何人的社交 handle。
        self.sw_signature = SwitchButton("电子签名（右下角）")
        self.sw_signature.setChecked(bool(win.config.get("signature_enabled", False)))
        self.sw_signature.checkedChanged.connect(
            lambda c: win._wp_update("signature_enabled", bool(c)))
        v3.addWidget(self.sw_signature)
        self.lay.addWidget(card3)
        self.lay.addStretch(1)

    def _set_focus(self, idx, win):
        self._focus = idx
        for i, b in enumerate(self.focus_btns):
            b.setText("■" if i == idx else "·")
        win._wp_update("wp_focus", idx)

    def _set_panel_alpha(self, value, win):
        """预设按钮：同步滑杆位置 + 配置 + 即时重刷（滑杆 valueChanged 链路复用）"""
        self.panel_alpha_slider.setValue(value)

    def _pick_cookie(self):
        f, _ = QFileDialog.getOpenFileName(self, "选择 cookies.txt", "", "All Files (*)")
        if f:
            self.cookie_edit_d.setText(f)

    def _toggle_captcha_echo(self):
        """明文显示一次（点击后再隐藏）"""
        p = QLineEdit.EchoMode.Normal if self.cap_twocaptcha.echoMode() == QLineEdit.EchoMode.Password else QLineEdit.EchoMode.Password
        for e in (self.cap_twocaptcha, self.cap_capsolver, self.cap_anticaptcha):
            e.setEchoMode(p)


# ════════════════════════════════════════════════════════════
# 主窗口
# ════════════════════════════════════════════════════════════
class KianaV9(FluentWindow):
    # 底图渲染结果经 _WpReadyEvent 投递（postEvent 线程安全，v2.18.1 弃跨线程 Signal）

    def __init__(self):
        super().__init__()
        self.config = load_config()
        # [v2.19.7 安全·扫描发现] 老版本把打码密钥明文存在 launcher_config.json：启动时
        # 立即加密迁移（读到明文 → 写 DPAPI 密文 → 从配置抹掉明文并落盘），不等下次保存。
        try:
            _before = bool(self.config.get("captcha_api_keys"))
            load_captcha_keys(self.config)
            if _before and "captcha_api_keys" not in self.config:
                save_config(self.config)
        except Exception:
            pass
        self.theme = self.config.get("theme", "dark")
        self.accent_name = self.config.get("accent", "蓝")
        # ── 底图状态（阶段 B） ──
        self._wp_pix = None       # 当前底图 QPixmap
        self._wp_old = None       # 交叉溶解旧图
        self._wp_fade = 1.0       # 溶解进度 0→1
        self._wp_fade_anim = None
        self._wp_busy = False
        self._wp_pending = False  # [v2.18.1] 忙时挂起的渲染请求
        self._last_accent_hex = None  # [v2.18.1] 取色去重缓存（避免 setThemeColor 风暴）
        self._wp_debounce = QTimer(self)
        self._wp_debounce.setSingleShot(True)
        self._wp_debounce.setInterval(250)
        self._wp_debounce.timeout.connect(self._wp_render)
        self._wp_result = {}  # worker 结果本体（事件只带 kind/seq）
        self.setWindowTitle("Kiana Vnext Plus")
        # [v2.18.1 性能] 声明整窗不透明：paintEvent 恒画满全窗（壁纸或主题底色），
        # Qt 免去每帧底层填充+透明合成路径（Fluent 半透明卡片重绘开销显著下降）
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        if APP_ICON.exists():
            self.setWindowIcon(QIcon(str(APP_ICON)))
        self.resize(1180, 760)
        # [v2.18.2] 最小尺寸兜底：FluentWindow 内容区 layout 的 sizeHint 会反超窗口
        # （离屏实测被撑到 1619px 高），小屏/低宽高比屏幕会顶出系统边界
        self.setMinimumSize(980, 620)

        # [v2.17.1] 电子签名：主窗口右下角水印（颜色随强调色/智能取色联动；设置页可关）
        # [v2.19.9] 文本改为从配置读取，默认**留空**——公开仓库不预置任何人的个人标识。
        # 想用自己的落款：在 launcher_config.json 里设 "signature_text"（或用设置页）。
        from PySide6.QtWidgets import QLabel as _QLabel
        from PySide6.QtGui import QFontDatabase as _QFD, QColor as _QColor
        self._sig_label = _QLabel(self)
        self._sig_label.setText(str(win.config.get("signature_text", "")))
        self._sig_label.setStyleSheet("background: transparent;")
        self._sig_font_family = None
        try:
            for fp in (Path(__file__).parent / "assets" / "fonts" / "MochiyPopOne-Regular.ttf",
                       Path(__file__).parent / "fonts" / "MochiyPopOne-Regular.ttf",
                       Path(__file__).parent / "assets" / "fonts" / "YuseiMagic-Regular.ttf"):
                if fp.exists():
                    fid = _QFD.addApplicationFont(str(fp))
                    if fid >= 0:
                        fams = _QFD.applicationFontFamilies(fid)
                        if fams:
                            self._sig_font_family = fams[0]
                            break
        except Exception:
            pass
        self._sig_apply_color(ACCENTS.get(self.accent_name, ACCENTS["蓝"]))
        # 页面栈会盖住签名——Show/Resize/激活时延时重 raise（覆盖页面切换/全屏抖动）
        self.installEventFilter(self)

        # 引擎桥（复用 v8——逻辑零改动）
        # [v2.18.1 修复·总根因] 此段原被缩进事故吞进 _sig_place——Show/Resize/
        # WindowActivate 每次都完整重建 EngineBridge+五个页面+重新 addSubInterface+
        # 全应用主题（1910 个控件 setStyleSheet ≈1.2s/次），即"最大化卡半天/点设置卡/
        # 左上角动效全没/越用越卡"的总根因。复位回 __init__，整个生命周期只跑一次。
        self.engine = EngineBridge(self)
        self.engine.log_line.connect(self._on_log)
        self.engine.finished.connect(self._on_finished)
        self.engine.started.connect(self._on_started)

        # 页面
        cfg = self.config
        self.home = HomePage(); self.home.build(cfg)
        self.logp = LogPage(); self.logp.build()
        self.datap = DataPage(); self.datap.build()
        self.taskp = TasksPage(); self.taskp.build()
        self.settings = SettingsPage(); self.settings.build(self)

        self.addSubInterface(self.home, FIF.HOME, "首页")
        self.addSubInterface(self.logp, FIF.DOCUMENT, "日志")
        self.addSubInterface(self.datap, FIF.FOLDER, "数据")
        self.addSubInterface(self.taskp, FIF.LIBRARY, "任务")
        self.addSubInterface(self.settings, FIF.SETTING, "设置", NavigationItemPosition.BOTTOM)

        self._apply_qfluent_theme()
        self._wire()

        # 统计卡显式注册（坑3 防线：废弃 findChildren 扫描）
        self._stat_labels = {
            "done": self.home.lbl_done, "fail": self.home.lbl_fail, "speed": self.home.lbl_speed,
            "d_done": self.datap.lbl_d_done, "d_fail": self.datap.lbl_d_fail,
            "d_pend": self.datap.lbl_d_pend, "d_total": self.datap.lbl_d_total,
        }

        self._pending_outdir_toast = False
        # 底图启动加载（文件夹+启动随机 / 单张）
        QTimer.singleShot(0, self._wp_render)
        # [v2.16] 首启环境自检：浏览器/YT 组件/打码密钥/代理 四项健康 → toast + 环境状态卡
        QTimer.singleShot(1200, self._env_selfcheck)

    def eventFilter(self, obj, event):
        etype = getattr(event, "type", lambda: None)()
        try:
            from PySide6.QtCore import QEvent as _QE
            if etype in (_QE.Type.Show, _QE.Type.Resize, _QE.Type.WindowActivate):
                from PySide6.QtCore import QTimer as _QT
                _QT.singleShot(0, self._sig_place)
        except Exception:
            pass
        return super().eventFilter(obj, event)

    def _sig_apply_color(self, hexcolor):
        """签名颜色=强调色同源；字体=艺术字体（默认 Mochiy Pop One，
        换字体改 assets/fonts 文件名首位即可）"""
        if getattr(self, "_sig_label", None) is None:
            return
        fam = self._sig_font_family or (self._sig_font_family or "Microsoft YaHei")
        try:
            from PySide6.QtGui import QFont as _QF
            f = _QF(fam, 15)
            f.setBold(False)
            self._sig_label.setFont(f)
        except Exception:
            pass
        self._sig_label.setStyleSheet(
            f"color: {hexcolor}; background: transparent;")
        self._sig_place()

    def _sig_place(self):
        if getattr(self, "_sig_label", None) is None:
            return
        if not bool(self.config.get("signature_enabled", False)):
            self._sig_label.hide()
            return
        self._sig_label.adjustSize()
        self._sig_label.show()
        self._sig_label.move(self.width() - self._sig_label.width() - 16,
                             self.height() - self._sig_label.height() - 10)
        self._sig_label.raise_()

    def _env_selfcheck(self):
        """首启自检（v2.16 可移植性）：检测浏览器渲染/YT 组件/打码密钥/代理是否就绪，
        各自现状写入 self._env_status 供环境卡展示，缺失项 toast 提示。"""
        self._env_status = {}
        try:
            # 1) 浏览器渲染（patchright chromium）
            import os as _os
            import pathlib as _pl
            browser_ok = False
            for cand in (getattr(self.cfg, 'browsers_path', ""),
                         _os.environ.get("PATCHRIGHT_BROWSERS_PATH", ""),
                         _os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""),
                         str(_pl.home() / "AppData" / "Local" / "ms-playwright")):
                if cand and _pl.Path(cand).is_dir() and any(_pl.Path(cand).glob("chromium*")):
                    browser_ok = True
                    break
            self._env_status["browser"] = ("✅" if browser_ok else "⚠️") + " 浏览器渲染"
            # 2) YT 组件（本地 PO Token server 或 bgutil 插件）
            pot_ok = False
            try:
                import socket
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(0.3)
                s.connect(("127.0.0.1", 4416))
                s.close()
                pot_ok = True
            except Exception:
                pass
            self._env_status["yt"] = ("✅" if pot_ok else "○") + " YT 组件"
            # 3) 打码 API 密钥（任一配置非空）—— [v2.19.7] 读 DPAPI 密文（旧明文会自动迁移）
            keys = load_captcha_keys(self.config)
            kfa = any((keys or {}).get(k) for k in ("twocaptcha", "capsolver", "anticaptcha"))
            self._env_status["captcha"] = ("✅" if kfa else "○") + " 打码密钥"
            # 4) 代理（KIANA_PROXY_LIST 或默认出口）
            proxy = bool(_os.environ.get("KIANA_PROXY_LIST", ""))
            self._env_status["proxy"] = ("✅" if proxy else "─") + " 代理直连"
            # 环境状态卡内容
            st = self._env_status.values() if hasattr(self, "_env_status") and self._env_status else []
            if st:
                self.home.privacy_status.setText("环境: " + " · ".join(st))
            # 缺失项 toast
            if not browser_ok:
                self._toast("浏览器渲染未就绪（验证码/贴吧渲染需 patchright install chromium）", "warning")
            if not pot_ok:
                self._toast("YT 组件未启用（需要 PO Token server，可用 YT 一键脚本拉起）", "info")
        except Exception:
            pass

    # ════ 底图系统（阶段 B）════
    def paintEvent(self, e):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        pix = getattr(self, "_wp_pix", None)
        if pix is not None:
            # [v2.18.1 性能] 尺寸不匹配时绝不做整幅 CPU 拉伸（raster 后端每帧重采样
            # 数十 ms → Fluent 半透明卡片每帧连带父窗重绘 = 动画/最大化卡死主因）。
            # 改按 cover 语义从旧图源裁切 blit（纯 memcpy 级），防抖 250ms 后由
            # worker 交出精确重裁图。
            pr = self.rect()
            if pix.width() == pr.width() and pix.height() == pr.height():
                painter.drawPixmap(pr, pix)
            else:
                win_ar = pr.width() / max(pr.height(), 1)
                src_ar = pix.width() / max(pix.height(), 1)
                if src_ar > win_ar:   # 源更宽 → 裁宽
                    ch = pix.height()
                    cw = int(ch * win_ar)
                    cx = (pix.width() - cw) // 2
                    cy = 0
                else:                 # 源更高 → 裁高
                    cw = pix.width()
                    ch = int(cw / win_ar)
                    cx = 0
                    cy = (pix.height() - ch) // 2
                painter.drawPixmap(pr, pix, pix.rect().adjusted(cx, cy,
                                   cx - (pix.width() - cw), cy - (pix.height() - ch)))
            old = getattr(self, "_wp_old", None)
            fade = getattr(self, "_wp_fade", 1.0)
            if old is not None and fade < 1:
                painter.setOpacity(1 - fade)
                painter.drawPixmap(pr, old)
                painter.setOpacity(1)
        else:
            super().paintEvent(e)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        cfg = getattr(self, "config", None)
        if cfg is not None and cfg.get("wp_mode", "off") != "off":
            self._wp_debounce.start()  # 防抖重渲染（坑11）
        try:
            self._sig_place()  # [v2.17.1] 签名固定右下角
        except Exception:
            pass

    def _wp_settings(self):
        return {
            "mode": self.config.get("wp_mode", "off"),
            "path": self.config.get("wp_path", ""),
            "folder": self.config.get("wp_folder", ""),
            "focus": int(self.config.get("wp_focus", 4)),
            "blur": int(self.config.get("wp_blur", 0)),
            "dim": int(self.config.get("wp_dim", 0)),
            "auto_dim": bool(self.config.get("wp_auto_dim", True)),
            "random_start": bool(self.config.get("wp_random_start", True)),
        }

    def _wp_render(self):
        s = self._wp_settings()
        if s["mode"] == "off" or not wallpaper.available():
            self._wp_set(None)
            return
        src = s["path"]
        if s["mode"] == "folder":
            src = wallpaper.pick_from_folder(s["folder"], exclude_name=os.path.basename(s["path"])
                                             if not s["random_start"] else None)
            if src:
                self.config["wp_path"] = src
                save_config(self.config)
        if not src or not os.path.isfile(src):
            self._wp_set(None)
            if s["mode"] != "off":
                self._toast("底图文件不存在，已回退纯色", "warning")
            return
        if self._wp_busy:
            # [v2.18.1 性能] 忙时记挂起：worker 完成后立即补跑一轮（旧防抖直接丢请求，
            # resize 风暴里最后一次渲染可能永远丢失）
            self._wp_pending = True
            return
        self._wp_busy = True
        self._wp_seq = getattr(self, "_wp_seq", 0) + 1  # 代数号：旧慢任务完成后不再覆盖
        threading.Thread(target=self._wp_worker, args=(src, dict(s), self._wp_seq),
                         daemon=True).start()

    def _wp_worker(self, src, s, seq):
        try:
            qimg = wallpaper.process(src, max(self.width(), 400), max(self.height(), 300),
                                     focus=s["focus"], blur=s["blur"], dim=s["dim"],
                                     theme=self.theme, auto_dim=s["auto_dim"])
        except Exception:
            qimg = None
        self._wp_busy = False
        self._wp_result = {"qimg": qimg, "src": src, "seq": seq,
                           "accent_auto": not bool(
                               self.config.get("wp_accent_lock", False))}
        QApplication.postEvent(self, _WpReadyEvent("wp", seq))

    def event(self, e):
        # [v2.18.1] worker→主线程结果分发（postEvent 线程安全机制）
        if isinstance(e, _WpReadyEvent):
            if e.kind == "wp":
                self._wp_on_ready(int(e.value))
            elif e.kind == "accent":
                self._apply_accent_hex(str(e.value))
            return True
        return super().event(e)

    def _wp_on_ready(self, seq: int):
        """[v2.18.1] 结果经 self._wp_result 属性传递（emit 只带代数号）；
        代数号守卫：resize 风暴中旧慢任务完成后不得覆盖新状态"""
        payload = getattr(self, "_wp_result", None) or {}
        if payload.get("seq", 0) != seq:
            return
        qimg = payload.get("qimg")
        src = payload.get("src", "")
        if qimg is None:
            self._wp_set(None)
            self._wp_kick_pending()
            return
        old = self._wp_pix
        self._wp_pix = QPixmap.fromImage(qimg)
        self._wp_old = old
        # 交叉溶解 450ms（QVariantAnimation 驱动，无 Qt 属性依赖）
        # [v2.18.1 性能] 限帧 ~30fps：全窗重绘走父窗 paintEvent（Fluent 半透明卡片
        # 每帧连带），不加限帧时溶解 27 帧全量 blit 与控件动画抢主线程
        if old is not None:
            from PySide6.QtCore import QVariantAnimation, QElapsedTimer
            self._wp_fade = 0.0
            anim = QVariantAnimation(self)
            anim.setDuration(450)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            _last = {"t": QElapsedTimer()}
            _last["t"].start()

            def _tick(v):
                self._wp_fade = float(v)
                if _last["t"].elapsed() >= 30:
                    _last["t"].restart()
                    self.update()

            anim.valueChanged.connect(_tick)
            anim.finished.connect(lambda: setattr(self, "_wp_old", None))
            anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
            self._wp_fade_anim = anim
        else:
            self._wp_fade = 1.0
        self.update()
        # 自动取色（wp_accent_lock=False 时随图变色）
        if not self.config.get("wp_accent_lock", False):
            def _accent_worker():
                hexcolor = wallpaper.extract_accent(src)
                if hexcolor:
                    # 事件回主线程（postEvent 线程安全——跨线程 Signal 偶发 arity 异常）
                    QApplication.postEvent(self, _WpReadyEvent("accent", hexcolor))
            threading.Thread(target=_accent_worker, daemon=True).start()
        self._wp_kick_pending()

    def _wp_kick_pending(self):
        """[v2.18.1 性能] 忙时挂起的渲染请求：worker 释放后立即补跑（不再丢最后一次）"""
        if getattr(self, "_wp_pending", False):
            self._wp_pending = False
            QTimer.singleShot(0, self._wp_render)

    def _apply_accent_hex(self, hexcolor):
        # [v2.18.1 性能] 同色守卫：resize/换页每次重渲染壁纸都会重新取色，而
        # setThemeColor 会触发 qfluentwidgets 全应用样式重算（遍历全部控件重新
        # polish——千级控件秒级卡顿）。同一张图 resize 多次取出的颜色恒相同，
        # 直接跳过即可根除"最大化/改设置卡半天"的主链路。
        if getattr(self, "_last_accent_hex", None) == hexcolor:
            return
        self._last_accent_hex = hexcolor
        self.config["accent_auto"] = hexcolor
        setThemeColor(QColor(hexcolor))
        # [v2.17.1] 电子签名颜色联动（与强调色/智能取色同源）
        try:
            self._sig_apply_color(hexcolor)
        except Exception:
            pass
        self.update()

    def _wp_set(self, pix):
        self._wp_pix = pix
        self._wp_old = None
        self.update()

    # ── 外观面板辅助（阶段 C）──
    def _wp_update(self, key, value):
        """统一配置变更入口：写键 → 即存 → 防抖重渲染（滑杆 debounce 由 QTimer 承担）"""
        self.config[key] = value
        save_config(self.config)
        self._wp_debounce.start()

    def _wp_set_mode_off(self):
        self._wp_update("wp_mode", "off")
        self._toast("已恢复纯色", "success")

    def _save_captcha_keys(self, k2, kc, ka):
        """[v2.16 M3] 打码 API 密钥保存。

        [v2.19.7 安全·扫描发现] 原实现把三个密钥**明文**写进 launcher_config.json，
        提示语却宣称"DPAPI 加密"（谎报）。现改为：
          · 密钥 → DPAPI 密文（data_root()/secrets/captcha_keys.bin，随当前用户加密）；
          · 配置里的明文键**删除**（不留第二份明文副本）；
          · DPAPI 真不可用时保持明文并在提示语里**明说未加密**——宁可告知，不谎称。
        """
        try:
            keys = {"twocaptcha": k2, "capsolver": kc, "anticaptcha": ka}
            any_key = any((k2, kc, ka))
            enc_ok = save_captcha_keys(keys)
            if enc_ok:
                self.config.pop("captcha_api_keys", None)   # 抹掉旧明文副本
            else:
                self.config["captcha_api_keys"] = keys      # DPAPI 不可用 → 保留明文但如实告知
            save_config(self.config)
            # 同时更新环境状态卡
            self._env_status = getattr(self, "_env_status", {})
            self._env_status["captcha"] = ("✅" if any_key else "○") + " 打码密钥"
            if not any_key:
                msg, kind = "打码密钥已清空", "info"
            elif enc_ok:
                msg, kind = "打码密钥已保存（DPAPI 加密）", "success"
            else:
                msg, kind = "打码密钥已保存，但本机 DPAPI 不可用 → 明文落盘", "warning"
            self._toast(msg, kind)
        except Exception as e:
            self._toast(f"保存失败: {e}", "error")

    def _save_llm_settings(self, enabled, key, base, model, budget=500):
        """[v2.16.1] LLM 设置保存（默认关；Key 仅本机 launcher_config，不打包；
        [v2.17 2.8] 月度预算一并保存）"""
        try:
            self.config["llm_enabled"] = bool(enabled)
            self.config["llm_key"] = key
            self.config["llm_api_base"] = base
            self.config["llm_model"] = model
            self.config["llm_budget_month"] = int(budget)
            save_config(self.config)
            self._toast("LLM 已启用（任务完成后增强）" if enabled else "LLM 已关闭（默认）", "success")
        except Exception as e:
            self._toast(f"保存失败: {e}", "error")

    def _save_ident_settings(self, enabled):
        """[v2.17 E-P2] 身份捆绑轮换开关保存（默认关；开启时下一任务生效）"""
        try:
            self.config["identity_bundle"] = bool(enabled)
            save_config(self.config)
            self._toast("身份捆绑已启用（下次任务生效）" if enabled
                        else "身份捆绑已关闭（默认）", "success")
        except Exception as e:
            self._toast(f"保存失败: {e}", "error")

    def _refresh_proxy_status(self):
        """[v2.16 M3] 代理状态卡：节点数/健康分/失败直方图（_safe_proxy 脱敏）"""
        try:
            em = getattr(self, "engine", None)
            c = getattr(em, "_engine_ref", None)
            if c is None or not hasattr(c, "exit_mgr"):
                self.settings.proxy_status.setText("（无运行中引擎 或 未启用代理——直连模式）")
                return
            emgr = c.exit_mgr
            nodes = getattr(emgr, "nodes", None) or {}
            # 脱敏摘要（避免显示带 user:pass 的代理串）
            from kiana_vnext_plus.sanitizer import sanitize_proxy
            lines = [f"节点数: {len(nodes)}  (直连模式则不设代理)"]
            alive = sum(1 for n in nodes.values() if getattr(n, 'state', None) is not None and
                        getattr(n, 'state', None).name in ("ACTIVE", "WARMING"))
            lines.append(f"活跃: {alive}  · 失败: {sum(1 for n in nodes.values() if getattr(n, 'consecutive_fails', 0) > 2)}")
            for i, (proxy, n) in enumerate(list(nodes.items())[:5]):
                fails = getattr(n, 'consecutive_fails', 0)
                score = getattr(n, 'stability_score', 0)
                lines.append(f"  {sanitize_proxy(str(proxy))[:50]}  健康{score:.0f} 失败{fails}")
                if i >= 4:
                    break
            self.settings.proxy_status.setText("\n".join(lines))
        except Exception as e:
            self.settings.proxy_status.setText(f"代理状态读取失败: {str(e)[:60]}")

    def _wp_pick_image(self):
        f, _ = QFileDialog.getOpenFileName(self, "选择底图", "",
                                           "图片 (*.jpg *.jpeg *.png *.webp *.bmp)")
        if not f:
            return
        self.config["wp_path"] = f
        self._wp_update("wp_mode", "single")

    def _run_priv_scan(self):
        """[v2.16 M5] 隐私扫描：调 tools/privacy_scanner 全项检查 → 结果 toast"""
        try:
            import subprocess as _sp
            import sys as _sys
            r = _sp.run([_sys.executable, str(Path(__file__).parent / "tools" / "privacy_scanner.py")],
                        capture_output=True, text=True, timeout=30)
            lines = [ln for ln in (r.stdout or "").splitlines() if "⚠️" in ln or "✅" in ln or "summary" in ln]
            self._toast("\n".join(lines[:4]), "warning" if "⚠️" in (r.stdout or "") else "success")
        except Exception as e:
            self._toast(f"隐私扫描失败: {e}", "error")

    def _wp_pick_folder(self):
        d = QFileDialog.getExistingDirectory(self, "选择底图文件夹")
        if not d:
            return
        self.config["wp_folder"] = d
        self._wp_update("wp_mode", "folder")

    def _wp_set_accent_lock(self, lock):
        """lock=True 手动色；False 自动取色"""
        self.config["wp_accent_lock"] = lock
        save_config(self.config)
        try:
            self.settings.accent_combo.setEnabled(lock)
            if lock and self.config.get("accent_auto"):
                # 锁定时回落到手动四色之一
                self._set_accent(self.accent_name)
        except Exception:
            pass

    # ── 主题 ──
    def _panel_qss(self, alpha_pct: int, light: bool) -> str:
        """面板玻璃 QSS：渐变玻璃 + 细描边。alpha_pct=面板不透明度百分比(40-95)"""
        a = max(40, min(95, int(alpha_pct))) / 100.0
        if light:
            top, bot, edge = f"rgba(255,255,255,{int(242*a)})", f"rgba(255,255,255,{int(220*a)})", "rgba(0,0,0,14)"
        else:
            top, bot, edge = f"rgba(30,37,46,{int(235*a)})", f"rgba(18,23,30,{int(185*a)})", "rgba(255,255,255,22)"
        return (f"background: qlineargradient(x1:0,y1:0,x2:0,y2:1,"
                f"stop:0 {top}, stop:1 {bot});"
                f"border: 1px solid {edge}; border-radius: 12px;")

    def _apply_qfluent_theme(self):
        setTheme(THEMES.get("亮色" if self.theme == "light" else "深色", Theme.DARK))
        setThemeColor(QColor(ACCENTS.get(self.accent_name, ACCENTS["蓝"])))
        # [v2.18.2] 根因B修复：原硬编码 rgba(22,27,34,235)（≈92% 不透明纯色块）
        # 视觉是"一块黑板盖在壁纸上"。改为渐变玻璃 + 描边，透明度由配置
        # panel_alpha（40-95，默认 65）控制——作者可再经设置页滑杆/预设调整。
        panel = self._panel_qss(self.config.get("panel_alpha", 65), self.theme == "light")
        try:
            for page in (self.logp, self.datap, self.taskp, self.settings):
                for card in page.findChildren(CardWidget):
                    card.setStyleSheet(panel)
            self.home.card_done.setStyleSheet(panel)
            self.home.card_fail.setStyleSheet(panel)
            self.home.card_speed.setStyleSheet(panel)
        except Exception:
            pass  # 构造早期页面未建齐时跳过

    def _set_theme(self, text):
        self.theme = "light" if text == "亮色" else "dark"
        self.config["theme"] = self.theme
        self._apply_qfluent_theme()
        save_config(self.config)
        # 蒙层颜色随主题变 → 底图重渲染
        if self.config.get("wp_mode", "off") != "off":
            self._wp_debounce.start()

    def _set_accent(self, name):
        if name not in ACCENTS:
            return
        self.accent_name = name
        self.config["accent"] = name
        self._apply_qfluent_theme()
        save_config(self.config)

    # [v2.18.2 作者新要求] 面板玻璃透明度：滑杆拖动只更新数值 label + 写内存配置
    # （valueChanged 由设置页接线到这个槽）；200ms 防抖后再存盘 + 重刷玻璃。
    # 独立 QTimer——不复用 _wp_debounce（它属于底图系统，会触发整张底图重渲染）。
    def _panel_alpha_changed(self, value, slider):
        self.config["panel_alpha"] = max(40, min(95, int(value)))
        t = getattr(self, "_panel_alpha_timer", None)
        if t is None:
            t = QTimer(self)
            t.setSingleShot(True)
            t.setInterval(200)
            t.timeout.connect(self._panel_alpha_apply)
            self._panel_alpha_timer = t
        t.start()

    def _panel_alpha_apply(self):
        try:
            v = int(self.config.get("panel_alpha", 65))
        except Exception:
            v = 65
        self.config["panel_alpha"] = max(40, min(95, v))
        save_config(self.config)
        self._apply_qfluent_theme()

    # ── 接线（v8 逻辑平移） ──
    def _wire(self):
        h = self.home
        h.start_btn.clicked.connect(self._start)
        h.pause_btn.clicked.connect(self._toggle_pause)
        h.stop_btn.clicked.connect(self._stop)
        h.retry_btn.clicked.connect(self._retry_dead)
        h.open_btn.clicked.connect(self._open_outdir)
        h.out_edit.textChanged.connect(lambda t: self.config.__setitem__("outdir", t))
        h.cookie_edit.textChanged.connect(self._on_cookie_home)
        h.filter_edit.textChanged.connect(lambda t: self.config.__setitem__("filter_text", t))
        for sw, key, default in ((h.sw_video, "dl_video", True), (h.sw_image, "dl_image", True),
                                 (h.sw_audio, "dl_audio", False), (h.sw_sanitize, "sanitize", True),
                                 (h.sw_robots, "robots_respect", False)):
            sw.checkedChanged.connect(lambda c, k=key, d=default: self.config.__setitem__(k, c))
        # 设置页 cookie 双向同步（坑9：双入口同步保留）
        h.cookie_edit.textChanged.connect(self._sync_cookie_to_settings)
        self.settings.cookie_edit_d.textChanged.connect(self._on_cookie_settings)

    def _sync_cookie_to_settings(self, t):
        if self.settings.cookie_edit_d.text() != t:
            self.settings.cookie_edit_d.blockSignals(True)
            self.settings.cookie_edit_d.setText(t)
            self.settings.cookie_edit_d.blockSignals(False)

    def _on_cookie_home(self, t):
        self.config["cookie_file"] = t.strip()
        save_config(self.config)

    def _on_cookie_settings(self, t):
        self.config["cookie_file"] = t.strip()
        save_config(self.config)
        self.home.refresh_from_config(self.config)

    # ── 启动/停止/暂停/重试（v8 逻辑平移，含 cookie 传入引擎修复） ──
    def _import_url_file(self):
        """[v2.17 1-4] 从文本文件导入 URL（逐行追加到 url_edit）"""
        try:
            from PySide6.QtWidgets import QFileDialog
            path, _ = QFileDialog.getOpenFileName(
                self, "导入 URL 文件", "", "文本文件 (*.txt *.md *.csv);;所有文件 (*)")
            if not path:
                return
            from pathlib import Path as _P
            text = _P(path).read_text(encoding="utf-8", errors="ignore")
            self._append_url_text(text)
            self._toast(f"已导入 {path}", "success")
        except Exception as e:
            self._toast(f"导入失败: {e}", "error")

    def _import_url_clipboard(self):
        """[v2.17 1-4] 剪贴板粘贴 URL（追加）"""
        try:
            from PySide6.QtWidgets import QApplication
            text = QApplication.clipboard().text() or ""
            self._append_url_text(text)
            self._toast("已从剪贴板追加", "success")
        except Exception as e:
            self._toast(f"粘贴失败: {e}", "error")

    def _append_url_text(self, text: str):
        """追加（保留已有内容；跳过空行）——_clean_urls 语义由引擎端保证"""
        cur = self.home.url_edit.toPlainText()
        merged = (cur.strip() + "\n" + text).strip()
        self.home.url_edit.setPlainText(merged)

    def _start(self):
        self._check_cookies()
        urls = []
        for line in self.home.url_edit.toPlainText().splitlines():
            line = line.strip()
            if not line:
                continue
            m = re.search(r'https?://[^\s\u4e00-\u9fff]+', line)
            if m:
                urls.append(m.group(0).rstrip('.,;:!?。，；：！？)】》"\''))
            elif '://' in line:
                urls.append(line)
        if not urls:
            self._toast("请先输入目标网址", "error")
            return
        h = self.home
        cfg = {
            "depth": h.depth_spin.value(), "pages": h.pages_spin.value(),
            "outdir": h.out_edit.text().strip(),
            "dl_video": h.sw_video.isChecked(), "dl_image": h.sw_image.isChecked(),
            "dl_audio": h.sw_audio.isChecked(), "no_sanitize": not h.sw_sanitize.isChecked(),
            "robots_respect": bool(h.sw_robots.isChecked()),
            "filter_words": h.filter_edit.text().strip(),
            "cookie_file": h.cookie_edit.text().strip(),  # v2.11 修复保留：自填 cookies 传入引擎
            "resolution": ["highest", "2160", "1440", "1080", "720", "480"][h.res_combo.currentIndex()],
            "subs": h.sw_subs.isChecked(), "thumb": h.sw_thumb.isChecked(),
            "info": h.sw_info.isChecked(),
            # [v2.16.1] LLM（设置页卡片，默认关；[v2.17 2.8] 预算 GUI 化）
            "llm_enabled": bool(self.config.get("llm_enabled", False)),
            "llm_key": str(self.config.get("llm_key", "")),
            "llm_api_base": str(self.config.get("llm_api_base", "")),
            "llm_model": str(self.config.get("llm_model", "")),
            "llm_budget_month": int(self.config.get("llm_budget_month", 500)),
            # [v2.17 E-P2] 身份捆绑轮换（设置页卡片，默认关）
            "identity_bundle": bool(self.config.get("identity_bundle", False)),
        }
        self.config.update({k: cfg[k] for k in
                            ("depth", "pages", "outdir", "dl_video", "dl_image",
                             "dl_audio", "filter_words", "resolution", "subs", "thumb", "info",
                             "robots_respect")})
        self.config["sanitize"] = h.sw_sanitize.isChecked()
        self.config["filter_text"] = cfg["filter_words"]
        save_config(self.config)
        self.engine.start(urls, cfg)

    def _stop(self):
        self.engine.stop()
        self._log("── 已请求停止 ──", "warn")

    def _toggle_pause(self):
        if self.engine.paused:
            self.engine.resume()
            self.home.pause_btn.setText("暂停")
        else:
            self.engine.pause()
            self.home.pause_btn.setText("继续")

    def _retry_dead(self):
        try:
            import sqlite3
            base = Path(str(self.config.get("outdir") or Path.home() / "Downloads" / "KianaVnextPlus"))
            projs = sorted(base.glob("cli_*"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not projs:
                self._toast("未找到历史任务目录", "warning")
                return
            db = projs[0] / "frontier.db"
            if not db.exists():
                self._toast("最新任务无 frontier.db", "warning")
                return
            con = sqlite3.connect(str(db))
            cur = con.execute(
                "UPDATE frontier SET status='pending', retry_count=0, scheduled_at=? WHERE status='dead'",
                (time.time(),))
            con.commit()
            n = cur.rowcount
            con.close()
            self._toast(f"已重置 {n} 个失败页 → 下次启动同 URL 自动续爬" if n
                        else "没有可重试的失败页", "success" if n else "warning")
        except Exception as e:
            self._toast(f"重试失败: {e}", "error")

    def _open_outdir(self):
        try:
            base = Path(str(self.config.get("outdir") or Path.home() / "Downloads" / "KianaVnextPlus"))
            base.mkdir(parents=True, exist_ok=True)
            projs = sorted(base.glob("cli_*"), key=lambda p: p.stat().st_mtime, reverse=True)
            target = projs[0] / "export" if projs else base
            if target.exists():
                subprocess.Popen(["explorer.exe", str(target)])
        except Exception:
            pass

    def _check_cookies(self):
        try:
            gui_cookie = self.home.cookie_edit.text().strip()
            _saved_f = os.environ.get("KIANA_COOKIE_FILES")
            _saved_1 = os.environ.get("KIANA_COOKIE_FILE")
            try:
                if gui_cookie:
                    os.environ["KIANA_COOKIE_FILES"] = gui_cookie
                    os.environ.pop("KIANA_COOKIE_FILE", None)
                else:
                    os.environ.pop("KIANA_COOKIE_FILES", None)
                    os.environ.pop("KIANA_COOKIE_FILE", None)
                from kiana_vnext_plus.cookie_health import check_sites
                r = check_sites()
            finally:
                if _saved_f is not None:
                    os.environ["KIANA_COOKIE_FILES"] = _saved_f
                else:
                    os.environ.pop("KIANA_COOKIE_FILES", None)
                if _saved_1 is not None:
                    os.environ["KIANA_COOKIE_FILE"] = _saved_1
                else:
                    os.environ.pop("KIANA_COOKIE_FILE", None)
            sites = {s["site"]: s for s in r.get("sites", [])}
            bits = []
            if "B站" in sites and sites["B站"].get("health"):
                hh = sites["B站"]["health"]
                bits.append(f"B站{'✅' if hh['ok'] else '⚠️'}")
            if "抖音" in sites:
                bits.append(f"抖音{'✅' if sites['抖音']['has_cookie'] else '—'}")
            if "贴吧" in sites:
                bits.append(f"贴吧{'✅' if sites['贴吧']['has_cookie'] else '—'}")
            # [v2.17 4.4] 快手/小红书域级弱检查（存在即健康态，弱检查不做在线断言）
            if "快手" in sites:
                bits.append(f"快手{'✅' if sites['快手']['has_cookie'] else '—'}")
            if "小红书" in sites:
                bits.append(f"小红书{'✅' if sites['小红书']['has_cookie'] else '—'}")
            from pathlib import Path as _P
            _bin = _P(data_root()) / "master.key.bin"
            enc = "🔐 密钥DPAPI加密" if _bin.exists() else "⚠️ 密钥未加密"
            self.home.privacy_status.setText(f"隐私状态: {enc} · Cookies: {' '.join(bits) or '无'}")
        except Exception:
            pass

    # ── 引擎信号 ──
    def _on_started(self):
        self.home.start_btn.setEnabled(False)
        self.home.stop_btn.setEnabled(True)
        self.home.pause_btn.setEnabled(True)
        self.home.pause_btn.setText("暂停")
        self.home.badge.setText("运行中")
        self.home.progress.setValue(0)

    def _on_finished(self, rc):
        self.home.start_btn.setEnabled(True)
        self.home.stop_btn.setEnabled(False)
        self.home.pause_btn.setEnabled(False)
        self.home.pause_btn.setText("暂停")
        self.badge = None  # noqa: 兼容占位（badge/progress 实际在 home 页）
        self.home.badge.setText("空闲")
        self._log(f"── 爬虫结束 (exit={rc}) ──", "ok" if rc == 0 else "err")
        if rc == 2:
            self._toast("引擎崩溃 (exit=2)——可修改参数后重试", "error")
        elif rc == 1:
            self._toast("爬取已停止", "warning")
        elif rc == 3:
            self._toast("引擎加载失败 (exit=3)——请重装或检查安装包", "error")
        else:
            self._toast("爬取完成", "success")
        if rc == 0:
            def _delayed_open():
                try:
                    base = Path(str(self.config.get("outdir") or Path.home() / "Downloads" / "KianaVnextPlus"))
                    projs = sorted(base.glob("cli_*"), key=lambda p: p.stat().st_mtime, reverse=True)
                    if projs:
                        export = projs[0] / "export"
                        if export.exists():
                            subprocess.Popen(["explorer.exe", str(export)])
                except Exception:
                    pass
            threading.Timer(8.0, _delayed_open).start()

    def _on_log(self, text, level):
        if level == "info" and ("done=" in text or "进度" in text):
            self._parse_progress(text)
        self._log(text, level)

    def _parse_progress(self, text):
        """进度解析：优先读 stats.jsonl 结构化（v2.15 持久化），regex 作 fallback。"""
        # [v2.16 M4] stats.jsonl 消费：读当前任务目录尾部行（结构化，替代脆弱 regex）
        try:
            base = Path(str(self.config.get("outdir") or Path.home() / "Downloads" / "KianaVnextPlus"))
            projs = sorted(base.glob("cli_*"), key=lambda p: p.stat().st_mtime, reverse=True)
            if projs and (projs[0] / "stats.jsonl").exists():
                lines = (projs[0] / "stats.jsonl").read_text(encoding="utf-8").strip().splitlines()
                if lines:
                    last = json.loads(lines[-1])
                    d, f2, pn = last.get("done", 0), last.get("failed", 0), last.get("pending", 0)
                    vals = {"done": str(d), "d_done": str(d), "fail": str(f2), "d_fail": str(f2),
                            "d_pend": str(pn), "d_total": str(d + f2 + pn)}
                    for k, v in vals.items():
                        if k in self._stat_labels:
                            self._stat_labels[k].setText(v)
                    if last.get("total") and last["total"] > 0:
                        self.home.progress.setValue(int(d / last["total"] * 100))
                    return
        except Exception:
            pass
        # fallback：regex（旧版/无 stats 时）
        m = re.search(r"done=(\d+) fail=(\d+) pend=(\d+)", text)
        if m:
            d, f2, pn = int(m.group(1)), int(m.group(2)), int(m.group(3))
            vals = {"done": str(d), "d_done": str(d), "fail": str(f2), "d_fail": str(f2),
                    "d_pend": str(pn), "d_total": str(d + f2 + pn)}
            for k, v in vals.items():
                if k in self._stat_labels:
                    self._stat_labels[k].setText(v)
        m = re.search(r"(\d+(?:\.\d+)?)p/s", text)
        if m:
            self._stat_labels["speed"].setText(m.group(1))
        m = re.search(r"(\d+(?:\.\d+)?)%", text)
        if m:
            try:
                self.home.progress.setValue(int(float(m.group(1))))
            except Exception:
                pass

    def _log(self, text, level="info"):
        self.logp.append(text, level)

    def _toast(self, msg, kind="info"):
        fn = {"success": InfoBar.success, "warning": InfoBar.warning,
              "error": InfoBar.error, "info": InfoBar.info}.get(kind, InfoBar.info)
        try:
            fn(title="", content=msg, orient=Qt.Horizontal, isClosable=True,
               position=InfoBarPosition.TOP, duration=3500, parent=self)
        except Exception:
            pass

    # ── 任务页逻辑（v8 平移） ──
    def _tasks_base(self) -> Path:
        return Path(str(self.config.get("outdir") or Path.home() / "Downloads" / "KianaVnextPlus"))

    def _refresh_tasks(self):
        try:
            base = self._tasks_base()
            items = []
            for d in sorted(base.glob("cli_*"), key=lambda p: p.stat().st_mtime, reverse=True)[:60]:
                try:
                    mt = time.strftime("%m-%d %H:%M", time.localtime(d.stat().st_mtime))
                    size_mb = sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) / 1024 / 1024
                    items.append(f"{d.name}  |  {mt}  |  {size_mb:.1f} MB")
                except Exception:
                    continue
            self.taskp.tasks_list.setPlainText("\n".join(items) or "暂无历史任务（点「开始爬取」创建）")
        except Exception as e:
            self.taskp.tasks_list.setPlainText(f"读取失败: {e}")

    def _selected_task_dir(self):
        name = self.taskp.tasks_list.textCursor().selectedText() or ""
        line = (name.strip().splitlines() or [""])[0].split("|")[0].strip()
        if not line.startswith("cli_"):
            return None
        d = self._tasks_base() / line
        return d if d.is_dir() else None

    def _open_selected_task(self):
        d = self._selected_task_dir()
        if not d:
            self._toast("请先在列表中点击选中一个任务行", "warning")
            return
        export = d / "export"
        subprocess.Popen(["explorer.exe", str(export if export.exists() else d)])

    def _delete_selected_task(self):
        d = self._selected_task_dir()
        if not d:
            self._toast("请先在列表中点击选中一个任务行", "warning")
            return
        import shutil
        shutil.rmtree(d, ignore_errors=True)
        self._toast(f"已删除: {d.name}", "success")
        self._refresh_tasks()


def main():
    app = QApplication(sys.argv)
    win = KianaV9()
    win.resize(1360, 860)
    win.show()
    # [v2.18.2] 兜底：首帧若被内容 sizeHint 反超撑大，拉回标准启动尺寸
    QTimer.singleShot(0, lambda: win.resize(1360, 860))
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
