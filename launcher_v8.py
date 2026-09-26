# -*- coding: utf-8 -*-
"""
Kiana Vnext Plus v8 — 旧版 GUI 壳（[v2.17 B1c] 诚实标注：当前打包入口为 launcher_v9
FluentWindow；本文件仅被 v9 复用 EngineBridge/_LineStream/_QtLogHandler/CONFIG_FILE
四个符号——其余约 60KB GUI 代码是 legacy 死壳，改造 v9 时勿以本文件为现行事实来源）
[v2.19.7 追加] 另被 v9 复用 load_captcha_keys/save_captcha_keys（DPAPI 密钥存取）两个符号。
v8 原定义：现代桌面应用 UI（PySide6 + QSS 深度定制）
对标 Telegram Desktop / QQ NT / 酷狗音乐
设计依据: Kiana_v8_现代桌面UI设计方案.docx
- 设计令牌系统（亮/暗/强调色三态主题）
- 三区布局：左侧导航 + 主工作区(卡片网格) + 右侧设置抽屉
- 真圆角/分层阴影/动效（QSS + QGraphicsDropShadowEffect + QPropertyAnimation）
- 状态驱动 + 信号槽，UI 全部真实绑定爬虫引擎（零死链、引擎零损耗）
"""
import sys
import os
import json
import subprocess
import threading
import time
import io
import logging
from pathlib import Path

from PySide6.QtCore import Qt, QPropertyAnimation, QEasingCurve, QTimer, QRect, Signal, QObject
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QStackedWidget, QPushButton, QToolButton, QLabel, QLineEdit, QPlainTextEdit, QSpinBox,
    QCheckBox, QComboBox, QFrame, QFileDialog, QGraphicsDropShadowEffect, QProgressBar)

PROJECT_DIR = Path(__file__).resolve().parent

def _launcher_config_path() -> Path:
    """[v2.16.1] 便携版补全：KIANA_PORTABLE=1 → exe 同级 KianaData/launcher_config.json
    （随包走）；否则保持原 LOCALAPPDATA 路径（本地默认不变）。"""
    if os.environ.get("KIANA_PORTABLE") == "1":
        try:
            from kiana_vnext_plus.config import data_root
            return Path(data_root()) / "launcher_config.json"
        except Exception:
            pass
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "KianaVnextPlus" / "launcher_config.json"

# onefile 打包下 __file__ 指向临时解压目录（不可持久）→ 配置固定到用户级目录
CONFIG_FILE = _launcher_config_path()
ASSETS = PROJECT_DIR / "assets"

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# [v2.19.7 安全·扫描发现] 打码 API 密钥：DPAPI 密文文件优先，旧明文配置读到即迁移
#   修复两个问题：
#     1. 界面宣称"DPAPI 加密"，实际明文写 launcher_config.json（本机任意进程/同步盘可读）；
#     2. 三个密钥框填了**从不传导到引擎**（EngineBridge 的 engine_cfg 翻译表缺键 →
#        crawler 里 captcha_api_keys 恒为默认空 → SolverEngine 的打码分支永远拿不到密钥，
#        GUI 是"能填、能存、不生效"的装饰）。
# ─────────────────────────────────────────────────────────────
_CAPTCHA_KEY_NAMES = ("twocaptcha", "capsolver", "anticaptcha")


def load_captcha_keys(cfg: dict) -> dict:
    """读取打码密钥：DPAPI 密文（secrets/captcha_keys.bin）优先；若只有旧明文配置则
    自动加密迁移（写密文 + 从配置里抹掉明文键）。任何失败 → 尽力返回可用字典。"""
    out = {}
    try:
        from kiana_vnext_plus.privacy_store import load_secret_json
        enc = load_secret_json("captcha_keys")
        if isinstance(enc, dict):
            out = {k: str(enc.get(k) or "") for k in _CAPTCHA_KEY_NAMES}
    except Exception:
        pass
    if not any(out.values()):
        legacy = (cfg or {}).get("captcha_api_keys") or {}
        if isinstance(legacy, dict) and any(str(legacy.get(k) or "").strip() for k in _CAPTCHA_KEY_NAMES):
            out = {k: str(legacy.get(k) or "") for k in _CAPTCHA_KEY_NAMES}
            if save_captcha_keys(out):          # 迁移成功 → 抹掉明文副本
                try:
                    cfg.pop("captcha_api_keys", None)
                except Exception:
                    pass
    return out


def save_captcha_keys(keys: dict) -> bool:
    """打码密钥 DPAPI 加密落盘。空值 → 删除密文文件。返回是否已加密保存
    （False = DPAPI 不可用，调用方必须诚实告知用户，不得谎称加密）"""
    try:
        from kiana_vnext_plus import privacy_store as _ps
        vals = {k: str((keys or {}).get(k) or "") for k in _CAPTCHA_KEY_NAMES}
        if not any(vals.values()):
            _ps.delete_secret("captcha_keys")
            return True
        return bool(_ps.save_secret_json("captcha_keys", vals))
    except Exception as e:
        logger.warning(f"打码密钥加密保存失败: {e}")
        return False


# ══════════════════════════════════════════════════════════════
# 一、设计令牌系统（Design Tokens）—— 所有组件只引用令牌
# ══════════════════════════════════════════════════════════════
TOKENS_LIGHT = {
    "bg/base": "#F5F7FA", "bg/elevated": "#FFFFFF", "bg/sunken": "#EBEEF3",
    "bg/hover": "rgba(0,0,0,0.04)", "bg/active": "rgba(0,0,0,0.08)",
    "text/primary": "#14181C", "text/secondary": "#5A6470", "text/disabled": "#B0B8C2",
    "accent": "#2E74B5", "accent/hover": "#265D94", "success": "#2E9E5B",
    "warning": "#D9822B", "danger": "#D64545", "border": "#D8DEE6",
    "glass": "rgba(255,255,255,0.65)", "scrim": "rgba(0,0,0,0.45)",
    "shadow/md": "0 4px 12px rgba(0,0,0,0.10)",
}
TOKENS_DARK = {
    "bg/base": "#0F1419", "bg/elevated": "#1A2029", "bg/sunken": "#0B0F14",
    "bg/hover": "rgba(255,255,255,0.06)", "bg/active": "rgba(255,255,255,0.10)",
    "text/primary": "#E8ECF1", "text/secondary": "#8B96A3", "text/disabled": "#5A6470",
    "accent": "#4FA3E8", "accent/hover": "#6DB5F0", "success": "#3FC27A",
    "warning": "#F0A34E", "danger": "#F26060", "border": "#2A323D",
    "glass": "rgba(20,26,34,0.60)", "scrim": "rgba(0,0,0,0.55)",
    "shadow/md": "0 4px 12px rgba(0,0,0,0.30)",
}
ACCENT_VARIANTS = {  # 品牌强调色
    "蓝": {"accent": "#2E74B5", "accent/hover": "#265D94", "accent/light": "#4FA3E8"},
    "紫": {"accent": "#7C5CD6", "accent/hover": "#6A48C2", "accent/light": "#9B7FEA"},
    "青": {"accent": "#0FA3A3", "accent/hover": "#0C8C8C", "accent/light": "#3CC4C4"},
    "粉": {"accent": "#D65C8E", "accent/hover": "#C04A7B", "accent/light": "#EA82AC"},
}
FONT_STACK = '"Microsoft YaHei UI", "Segoe UI Variable", "Segoe UI"'
FONT_MONO = '"Cascadia Code", "JetBrains Mono", "Consolas"'

def build_qss(T, font_stack=FONT_STACK, mono=FONT_MONO):
    """令牌 → 全局 QSS（主题切换 = 替换令牌表，零组件改动）"""
    return f"""
    QMainWindow, QWidget {{ background: {T['bg/base']}; color: {T['text/primary']}; font-family: {font_stack}; }}
    QToolTip {{ background: {T['bg/elevated']}; color: {T['text/primary']}; border: 1px solid {T['border']}; border-radius: 6px; padding: 6px 10px; }}
    /* ── 导航栏 ── */
    #NavBar {{ background: {T['glass']}; border-right: 1px solid {T['border']}; }}
    #NavBtn {{ border: none; border-radius: 12px; padding: 10px; background: transparent; color: {T['text/secondary']}; }}
    #NavBtn:hover {{ background: {T['bg/hover']}; }}
    #NavBtn:checked {{ background: {T['accent']}; color: white; }}
    /* ── 卡片 ── */
    #Card {{ background: {T['glass']}; border: 1px solid {T['border']}; border-radius: 14px; }}
    #CardTitle {{ font-size: 15px; font-weight: 600; color: {T['text/primary']}; }}
    #CardSub {{ font-size: 12px; color: {T['text/secondary']}; }}
    #StatNum {{ font-size: 28px; font-weight: 700; color: {T['text/primary']}; }}
    #StatLabel {{ font-size: 11px; color: {T['text/secondary']}; }}
    /* ── 按钮 ── */
    QPushButton {{ border-radius: 10px; padding: 7px 16px; font-size: 13px; background: {T['bg/elevated']}; color: {T['text/primary']}; border: 1px solid {T['border']}; }}
    QPushButton:hover {{ background: {T['bg/hover']}; }}
    QPushButton:pressed {{ background: {T['bg/active']}; }}
    QPushButton:disabled {{ color: {T['text/disabled']}; }}
    #PrimaryBtn {{ background: {T['accent']}; color: white; border: none; font-weight: 600; }}
    #PrimaryBtn:hover {{ background: {T['accent/hover']}; }}
    #DangerBtn {{ background: transparent; color: {T['danger']}; border: 1px solid {T['danger']}; }}
    #DangerBtn:hover {{ background: {T['danger']}; color: white; }}
    #GhostBtn {{ background: transparent; border: none; color: {T['text/secondary']}; }}
    #GhostBtn:hover {{ background: {T['bg/hover']}; color: {T['text/primary']}; }}
    #PillBtn {{ border-radius: 999px; padding: 5px 14px; font-size: 12px; background: {T['bg/elevated']}; border: 1px solid {T['border']}; }}
    #PillBtn:checked {{ background: {T['accent']}; color: white; border-color: {T['accent']}; }}
    /* ── 输入框 ── */
    QLineEdit, QPlainTextEdit, QSpinBox, QComboBox {{ background: {T['bg/sunken']}; border: 1px solid {T['border']}; border-radius: 10px; padding: 6px 10px; font-size: 13px; selection-background-color: {T['accent']}; }}
    QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QComboBox:focus {{ border: 2px solid {T['accent']}; padding: 5px 9px; }}
    QPlainTextEdit {{ font-family: {mono}; font-size: 12px; }}
    QComboBox::drop-down {{ border: none; width: 24px; }}
    QSpinBox::up-button, QSpinBox::down-button {{ background: transparent; border: none; width: 18px; }}
    /* ── 开关（checkbox 绘制为圆角方块） ── */
    QCheckBox {{ spacing: 8px; font-size: 13px; }}
    QCheckBox::indicator {{ width: 18px; height: 18px; border-radius: 5px; border: 2px solid {T['border']}; background: {T['bg/sunken']}; }}
    QCheckBox::indicator:hover {{ border-color: {T['accent']}; }}
    QCheckBox::indicator:checked {{ background: {T['accent']}; border-color: {T['accent']}; }}
    /* ── 滑块 ── */
    QSlider::groove:horizontal {{ height: 4px; border-radius: 2px; background: {T['border']}; }}
    QSlider::sub-page:horizontal {{ background: {T['accent']}; border-radius: 2px; }}
    QSlider::handle:horizontal {{ width: 16px; height: 16px; margin: -6px 0; border-radius: 8px; background: white; border: 2px solid {T['accent']}; }}
    /* ── 进度条 ── */
    QProgressBar {{ border: none; border-radius: 4px; background: {T['bg/sunken']}; height: 8px; text-align: center; font-size: 10px; color: {T['text/disabled']}; }}
    QProgressBar::chunk {{ border-radius: 4px; background: {T['accent']}; }}
    /* ── 日志分级 ── */
    #LogView {{ background: {T['bg/sunken']}; border: 1px solid {T['border']}; border-radius: 10px; font-family: {mono}; font-size: 12px; color: {T['text/secondary']}; }}
    /* ── 抽屉 / 弹窗 ── */
    #Drawer {{ background: {T['glass']}; border-left: 1px solid {T['border']}; }}
    #DrawerTitle {{ font-size: 20px; font-weight: 600; }}
    #SectionTitle {{ font-size: 13px; font-weight: 600; color: {T['text/secondary']}; }}
    #Toast {{ background: {T['bg/elevated']}; border: 1px solid {T['border']}; border-radius: 10px; }}
    #StatusBadge {{ border-radius: 999px; padding: 3px 10px; font-size: 11px; }}
    """

# ══════════════════════════════════════════════════════════════
# 二、引擎桥接（零损耗：subprocess + env 传递，与原 launcher 完全一致）
# ══════════════════════════════════════════════════════════════
def _scrub(text: str) -> str:
    """[v2.19.7 安全·扫描发现] 非 logging 出口（print 桥 / Qt 信号）的脱敏。

    logging 侧靠 Handler Filter 自动覆盖，但这两条是**旁路**：`_LineStream` 转的是
    print（yt-dlp 等第三方库的进度/错误行常带完整 URL），`log_line.emit` 转的是异常
    文本与 traceback（`Formatter.format` 会把 exc_info 也拼进去，Filter 只看 getMessage
    拿不到那部分）。在出口做一次幂等脱敏，异常一律放行原文（脱敏不该反过来打断日志）。
    """
    try:
        from kiana_vnext_plus.sanitizer import sanitize_text, sanitize_url
        return sanitize_text(sanitize_url(str(text)))
    except Exception:
        return text


class _LineStream(io.TextIOBase):
    """捕获 print 输出 → 逐行转发 Qt 信号（进程内引擎的 stdout 桥）"""
    def __init__(self, sig):
        super().__init__()
        self.sig = sig
        self._buf = ""

    def write(self, s):
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.strip()
            if line:
                lv = "info"
                low = line.lower()
                if "error" in low or "traceback" in low or "失败" in line:
                    lv = "err"
                elif "warn" in low or "重试" in line:
                    lv = "warn"
                elif "success" in low or "完成" in line:
                    lv = "ok"
                # [v2.19.7] 出口脱敏（此路不过 logging，Filter 覆盖不到；见 _scrub 说明）
                self.sig.emit(_scrub(line), lv)
        return len(s)

    def flush(self):
        if self._buf.strip():
            self.sig.emit(self._buf.strip(), "info")
            self._buf = ""


class _QtLogHandler(logging.Handler):
    """捕获 logging 输出 → Qt 信号（引擎 logger 桥）"""
    def __init__(self, sig):
        super().__init__()
        self.sig = sig

    def emit(self, record):
        try:
            lv = "err" if record.levelno >= logging.ERROR else "warn" if record.levelno >= logging.WARNING else "info"
            # [v2.19.7 安全·扫描发现] Handler Filter 只脱敏 record.getMessage()；而
            # `Formatter.format()` 还会把 exc_info 的 traceback 自动拼进来（异常里常带
            # 完整 URL/API Key）→ 在格式化结果上再脱敏一次（幂等）。
            self.sig.emit(_scrub(self.format(record)), lv)
        except Exception:
            pass


class EngineBridge(QObject):
    """UI ↔ 爬虫引擎桥接：进程内引擎（单文件 exe 自包含，零外部依赖）
    后台线程跑 asyncio 引擎；print/logging 双路捕获转发为 Qt 信号；软停止（_should_stop）"""
    log_line = Signal(str, str)       # (text, level)
    finished = Signal(int)            # exit code
    started = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread = None
        self._loop = None
        self._crawler = None
        self._engine_ref = None  # [FIXED & MODIFIED] v2.11 运行中引擎引用（on_engine 回调）

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, urls, cfg):
        """启动进程内引擎（单文件模式：直接 import run_crawler 线程内跑）"""
        if self.running:
            return
        self._engine_ref = None
        # [FIXED & MODIFIED] v2.11 KIANA_CRYPTO_KEY 机制整体删除（Fernet 链零消费者）
        # [FIXED & MODIFIED] v2.10.5 用户自填 cookies：支持分号分隔多文件（KIANA_COOKIE_FILES），
        # 密钥从不打包进程序；留空则用默认位置。设置时清除旧环境变量避免残留。
        if cfg.get("cookie_file"):
            # [FIXED & MODIFIED] v2.11 GUI 自填 cookies 真正传入引擎（原 _start 的 cfg
            # 缺 cookie_file 键 → 此处恒为空 → GUI 设置的路径永不生效）
            os.environ["KIANA_COOKIE_FILES"] = cfg["cookie_file"]
            os.environ.pop("KIANA_COOKIE_FILE", None)
        elif "KIANA_COOKIE_FILES" in os.environ or "KIANA_COOKIE_FILE" in os.environ:
            os.environ.pop("KIANA_COOKIE_FILES", None)
            os.environ.pop("KIANA_COOKIE_FILE", None)
        self._thread = threading.Thread(target=self._run_engine, args=(urls, cfg), daemon=True)
        self._thread.start()
        self.started.emit()

    def _run_engine(self, urls, cfg):
        import asyncio
        import logging
        import sys
        # 引擎源码定位：源码模式 PROJECT_DIR；onefile 模式 _MEIPASS（--add-data 已含 run_crawler.py）
        for p in (str(PROJECT_DIR), getattr(sys, "_MEIPASS", "")):
            if p and p not in sys.path:
                sys.path.insert(0, p)
        try:
            import run_crawler
        except ImportError as e:
            self.log_line.emit(f"引擎加载失败: {e}", "err")
            self.finished.emit(3)
            return
        # Proactor 性能策略（Windows）
        if sys.platform == "win32":
            try:
                asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
            except Exception:
                pass
        # UI cfg → 引擎 crawl() 格式（键名转换，缺省默认值）
        engine_cfg = {
            "crawl_depth": int(cfg.get("depth", 5)),
            "max_pages": int(cfg.get("pages", 500)),
            "download_path": str(cfg.get("outdir") or Path.home() / "Downloads" / "KianaVnextPlus"),
            "log_level": "INFO",
            "no_sanitize": bool(cfg.get("no_sanitize", False)),
            "robots_respect": bool(cfg.get("robots_respect", False)),
            "dl_video": bool(cfg.get("dl_video", False)),
            "dl_image": bool(cfg.get("dl_image", False)),
            "dl_audio": bool(cfg.get("dl_audio", False)),
            "filter_words": str(cfg.get("filter_words", "")),
            # [v2.16.1] 画质/副产物 GUI 化接线（v9 首页新增控件 → 引擎消费）
            "resolution": str(cfg.get("resolution", "highest")),
            "subs": bool(cfg.get("subs", True)),
            "thumb": bool(cfg.get("thumb", True)),
            "info": bool(cfg.get("info", True)),
            # [v2.16.1] LLM 开关透传（默认关——关闭时引擎不构建客户端）；[v2.17 2.8] 预算
            "llm_enabled": bool(cfg.get("llm_enabled", False)),
            "llm_key": str(cfg.get("llm_key", "")),
            "llm_api_base": str(cfg.get("llm_api_base", "")),
            "llm_model": str(cfg.get("llm_model", "")),
            "llm_budget_month": int(cfg.get("llm_budget_month", 500)),
            # [FIXED & MODIFIED] v2.17 稳定性门禁：GUI「身份捆绑」开关曾在 v9 cfg 里
            # 存在但此处翻译表丢键 → 引擎恒关（UI 谎报生效）。补透传（默认关不变）。
            "identity_bundle": bool(cfg.get("identity_bundle", False)),
            # [v2.19.7 安全·扫描发现] 打码密钥此前**不在翻译表**：GUI 三个密钥框能填能存，
            # 引擎侧 crawler 读到的恒是默认空 dict → SolverEngine 的 2captcha/capsolver/
            # anticaptcha 分支永远无密钥（UI 死链）。这里经 DPAPI 密文读取并透传。
            "captcha_api_keys": load_captcha_keys(cfg),
        }
        # [v2.19.8 修复] 单页看门狗：GUI 的「高级键」page_timeout（手写在 launcher_config.json）
        # 此前同样**到不了引擎**（翻译表缺键 + crawl() 的 gcfg 键表也缺）→ 恒用默认 300s。
        # 只在配置文件真的写了这个键时透传，保持 DEFAULT_GLOBAL 是唯一默认源。
        _pt = cfg.get("page_timeout", None)
        if _pt is not None:
            try:
                engine_cfg["page_timeout"] = max(0, int(_pt))
            except (TypeError, ValueError):
                self.log_line.emit(f"page_timeout 非法值已忽略: {_pt!r}", "warn")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        # 双路捕获：print（进度行）+ logging（引擎日志）
        out = _LineStream(self.log_line)
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout = out
        sys.stderr = out
        lh = _QtLogHandler(self.log_line)
        lh.setFormatter(logging.Formatter("%(message)s"))
        # [v2.19 安全 P0] GUI 日志 handler 同样挂脱敏 Filter（原实现只挂 root logger，
        # 对子 logger 无效——引擎日志里的 token/API Key 形态会原样进界面与日志）
        try:
            from kiana_vnext_plus.config import attach_log_sanitizer
            attach_log_sanitizer(lh)
        except Exception:
            pass
        logging.getLogger().addHandler(lh)
        rc = 0
        try:
            self._crawler = loop.run_until_complete(
                run_crawler.crawl(urls, engine_cfg, on_engine=lambda c: setattr(self, "_engine_ref", c)))
        except asyncio.CancelledError:
            rc = 1
        except Exception as e:
            import traceback
            # [v2.19.7 安全·扫描发现] 这条路径**不过 logging**（直接 emit Qt 信号）→
            # 日志 Filter 覆盖不到；异常串里常带完整请求 URL（含 ?token=/api_key=）。
            # 在出口处显式脱敏（signature 见 kiana_vnext_plus.sanitizer）。
            self.log_line.emit(_scrub(f"引擎错误: {e}"), "err")
            self.log_line.emit(_scrub(traceback.format_exc()[-500:]), "err")
            rc = 2
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr
            logging.getLogger().removeHandler(lh)
            self._loop = None
            self._crawler = None
            self._thread = None
            self.finished.emit(rc)

    def stop(self):
        """软停止：置 _should_stop（引擎循环自然退出），超时兜底取消任务
        [FIXED & MODIFIED] v2.11 停止断链修复：原 self._crawler 在 run_until_complete
        返回后才赋值——运行期恒 None，「停止」从未走过软停止（一直暴力取消全部任务）。
        现用 on_engine 回调交出的运行中引用。"""
        c = self._engine_ref
        if c is not None:
            c._should_stop = True
            self.log_line.emit("── 已请求停止，等待引擎退出 ──", "warn")
        elif self._loop is not None:
            # 引擎尚未创建 crawler：取消任务兜底
            try:
                self._loop.call_soon_threadsafe(self._cancel_all)
            except Exception:
                pass

    def pause(self):
        """[FIXED & MODIFIED] v2.11 暂停：主循环停止取批（已入队任务处理完为止）"""
        c = self._engine_ref
        if c is not None:
            c.pause()
            self.log_line.emit("── 已暂停（点「继续」恢复抓取） ──", "warn")

    def resume(self):
        """[FIXED & MODIFIED] v2.11 继续"""
        c = self._engine_ref
        if c is not None:
            c.resume()
            self.log_line.emit("── 已继续 ──", "ok")

    @property
    def paused(self) -> bool:
        c = self._engine_ref
        return bool(c is not None and getattr(c, "paused", False))

    def _cancel_all(self):
        import asyncio
        for t in asyncio.all_tasks(self._loop):
            t.cancel()

# ══════════════════════════════════════════════════════════════
# 三、可复用组件
# ══════════════════════════════════════════════════════════════
def make_card(title, sub="", parent=None):
    """卡片：radius/lg + 玻璃底 + 分层阴影"""
    card = QFrame(parent)
    card.setObjectName("Card")
    lay = QVBoxLayout(card)
    lay.setContentsMargins(20, 16, 20, 16)
    lay.setSpacing(8)
    if title:
        t = QLabel(title)
        t.setObjectName("CardTitle")
        lay.addWidget(t)
    if sub:
        s = QLabel(sub)
        s.setObjectName("CardSub")
        lay.addWidget(s)
    eff = QGraphicsDropShadowEffect(card)
    eff.setBlurRadius(24)
    eff.setOffset(0, 4)
    eff.setColor(QColor(0, 0, 0, 60))
    card.setGraphicsEffect(eff)
    return card, lay

def make_stat_card(num, label):
    card, lay = make_card("", "")
    n = QLabel(num)
    n.setObjectName("StatNum")
    l = QLabel(label)
    l.setObjectName("StatLabel")
    lay.addWidget(n)
    lay.addWidget(l)
    return card

# ══════════════════════════════════════════════════════════════
# 四、主窗口
# ══════════════════════════════════════════════════════════════
class KianaV8(QMainWindow):
    def __init__(self, hidden=False):
        super().__init__()
        self.setWindowTitle("Kiana Vnext Plus")
        self.resize(1080, 720)
        self.setMinimumSize(860, 600)
        self.setObjectName("MainWindow")
        self.config = self._load_config()
        self.theme = self.config.get("theme", "dark")          # light/dark/system
        self.accent_name = self.config.get("accent", "蓝")
        self._hidden = hidden
        self._drawer_open = False
        self._toasts = []
        # 令牌 + 样式
        self.T = self._tokens()
        self.setStyleSheet(build_qss(self.T))
        self._build_ui()
        # 引擎桥接
        self.engine = EngineBridge(self)
        self.engine.log_line.connect(self._on_log)
        self.engine.finished.connect(self._on_finished)
        self.engine.started.connect(self._on_started)
        # 日志过滤默认全开
        self._log_filter = {"info": True, "ok": True, "warn": True, "err": True}
        self._log_lines = []
        if hidden:
            self.hide()

    # ── 令牌 ──
    def _tokens(self):
        T = dict(TOKENS_DARK if self.theme != "light" else TOKENS_LIGHT)
        acc = ACCENT_VARIANTS.get(self.accent_name, ACCENT_VARIANTS["蓝"])
        T.update(acc)
        return T

    def _load_config(self):
        try:
            if CONFIG_FILE.exists():
                d = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                d.pop("bg_path", None)  # 背景图功能已移除，清理旧键
                d.pop("glass_wp", None)  # [FIXED & MODIFIED] 毛玻璃通透度已移除
                d.pop("font_family", None)  # [FIXED & MODIFIED] 字体自定义已移除
                # [v2.16.1] LLM 恢复接入（默认关）——不再清洗 llm_* 键；仅清理"历史
                # 非默认键"（旧版 llm_client 曾用别的键名，现统一 4 键）
                d.pop("llm_api_key", None)
                d.pop("llm_format", None)
                d.pop("llm_timeout", None)
                # [FIXED & MODIFIED] v2.10.5c 旧配置键迁移：filter_text 已废弃 → filter_words
                if "filter_words" not in d and d.get("filter_text"):
                    d["filter_words"] = d.pop("filter_text")
                return d
        except Exception:
            pass
        return {}

    def _save_config(self):
        try:
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(json.dumps(self.config, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    # ── UI 构建 ──
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        # 左导航
        root.addWidget(self._build_nav())
        # 主区
        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_home_page())   # 0 首页
        self.stack.addWidget(self._build_log_page())    # 1 日志
        self.stack.addWidget(self._build_data_page())   # 2 数据
        self.stack.addWidget(self._build_tasks_page())  # 3 任务（v2.11 任务中心）
        root.addWidget(self.stack, 1)
        # 右抽屉（初始隐藏，从右滑入）
        self.drawer = self._build_drawer()
        self.drawer.setParent(central)
        self.drawer.hide()
        self._nav_to("home")

    def _build_nav(self):
        nav = QFrame()
        nav.setObjectName("NavBar")
        nav.setFixedWidth(64)
        lay = QVBoxLayout(nav)
        lay.setContentsMargins(8, 12, 8, 12)
        lay.setSpacing(4)
        self.nav_btns = []
        for key, label in [("home", "首页"), ("log", "日志"), ("data", "数据"), ("tasks", "任务")]:
            b = QToolButton()
            b.setObjectName("NavBtn")
            b.setText(label)
            b.setCheckable(True)
            b.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            b.setFixedHeight(52)
            b.clicked.connect(lambda _=False, k=key: self._nav_to(k))
            lay.addWidget(b)
            self.nav_btns.append(b)
        lay.addStretch(1)
        # 底部：主题切换 + 设置
        self.theme_btn = QToolButton()
        self.theme_btn.setObjectName("NavBtn")
        self.theme_btn.setText("☾" if self.theme == "light" else "☀")
        self.theme_btn.setToolTip("切换主题")
        self.theme_btn.clicked.connect(self._toggle_theme)
        lay.addWidget(self.theme_btn)
        self.gear_btn = QToolButton()
        self.gear_btn.setObjectName("NavBtn")
        self.gear_btn.setText("⚙")
        self.gear_btn.setToolTip("设置")
        self.gear_btn.clicked.connect(self._toggle_drawer)
        lay.addWidget(self.gear_btn)
        return nav

    def _nav_to(self, key):
        idx = {"home": 0, "log": 1, "data": 2, "tasks": 3}[key]
        self.stack.setCurrentIndex(idx)
        for i, b in enumerate(self.nav_btns):
            b.setChecked(i == idx)
        if key == "tasks":
            self._refresh_tasks()

    # ── 任务页（v2.11 任务中心：历史任务列表 + 产物直达/删除）──
    def _build_tasks_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(12)
        card, cl = make_card("历史任务", "保留 7 天 / 50GB 自动清理 · 双击打开产物目录")
        lay.addWidget(card)
        tool = QHBoxLayout()
        tool.setSpacing(8)
        refresh_btn = QPushButton("刷新")
        refresh_btn.setObjectName("GhostBtn")
        refresh_btn.clicked.connect(self._refresh_tasks)
        tool.addWidget(refresh_btn)
        tool.addStretch(1)
        cl.addLayout(tool)
        self.tasks_list = QPlainTextEdit()
        self.tasks_list.setObjectName("LogView")
        self.tasks_list.setReadOnly(True)
        cl.addWidget(self.tasks_list)
        row = QHBoxLayout()
        row.addStretch(1)
        open_btn = QPushButton("打开选中任务产物")
        open_btn.setObjectName("PrimaryBtn")
        open_btn.clicked.connect(self._open_selected_task)
        row.addWidget(open_btn)
        del_btn = QPushButton("删除选中任务")
        del_btn.clicked.connect(self._delete_selected_task)
        row.addWidget(del_btn)
        cl.addLayout(row)
        return page

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
            self.tasks_list.setPlainText("\n".join(items) or "暂无历史任务（点「开始爬取」创建）")
        except Exception as e:
            self.tasks_list.setPlainText(f"读取失败: {e}")

    def _selected_task_dir(self):
        name = self.tasks_list.textCursor().selectedText() or ""
        line = (name.strip().splitlines() or [""])[0].split("|")[0].strip()
        if not line.startswith("cli_"):
            return None
        d = self._tasks_base() / line
        return d if d.is_dir() else None

    def _open_selected_task(self):
        d = self._selected_task_dir()
        if not d:
            self._toast("请先在列表中点击选中一个任务行", self.T["warning"])
            return
        export = d / "export"
        subprocess.Popen(["explorer.exe", str(export if export.exists() else d)])

    def _delete_selected_task(self):
        d = self._selected_task_dir()
        if not d:
            self._toast("请先在列表中点击选中一个任务行", self.T["warning"])
            return
        import shutil as _sh
        _sh.rmtree(d, ignore_errors=True)
        self._toast(f"已删除: {d.name}", self.T["success"])
        self._refresh_tasks()

    # ── 首页：任务控制 + 状态 ──
    def _build_home_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(20)
        # [FIXED & MODIFIED] v2.10.4 隐身状态卡：引擎已移除代理自动探测（端口 7897 误报坏代理）
        # → 一律直连模式；作者要隐身自行开全局代理（TUN），引擎无需感知
        stealth_card, sl = make_card("隐身状态", "引擎直连模式（隐身请自行开启全局代理）")
        lay.addWidget(stealth_card)
        srow = QHBoxLayout()
        srow.setSpacing(16)
        self.stealth_badge = QLabel("● 直连")
        self.stealth_badge.setStyleSheet(
            "QLabel { background: #6b4a1f; color: #f5e5d9; border-radius: 10px;"
            " padding: 4px 14px; font-size: 12px; font-weight: 600; }")
        srow.addWidget(self.stealth_badge)
        self.stealth_ip = QLabel("引擎不再自动走代理；可在代理软件中开启全局路由")
        self.stealth_ip.setObjectName("SectionTitle")
        srow.addWidget(self.stealth_ip, 1)
        sl.addLayout(srow)
        # [FIXED & MODIFIED] v2.11 隐私状态行（密钥加密/cookies 三站登录态——自检后刷新）
        self.privacy_status = QLabel("隐私状态: 自检待运行…")
        self.privacy_status.setObjectName("SectionTitle")
        prow = QHBoxLayout()
        prow.addWidget(self.privacy_status, 1)
        sl.addLayout(prow)
        # 任务控制卡
        card, cl = make_card("任务控制", "输入网址，配置抓取参数，启动爬虫")
        lay.addWidget(card)
        # URL（多行，按行拆分）
        url_lbl = QLabel("目标网址（每行一个）")
        url_lbl.setObjectName("SectionTitle")
        cl.addWidget(url_lbl)
        self.url_edit = QPlainTextEdit()
        self.url_edit.setPlaceholderText("https://example.com\nhttps://example.org/page1")
        self.url_edit.setFixedHeight(64)
        cl.addWidget(self.url_edit)
        # 参数行
        row = QHBoxLayout()
        row.setSpacing(16)
        row.addWidget(QLabel("深度"))
        self.depth_spin = QSpinBox()
        self.depth_spin.setRange(1, 50)
        self.depth_spin.setValue(int(self.config.get("depth", 5)))
        row.addWidget(self.depth_spin)
        row.addWidget(QLabel("页数"))
        self.pages_spin = QSpinBox()
        self.pages_spin.setRange(1, 100000)
        self.pages_spin.setValue(int(self.config.get("pages", 500)))
        row.addWidget(self.pages_spin)
        row.addWidget(QLabel("输出目录"))
        self.out_edit = QLineEdit(str(self.config.get("outdir", "")))
        self.out_edit.setPlaceholderText("留空=默认 Downloads\\KianaVnextPlus")
        row.addWidget(self.out_edit, 1)
        browse = QPushButton("浏览")
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        cl.addLayout(row)
        # [FIXED & MODIFIED] v2.5.1 B站会员 Cookie 文件——首页直接可设置（原仅抽屉内，作者反馈找不到）
        crow = QHBoxLayout()
        crow.setSpacing(16)
        crow.addWidget(QLabel("B站会员Cookie"))
        self.cookie_edit = QLineEdit(str(self.config.get("cookie_file", "")))
        self.cookie_edit.setPlaceholderText("留空=自动用默认位置（LOCALAPPDATA\\KianaVnextPlus\\cookies.txt）；多个 cookie 文件用分号分隔")
        self.cookie_edit.textChanged.connect(self._on_cookie_path)
        crow.addWidget(self.cookie_edit, 1)
        cpick = QPushButton("选择文件…")
        cpick.setObjectName("PrimaryBtn")
        cpick.clicked.connect(self._pick_cookie_file)
        crow.addWidget(cpick)
        cclear = QPushButton("清空")
        cclear.clicked.connect(self._clear_cookie_file)
        crow.addWidget(cclear)
        cl.addLayout(crow)
        # [FIXED & MODIFIED] 内容过滤白名单输入（tkinter→PySide6 迁移缺口补回）
        frow = QHBoxLayout()
        frow.setSpacing(16)
        frow.addWidget(QLabel("过滤白名单"))
        self.filter_edit = QLineEdit(str(self.config.get("filter_text", "")))
        self.filter_edit.setPlaceholderText("逗号分隔关键词，只爬包含关键词的页面（留空=不过滤）")
        frow.addWidget(self.filter_edit, 1)
        cl.addLayout(frow)
        # 勾选组
        checks = QHBoxLayout()
        checks.setSpacing(20)
        self.chk_video = QCheckBox("下载视频")
        self.chk_image = QCheckBox("下载图片")
        self.chk_audio = QCheckBox("下载音频")
        self.chk_sanitize = QCheckBox("内容脱敏")
        # [FIXED & MODIFIED] 视频/图片默认勾选（作者期望给链接就下载；原默认 False 导致"只有标题"）
        self.chk_video.setChecked(bool(self.config.get("dl_video", True)))
        self.chk_image.setChecked(bool(self.config.get("dl_image", True)))
        self.chk_audio.setChecked(bool(self.config.get("dl_audio", False)))
        self.chk_sanitize.setChecked(bool(self.config.get("sanitize", True)))
        for c in (self.chk_video, self.chk_image, self.chk_audio, self.chk_sanitize):
            checks.addWidget(c)
        checks.addStretch(1)
        cl.addLayout(checks)
        # 操作按钮
        btns = QHBoxLayout()
        btns.setSpacing(12)
        self.start_btn = QPushButton("开始爬取")
        self.start_btn.setObjectName("PrimaryBtn")
        self.start_btn.setFixedHeight(36)
        self.start_btn.clicked.connect(self._start)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setObjectName("DangerBtn")
        self.stop_btn.setFixedHeight(36)
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop)
        # [FIXED & MODIFIED] v2.11 任务中心：暂停/继续 + 失败页重试
        self.pause_btn = QPushButton("暂停")
        self.pause_btn.setFixedHeight(36)
        self.pause_btn.setEnabled(False)
        self.pause_btn.clicked.connect(self._toggle_pause)
        self.retry_btn = QPushButton("重试失败页")
        self.retry_btn.setFixedHeight(36)
        self.retry_btn.clicked.connect(self._retry_dead)
        btns.addWidget(self.start_btn)
        btns.addWidget(self.pause_btn)
        btns.addWidget(self.stop_btn)
        btns.addWidget(self.retry_btn)
        # [FIXED & MODIFIED] v2.5.7 打开输出目录按钮（产物在 export/域名/ 子目录，随时可直达）
        self.open_btn = QPushButton("打开输出目录")
        self.open_btn.setFixedHeight(36)
        self.open_btn.clicked.connect(self._open_outdir)
        btns.addWidget(self.open_btn)
        btns.addStretch(1)
        cl.addLayout(btns)
        # 状态 + 进度
        status_row = QHBoxLayout()
        self.badge = QLabel("空闲")
        self.badge.setObjectName("StatusBadge")
        self.badge.setStyleSheet(f"#StatusBadge {{ background: {self.T['bg/sunken']}; color: {self.T['text/secondary']}; }}")
        status_row.addWidget(self.badge)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        status_row.addWidget(self.progress, 1)
        cl.addLayout(status_row)
        # 数据统计卡
        stats = QHBoxLayout()
        stats.setSpacing(16)
        self.stat_done = make_stat_card("0", "已抓取")
        self.stat_fail = make_stat_card("0", "失败")
        self.stat_speed = make_stat_card("0", "页面/秒")
        for c in (self.stat_done, self.stat_fail, self.stat_speed):
            stats.addWidget(c)
        lay.addLayout(stats)
        lay.addStretch(1)
        return page

    # ── 日志页 ──
    def _build_log_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(12)
        card, cl = make_card("运行日志", "分级着色 · 等宽字体 · 实时滚动")
        lay.addWidget(card)
        tool = QHBoxLayout()
        tool.setSpacing(8)
        self.filter_combo = QComboBox()
        self.filter_combo.addItems(["全部", "INFO", "成功", "警告", "错误"])
        self.filter_combo.currentIndexChanged.connect(self._apply_log_filter)
        tool.addWidget(self.filter_combo)
        tool.addStretch(1)
        clear_btn = QPushButton("清空")
        clear_btn.setObjectName("GhostBtn")
        clear_btn.clicked.connect(self._clear_log)
        tool.addWidget(clear_btn)
        cl.addLayout(tool)
        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("LogView")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        cl.addWidget(self.log_view)
        return page

    # ── 数据页 ──
    def _build_data_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(20)
        card, cl = make_card("数据统计", "本次运行数据汇总")
        lay.addWidget(card)
        grid = QHBoxLayout()
        grid.setSpacing(16)
        self.data_done = make_stat_card("0", "已抓取页面")
        self.data_fail = make_stat_card("0", "失败页面")
        self.data_pend = make_stat_card("0", "待处理")
        self.data_total = make_stat_card("0", "总计")
        for c in (self.data_done, self.data_fail, self.data_pend, self.data_total):
            grid.addWidget(c)
        cl.addLayout(grid)
        lay.addStretch(1)
        return page

    # ── 右抽屉：设置 ──
    def _build_drawer(self):
        d = QFrame()
        d.setObjectName("Drawer")
        d.setFixedWidth(320)
        lay = QVBoxLayout(d)
        lay.setContentsMargins(20, 20, 20, 20)
        lay.setSpacing(12)
        t = QLabel("设置")
        t.setObjectName("DrawerTitle")
        lay.addWidget(t)
        # 主题
        sec = QLabel("外观")
        sec.setObjectName("SectionTitle")
        lay.addWidget(sec)
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["深色", "亮色"])
        self.theme_combo.setCurrentIndex(0 if self.theme != "light" else 1)
        self.theme_combo.currentIndexChanged.connect(lambda i: self._set_theme("light" if i == 1 else "dark"))
        lay.addWidget(self.theme_combo)
        self.accent_combo = QComboBox()
        self.accent_combo.addItems(list(ACCENT_VARIANTS.keys()))
        self.accent_combo.setCurrentText(self.accent_name)
        self.accent_combo.currentTextChanged.connect(self._set_accent)
        lay.addWidget(self.accent_combo)
        # [FIXED & MODIFIED] v2.5.3 B站会员 Cookie（设置抽屉 + 首页双入口，作者明确要求设置内可配）
        sec = QLabel("B站会员")
        sec.setObjectName("SectionTitle")
        lay.addWidget(sec)
        self.cookie_edit_d = QLineEdit(str(self.config.get("cookie_file", "")))
        self.cookie_edit_d.setPlaceholderText("留空=自动用默认位置\n(LOCALAPPDATA\\KianaVnextPlus\\cookies.txt)")
        self.cookie_edit_d.textChanged.connect(self._on_cookie_path_d)
        lay.addWidget(self.cookie_edit_d)
        btn_row = QHBoxLayout()
        pick_btn = QPushButton("选择文件…")
        pick_btn.setObjectName("PrimaryBtn")
        pick_btn.clicked.connect(self._pick_cookie_file_d)
        btn_row.addWidget(pick_btn)
        clear_btn = QPushButton("清空")
        clear_btn.clicked.connect(self._clear_cookie_file_d)
        btn_row.addWidget(clear_btn)
        lay.addLayout(btn_row)
        lay.addStretch(1)
        return d

    def _pick_cookie_file_d(self):
        """设置抽屉：选择 cookies.txt（与首页同步）"""
        path, _ = QFileDialog.getOpenFileName(self, "选择 Cookie 文件 (cookies.txt)",
                                              str(Path.home() / "Downloads"),
                                              "Cookie 文件 (*.txt);;所有文件 (*.*)")
        if path:
            self.cookie_edit_d.setText(path)
            self.config["cookie_file"] = path
            self._save_config()
            if hasattr(self, "cookie_edit"):
                self.cookie_edit.setText(path)  # 同步首页
            self._toast(f"Cookie 文件已设置: {Path(path).name}")

    def _clear_cookie_file_d(self):
        self.cookie_edit_d.clear()
        self.config.pop("cookie_file", None)
        self._save_config()
        if hasattr(self, "cookie_edit"):
            self.cookie_edit.clear()  # 同步首页
        self._toast("Cookie 文件已清空（将使用默认位置）")

    def _on_cookie_path_d(self, text):
        if text.strip():
            self.config["cookie_file"] = text.strip()
            self._save_config()
            if hasattr(self, "cookie_edit"):
                self.cookie_edit.setText(text.strip())  # 同步首页

    def _pick_cookie_file(self):
        """[FIXED & MODIFIED] 选择 cookies.txt（B站会员身份）"""
        path, _ = QFileDialog.getOpenFileName(self, "选择 Cookie 文件 (cookies.txt)",
                                              str(Path.home() / "Downloads"),
                                              "Cookie 文件 (*.txt);;所有文件 (*.*)")
        if path:
            self.cookie_edit.setText(path)
            self.config["cookie_file"] = path
            self._save_config()
            self._toast(f"Cookie 文件已设置: {Path(path).name}")

    def _clear_cookie_file(self):
        self.cookie_edit.clear()
        self.config.pop("cookie_file", None)
        self._save_config()
        self._toast("Cookie 文件已清空（将使用默认位置）")

    def _on_cookie_path(self, text):
        if text.strip():
            self.config["cookie_file"] = text.strip()
            self._save_config()

    # ── 抽屉动效 ──
    def _toggle_drawer(self):
        if self._drawer_open:
            self._close_drawer()
        else:
            self._open_drawer()

    def _open_drawer(self):
        self._drawer_open = True
        self.drawer.show()
        self.drawer.raise_()
        geo = self.drawer.geometry()
        self.drawer.setGeometry(self.width(), 0, 320, self.height())
        self._anim = QPropertyAnimation(self.drawer, b"geometry")
        self._anim.setDuration(220)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.setStartValue(QRect(self.width(), 0, 320, self.height()))
        self._anim.setEndValue(QRect(self.width() - 320, 0, 320, self.height()))
        self._anim.start()

    def _close_drawer(self):
        self._drawer_open = False
        self._anim = QPropertyAnimation(self.drawer, b"geometry")
        self._anim.setDuration(220)
        self._anim.setEasingCurve(QEasingCurve.InCubic)
        self._anim.setStartValue(self.drawer.geometry())
        self._anim.setEndValue(QRect(self.width(), 0, 320, self.height()))
        self._anim.finished.connect(self.drawer.hide)
        self._anim.start()

    # ── Toast ──
    def _toast(self, msg, color=None):
        t = QLabel(msg)
        t.setObjectName("Toast")
        if color:
            t.setStyleSheet(f"#Toast {{ background: {self.T['bg/elevated']}; border-left: 3px solid {color}; border-radius: 10px; padding: 10px 14px; }}")
        t.setParent(self)
        t.adjustSize()
        n = len(self._toasts)
        t.move(self.width() - t.width() - 24, self.height() - 60 - n * 52)
        t.show()
        t.raise_()
        self._toasts.append(t)
        QTimer.singleShot(3000, lambda: (t.hide(), t.deleteLater()))
        QTimer.singleShot(3100, lambda: self._toasts.__delitem__(0) if self._toasts else None)

    # ── 主题 / 强调色 / 字体 ──
    def _toggle_theme(self):
        self._set_theme("light" if self.theme != "light" else "dark")

    def _set_theme(self, theme):
        self.theme = theme
        self.config["theme"] = theme
        self.theme_btn.setText("☾" if theme == "light" else "☀")
        self._apply_theme()

    def _set_accent(self, name):
        self.accent_name = name
        self.config["accent"] = name
        self._apply_theme()

    def _apply_theme(self):
        self.T = self._tokens()
        self.setStyleSheet(build_qss(self.T))
        self._save_config()

    # ── 爬虫控制 ──
    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if d:
            self.out_edit.setText(d)

    def _check_cookies(self):
        """[FIXED & MODIFIED] v2.10.5c cookies 登录态自检（弹 toast 提示）
        [FIXED & MODIFIED] v2.11 读 GUI 自填路径（原只读默认位置——自填路径自检盲区）"""
        try:
            gui_cookie = self.cookie_edit.text().strip()
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
            for site in r.get("sites", []):
                if site["site"] == "B站" and site.get("health") is not None:
                    h = site["health"]
                    self._toast(f"B站登录态: {'✅ 有效' if h['ok'] else '⚠️ ' + h['msg']}",
                                self.T["success"] if h["ok"] else self.T["warning"])
                elif site["site"] == "抖音" and not site["has_cookie"]:
                    self._toast("抖音 cookies 未配置（抖音视频解析可能受限）", self.T["warning"])
                elif site["site"] == "贴吧" and not site["has_cookie"]:
                    self._toast("贴吧 cookies 未配置（贴吧采集可能受限）", self.T["warning"])
            # [FIXED & MODIFIED] v2.11 隐私状态行：密钥加密状态 + 三站 cookies 概览
            try:
                from pathlib import Path as _P
                _bin = _P(os.environ.get("LOCALAPPDATA", "")) / "KianaVnextPlus" / "master.key.bin"
                _enc = "🔐 密钥DPAPI加密" if _bin.exists() else "⚠️ 密钥未加密"
                _ck = {s["site"]: bool(s["has_cookie"]) for s in r.get("sites", [])}
                _cstr = " ".join(f"{k}{'✅' if v else '—'}" for k, v in _ck.items()) or "无 cookies"
                self.privacy_status.setText(f"隐私状态: {_enc} · Cookies: {_cstr}")
            except Exception:
                pass
        except Exception:
            pass  # 自检失败不影响 GUI

    def _start(self):
        # [FIXED & MODIFIED] v2.10.5c cookies 登录态自检（爬取开始前提示密钥是否有效）
        self._check_cookies()
        # [FIXED & MODIFIED] v2.6.0 URL 清洗：作者常从微信/文档复制分享文本整段粘贴
        # （"【标题】https://xxx?tk=..." 混合行）——正则提取每行的 http(s) URL，剥掉中文/标点尾巴
        import re as _re
        urls = []
        for line in self.url_edit.toPlainText().splitlines():
            line = line.strip()
            if not line:
                continue
            m = _re.search(r'https?://[^\s\u4e00-\u9fff]+', line)
            if m:
                u = m.group(0).rstrip('.,;:!?。，；：！？)】》"\'' )
                urls.append(u)
            elif '://' in line:
                urls.append(line)
        if not urls:
            self._toast("请先输入目标网址", self.T["danger"])
            return
        cfg = {
            "depth": self.depth_spin.value(), "pages": self.pages_spin.value(),
            "outdir": self.out_edit.text().strip(),
            "dl_video": self.chk_video.isChecked(), "dl_image": self.chk_image.isChecked(),
            "dl_audio": self.chk_audio.isChecked(), "no_sanitize": not self.chk_sanitize.isChecked(),
            "filter_words": self.filter_edit.text().strip(),  # [FIXED & MODIFIED] 过滤词 UI 接入
            "cookie_file": self.cookie_edit.text().strip(),  # [FIXED & MODIFIED] v2.11 GUI 自填 cookies 真正传入引擎
        }
        self.config.update({
            "depth": cfg["depth"], "pages": cfg["pages"], "outdir": cfg["outdir"],
            "dl_video": cfg["dl_video"], "dl_image": cfg["dl_image"], "dl_audio": cfg["dl_audio"],
            "sanitize": self.chk_sanitize.isChecked(),
            # [FIXED & MODIFIED] v2.10.5c launcher 键名统一：持久化用 sanitize/filter_words
            # （与引擎 cfg 一致；原 filter_text/no_sanitize 分裂导致旧配置首启动丢开关）
            "filter_words": cfg["filter_words"],
        })
        self._save_config()
        self._log("── 启动爬虫 ──", "title")
        self.engine.start(urls, cfg)

    def _stop(self):
        self.engine.stop()
        self._log("── 已请求停止 ──", "warn")

    # ── 引擎信号 ──
    def _on_started(self):
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.pause_btn.setEnabled(True)
        self.pause_btn.setText("暂停")
        self.badge.setText("运行中")
        self.badge.setStyleSheet(f"#StatusBadge {{ background: {self.T['success']}; color: white; }}")

    def _open_outdir(self):
        """[FIXED & MODIFIED] v2.5.7 打开输出目录（最新 cli_xxx/export 或根目录）"""
        try:
            base = Path(str(self.config.get("outdir") or Path.home() / "Downloads" / "KianaVnextPlus"))
            base.mkdir(parents=True, exist_ok=True)
            projs = sorted(base.glob("cli_*"), key=lambda p: p.stat().st_mtime, reverse=True)
            target = projs[0] / "export" if projs else base
            if target.exists():
                subprocess.Popen(["explorer.exe", str(target)])
                self._toast(f"已打开: {target}")
            else:
                self._toast("输出目录不存在")
        except Exception as e:
            logger.debug(f"open outdir btn: {e}")

    def _toggle_pause(self):
        """[FIXED & MODIFIED] v2.11 暂停/继续 toggle"""
        if self.engine.paused:
            self.engine.resume()
            self.pause_btn.setText("暂停")
        else:
            self.engine.pause()
            self.pause_btn.setText("继续")
        # 按钮态在下一条日志/状态刷新时自然对齐

    def _retry_dead(self):
        """[FIXED & MODIFIED] v2.11 失败页重试：最新任务目录 frontier.db 的 dead → pending
        （下次以同 URL 启动即续爬；独立于引擎是否在跑）"""
        try:
            import sqlite3 as _sql
            base = Path(str(self.config.get("outdir") or Path.home() / "Downloads" / "KianaVnextPlus"))
            projs = sorted(base.glob("cli_*"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not projs:
                self._toast("未找到历史任务目录", self.T["warning"])
                return
            db = projs[0] / "frontier.db"
            if not db.exists():
                self._toast("最新任务无 frontier.db", self.T["warning"])
                return
            con = _sql.connect(str(db))
            cur = con.execute(
                "UPDATE frontier SET status='pending', retry_count=0, scheduled_at=? WHERE status='dead'",
                (time.time(),))
            con.commit()
            n = cur.rowcount
            con.close()
            if n > 0:
                self._toast(f"已重置 {n} 个失败页 → 下次启动同 URL 自动续爬", self.T["success"])
            else:
                self._toast("没有可重试的失败页", self.T["warning"])
        except Exception as e:
            self._toast(f"重试失败: {e}", self.T["danger"])

    def _on_finished(self, rc):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.pause_btn.setEnabled(False)
        self.pause_btn.setText("暂停")
        self.badge.setText("空闲")
        self.badge.setStyleSheet(f"#StatusBadge {{ background: {self.T['bg/sunken']}; color: {self.T['text/secondary']}; }}")
        self._log(f"── 爬虫结束 (exit={rc}) ──", "ok" if rc == 0 else "err")
        # [FIXED & MODIFIED] v2.11 退出码语义化：rc=2 引擎崩溃给出可重试提示（原一律"爬取异常"）
        if rc == 2:
            self._toast("引擎崩溃 (exit=2)——可修改参数后点「开始爬取」重试", self.T["danger"])
        elif rc == 1:
            self._toast("爬取已停止", self.T["warning"])
        elif rc == 3:
            self._toast("引擎加载失败 (exit=3)——请重装或检查安装包", self.T["danger"])
        else:
            self._toast("爬取完成", self.T["success"])
        # [FIXED & MODIFIED] v2.5.9 完成后自动打开产物目录——作者反馈"空文件夹"根因：
        # 产物按域名分类在 export/域名/ 子目录，顶层无文件被误判为爬取失败。
        # [FIXED & MODIFIED] v2.5.9 延迟 8s 打开：视频 worker 异步收尾，立即打开 videos 可能还空
        if rc == 0:
            def _delayed_open():
                try:
                    base = Path(str(self.config.get("outdir") or Path.home() / "Downloads" / "KianaVnextPlus"))
                    projs = sorted(base.glob("cli_*"), key=lambda p: p.stat().st_mtime, reverse=True)
                    if projs:
                        export = projs[0] / "export"
                        if export.exists():
                            subprocess.Popen(["explorer.exe", str(export)])
                            self._log(f"产物目录已打开: {export}", "ok")
                except Exception as e:
                    logger.debug(f"open outdir: {e}")
            threading.Timer(8.0, _delayed_open).start()

    def _on_log(self, text, level):
        if level == "info" and ("done=" in text or "进度" in text):
            self._parse_progress(text)
        # [FIXED & MODIFIED] v2.10.4 隐身状态卡解析已移除（引擎不再输出出口 IP/代理模式日志）
        self._log(text, level)

    def _parse_progress(self, text):
        """解析进度行 done=12 fail=0 pend=3 → 更新统计卡
        [FIXED & MODIFIED] 缓存 StatNum 引用（原每行 findChildren 遍历低效 + 死代码）
        [FIXED & MODIFIED] v2.11 数据页 4 卡激活（原 data_done/data_fail/data_pend/data_total
        死控件永不更新）——done/fail/pend/total 同步刷新"""
        import re
        if not hasattr(self, "_stat_labels"):
            self._stat_labels = {}
            for key, card in (("done", self.stat_done), ("fail", self.stat_fail), ("speed", self.stat_speed),
                              ("d_done", self.data_done), ("d_fail", self.data_fail),
                              ("d_pend", self.data_pend), ("d_total", self.data_total)):
                for w in card.findChildren(QLabel):
                    if w.objectName() == "StatNum":
                        self._stat_labels[key] = w
                        break
        m = re.search(r"done=(\d+) fail=(\d+) pend=(\d+)", text)
        if m:
            d, f2, pn = int(m.group(1)), int(m.group(2)), int(m.group(3))
            for key, val in (("done", str(d)), ("d_done", str(d)), ("fail", str(f2)),
                             ("d_fail", str(f2)), ("d_pend", str(pn)), ("d_total", str(d + f2 + pn))):
                if key in self._stat_labels:
                    self._stat_labels[key].setText(val)
            return
        m = re.search(r"done=(\d+)", text)
        if m and "done" in self._stat_labels:
            self._stat_labels["done"].setText(m.group(1))
        m = re.search(r"fail=(\d+)", text)
        if m and "fail" in self._stat_labels:
            self._stat_labels["fail"].setText(m.group(1))
        m = re.search(r"(\d+(?:\.\d+)?)p/s", text)
        if m and "speed" in self._stat_labels:
            self._stat_labels["speed"].setText(m.group(1))
        m = re.search(r"(\d+(?:\.\d+)?)%", text)
        if m:
            try:
                self.progress.setValue(int(float(m.group(1))))
            except Exception:
                pass

    # ── 日志 ──
    def _log(self, text, level="info"):
        self._log_lines.append((text, level))
        if len(self._log_lines) > 5000:
            self._log_lines = self._log_lines[-5000:]
        self._append_log_line(text, level)

    def _append_log_line(self, text, level):
        colors = {"info": self.T["text/secondary"], "ok": self.T["success"],
                  "warn": self.T["warning"], "err": self.T["danger"],
                  "title": self.T["accent"], "ts": self.T["text/disabled"]}
        if not self._log_filter.get(level, True):
            return
        c = colors.get(level, self.T["text/secondary"])
        self.log_view.appendHtml(f'<span style="color:{c};">{text}</span>')
        sb = self.log_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _apply_log_filter(self, idx):
        self._log_filter = {"info": idx in (0, 1), "ok": idx in (0, 2),
                            "warn": idx in (0, 3), "err": idx in (0, 4)}
        self.log_view.clear()
        for t, lv in self._log_lines:
            if self._log_filter.get(lv, True):
                self._append_log_line(t, lv)

    def _clear_log(self):
        self.log_view.clear()
        self._log_lines.clear()

    # ── 关闭 ──
    def closeEvent(self, ev):
        self.engine.stop()
        self._save_config()
        super().closeEvent(ev)

def main():
    import traceback as _tb
    debug_log = None
    if "--debug-log" in sys.argv:
        try:
            dl = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "KianaVnextPlus" / "startup.log"
            dl.parent.mkdir(parents=True, exist_ok=True)
            debug_log = open(dl, "w", encoding="utf-8")
            debug_log.write("startup begin\n"); debug_log.flush()
        except Exception:
            pass
    try:
        app = QApplication(sys.argv)
        if debug_log:
            debug_log.write("QApplication ok\n"); debug_log.flush()
        app.setApplicationName("Kiana Vnext Plus")
        hidden = "--hidden" in sys.argv
        w = KianaV8(hidden=hidden)
        if debug_log:
            debug_log.write("window created\n"); debug_log.flush()
        if not hidden:
            w.show()
            if debug_log:
                debug_log.write("window shown\n"); debug_log.flush()
        rc = app.exec()
        if debug_log:
            debug_log.write(f"exec ended rc={rc}\n"); debug_log.flush()
        sys.exit(rc)
    except Exception:
        if debug_log:
            debug_log.write(_tb.format_exc()); debug_log.flush()
        raise
    finally:
        if debug_log:
            debug_log.close()

if __name__ == "__main__":
    main()
