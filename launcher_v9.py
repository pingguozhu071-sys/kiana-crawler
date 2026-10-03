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

# [v6] 内置第三方部件（`vendor/`）—— 同 `run_crawler.py`：插在最前面，
# 让 `import camoufox` / `import ddddocr` 优先取随工程打包的副本。
# 源码不入 git 的理由见 `vendor/README.md`。
_VENDOR_DIR = PROJECT_DIR / "vendor"
if _VENDOR_DIR.is_dir():
    sys.path.insert(0, str(_VENDOR_DIR))

from PySide6.QtCore import Qt, QTimer, Signal, QEvent, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QIcon, QColor, QPainter, QPixmap
from PySide6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
                               QPlainTextEdit, QLabel, QLineEdit, QSpinBox, QFileDialog,
                               QGridLayout, QFrame)

from kiana_vnext_plus import wallpaper, config as _kcfg
DATA_ROOT = _kcfg.data_root

# [v7] 「Cookies 管理」的**来源解析 / 增删**全部走引擎包里的唯一实现，
# 界面上**一行解析逻辑都不自己写** —— "界面显示的来源"与"引擎真正读的来源"
# 必须同源，否则就是本工程反复吃亏的"界面改了、引擎没变"。
from kiana_vnext_plus.cookie_utils import (  # noqa: E402
    SOURCE_ORIGIN_LABEL, cookie_paths_add_front, cookie_paths_drop,
    resolve_cookie_sources,
)

# [v2.18.1] worker→主线程投递改用 QApplication.postEvent（文档明确的线程安全 API）。
# 原 SignalInstance.emit 跨 threading.Thread 在部分环境下偶发
# "TypeError: only accepts 0 argument(s)"——heisenbug 不纠缠，事件机制一劳永逸。
_WP_EVT = QEvent.registerEventType()


class _WpReadyEvent(QEvent):
    def __init__(self, kind: str, value):
        super().__init__(QEvent.Type(_WP_EVT))
        self.kind = kind    # "wp"=壁纸渲染完成(value=seq) / "accent"=取色完成(value=hex)
        self.value = value


# [v6+] 「登录并取 cookies」的后台线程走**同一个**投递机制（postEvent），不新开一条通路：
# 一个进程里两套 worker→主线程机制，早晚会有一套没人记得住怎么用。
_LOGIN_EVT = QEvent.registerEventType()


class _LoginEvent(QEvent):
    """登录相关后台线程 → 主线程的单向投递。

    kind:
      "login"        一次取登录态跑完（value = `open_profile_for_login` 的结果字典）
      "privacy"      隐私状态行的文本算好了（value = 文本）
      "cookies_check"「Cookies 管理」的有效性核验结论（value = 文本）
      "priv_scan"    设置页「隐私扫描」的子进程跑完了（value = (文本, kind) 二元组）
      "tasks"        历史任务列表统计完了（value = 要填进列表的文本）
      "retry_dead"   「重试失败页」的 sqlite UPDATE 跑完了（value = (文本, kind)）
      "url_file"     「导入 URL 文件」读盘完成（value = (文本|None, 路径, 错误|"")）

    为什么不带结果之外的任何东西：投递的东西越少，跨线程的"共享可变状态"越少。
    """
    def __init__(self, kind: str, value):
        super().__init__(QEvent.Type(_LOGIN_EVT))
        self.kind = kind
        self.value = value


from qfluentwidgets import (FluentWindow, FluentIcon as FIF, NavigationItemPosition,
                            PrimaryPushButton, PushButton, ToolButton, BodyLabel, TitleLabel,
                            SubtitleLabel, CardWidget, SwitchButton, ComboBox,
                            ProgressBar, InfoBar, InfoBarPosition, setTheme, setThemeColor,
                            Theme, Slider, ScrollArea)

from launcher_v8 import (CONFIG_FILE, EngineBridge, load_captcha_keys,
                         save_captcha_keys, load_llm_key, save_llm_key)

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
# 一键取登录态（首页「登录并取 cookies」按钮背后的一切）
#
# 为什么单独成段、且**一格 Qt 控件都不碰**：本功能最容易出的事故是把
# **"没登录成功"报成"成功"**。`open_profile_for_login` 的 `ok` 语义只是
# "写出了 cookies.txt"（它自己的 docstring 写明：`login_cookies` 只用于提前收工，
# **不是导出闸门**），而未登录也会有匿名 cookie（buvid 之类）⇒ 文件非空、ok=True。
# **只看 ok 就会谎报**。这类判断必须能脱离界面、脱离浏览器被验证，
# 所以判定与措辞都放在这一层：界面只负责"把选择递进来、把结论显示出去"。
# ════════════════════════════════════════════════════════════

def login_site_choices() -> list:
    """站点下拉的选项 —— 名字**只从 `cookie_profile.site_options()` 取**。

    自己编一套中文名 = 把站点登记表复制成两份：登记表将来加站/改名，
    下拉里那个名字就会和引擎真正认的站点对不上（`cookie_armory` 的
    "存进去查不到"就是这么来的）。读不到就返回空表，界面据此禁用按钮并说明。
    """
    try:
        from kiana_vnext_plus import cookie_profile as cp
        return [{"key": o["key"], "site_key": o["site_key"], "label": o["label"]}
                for o in cp.site_options()]
    except Exception:
        return []


def site_display_name(site: str) -> str:
    """站点键 → 中文名（读不到就原样返回输入，**绝不现编一个名字**）。"""
    try:
        from kiana_vnext_plus import cookie_profile as cp
        return str(cp.SITE_PROFILES[cp.resolve_site(site)]["label"])
    except Exception:
        return str(site or "")


def login_site_key_at(choices, index) -> str:
    """下拉索引 → 站点键（**纯查表**；越界/非法一律返回空串 = "选不出来"）。

    下拉项与 `choices` 同源同序（`HomePage.build` 就是按这个顺序 `addItem` 的），
    所以索引就是取键的入口。抽成函数是为了让"越界会怎样"能被单测 ——
    那一步一旦给出空键，调用方就必须**什么都不做**：拿一个错位的站去建目录、写 cookies，
    比不写糟得多（`cookie_profile.resolve_site` 宁可报错也不猜，也是这个道理）。
    """
    try:
        items = list(choices or [])
        # 只认**整数**索引：`int(1.9)` 会悄悄截成 1（= 选中另一个站），
        # 而 `True` 在 Python 里也是 int —— 这两种都得拦掉。
        # Qt 的 `currentIndex()` 本来就只给整数，出现别的形态说明"这个索引不可信"。
        if isinstance(index, bool) or not isinstance(index, int):
            return ""
        if 0 <= index < len(items):
            return str(items[index]["key"])
    except Exception:
        pass
    return ""


def _site_login_cookie_names(site: str) -> tuple:
    """该站登记表里登记的"登录凭据" cookie 名（未登记 → 空元组 = 不猜）。

    只用于**措辞**（"没找到 SESSDATA 这类登录凭据"）与"能不能判定"；
    判定本身不在这里（见 `verify_login_cookie`）。
    """
    try:
        from kiana_vnext_plus import cookie_profile as cp
        meta = cp.SITE_PROFILES.get(cp.resolve_site(site)) or {}
        return tuple(str(x) for x in (meta.get("login_cookies") or ()))
    except Exception:
        return ()


def _looks_like_a_real_credential(row) -> bool:
    """这一行的值看起来"真是一份凭据"么（不是空值、不是登出后的占位）。

    **为什么还要这一层**：登出时服务端发的常是 `SESSDATA=deleted; Expires=<过去>`，
    在浏览器把它彻底删掉之前的那段时间里，只看"名字 + 域"会判成"登录态还在"
    —— 那就把"已登出"报成了"✅ 已就位"。空值同理。
    这一层只会让核验**更容易判失败**（界面会说"是否重试"），方向是安全的：
    宁可让用户重试一次，也不要把登出说成成功。
    """
    return str(row.get("value") or "").strip().lower() not in ("", "deleted")


def verify_login_cookie(site: str, cookies_path) -> bool | None:
    """刚写出的 cookies.txt 里**到底有没有该站的登录凭据**（三态）。

    True  = 有（例：域名沾边的 `SESSDATA`）→ 这时才可以说"登录态已就位"
    False = 文件里有 cookie，但没有该站的登录凭据 → **多半是没登录成功**
    None  = 该站没登记登录凭据名（快手/公众号）或文件不存在 → **不猜**
            （文件在、但读不动时会**抛**异常，由调用方 `CookieLoginBridge._verify_safe`
             转成"无法判定 + 原因" —— 在这里吞掉异常就没人知道为什么判不出来了）

    判定**复用** `cookie_profile._detect_login_cookie`：哪些名字算登录凭据、
    域名要不要沾边，都是登记表里的知识，这里再写一份必然漂移 ——
    "同一能力多份实现"是本工程最稳定的缺陷模式（`cookie_armory:130` 记着血案）。
    本函数只读磁盘，不开浏览器、不改任何文件。
    """
    if not _site_login_cookie_names(site):
        return None                      # 登记表没列 → 不猜（与 cookie_profile 同取向）
    p = Path(str(cookies_path or ""))
    if not p.is_file():
        return None
    from kiana_vnext_plus import cookie_profile as cp
    from kiana_vnext_plus.cookie_utils import parse_netscape_cookies
    rows = parse_netscape_cookies(p.read_text(encoding="utf-8-sig", errors="ignore"))
    if not rows:
        return False
    return bool(cp._detect_login_cookie([r for r in rows if _looks_like_a_real_credential(r)], site))


def merge_cookie_path_value(cur: str, new_path: str) -> str:
    """把新取到的 cookies 路径并进首页 Cookies 框的值：**新值排最前**。

    ⚠️ 顺序不是随手定的：引擎把多份 cookie 合并成一份时是**先到先得**
    （`universal_downloader._ensure_cookie_file`：`if key in seen: continue`），
    所以新 cookies 必须排在旧文件**前面**，才盖得住同名的旧 SESSDATA。

    [v7] 实现**转发**到 `cookie_utils.cookie_paths_add_front`：本函数原先自己写了一遍
    分号拼接 + `parse_cookie_file_list` 去重，而「Cookies 管理」那一侧也要做同一件事。
    同一能力两份实现 = 迟早漂移（本工程最稳定的缺陷模式），故收敛到引擎包那一份。
    名字保留（既有单测与调用方都在用它）。
    """
    return cookie_paths_add_front(cur, new_path)


# ════════════════════════════════════════════════════════════
# [v7] 「Cookies 管理」的纯逻辑层（**不碰 Qt、不开浏览器**，全部可脱离界面单测）
#
# 三条判据都只做一件事：把"界面上看到的"与"引擎真正会读的"钉死成同一件事。
# ════════════════════════════════════════════════════════════

#: 来源种类 → 列表里那一列的短标注。**从引擎包的同一张表取**，不在这儿另编一份。
_ORIGIN_TAG = dict(SOURCE_ORIGIN_LABEL)


def cookie_source_rows(box_value: str) -> list:
    """输入框当前值 → **现在真正生效的每一个 cookies 来源**（逐项，带来源与存在性）。

    直接转发 `cookie_utils.resolve_cookie_sources()`（**传框里的值**，不是读环境变量）：
    两条理由 ——
      ① 优先级链（显式 → 环境单文件 → 默认位置 → 配置档兜底）只有那一份实现；
      ② 框里的值**还没落进环境变量**（env 是 `EngineBridge.start()` 那一刻才写的），
         只读 env 会让界面显示**上一次任务**的 cookies —— 那是假信息，比不显示更糟。

    [v7 修复·**真机语义**] 过滤掉 `profiles/` 下的**聚合产物** `profiles/cookies.all.txt`：
    它是 `tools/cookie_login.py --merge` 为"喂 yt-dlp"而生成的**派生物**，
    `profiles_cookie_files()` 只扫 `profiles/*/cookies.txt`（一级子目录）
    ⇒ **引擎事实上读不到它**。把它列进"生效来源"就是谎报（用户会以为它在起作用）。
    这里按引擎的真实行为过滤，并**如实标注**（不是静默丢弃，见 `_cookies_manager_text`）。
    """
    rows = [dict(r) for r in resolve_cookie_sources(box_value)]
    if not str(box_value or "").strip():
        return [r for r in rows if not _is_merge_artifact(str(r.get("path") or ""))]
    return rows


def _is_merge_artifact(path: str) -> bool:
    """这份文件是不是 `profiles/` 根下的**聚合派生物**（引擎读不到它）。

    判据取自 `cookie_utils.profiles_cookie_files()` 的实现：它只遍历
    `profiles/*/cookies.txt` 这一层。所以"在 profiles 根目录下的 cookies*.txt"
    都不会被引擎自动发现 —— 典型就是 `--merge` 写出的 `cookies.all.txt`。
    """
    try:
        p = Path(str(path or ""))
        return (p.name.lower().startswith("cookies")
                and p.parent.name.lower() == "profiles")
    except Exception:
        return False


def source_paths_of(box_value: str) -> list:
    """框里**用户显式写的**路径列表（保序）。用于"删除后还剩几个"这类判断。"""
    from kiana_vnext_plus.cookie_utils import parse_cookie_file_list
    return parse_cookie_file_list(box_value)


def login_url_usable(url: str) -> tuple:
    """自定义登录页能不能用 → `(bool, 说明)`。判据**照抄** `open_profile_for_login` 的闸。

    为什么在界面上先判一次：那道闸在**后台线程**里跑，拒绝时界面只会看到
    `status="bad_url"`（"拒绝导航"）—— 用户不知道自己敲错了什么。
    这里提前给出**同一判据**的可读说明，属于"提前告知"，不是第二份安全判定：
    真正的闸仍在 `cookie_profile.open_profile_for_login`，**一行都没动**。
    """
    t = str(url or "").strip()
    if not t:
        return True, ""
    ok = (t.startswith("https://") or t.startswith("http://127.0.0.1")
          or t.startswith("http://localhost"))
    if ok:
        return True, ""
    return False, ("自定义登录页只认 https（本机回环 http://127.0.0.1 / http://localhost 例外）"
                   "—— 明文 http 会被拒绝导航，浏览器根本不会打开。")


def site_key_from_login_url(url: str) -> str:
    """自定义登录页 → 站点键；**认不出来返回空串**（= 界面必须什么都不做）。

    站点键决定"cookies 落到哪个配置档目录"，所以宁可不做也不能猜
    （`cookie_profile.resolve_site` 宁可报错也不猜，同一个道理）。

    [v7 已评估的边界] 于是"自定义域名"**必须**命中登记表（如 `space.bilibili.com`
    → bilibili.com）。想登录**任何**任意站点需要"给新站点建配置档目录"的新能力，
    那要动 `cookie_profile` 的键映射，本轮**不做**（宁可少做，不猜）。
    """
    t = str(url or "").strip()
    if not t:
        return ""
    try:
        from kiana_vnext_plus import cookie_profile as cp
        return str(cp.resolve_site(t))
    except Exception:
        return ""


def _redact_path_for_list(text: str) -> str:
    """列表里显示的长路径**只做显示层收敛**，绝不改任何真实值。

    保留盘符/文件名，中间省略 —— 列表要的是"能认出是哪一份"，
    完整路径在下面那行状态里给（见 `_cookies_manager_text`）。
    """
    s = str(text or "")
    if len(s) <= 64:
        return s
    return s[:26] + " … " + s[-34:]


def _verdict(ok: bool, level: str, retry: bool, headline: str, detail: str = "") -> dict:
    """结论字典：能不能用（ok）/ 什么语气（level）/ 值不值得建议重试 / 说什么。"""
    return {"ok": bool(ok), "level": level, "retry": bool(retry),
            "headline": str(headline), "detail": str(detail)}


def summarize_login_result(res: dict, login_cookie: bool | None = None) -> dict:
    """一次取登录态的结果 → 界面要说的结论（`{ok, level, retry, headline, detail}`）。

    **判据只有一条：登录凭据真的在文件里才叫成功**（`login_cookie is True`）。
    文件写出来了但凭据不在 → 一律按失败措辞，并把"文件在哪、有几条"如实带上供排查。
    `login_cookie is None`（该站不判定）→ 只说"无法确认"，**不说"成功"**。

    `ok` 与 `level` 刻意分开：**"无法确认"也把 cookies 回填给引擎**（用户确实登录了
    就可能有效），但**"明确没凭据"绝不回填** —— 那只会把"没登录"伪装成"已配好"。
    """
    res = dict(res or {})
    status = str(res.get("status") or "unknown")
    n = int(res.get("cookie_count") or 0)
    ck = str(res.get("cookies_path") or "")
    site = str(res.get("site") or "")
    label = site_display_name(site) or site or "该站"
    note = str(res.get("note") or "").strip()
    vnote = str(res.get("verify_note") or "").strip()
    names = "、".join(_site_login_cookie_names(site))

    def _detail(*parts) -> str:
        return "；".join(str(x) for x in parts if str(x or "").strip())

    if status == "ok":
        where = f" → {ck}" if ck else ""
        if login_cookie is True:
            return _verdict(True, "success", False,
                            f"✅ {label} cookies 已就位（{n} 条，含登录凭据）{where}",
                            _detail(f"登录凭据：{names}" if names else "", note, vnote))
        if login_cookie is False:
            return _verdict(False, "warning", True,
                            f"⚠️ 写出了 {n} 条 cookie，但**里面没有 {label} 的登录凭据**"
                            f"（{names or '登记表未列'}）—— 多半是没登录成功就关了窗口。是否重试？",
                            _detail(f"文件：{ck}" if ck else "", note, vnote))
        # 到此 = 核验给不出 True/False。**两种原因要说清是哪一种**，不能都念同一句。
        if vnote:
            return _verdict(False, "warning", False,
                            f"⚠️ {label} cookies 已写出（{n} 条）{where}；"
                            f"但**登录凭据核验没能完成**（原因见日志）—— **未自动接给引擎**",
                            _detail(f"文件：{ck}" if ck else "", vnote, note))
        return _verdict(False, "warning", False,
                        f"⚠️ {label} cookies 已写出（{n} 条）{where}；"
                        f"但该站**没登记登录凭据名，无法确认是否已登录** —— **未自动接给引擎**"
                        f"（确认已登录后：清空首页 Cookies 框，引擎会自动发现配置档）",
                        _detail(note, vnote))
    if status == "no_login":
        return _verdict(False, "warning", True,
                        f"⚠️ 没检测到有效登录态：{label} 一条 cookie 都没拿到 —— 是否重试？",
                        _detail(note or "9 成是没登录成功，或登录后没等页面跳转就关了窗口。", vnote))
    if status == "cancelled":
        return _verdict(False, "warning", False,
                        f"⚠️ 已取消（程序退出）：本次没把结果取回来 —— 想用登录态就再点一次"
                        f"（{label} 的配置档里登录态还在）",
                        _detail(note, vnote))
    if status == "no_browser":
        return _verdict(False, "error", False,
                        "❌ 取登录态失败：没装浏览器引擎（patchright / playwright 都 import 不到）",
                        _detail(note or "本工程栈里应当有 patchright；装好再点一次。"))
    if status == "bad_url":
        return _verdict(False, "error", False, "❌ 登录页地址不合法，已拒绝打开浏览器", note)
    if status == "exception":
        return _verdict(False, "error", True,
                        "❌ 取登录态时出错（浏览器或事件循环异常）—— 可重试一次", _detail(note, vnote))
    return _verdict(False, "error", True,
                    f"❌ 取登录态失败（未预期状态：{status}）—— 可重试一次", _detail(note, vnote))


def _default_login_opener(site: str, url=None):
    """默认协程：`cookie_profile.open_profile_for_login`（**有头**，等用户关窗口）。

    **不传 `wait_for_login=True`**：那会在"检测到登录凭据"时自动关掉用户的浏览器，
    而本功能对用户的承诺是"**你关窗口 = 完成**"（与 CLI 默认一致：
    "已经登录过、只想再导一次"的人不该被抢走窗口）。
    **也不传 `timeout_s`**：等多久由用户决定；中途超时导出会让"到底登录成了没有"变含糊。

    [v7] 新增透传 `url`（自定义登录页）：`open_profile_for_login` 本来就收它，
    原先只是没从界面接出来。**闸仍在它那边**（只认 https + 回环 http），
    这里不做第二份安全判定，只是把用户的意图原样递进去。
    """
    from kiana_vnext_plus import cookie_profile as cp
    if url:
        return cp.open_profile_for_login(site, url=str(url))
    return cp.open_profile_for_login(site)


def _cookies_manager_text(rows) -> str:
    """来源列表 → 列表控件的文本（**纯函数**，可脱离 Qt 单测）。

    一行一项，形如：
        `1. ✅ 显式指定      C:\\Users\\…\\bilibili_cookies.txt`
    最前面那个 ✅/⚠️ 是**文件在不在**（不是"登录态有效"—— 那个要联网，
    只能由「检查有效性」在后台算出来，见 `_cookies_health_text`）。
    列表里**绝不显示任何 cookie 值**（路径不是凭据，值才是）。

    [v7 决策] 第一行刻意加一条"先到先得"的说明：引擎合并多份 cookies 时
    同名项**先出现的赢**，而这件事原先在任何界面上都看不到 ——
    用户只能靠"改了顺序结果没变/变了"去猜。
    """
    rows = list(rows or [])
    head = "（按顺序生效：引擎合并多份 cookies 时**同名项先出现的赢**，排前面的压得住后面的）"
    if not rows:
        return head + "\n（没有解析到任何 cookies 来源 —— 也没配、也没发现配置档）"
    lines = [head]
    for i, r in enumerate(rows, 1):
        mark = "✅" if r.get("exists") else "⚠️"
        if not r.get("exists"):
            mark += "文件不存在"
        else:
            mark += "文件在"
        size = int(r.get("size") or 0)
        size_txt = f"{size} B" if size else "空文件"
        lines.append(f"{i}. {mark}（{size_txt}） {r.get('origin_label')}　{_redact_path_for_list(str(r.get('path') or ''))}")
    return "\n".join(lines)


def _cookies_manager_note(rows) -> str:
    """列表下方那句"更重要的是什么" —— **纯函数**。

    它只回答一个问题：**现在这样配，引擎实际上会读到什么**。
    这一句是本卡存在的主要理由（历史事故：界面填着路径、引擎全程按无 cookies 跑）。
    """
    rows = list(rows or [])
    if not rows:
        # 空列表时**必须把"默认位置在哪"写出来**：否则用户看到"没有任何来源"
        # 却不知道该把文件放哪儿才会被自动发现（那正是最容易白折腾的一步）。
        from kiana_vnext_plus.cookie_utils import default_cookie_file
        return ("当前**没有任何 cookies 来源**：引擎会按无 cookies 抓取"
                "（B站等站点只给 480P）。点「登录并取 cookies」拿一份，或「添加文件…」选一份。\n"
                f"自动发现的默认位置是：{default_cookie_file()}")
    missing = [str(r.get("path") or "") for r in rows if not r.get("exists")]
    empty = [str(r.get("path") or "") for r in rows if r.get("exists") and not int(r.get("size") or 0)]
    bits = [f"共 {len(rows)} 个来源，按上面的顺序生效。"]
    if missing:
        bits.append(f"⚠️ 其中 {len(missing)} 个**文件不存在** → 引擎按无 cookies 处理它们"
                    f"（常见原因：浏览器下载重名文件时改名成 'xxx (1).txt'，配置里还指着旧路径）："
                    + "；".join(missing))
    if empty:
        bits.append(f"⚠️ 有 {len(empty)} 个是**空文件** → 等于没配：" + "；".join(empty))
    if all(str(r.get("origin") or "") != "explicit" for r in rows):
        bits.append("这些是**自动发现**来的（输入框里什么都没写）；"
                    "往输入框里写任何一项，自动发现就**不再生效**（尊重用户显式选择，语义如此）。")
    if any(str(r.get("origin") or "") == "explicit" for r in rows):
        bits.append("**下面这些是本次任务真会用的清单**（首页 Cookies 框就是它；改动即存即生效）。")
    return "".join(bits)


def _cookies_health_text(rows, privacy_line: str = "") -> str:
    """逐项核验结论 + 那一行聚合的隐私状态 —— **纯函数**（不联网、不碰 Qt）。

    ⚠️ **诚实的边界，必须写在界面上**：
      · per-file 那三态用的是 `verify_login_cookie(site, path)`，**粒度是"站点"**：
        而 `cookie_utils.cookie_source_files()` 的语义是**多份文件合并**（先到先得），
        所以"B站的 SESSDATA 在哪一份里"这个问题**单个文件回答不了**。
        逐项写"✅ 有登录凭据"就会谎报（同一份文件里可能有匿名 cookie 而已）。
        ⇒ 所以逐项只报**文件层面的事实**，把"该站登录凭据**在所有来源里**在不在这里"
          这一条**显式标成聚合判断**，并说明它不能归因到某一行。
      · 在线核验（B站 nav 接口）无论"明确未登录""网络不通""风控"都必须分开说
        —— `cookie_health` 的三态（ok/expired/unknown）原样传达，**不合并成"失败"**。
    """
    rows = list(rows or [])
    lines = [f"逐项（{len(rows)} 个来源）："]
    if not rows:
        lines.append("  （没有来源可查）")
    for i, r in enumerate(rows, 1):
        p = str(r.get("path") or "")
        if not r.get("exists"):
            lines.append(f"  {i}. ⚠️ 文件不存在 → 引擎按无 cookies 处理：{p}")
            continue
        lines.append(f"  {i}. ✅ 文件在（{int(r.get('size') or 0)} B）：{p}")
    # 聚合判断：哪些站在**所有来源合起来**之后有登录凭据
    sites = sorted({str(o["site_key"]) for o in login_site_choices()})
    aggregate = []
    for key in sites:
        names = _site_login_cookie_names(key)
        if not names:
            aggregate.append(f"{site_display_name(key)}：**无法判定**（登记表没登记登录凭据名，不猜）")
            continue
        verdict = None
        for r in rows:
            if not r.get("exists"):
                continue
            try:
                v = verify_login_cookie(key, str(r.get("path") or ""))
            except Exception as e:
                verdict = f"核验出错（{type(e).__name__}）"
                break
            if v is True:
                verdict = "✅ 有登录凭据"
                break
            if v is False and verdict is None:
                verdict = "⚠️ 只有匿名 cookie（没有该站登录凭据）"
        aggregate.append(f"{site_display_name(key)}：{verdict or '—（这些来源里没看到该站 cookie）'}")
    lines.append("聚合（把上面所有来源合起来看 —— **无法归因到某一行**，因为引擎本来就是合并读的）：")
    lines += [f"  · {a}" for a in aggregate]
    if privacy_line:
        lines.append(privacy_line)
    return "\n".join(lines)


class CookieLoginBridge:
    """把 `open_profile_for_login`（async、要等用户几分钟）搬到后台线程跑。

    **线程 + 独立 asyncio 事件循环这一步照 `launcher_v8.EngineBridge` 同款**
    （daemon 线程 + `asyncio.new_event_loop()` + `run_until_complete()`）：
    GUI 主线程跑的是 Qt 事件循环，把 `asyncio.run()` 扔进主线程 = **界面冻死**
    —— 引擎桥当年就是为这个才起后台线程的。

    **回主线程不用 Signal，用注入的投递函数**（GUI 侧接的正是本文件既有的
    `QApplication.postEvent`）：本工程的开发规范 陷阱表写着"worker 线程 emit 信号偶发崩"
    （v2.18.1 的壁纸 worker 已统一改成 postEvent）。

    协程工厂 / 核验函数 / 投递函数**都可注入** —— 于是"线程 + 事件循环 + 核验 + 投递"
    这条链能**脱离 Qt、脱离浏览器**被单测（喂一个假协程就能验异常分支到底报了什么）。
    """

    def __init__(self, deliver, opener=None, verify=None):
        self._deliver = deliver                    # (kind, payload) → 主线程，须线程安全
        self._opener = opener or _default_login_opener
        self._verify = verify or verify_login_cookie
        self._thread = None
        self._loop = None
        self._task = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, site: str, url=None) -> bool:
        """起后台线程；已在跑则**不起第二个**（同一配置档目录开两个 Chrome 会撞 singleton 锁）。

        `url`（[v7 新增]）：自定义登录页。`open_profile_for_login` **本来就收这个参数**
        （`cookie_profile` 一行都不用改），原先只是界面没开口子。
        传空/None = 用登记表里该站的默认登录页。

        它**在起线程前取好并绑进闭包**：登录期间用户改了输入框也不会影响这一次
        （"跑着跑着目标变了"是最难查的一类怪事）。
        """
        if self.running:
            return False
        target = str(url or "").strip() or None
        self._thread = threading.Thread(target=self._work, args=(str(site), target), daemon=True)
        self._thread.start()
        return True

    def stop(self) -> bool:
        """软取消（关程序时用）：让登录协程在下一个 await 点退出。

        必须这么关、而不是"直接退进程"：`open_profile_for_login` 的 `finally` 里那句
        `await ctx.close()` 才是**真正关掉浏览器进程**的地方；硬退会留下 Chrome
        占着配置档目录，下次登录直接撞上 singleton 锁起不来。
        （尽力而为：daemon 线程随进程终止，来不及跑完就只剩用户手动关窗口。）
        """
        loop, task = self._loop, self._task
        if loop is None or task is None:
            return False
        try:
            loop.call_soon_threadsafe(task.cancel)   # 跨线程碰 loop 只认这一个 API
            return True
        except Exception:
            return False

    def _work(self, site: str, url=None):
        try:
            res = self._login_and_verify(site, url)
        except Exception as e:              # 兜底：`_login_and_verify` 号称不抛，但别赌
            res = {"ok": False, "status": "exception", "site": site,
                   "note": f"{type(e).__name__}: {e}"}
        finally:
            self._task = None
            self._loop = None
            self._thread = None
        try:
            self._deliver("login", res)
        except Exception:
            pass                            # 投递失败不再抛回线程（那边没人接）

    def _login_and_verify(self, site: str, url=None) -> dict:
        """**本线程**起独立事件循环把登录跑完 → 立刻核验产物。永远返回 dict。

        `open_profile_for_login` 自称"永不抛业务异常"，但 import 失败 / 事件循环
        策略异常仍可能抛 —— 那样界面会永远停在"等待登录中"（比报错更糟）。
        故这里把所有异常都转成 `status="exception"`，让界面**如实显示失败**。
        """
        import asyncio
        res: dict = {}
        try:
            if sys.platform == "win32":
                try:
                    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
                except Exception:
                    pass                    # 策略设不上也继续（bridge 模式常常本就没配）
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            try:
                task = loop.create_task(self._opener(site, url))
                self._task = task
                try:
                    res = dict(loop.run_until_complete(task))
                except asyncio.CancelledError:
                    # 取消（退出程序）发生在导出**之前** → 什么都没写；如实说"没取回来"
                    res = {"ok": False, "status": "cancelled", "site": site,
                           "cookies_path": None, "cookie_count": 0,
                           "note": "已取消（程序退出）"}
                except Exception as e:
                    res = {"ok": False, "status": "exception", "site": site,
                           "note": f"{type(e).__name__}: {e}"}
            finally:
                try:
                    # 不关会留下 selector / 子进程 handler（反复取登录态会越积越多）
                    loop.close()
                except Exception:
                    pass
        except Exception as e:
            res = {"ok": False, "status": "exception", "site": site,
                   "note": f"{type(e).__name__}: {e}"}
        verified, vnote = self._verify_safe(site, res.get("cookies_path"))
        res["login_cookie_verified"] = verified
        if vnote:
            res["verify_note"] = vnote
        return res

    def _verify_safe(self, site: str, cookies_path) -> tuple:
        """核验产物 → `(True/False/None, 出错说明)`。

        **核验本身出错也不许静默**：出错说明会进结果字典、再由界面写进日志页
        （状态行那边只能说"无法确认"，原因得让人查得到）。
        """
        if not cookies_path:
            return None, ""
        try:
            return self._verify(site, cookies_path), ""
        except Exception as e:
            return None, f"核验 cookies 文件时出错（按「无法判定」处理）：{type(e).__name__}: {e}"


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
        # [v6] 占位文案点明"要自己选"，别让人以为"留空引擎会自己找" ——
        # `_ensure_cookie_file()` 确实会看默认位置，但**没有**的时候就是没有 cookies，
        # B站会因此只给 480P。界面上把这点说清楚。
        self.cookie_edit.setPlaceholderText(
            "点右侧「选择文件…」选你的 cookies.txt（留空=无 cookies，B站等站点只能拿 480P）")
        crow.addWidget(self.cookie_edit, 1)
        cp = PushButton("选择文件…"); cp.clicked.connect(self._pick_cookie)
        crow.addWidget(cp)
        cc = PushButton("清空"); cc.clicked.connect(lambda: self.cookie_edit.clear())
        crow.addWidget(cc)
        self.lay.addLayout(crow)

        # [v6] cookies **路径失效**的界面提示。
        # 引擎侧早就会 logger.error 报这件事，但**界面上一声不吭** ——
        # 用户看着输入框里填着路径、以为配好了，实际全程按无 cookies 跑（B站只给 480P）。
        # 这条与「按站身份池」状态行同一个原则：**如实说，别给"看起来生效了"的错觉**。
        self.cookie_status = BodyLabel("")
        self.lay.addWidget(self.cookie_status)

        # ── [v7 重排] 「登录取 cookies」那一整行**已挪到设置页**（用户要求：主页面别堆 UI）──
        # 首页**只留这一行输入框**：它是每次抓取都要看一眼/对一下的东西
        # （"这一趟到底带没带登录态"），留在首页是有用的；而登录/管理属于
        # "配一次就完事"的动作，正在设置页的「Cookies 管理」里。
        # 输入框**依然是权威值**：`_start()` 把它当成 `cookie_file` 交给引擎。
        # 状态行仍在（`cookie_status`，上一段建的），但它上面的提示语要**如实**说清
        # cookie 现在到底从哪来 —— 不能因为搬走了按钮就让用户以为"这儿没入口 = 没有这功能"。

        # [v6 M1-f] 接管已登录浏览器 —— 复用**你自己登录的**浏览器会话，到达登录墙后的内容。
        # 六跳接线见 本工程的开发规范 第七节；此处是第①跳（控件 create）。
        # 动效：直接用工程现成的 _switch() → SwitchButton（自带滑块过渡动画），
        # 与上面 5 个开关**完全同款**，不自造动画、不引入新视觉风格。
        # 默认关：与 config.DEFAULT_GLOBAL 的 cdp_attach=False 一致。
        drow = QHBoxLayout()
        drow.setSpacing(8)
        self.sw_cdp = self._switch("接管已登录浏览器", drow,
                                   bool(cfg.get("cdp_attach", False)))
        drow.addWidget(BodyLabel("调试端口"))
        self.cdp_port_spin = QSpinBox()
        self.cdp_port_spin.setRange(1, 65535)
        self.cdp_port_spin.setValue(int(cfg.get("cdp_port", 9222)))
        self.cdp_port_spin.setFixedWidth(90)
        drow.addWidget(self.cdp_port_spin)
        drow.addStretch(1)
        self.lay.addLayout(drow)

        # 状态行：如实显示"到底连上没有"。不靠"看起来生效了"——
        # 接管失败的后果是"登录后才可见的内容取不到"，界面上必须看得见。
        self.cdp_status = BodyLabel("")
        self.lay.addWidget(self.cdp_status)

        # ── [v6 P1] 按站身份池（用户点名"必须得要"）──────────────────────────
        # 与上一条「接管已登录浏览器」**并列**，两者互补：
        #   · 接管浏览器：用**你自己那个浏览器**的登录态（一个身份）
        #   · 身份池    ：用**按站入库的 cookies**，可多账号轮换、带健康分/额度/冷却
        # 动效同款（`_switch` → SwitchButton 自带过渡动画），不引入新视觉风格。
        # **默认关**——风险敏感特性（用你的账号跑机器量级访问），
        # 与 `config.DEFAULT_GLOBAL` 的 `cookie_armory_enabled=False` 一致。
        arow = QHBoxLayout()
        arow.setSpacing(8)
        self.sw_armory = self._switch("按站身份池", arow,
                                      bool(cfg.get("cookie_armory_enabled", False)))
        # [v6 修复·用户实测发现] **导入入口**。
        # 原来只有开关没有入口：状态行会诚实地说"库里没有任何身份"，
        # 但**界面里没有任何地方能加** —— 于是这个功能对使用者等于不存在
        # （比"静默失效"更糟：它明说了缺什么，却不给补的办法）。
        # 现在一个按钮搞定：选文件 → 自动识别站点 → 入库 → 刷新状态行。
        arm_imp = PushButton("导入身份…")
        arm_imp.setToolTip("选择一个 cookies.txt，把它加密存进按站身份池（可多账号轮换）")
        arm_imp.clicked.connect(lambda: self.window()._import_identity())
        arow.addWidget(arm_imp)
        arm_list = PushButton("查看身份")
        arm_list.setToolTip("列出库里的身份与健康分（不含 cookie 明文）")
        arm_list.clicked.connect(lambda: self.window()._show_identities())
        arow.addWidget(arm_list)
        arow.addStretch(1)
        self.lay.addLayout(arow)

        # 状态行：**如实说"库里有没有身份"**。开着一片空白会让人以为"已经生效了"，
        # 而实际上取不到身份时抓取是**按无 cookie 跑**的（第 36 轮踩过这个坑：
        # 功能建好了但没入口/没提示，等于没做）。
        self.armory_status = BodyLabel("")
        self.lay.addWidget(self.armory_status)

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
        # [v6 P2] 两个原本"只能改配置文件、界面无入口"的键：
        #   · 显示浏览器窗口 ←→ `headless`（**反向**：勾上=有头，headless=False）
        #     无头是默认（省内存、不弹窗）；要看渲染过程/手动过验证码时才需要打开。
        #   · 导出 Markdown ←→ `export_markdown`（每页留一份 Markdown 快照）
        # [v6 修复·用户实测反馈] 用户把它勾上后**一次窗口都没弹**，以为坏了。
        # 查证：浏览器是**回退层**，只有协议层拿不到页面时才启动；
        # v6 修完 `_needs_solver` 误报后 B站走纯协议层 ⇒ **根本不产生窗口**。
        # **不是坏了，是文案没写清条件。** 改名 + 挂 tooltip。
        self.sw_headful = self._switch(
            "浏览器渲染时显示窗口", checks,
            not bool(cfg.get("headless", True)),
            tip=("只在引擎**需要浏览器兜底**时才生效"
                 "（协议层拿不到页面 / 需要 JS 渲染 / 要手动过验证码）。\n"
                 "纯协议抓取（B站现在就是）**不会起浏览器，也就没有窗口** ——\n"
                 "这是好事：更快、省内存。\n"
                 "想确认开关生效：抓一个必须渲染的站（如公众号文章）看窗口。"))
        self.sw_md = self._switch("导出 Markdown", checks,
                                  bool(cfg.get("export_markdown", True)))
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

        # [v2.19.9 删除] 这里原来是「隐私状态: 自检待运行…」那一行小字。
        # 删它的判据：**只写不读**（4 处 setText、0 处读取，没有点击、没有联动），
        # 而它想说的三句在别处都有、且更完整 ——
        #   · 🔐 密钥DPAPI加密  → 设置页「打码 API 密钥」卡；
        #   · Cookies: B站✅ …  → 设置页「检查有效性」的输出里**逐字重复**同一串；
        #   · 环境: 浏览器渲染 / YT 组件 → 那两个**从来没显示过**（见 `_env_selfcheck` 的删除说明）。
        # ⇒ 留在首页只是"多一行会变的小字"。**算文本的纯函数 `_privacy_status_text()`
        # 保留**（设置页「检查有效性」在复用），登录态探测结果改写日志页 ——
        # 见 `_refresh_privacy_status_async` / `_on_privacy_status`。

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

    def _switch(self, text, lay, checked, tip=""):
        """一个「文字 + 开关」的行。`tip` 会挂成**行级 tooltip**。

        [v6 修复·界面文案撒谎] 用户实测：把「显示浏览器窗口」勾上，
        **一次窗口都没弹**，以为是坏了。查证后是**文案没写清条件**：
        浏览器是**回退层**，只有协议层拿不到页面（403/渲染站/过验证码）时才启动；
        纯协议抓取（B 站现在就是）根本不产生窗口。
        ⇒ 加 `tip`，把"**什么条件下才生效**"写在明面上。
        """
        w = QWidget()
        h = QHBoxLayout(w); h.setContentsMargins(0, 0, 0, 0)
        lbl = BodyLabel(text)
        h.addWidget(lbl)
        sw = SwitchButton()
        sw.setChecked(checked)
        h.addWidget(sw)
        if tip:
            # 挂在整行上（开关本身太小，鼠标不容易停上去）
            w.setToolTip(tip)
            lbl.setToolTip(tip)
            sw.setToolTip(tip)
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
        """设置页改动后回写首页 cookie 行（双入口同步）

        [v7] 设置页那一格现在是**只读镜像**，唯一的写入口是下面这行 ——
        全工程只有"首页 Cookies 框"一个权威值，避免"这边改了那边没改"。
        """
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
        # [v2.19.9] 挂到页面上：刷新改成后台线程后，槽里要拿它做"统计中禁用 / 回来还原"
        self.refresh_btn = refresh_btn
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

        # ── [v7] 「Cookies 管理」卡：管理**多个** cookies 来源 + 登录也在这里 ──────
        # 用户原话："你现在无法同时管理多个 cookies 啊，然后主页面还堆了一堆 UI 其实挺乱的"
        # ⇒ ① 登录从首页搬到这里；② 列出**当前真正生效的每一个来源**（含"从哪来"）。
        #
        # 为什么"列出真实来源"是这张卡的核心：引擎一直就支持多份 cookies
        # （`KIANA_COOKIE_FILES` 分号/换行分隔，`_ensure_cookie_file` 合并时**先到先得**），
        # 缺的是**看得见**。看不见的后果在本工程有前科：用户在界面上"加好了"，
        # 引擎其实按无 cookies 跑（B站只给 480P），而界面一片正常。
        #
        # ⚠️ 这张卡**一行 cookie 解析都不自己做**：来源列表来自
        # `cookie_utils.resolve_cookie_sources()`（引擎同源），增删走
        # `cookie_paths_add_front` / `cookie_paths_drop`。
        card2 = CardWidget()
        v2 = QVBoxLayout(card2); v2.setContentsMargins(20, 16, 20, 16); v2.setSpacing(10)
        v2.addWidget(SubtitleLabel("Cookies 管理（多个来源 · 登录也在这里）"))

        # ① 登录块：站点下拉 + **自定义登录页**（像 yt-dlp 那样自己填网址）+ 登录按钮
        self.login_sites = login_site_choices()
        lrow = QHBoxLayout()
        lrow.setSpacing(8)
        lrow.addWidget(BodyLabel("站点"))
        # 下拉项与 `self.login_sites` **同源同序**（索引即站点键）——
        # 不用 ComboBox 的 userData：名字要来自登记表，映射关系留在自己手里最不容易错位。
        self.login_site_combo = ComboBox()
        for _o in self.login_sites:
            self.login_site_combo.addItem(str(_o["label"]))
        for _i, _o in enumerate(self.login_sites):
            if _o["site_key"] == "bilibili":      # 默认 B站；列表里没有就保持第 0 项
                self.login_site_combo.setCurrentIndex(_i)
                break
        lrow.addWidget(self.login_site_combo)
        self.login_btn = PushButton(FIF.GLOBE, "登录并取 cookies")
        self.login_btn.setToolTip(
            "打开一个**专用**浏览器窗口让你登录（与你的日常浏览器互不影响）；\n"
            "登录完成后**关掉那个窗口**，cookies 会自动就位并让引擎用上。\n"
            "要多个站点就多点几次 —— 每个站一个独立配置档。")
        lrow.addWidget(self.login_btn)
        lrow.addStretch(1)
        v2.addLayout(lrow)

        # [v7 新增] 自定义登录页 —— `open_profile_for_login(url=...)` **本来就收这个参数**，
        # 只是原先界面没开口子（像 yt-dlp 那样"网站自己填"是这个功能的承诺之一）。
        # 填了就**优先于**上面的下拉（见 `_login_target`）：用户刚填的网址必须说了算。
        self.login_url_edit = QLineEdit("")
        self.login_url_edit.setPlaceholderText(
            "自定义登录页（选填，如 https://space.bilibili.com/ —— 填了就优先于上面的站点下拉）")
        self.login_url_edit.setToolTip(
            "站点标识只从网址**推**出来，且**必须在站点登记表里认得出来**（bilibili.com / douyin.com …）；\n"
            "推不出来就拒绝打开浏览器 —— 拿一个猜的站名去建目录写 cookies 比不做更糟。\n"
            "只认 https（本机回环 http://127.0.0.1 例外）。")
        v2.addWidget(self.login_url_edit)

        # ② 来源列表（等宽/只读；点击某行 = 选中该项，下面按钮作用于选中项）
        self.cookie_sources_view = QPlainTextEdit()
        self.cookie_sources_view.setReadOnly(True)
        self.cookie_sources_view.setFixedHeight(132)
        self.cookie_sources_view.setPlaceholderText("（下面点「刷新列表」读一次当前生效的 cookies 来源）")
        v2.addWidget(self.cookie_sources_view)

        # ③ 状态行：进行中与结果都写这里。**不许出现"没提示 = 成功了"** ——
        # 失败（尤其"关了窗口但其实没登录"、"检测发现全是匿名 cookie"）必须看得见。
        # 开自动换行：要认真说清"cookies 存到哪了"（完整路径很长），
        # 不换行就会被控件宽度截掉 —— 那等于没说。
        self.login_status = BodyLabel("")
        self.login_status.setWordWrap(True)
        v2.addWidget(self.login_status)
        self.cookie_manager_status = BodyLabel("")
        self.cookie_manager_status.setWordWrap(True)
        v2.addWidget(self.cookie_manager_status)

        # ④ 操作按钮
        row3 = QHBoxLayout()
        row3.setSpacing(8)
        c_refresh = PushButton("刷新列表")
        c_refresh.setToolTip("重新读一次「现在引擎到底会读哪几个 cookies 文件」（不联网、不开浏览器）")
        c_refresh.clicked.connect(lambda: win._refresh_cookie_sources())
        c_add = PushButton("添加文件…")
        c_add.setToolTip("把一份已有的 cookies.txt 加进列表；加进来的排**最前**（引擎合并是先到先得，"
                         "排前面才盖得住同名的旧凭据）")
        c_add.clicked.connect(lambda: win._add_cookie_file())
        c_del = PushButton("移除选中")
        c_del.setToolTip("只从这份配置里移除，**不删磁盘上的文件**")
        c_del.clicked.connect(lambda: win._remove_selected_cookie_source())
        c_front = PushButton("设为最优先")
        c_front.setToolTip("把选中项移到最前：引擎合并多份 cookies 时是**先到先得**，"
                           "顺序决定同名的旧 SESSDATA 会不会被盖住")
        c_front.clicked.connect(lambda: win._promote_selected_cookie_source())
        for _b in (c_refresh, c_add, c_del, c_front):
            row3.addWidget(_b)
        row3.addStretch(1)
        v2.addLayout(row3)

        row3b = QHBoxLayout()
        row3b.setSpacing(8)
        c_check = PushButton(FIF.SYNC, "检查有效性")
        c_check.setToolTip("逐个文件核对站点的**登录凭据**在不在（含一次 B站在线核验）。\n"
                           "计算在后台线程里跑，界面不会假死；结果如实区分"
                           "\"有登录凭据 / 只有匿名 cookie / 无法判定\"。")
        c_check.clicked.connect(lambda: win._check_cookie_sources_async())
        c_open = PushButton("打开所在目录")
        c_open.setToolTip("在资源管理器里定位选中的那份 cookies.txt")
        c_open.clicked.connect(lambda: win._open_selected_cookie_dir())
        c_clear = PushButton("清空全部")
        c_clear.setToolTip("把输入框清空 → 引擎回到**自动发现**（默认位置 → profiles/*/cookies.txt）")
        c_clear.clicked.connect(lambda: win._clear_cookie_box())
        for _b in (c_check, c_open, c_clear):
            row3b.addWidget(_b)
        row3b.addStretch(1)
        v2.addLayout(row3b)

        # ⑤ 与首页 Cookies 框同步的镜像（**只读**）：单一权威值仍在首页那个框里。
        # [v7 决策] 这一格刻意**不做成第二个可编辑入口**：同一份配置两个可写入口，
        # 早晚会"这边改了那边没改"。它现在只是"首页那个框现在是什么"的忠实镜像，
        # 保留它让 `_sync_cookie_to_settings` / `refresh_from_config` 这条既有同步链
        # **一行都不用改**（v9_smoke 里那条 `cookie_sync` 断言也继续有效）。
        self.cookie_edit_d = QLineEdit(str(win.config.get("cookie_file", "")))
        self.cookie_edit_d.setReadOnly(True)
        self.cookie_edit_d.setPlaceholderText(
            "（首页 Cookies 框的镜像 · 留空=引擎自动发现：默认位置 → profiles/*/cookies.txt）")
        v2.addWidget(self.cookie_edit_d)
        self.lay.addWidget(card2)

        if not self.login_sites:                  # 站点表读不到 → 登录按钮直接禁用并说明原因
            self.login_btn.setEnabled(False)
            self.login_site_combo.setEnabled(False)
            self.login_status.setText(
                "⚠️ 站点列表读不到（引擎包 cookie_profile 不可用）—— 取登录态不可用")

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
        self.llm_key = QLineEdit(load_llm_key(win.config))
        self.llm_key.setPlaceholderText("API Key（DPAPI 加密存本机 secrets/，不随包分发）")
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
        # [v2.19.9] 挂到页面上：`_run_priv_scan` 的槽里要拿它做"扫描中禁用 / 回来还原"
        # 的反馈。原来它只是个局部变量 → 槽里够不着，也就没法给任何状态反馈。
        self.priv_scan_btn = priv_scan
        row5.addWidget(proxy_refresh); row5.addWidget(priv_scan); row5.addStretch(1)
        v5.addLayout(row5)
        self.lay.addWidget(card5)

        # ── [v6 P2] 代理源拉取 + 指纹库更新（原为配置文件死键，界面无入口）──────────
        # 审计测试曾把这两个标成 `GAP`：「看起来是用户会想控的功能，但当前无入口」。
        # 两者都是**默认关**且**联网**的特性，所以放设置页、带明确文字说明，
        # 不放在首页（首页开关是每次抓取都要看的，这两个是配一次就完事）。
        card5b = CardWidget()
        v5b = QVBoxLayout(card5b); v5b.setContentsMargins(20, 16, 20, 16); v5b.setSpacing(12)
        v5b.addWidget(SubtitleLabel("代理源与指纹库（选填，默认关闭）"))
        self.sw_proxy_fetch = SwitchButton("自动抓取代理（按间隔从代理源拉取，自动换批）")
        self.sw_proxy_fetch.setChecked(bool(win.config.get("proxy_fetcher_enabled", False)))
        v5b.addWidget(self.sw_proxy_fetch)
        # 与上者**配套**：开了拉取却不填来源，等于开了个空转的开关。
        self.proxy_src_edit = QLineEdit(str(win.config.get("proxy_source", "")))
        self.proxy_src_edit.setPlaceholderText(
            "代理源：api:https://provider/api?token=xxx   或   ip:port,ip:port（逗号分隔）")
        v5b.addWidget(self.proxy_src_edit)
        self.sw_fp_update = SwitchButton("指纹库自动更新（联网拉取最新指纹）")
        self.sw_fp_update.setChecked(bool(win.config.get("fingerprint_update_enabled", False)))
        v5b.addWidget(self.sw_fp_update)
        self.lay.addWidget(card5b)

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

        # [v2.18.2 用户新要求] 面板玻璃透明度：滑杆 40-95 + 预设组，即调即生效。
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
        # [v2.17.1] 电子签名开关（默认开；颜色随强调色联动）
        self.sw_signature = SwitchButton("电子签名（右下角）")
        self.sw_signature.setChecked(bool(win.config.get("signature_enabled", True)))
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
        """选一份 cookies.txt —— 现在由「Cookies 管理」的「添加文件…」使用。

        [v7] 原挂在本卡的「选择文件…」按钮上；那个按钮已随重排合并进管理卡，
        路径与返回约定不变（`All Files (*)`，因为 Netscape 文件的扩展名不一定是 .txt）。
        """
        f, _ = QFileDialog.getOpenFileName(self, "选择 cookies.txt", "", "All Files (*)")
        return f or ""

    def _toggle_captcha_echo(self):
        """明文显示一次（点击后再隐藏）"""
        p = QLineEdit.EchoMode.Normal if self.cap_twocaptcha.echoMode() == QLineEdit.EchoMode.Password else QLineEdit.EchoMode.Password
        for e in (self.cap_twocaptcha, self.cap_capsolver, self.cap_anticaptcha):
            e.setEchoMode(p)


# ════════════════════════════════════════════════════════════
# [v2.19.9] 隐私扫描的"能不能跑"判据 —— 刻意做成**模块级纯函数**：
# 它不碰 Qt、不碰子进程，于是那个"最坏的分支"（打包版会弹第二个窗口）
# 可以脱离界面被单测钉死。写成 KianaV9 的方法就得先离屏构造整个窗口才验得了。
# ════════════════════════════════════════════════════════════
def _priv_scan_script_path() -> Path:
    """被 GUI 调用的扫描脚本路径（唯一一处拼接，便于测试替换）。"""
    return Path(__file__).parent / "tools" / "privacy_scanner.py"


def _read_last_json_line(path, tail_bytes: int = 65536):
    """只读文件**尾部** tail_bytes，返回**最后一个能解析成 JSON 的行**；读不到返回 None。

    **为什么不能 `read_text()` 读整份**：`stats.jsonl` 是**追加式**的、随爬取无限增长，
    而调用它的 `_parse_progress()` 会被**每一条进度日志**触发一次（`_on_log` → 它）——
    等于每隔几秒就把整份进度文件重读一遍，还是在 GUI 主线程上。
    尾部读把代价从"与历史长度成正比"压成**常数**。

    **为什么是从后往前扫、而不是只取最后一行**（这一条是[对抗性验证]发现的）：
    引擎**一边追加**、GUI **一边读**，`seek(size - tail_bytes)` 完全可能正好切在一行中间，
    而最后一行也完全可能正被写到一半。只取"最后一个元素"的话，撞上这两件事就返回 None
    ⇒ 悄悄掉进 regex 兜底。所以这里的循环是**真的在往回找**第一条完整的记录。

    ⚠️ 不需要"先 `readline()` 丢掉被切开的半行"：seek 造成的残行只会出现在
    **第一个**元素上，而从后往前扫根本走不到它（我原先写了那一句并注释成"防截断"，
    对抗性验证证明它**一行行为都不影响**，是句会误导后来者的死代码，已删）。
    """
    try:
        size = path.stat().st_size
        if size <= 0:
            return None
        with open(path, "rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
            data = f.read()
        for raw in reversed(data.splitlines()):
            raw = raw.strip()
            if not raw:
                continue
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception:
                continue            # 残行/坏行 → 继续往回找（追加写入时这是常态）
        return None
    except Exception:
        return None


def _priv_scan_unavailable_reason() -> str:
    """返回"这次扫描跑不了"的理由；空串 = 可以跑。

    **为什么要有这个守卫**（不是防御性编程，是已核实的真事故路径）：
    `KianaLauncher.spec` 的 datas 里**没有** `tools/`（只有 assets / run_crawler.py /
    rules/sites / ddddocr / qfluentwidgets / browsers / pot_server），
    而冻结后 `sys.executable` 指向 **GUI 自己**。于是"用解释器跑脚本"这行在打包版里变成
    `KianaLauncher.exe <一个不存在的路径>` —— 那个 exe **不看 argv**
    （`main()` 是 `QApplication(sys.argv)`），后果是**当着用户的面再弹一个 Kiana 窗口**，
    而父进程还要 `capture_output` 等它 30 秒才超时杀掉它。
    本工程的纪律是"不许在前台冒弹窗"，所以这里**如实拒绝**，不静默、也不弹窗。
    """
    if getattr(sys, "frozen", False):
        return ("隐私扫描在打包版中不可用：tools/ 未随包分发，且打包后的可执行文件"
                "就是 GUI 自身 —— 拿它跑脚本会再弹出一个 Kiana 窗口。"
                "请在源码目录运行：python tools/privacy_scanner.py")
    if not _priv_scan_script_path().exists():
        return "隐私扫描不可用：找不到 tools/privacy_scanner.py（请在源码目录运行）"
    return ""


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
        # [v2.19.9 安全] LLM Key 走**同一套**迁移时机与机制。为什么启动时就迁移、而不是
        # 等用户下次点「保存 LLM 设置」：用户的真实密钥此刻正明文躺在 launcher_config.json
        # 里，"等他再点一次保存"等于让它继续明文躺着（打码密钥那条也是这么改的）。
        # 迁移只在**加密成功**后才会把明文键从内存字典里抹掉，再落盘。
        try:
            _llm_before = bool(self.config.get("llm_key"))
            load_llm_key(self.config)
            if _llm_before and "llm_key" not in self.config:
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
        from PySide6.QtWidgets import QLabel as _QLabel
        from PySide6.QtGui import QFontDatabase as _QFD, QColor as _QColor
        self._sig_label = _QLabel(self)
        self._sig_label.setText("［@ちょう_0831］")
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

        # 「登录并取 cookies」的后台桥：**照 EngineBridge 同款**（daemon 线程 + 独立
        # asyncio 事件循环），但回主线程走 postEvent（理由见 CookieLoginBridge 的 docstring）。
        # 与引擎桥**分开两个对象**：一个桥一个职责，别把"跑爬虫"和"等用户登录"混在一个线程里
        # （登录要等几分钟，混在一起会让"停止爬取"这类操作跟着一起傻等）。
        self._login_bridge = CookieLoginBridge(self._deliver_async)

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
        # [v2.19.9 删除] 这里原来挂着 `QTimer.singleShot(1200, self._env_selfcheck)`
        # —— 那个"首启环境自检"是**死代码**（第一句就 `self.cfg`，全文件从未赋值
        # ⇒ 立刻 AttributeError，被函数末尾的 `except Exception: pass` 吞掉），
        # 详见被删函数的说明。定时器一并清掉，免得留一个每启一次空跑一次的回调。

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
        """签名颜色=强调色同源；字体=艺术字体（预览图 docs/signature_preview.png 任选，
        默认 Mochiy Pop One；换字体改 assets/fonts 文件名首位即可）"""
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
        if not bool(self.config.get("signature_enabled", True)):
            self._sig_label.hide()
            return
        self._sig_label.adjustSize()
        self._sig_label.show()
        self._sig_label.move(self.width() - self._sig_label.width() - 16,
                             self.height() - self._sig_label.height() - 10)
        self._sig_label.raise_()

    # [v2.19.9 删除] `_env_selfcheck()` —— **整函数删除**（连同挂在它上面的
    # `QTimer.singleShot(1200, …)` 与 `_save_captcha_keys` 里那两行 `_env_status` 记账）。
    #
    # 为什么是删而不是修：它是**死代码**，离屏实测（构造 KianaV9 → 调它）确认 ——
    #   · 第一句就 `getattr(self.cfg, 'browsers_path', "")`，而 `self.cfg`
    #     **全文件从未被赋值**（`__init__` 里赋的是 `self.config`，见 1567 行附近）
    #     ⇒ 求值 `self.cfg` 本身立刻抛 AttributeError；
    #   · 那个 AttributeError 被函数末尾的 `except Exception: pass` 吞掉
    #     ⇒ **函数体第一句之后一行都没执行过**：`_env_status` 恒为空字典、
    #     控件文本没变过（实测前后都是 "隐私状态: 自检待运行…"）、
    #     两条"缺失项 toast"（浏览器渲染 / YT 组件）**从未弹过**。
    # 自 v2.16 起它就是这样，所以删掉它 **0 用户可见损失**。
    #
    # 为什么不改成"按需触发的环境自检按钮"：那是**加一个新交互**（新控件 + 新接线 +
    # 新测试面），而用户这一轮要的是"删掉那行小字"。四项里两项（打码密钥 → 设置页
    # 「打码 API 密钥」卡；代理 → 设置页代理状态卡）本来就有显示位，另两项
    # （浏览器渲染 / YT 组件）属于**可移植性/装机**检查，`tools/release_check.py`
    # 与安装流程已经覆盖 —— 真要做成 GUI 按钮，应当作为独立任务点名来做，
    # 而不是借一次"删小字"的修复顺手升温。**拿不准就选保守的**。

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
        # [v6+] 登录线程 / 隐私状态计算线程的投递（同一条 postEvent 通路）
        if isinstance(e, _LoginEvent):
            if e.kind == "login":
                self._on_login_done(e.value)
            elif e.kind == "privacy":
                # [v2.19.9] 原来这里是 `self.home.privacy_status.setText(...)`。
                # 那个控件已删（只写不读的装饰），登录态改记**日志页**。
                # 硬要求不变：**后台探测的结果必须落到看得见的地方** ——
                # 不许因为"异步了"就把"开始前告诉用户登录态"这件事悄悄丢掉。
                self._on_privacy_status(str(e.value))
            elif e.kind == "cookies_check":
                # [v7] 「Cookies 管理」的后台有效性核验回来了（联网那一步在线程里跑完的）
                self._on_cookie_check_done(str(e.value))
            elif e.kind == "priv_scan":
                # [v2.19.9] 设置页「隐私扫描」的子进程跑完了（原来在主线程同步跑，
                # 最长冻 30 秒）。value = (文本, kind) 二元组。
                self._on_priv_scan_done(e.value)
            elif e.kind == "tasks":
                # [v2.19.9] 历史任务列表在后台统计完了（原来在主线程 rglob 整个产物树）
                self._on_tasks_done(e.value)
            elif e.kind == "retry_dead":
                # [v2.19.9] 「重试失败页」的 sqlite UPDATE 在后台跑完了
                self._on_retry_dead_done(e.value)
            elif e.kind == "url_file":
                # [v2.19.9] 「导入 URL 文件」的读盘在后台做完了（value = (text, path, err)）
                self._on_url_file_loaded(e.value)
            return True
        return super().event(e)

    def _on_privacy_status(self, text: str):
        """后台探测回来的登录态 → 日志页。

        **为什么落在日志页**（而不是 toast / 设置页那行状态）：
          · 登录态只是**提示**、不是爬取的前置条件（`_start()` 里没有任何分支读它）
            ⇒ 不该用 toast 打断 —— 而且它回来得晚（可能几秒后），
              那时弹一个"开始前的提示"反而莫名其妙；原来那行小字也从不打断人；
          · 设置页 `cookie_manager_status` 是「检查有效性」的输出位 ——
            两个来源抢同一个标签，会互相覆盖、谁都读不懂；
          · 日志页是本窗口里一直看得见、还能往前翻的地方，且 `_on_login_done`
            本来就把登录结论写在那里 —— 同一条链的结论放在同一处。
        文本**原样**记（措辞自带 ✅/⚠️，这里不补前缀，否则会出现"✅ ✅"）。
        """
        self._log(text, "info")

    # 关窗时等登录窗口收工的护栏：200ms 一轮，最多 40 轮 = 8 秒
    _LOGIN_CLOSE_TICK_MS = 200
    _LOGIN_CLOSE_MAX_TICKS = 40

    def closeEvent(self, e):
        """退出前**先把登录用的浏览器收干净**：请求取消 → 等它一下 → 再退。

        为什么不能"请求取消"就完事（这是审查抓到的真问题）：登录协程跑在 **daemon 线程**
        里，而 `stop()` 只是往它的事件循环投一个 `task.cancel()` 就返回；解释器退出时
        **不 join daemon 线程**，那句 `await ctx.close()`（还要再驱动一次事件循环）几乎必然
        跑不完 ⇒ 留下一个占着配置档目录的 chrome.exe，下次点按钮就是"撞 singleton 锁、
        点了没反应"。所以这里 `ignore()` 掉这次关闭，用 QTimer 等线程真的收工（或等满 8 秒）
        再关 —— 那 8 秒里界面还在，用户看得见"正在关闭…"。
        """
        busy = False
        try:
            bridge = getattr(self, "_login_bridge", None)
            busy = bool(bridge is not None and bridge.running)
        except Exception:
            busy = False
        # `_login_close_gave_up`：等超时了就别再拦（否则窗口永远关不掉，变成新的坑）
        if busy and not getattr(self, "_login_close_gave_up", False):
            if not getattr(self, "_login_close_started", False):
                self._login_close_started = True
                try:
                    bridge.stop()
                except Exception:
                    pass
                try:
                    # [v7] 提示语写在**设置页**那行 —— 登录是在那儿发起的
                    # （首页那行已随重排撤掉；写在那儿用户根本看不到）。
                    self.settings.login_status.setText(
                        "⏳ 正在关闭登录用的浏览器窗口…（最多等 8 秒）")
                    self._log("── 退出中：先收掉登录用的浏览器窗口 ──", "warn")
                except Exception:
                    pass
                t = QTimer(self)
                t.setInterval(self._LOGIN_CLOSE_TICK_MS)
                t.timeout.connect(self._close_when_login_done)
                self._login_close_timer = t
            try:
                self._login_close_timer.start()
            except Exception:
                pass
            e.ignore()                     # 还没收干净：先别关
            return
        super().closeEvent(e)

    def _close_when_login_done(self):
        """等登录线程收工 → 再真正关窗；等满 8 秒也照关（但会在日志里如实说明）。"""
        ticks = getattr(self, "_login_close_ticks", 0) + 1
        self._login_close_ticks = ticks
        running = False
        try:
            bridge = getattr(self, "_login_bridge", None)
            running = bool(bridge is not None and bridge.running)
        except Exception:
            running = False
        if running and ticks < self._LOGIN_CLOSE_MAX_TICKS:
            return
        try:
            self._login_close_timer.stop()
        except Exception:
            pass
        if running:
            # 实话实说：进程要退了，那个浏览器窗口可能留下，得让用户手动关
            self._login_close_gave_up = True
            self._log("⚠️ 登录窗口 8 秒内没收工 —— 直接退出；桌面上若还留着浏览器窗口，请手动关掉", "warn")
        self.close()

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
            # [v2.19.9] 这里原来还有两行"顺手更新环境状态卡"的 `_env_status` 记账 ——
            # 那张卡随 `_env_selfcheck` 一起删了（`_env_status` 全仓 0 个读取点），
            # 留着就是又一个只写不读的汇。
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
        """[v2.16.1] LLM 设置保存（默认关；[v2.17 2.8] 月度预算一并保存）。

        **[v2.19.9 安全] API Key 不再明文落 launcher_config.json** —— 改走与打码密钥
        **同一套** DPAPI 机制（`launcher_v8.save_llm_key`，`secrets/llm_key.bin`），
        并把配置里的明文键删掉（不留第二份副本）。判断依据与代价见
        `launcher_v8.py` 里 `_LLM_KEY_SECRET` 上方那段说明。

        提示语按 `enc_ok` 分支：DPAPI 真不可用时**明说没加密** —— 宁可告知，不谎称
        （这条纪律与 `_save_captcha_keys` 逐字同款，也被测试钉住）。
        """
        try:
            self.config["llm_enabled"] = bool(enabled)
            self.config["llm_api_base"] = base
            self.config["llm_model"] = model
            self.config["llm_budget_month"] = int(budget)
            enc_ok = save_llm_key(key)
            if enc_ok:
                self.config.pop("llm_key", None)   # 抹掉旧明文副本
            else:
                self.config["llm_key"] = key       # DPAPI 不可用 → 保留明文但如实告知
            save_config(self.config)
            if key and not enc_ok:
                self._toast("LLM 设置已保存，但本机 DPAPI 不可用 → API Key 明文落盘",
                            "warning")
            elif enabled and not (key and base):
                # [v2.19.9] 开着开关却没填 Key/地址时**不许再报"已启用"**：
                # `run_crawler._maybe_llm_enhancer` 要求 key 与 base **同时**非空才构建
                # 客户端，否则整趟爬完什么都不会发生 —— 而界面说"已启用"，
                # 正是本工程最忌讳的那类"用无关检查冒充成功"。
                missing = "API Key" if not key else "API 地址"
                self._toast(f"LLM 已开启，但没填{missing} → 本次不会真的调用 LLM", "warning")
            elif enabled:
                self._toast("LLM 已启用（任务完成后增强）", "success")
            else:
                self._toast("LLM 已关闭（默认）", "success")
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
        """[v2.16 M5] 隐私扫描：调 tools/privacy_scanner 全项检查 → 结果 toast。

        **[v2.19.9 修复·主线程假死最长 30 秒]** 原实现是在**本槽里同步**
        `subprocess.run(..., timeout=30)` —— 子进程要读 %TEMP%、扫用户配置、抽查日志，
        最坏 30 秒里 Qt 事件循环**完全不转**：窗口一片白、点什么都没反应。
        这与上一轮修掉的 `_start()`（最长 10 秒）是同一类，只是更久，而且
        超时上限本身也更大（30s vs 10s）。

        现在照本文件既有的两条同款路径（`_refresh_privacy_status_async` /
        `_check_cookie_sources_async`）搬进后台：daemon 线程跑子进程 +
        `_deliver_async`（= `QApplication.postEvent`）回主线程。
        **不用跨线程 Signal**（本工程的开发规范 陷阱表：worker 线程 emit 信号偶发崩），
        **不用 `asyncio.run()`**（会把事件循环塞进 Qt 主线程）。

        **判据一个字没改**：结果仍旧是"从 privacy_scanner 的 stdout 里挑几行 → toast"，
        挑法与 kind 判定都与同步版逐字一致，只是不再占着主线程。

        扫描期间按钮**禁用 + 文字改成「扫描中…」**：既给反馈，也防连点起 N 个子进程
        （每个子进程都要扫一遍 %TEMP% 与日志）。
        """
        if getattr(self, "_priv_scan_busy", False):
            self._toast("上一次隐私扫描还没跑完…", "info")
            return
        # [v2.19.9] 打包版守卫：冻结后 `sys.executable` 就是 GUI 自身，拿它跑脚本
        # 会**再弹一个 Kiana 窗口**（`main()` 里是 `QApplication(sys.argv)`，不看参数）。
        # 详见 `_priv_scan_unavailable_reason()` 的说明。
        reason = _priv_scan_unavailable_reason()
        if reason:
            self._toast(reason, "warning")
            return
        btn = getattr(self.settings, "priv_scan_btn", None)
        self._priv_scan_busy = True
        if btn is not None:
            btn.setEnabled(False)
            btn.setText("扫描中…")
        # 命令行在主线程拼好：线程里既不碰 Qt，也不依赖 self 上的控件状态
        cmd = [sys.executable, str(_priv_scan_script_path())]

        def _work():
            try:
                # [v2.19.9 修复·配套] **必须显式用 utf-8 解码**：扫描器内部已把自己的
                # stdout 改成 UTF-8（否则它 print ⚠️/✅ 时会在 GBK 控制台上抛
                # UnicodeEncodeError —— 而 GUI 这条路是管道，它**必崩**，
                # 于是这个按钮从来没成功过）。这里若还按 `text=True` 走 locale(cp936)，
                # 就变成"那边写 UTF-8、这边按 GBK 解" → 满屏乱码（比崩更难看懂）。
                r = subprocess.run(cmd, capture_output=True, timeout=30,
                                   encoding="utf-8", errors="replace")
                out = r.stdout or ""
                lines = [ln for ln in out.splitlines()
                         if "⚠️" in ln or "✅" in ln or "summary" in ln]
                payload = ("\n".join(lines[:4]),
                           "warning" if "⚠️" in out else "success")
            except Exception as e:
                payload = (f"隐私扫描失败: {e}", "error")
            self._deliver_async("priv_scan", payload)

        threading.Thread(target=_work, daemon=True).start()

    def _on_priv_scan_done(self, payload):
        """后台隐私扫描回来了：先**还原按钮**，再弹 toast（结论位置与同步版一致）。

        还原放在最前面且不放在 try 里：万一 payload 结构不对，按钮也绝不能永久灰掉
        —— "扫一次按钮就废了"比"这次结论没显示"更难查、也更难自己恢复。
        """
        self._priv_scan_busy = False
        btn = getattr(self.settings, "priv_scan_btn", None)
        if btn is not None:
            btn.setEnabled(True)
            btn.setText("隐私扫描")
        try:
            msg, kind = payload
        except Exception:
            msg, kind = str(payload), "info"
        self._toast(msg, kind)

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
        # panel_alpha（40-95，默认 65）控制——用户可再经设置页滑杆/预设调整。
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

    # [v2.18.2 用户新要求] 面板玻璃透明度：滑杆拖动只更新数值 label + 写内存配置
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
        # 设置页 cookie 镜像（坑9：双入口同步保留 —— 现在设置页那格是**只读镜像**，
        # 权威值只有首页 Cookies 框一个，见 SettingsPage.build 里的说明）
        h.cookie_edit.textChanged.connect(self._sync_cookie_to_settings)
        # [v6] 路径一变就重新判一次（失效→有效要能立刻恢复，反之要立刻报警）
        h.cookie_edit.textChanged.connect(lambda _t: self._refresh_cookie_status())
        # [v7] 路径一变，「Cookies 管理」那张列表也要跟着变 ——
        # 否则用户加了来源、列表却还是旧的（"界面显示的和实际的不是一件事"）。
        h.cookie_edit.textChanged.connect(lambda _t: self._refresh_cookie_sources())
        # [v6+] 一键取登录态（**v7 起在设置页的 Cookies 管理卡里**）
        self.settings.login_btn.clicked.connect(self._on_login_cookies)
        # [v7] 自定义登录页：填了就优先于站点下拉 —— 所以要挂 tooltip 级的即时说明，
        # 让用户敲完就知道"这个网址能不能用"（判据与引擎那道闸同源，见 login_url_usable）
        self.settings.login_url_edit.textChanged.connect(self._on_login_url_changed)
        # [v7] 「Cookies 管理」建窗时先填一次列表：**空白列表会被误读成"没有 cookies"**，
        # 而实际可能正读着配置档兜底（那正是最需要看见的情况）。
        self._refresh_cookie_sources()
        # [v7 修正] 原为 `self.settings.cookie_edit_d.textChanged.connect(self._on_cookie_settings)`。
        # 设置页那一格现在是**只读镜像**（写它 = 程序性 setText，Qt 照样会发信号），
        # 于是那次信号会反过来把镜像的文字写进 `cookie_file` —— 一份配置两个方向同时写。
        # ⇒ 连线取消；权威值只有首页那个框（`_on_cookie_home`）。
        # `_on_cookie_settings` 保留（`cookie_edit_d` 仍是它的同步目标、`_wire` 里
        # 的 `refresh_from_config` 链也还在用），只是不再作为输入源。
        # [v6 M1-f] 第②跳：接管开关 + 端口接线（状态行在建窗后刷一次）
        h.sw_cdp.checkedChanged.connect(self._on_cdp_toggle)
        # [v6 P1] 第②跳：按站身份池开关（状态行在建窗后刷一次）
        h.sw_armory.checkedChanged.connect(self._on_armory_toggle)
        h.cdp_port_spin.valueChanged.connect(self._on_cdp_port_changed)
        self._refresh_cdp_status()
        # [v6 P1] 身份池状态行同样在建窗后刷一次 —— 开着但库里空，必须在界面看得见
        self._refresh_armory_status()
        self._refresh_cookie_status()   # [v6] 建窗时也判一次：旧配置里的失效路径要看得见
        # ── [v6 P2] 5 个缺口键的第②跳接线 ──────────────────────────────────
        # 首页两个（勾选组里，每次抓取都会看到）
        h.sw_headful.checkedChanged.connect(
            lambda c: self._set_cfg("headless", not c))   # **反向**：显示窗口 = 非无头
        h.sw_md.checkedChanged.connect(lambda c: self._set_cfg("export_markdown", c))
        # 设置页三个（配一次就完事，默认关 + 联网）
        st = self.settings
        st.sw_proxy_fetch.checkedChanged.connect(
            lambda c: self._set_cfg("proxy_fetcher_enabled", c))
        st.proxy_src_edit.editingFinished.connect(
            lambda: self._set_cfg("proxy_source", st.proxy_src_edit.text().strip()))
        st.sw_fp_update.checkedChanged.connect(
            lambda c: self._set_cfg("fingerprint_update_enabled", c))

    def _set_cfg(self, key, value):
        """把界面上的一个值落进配置（P2 五个缺口键统一走这里）。

        输入框用 `editingFinished`（失焦/回车）而不是 `textChanged`：
          · `textChanged` 每敲一个字就写一次盘 —— 浪费且卡；
          · 但**也不能不存**：本工程**没有 closeEvent 兜底保存**，
            只更新内存的话，用户敲的代理源重启就没了（我第一版就是这个坑）。
        """
        self.config[key] = value
        save_config(self.config)

    def _sync_cookie_to_settings(self, t):
        """权威值（首页框）→ 设置页镜像。

        ⚠️ **必须 blockSignals**：镜像是只读控件，但 `setText()` 是**程序性写入**，
        Qt 照样会发 `textChanged`；若不屏蔽就会走到 `_on_cookie_settings`，
        于是同一份配置在两张卡之间来回写（本函数正是环路的另一半）。
        既有的 `_sync_cookie_to_settings` 本来就屏蔽了，这里把**反向**那一半
        （`_refresh_cookie_sources` 里回填镜像）也照做 —— 否则"刷新列表"会变成一次写入。
        """
        if self.settings.cookie_edit_d.text() != t:
            self.settings.cookie_edit_d.blockSignals(True)
            self.settings.cookie_edit_d.setText(t)
            self.settings.cookie_edit_d.blockSignals(False)

    def _refresh_cookie_status(self):
        """cookies 路径失效时在界面直接说 —— 别让用户以为配好了。

        与引擎侧同一判据（`url_utils.cookie_file_warning`），
        与「按站身份池」状态行同一原则：**如实说，别给错觉**。
        """
        try:
            from kiana_vnext_plus.url_utils import cookie_file_warning
            w = cookie_file_warning(self.home.cookie_edit.text())
            self.home.cookie_status.setText(w)
        except Exception as e:
            self.home.cookie_status.setText(f"⚠️ cookies 路径检查失败: {e}")

    def _on_cookie_home(self, t):
        self.config["cookie_file"] = t.strip()
        save_config(self.config)

    def _on_cookie_settings(self, t):
        self.config["cookie_file"] = t.strip()
        save_config(self.config)
        self.home.refresh_from_config(self.config)

    # ── [v6+] 一键取登录态：按钮 / 结果 / 状态行刷新 ──────────────────────
    def _deliver_async(self, kind, payload):
        """后台线程 → 主线程投递（登录那条链**唯一**的通路）。

        用 `QApplication.postEvent` 而不是跨线程 Signal：本工程有过"worker 线程 emit
        信号偶发崩"的记录（本工程的开发规范 陷阱表；v2.18.1 的壁纸 worker 已统一改过来）。
        postEvent 是 Qt 文档明确的线程安全 API，且**不要求接收方还在事件循环里**。
        """
        QApplication.postEvent(self, _LoginEvent(kind, payload))

    def _selected_login_site(self) -> str:
        """登录目标 → 站点键。**自定义登录页优先于站点下拉。**

        映射与边界判定都在 `login_site_key_at` / `site_key_from_login_url` 里（都可单测）。
        返回空串 = "选不出来" → 调用方必须**什么都不做**（拿错站去建目录写 cookies 更糟）。

        [v7 权衡] 填了自定义网址时**不看下拉**：不去把网址回填成某个站，
        因为"用户刚敲的那个网址必须说了算"。副作用是下拉那一刻是"摆设"——
        所以状态行会明说当前用的是哪个（`_login_target_desc`），不做静默。
        """
        try:
            raw = self.settings.login_url_edit.text().strip()
        except Exception:
            raw = ""
        if raw:
            return site_key_from_login_url(raw)
        try:
            idx = self.settings.login_site_combo.currentIndex()
        except Exception:
            return ""
        return login_site_key_at(getattr(self.settings, "login_sites", []) or [], idx)

    def _login_target_desc(self) -> str:
        """给状态行/确认框用的一句话：这次到底要打开哪个网址（以及它被认成哪个站）。"""
        site = self._selected_login_site()
        label = site_display_name(site) or site or "（认不出站点）"
        try:
            raw = self.settings.login_url_edit.text().strip()
        except Exception:
            raw = ""
        return f"{label}（站点键 {site}）" if not raw else f"{label}（从自定义网址推出站点键 {site}）"

    def _on_login_url_changed(self, _t=None):
        """自定义网址一变就**当场**说它能不能用（别等点了按钮才发现被闸拦下）。"""
        try:
            raw = self.settings.login_url_edit.text().strip()
        except Exception:
            return
        if not raw:
            self.settings.login_status.setText("")
            return
        ok, why = login_url_usable(raw)
        if not ok:
            self.settings.login_status.setText(f"⚠️ {why}")
            return
        key = site_key_from_login_url(raw)
        if not key:
            from kiana_vnext_plus import cookie_profile as cp
            known = "、".join(sorted(m["site_key"] for m in cp.SITE_PROFILES.values()))
            self.settings.login_status.setText(
                f"⚠️ 从 {raw} 里**认不出站点**（cookies 要落到哪个配置档目录由它决定，"
                f"猜一个比不做更糟）。可用站点：{known}　"
                f"→ 也可以清空这一格、改用上面的站点下拉。")
            return
        self.settings.login_status.setText(
            f"自定义登录页可用：将用 {raw} 打开，站点键 {key}（cookies 落到它的配置档目录）。")

    def _on_login_cookies(self):
        """「登录并取 cookies」：先讲清楚要发生什么 → 后台线程开有头浏览器 → 等用户关窗口。

        本方法**只做"递选择 + 改界面文案"**，一步阻塞操作都不做 ——
        真正等用户的那几分钟在 `CookieLoginBridge` 的线程里。

        [v7] 位置从首页搬到设置页的「Cookies 管理」卡；**判定与措辞一个字没改**。
        """
        st = self.settings
        if self._login_bridge.running:
            self._toast("上一次登录还没结束：先关掉那个浏览器窗口，或等它自己结束", "warning")
            return
        try:
            raw_url = st.login_url_edit.text().strip()
        except Exception:
            raw_url = ""
        # 网址闸**先判一次**（判据与 open_profile_for_login 那道闸同源）：
        # 那道闸在后台线程里，拒绝时界面只会看到 bad_url —— 用户不知道自己敲错了什么。
        ok_url, why_url = login_url_usable(raw_url)
        if not ok_url:
            st.login_status.setText(f"⚠️ {why_url}")
            self._toast(why_url, "error")
            return
        site = self._selected_login_site()
        if not site:
            if raw_url:
                msg = (f"从 {raw_url} 里认不出站点 —— 拒绝打开浏览器"
                       f"（宁可不做，也不拿猜的站名去建目录、写 cookies）")
            else:
                msg = "站点列表读不到（引擎包 cookie_profile 不可用）—— 无法取登录态"
            st.login_status.setText(f"⚠️ {msg}")
            self._toast(msg, "error")
            return
        label = site_display_name(site)
        target = ""
        try:
            from kiana_vnext_plus import cookie_profile as cp
            # `profile_dir()` 默认 create=False —— 只是**算路径**给用户看，不碰磁盘
            target = str(cp.profile_dir(site) / "cookies.txt")
        except Exception:
            pass
        # 开始前把话说清楚（这是硬要求）：会弹**有头**浏览器、**关窗口 = 完成**、
        # 不想登录就直接关掉（那样什么都不会写）。不让用户"点完才发现弹了个窗口"。
        from PySide6.QtWidgets import QMessageBox
        _open_what = f"自定义登录页 {raw_url}" if raw_url else f"【{label}】的默认登录页"
        ask = QMessageBox.question(
            self, "登录并取 cookies",
            f"即将打开{_open_what}的专用浏览器窗口（不是你日常用的那个浏览器）。\n\n"
            f"① 在弹出的窗口里完成登录（扫码 / 输密码都行）；\n"
            f"② 登录完成后【直接关闭那个窗口】—— cookies 会自动就位。\n\n"
            f"cookies 会写到：\n{target or '该站配置档目录下的 cookies.txt'}\n\n"
            f"成功后它会被加进「Cookies 管理」列表的**最前面**"
            f"（引擎合并多份 cookies 是先到先得，排前面才生效）。\n\n"
            f"不想登录就直接关掉那个窗口：本程序**不会写出任何东西**，也不会动你日常"
            f"浏览器的数据（两边互不影响）。",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
        if ask != QMessageBox.StandardButton.Ok:
            st.login_status.setText("已取消（没有打开浏览器）")
            return
        self._set_login_busy(True,
                             f"⏳ 等待登录中…请在【{label}】窗口里登录，"
                             f"登录完成后关掉那个窗口（不想登录也直接关掉）")
        self._log(f"── 等待 {label} 登录：专用配置档窗口已开，关掉它即完成 ──", "title")
        if not self._login_bridge.start(site, url=(raw_url or None)):
            self._set_login_busy(False, "⚠️ 上一次登录还没结束（没有重复打开浏览器）")

    def _set_login_busy(self, busy: bool, text: str = ""):
        """进行中：按钮与下拉禁用 + 状态行说明；结束：恢复。

        禁用是**唯一**能防"点两次开两个浏览器"的地方（同一个配置档目录开两个
        Chrome 会撞 singleton 锁，第二个窗口直接起不来，看起来像"点了没反应"）。

        [v7] 自定义登录页那格**不禁用**：登录期间改它不会影响正在跑的那一次
        （url 在 `start()` 时就已取好并传给桥），却能让用户顺便准备下一个站点。
        """
        st = self.settings
        st.login_btn.setEnabled(not busy)
        if busy:
            st.login_site_combo.setEnabled(False)
        elif list(getattr(st, "login_sites", []) or []):
            st.login_site_combo.setEnabled(True)
        if text:
            st.login_status.setText(text)

    def _on_login_done(self, res):
        """后台线程回来了：如实报告 → 让刚拿到的 cookies 真被引擎用上 → 刷列表与状态行。

        **"成功"只认一件事**：核验到该站的登录凭据真的在文件里（`login_cookie_verified`）。
        "文件非空"不算 —— 未登录也会有匿名 cookie，那正是"看起来成功"的高发点。
        """
        self._set_login_busy(False)
        res = dict(res or {})
        s = summarize_login_result(res, res.get("login_cookie_verified"))
        self.settings.login_status.setText(s["headline"])
        # 结论文字**自带** ✅/⚠️/❌，这里不再补一个前缀（否则日志里会出现"✅ ✅ …"）
        self._log(s["headline"], "ok" if s["ok"] else ("warn" if s["level"] == "warning" else "err"))
        if s["detail"]:
            self._log("    " + s["detail"], "info")
        self._toast(s["headline"], s["level"])
        if s["retry"]:
            self.settings.login_status.setText(s["headline"] + "（想重试就再点一次按钮）")
        if s["ok"]:
            self._use_fresh_cookies(res.get("cookies_path"))
        # [v7] 列表也要刷：登录前那份列表可能还没有这个配置档来源，
        # 不刷就会出现"上面说 ✅ 已就位、下面列表里找不到它"的自相矛盾界面。
        self._refresh_cookie_sources()
        # **无论成败都要刷那一行隐私状态**：成功要看到它变 ✅；失败也要看到它**没变**
        # （不刷的话，界面会停留在上一次的判断上 —— 那本身就是一种谎报）。
        self._refresh_privacy_status_async()

    def _use_fresh_cookies(self, path):
        """把刚写出的 cookies.txt 路径填进首页 Cookies 框 —— 不填，引擎**可能根本不用它**。

        为什么必须做：引擎的 cookies 来源优先级是
            `KIANA_COOKIE_FILES`(← 就是这个框) > 默认位置 > `profiles/` 配置档兜底。
        只要框里**有任何**路径，那条"自动发现配置档"的兜底就**不再生效**（语义如此）。
        于是"取完 cookies、框里却还指着旧文件" = 新登录**静默不生效**，而界面显示成功
        —— 正是本工程最忌讳的那种。填进去（旧值前置保留，见 `merge_cookie_path_value`）
        才能让"取到了"和"用上了"是同一件事。
        """
        try:
            p = str(path or "")
            if not p:
                return
            cur = self.home.cookie_edit.text().strip()
            new = merge_cookie_path_value(cur, p)
            if new == cur:
                return
            # 旧值只在日志里留个底（是个路径，不是凭据）；框里也能看到它还在（分号后面）
            self._log(f"已把新 cookies 路径排到首页 Cookies 框最前（原值：{cur or '（空）'}）", "info")
            # setText 会触发 textChanged → 落配置 + 同步设置页镜像 + 刷新路径状态行 + 刷管理列表（既有接线）
            self.home.cookie_edit.setText(new)
        except Exception as e:
            self._toast(f"cookies 路径没能自动填进输入框（请手动填）：{e}", "warning")

    # ── [v7] 「Cookies 管理」：列出 / 增 / 删 / 看状态 ──────────────────────
    def _cookie_box_value(self) -> str:
        """首页 Cookies 框的当前值 = **唯一权威值**（引擎 `_start()` 读的也是它）。"""
        try:
            return self.home.cookie_edit.text().strip()
        except Exception:
            return ""

    def _set_cookie_box_value(self, value: str):
        """改权威值（会沿既有接线落配置 + 同步镜像 + 刷两条状态行）。

        改动**不直接写 `os.environ`**：env 是 `EngineBridge.start()` 那一刻
        按 `cookie_file` 写的，且 `_privacy_status_text()` 也要临时改它 ——
        界面上再插一手就成了第三条并发写同一全局变量的线
        （"取完 cookies 反而把本次爬取要用的 cookies 抹了"那类事故）。
        ⇒ 一次任务的 cookie 以 `_start()` 那一刻的配置为准，语义明确。
        """
        if value != self.home.cookie_edit.text():
            self.home.cookie_edit.setText(value)

    def _cookie_source_rows(self) -> list:
        return cookie_source_rows(self._cookie_box_value())

    def _refresh_cookie_sources(self, *_a):
        """重画来源列表 + 写下方的说明行。**不联网、不开浏览器**（纯读磁盘）。"""
        try:
            rows = self._cookie_source_rows()
        except Exception as e:
            self.settings.cookie_manager_status.setText(f"⚠️ 读取 cookies 来源失败：{type(e).__name__}: {e}")
            return
        # 镜像（只读）跟随权威值：两种驱动都要覆盖 —— (a) 首页框变了触发本函数，
        # (b) 本函数被显式调用（如"刷新列表"）。blockSignals 防止与 _on_cookie_settings 打环。
        try:
            cur = self._cookie_box_value()
            if self.settings.cookie_edit_d.text() != cur:
                self.settings.cookie_edit_d.blockSignals(True)
                self.settings.cookie_edit_d.setText(cur)
                self.settings.cookie_edit_d.blockSignals(False)
        except Exception:
            pass
        self.settings.cookie_sources_view.setPlainText(_cookies_manager_text(rows))
        self.settings.cookie_manager_status.setText(_cookies_manager_note(rows))

    def _selected_cookie_source(self) -> dict:
        """列表里选中的那一项（`{}` = 没选中/选不中）。

        ⚠️ **行号要减 1**：列表文本的第 0 行是表头说明（"按顺序生效…"），
        第 1 行才是第 1 个来源 —— 所以 `rows[blockNumber() - 1]`。
        （我第一版忘了这个偏移，于是**第 1 项永远选不中、最后一行反而越界**：
        "移除/设最优先"点下去什么也不做。离屏测试当场抓住。）

        选中行号对不上任何一项时返回 `{}` = 调用方**什么都不做** ——
        "看着选中了其实没选"时乱删会毁掉用户的配置。
        """
        try:
            rows = self._cookie_source_rows()
            idx = int(self.settings.cookie_sources_view.textCursor().blockNumber()) - 1
        except Exception:
            return {}
        if 0 <= idx < len(rows):
            return rows[idx]
        return {}

    def _add_cookie_file(self):
        """「添加文件…」：选一份 cookies.txt → 排到最前（`cookie_paths_add_front`）。"""
        f = self.settings._pick_cookie()
        if not f:
            return
        cur = self._cookie_box_value()
        new = cookie_paths_add_front(cur, f)
        if new == cur:
            self.settings.cookie_manager_status.setText(f"这一份已经在列表里了：{f}")
            return
        self._set_cookie_box_value(new)
        self._log(f"已加入 cookies 来源（排最前）：{f}", "info")
        self.settings.cookie_manager_status.setText(
            f"已加入并排在**最前**：{f} —— 引擎合并多份 cookies 是先到先得，排前面才盖得住同名旧凭据。")

    def _remove_selected_cookie_source(self):
        """「移除选中」：只从**配置**里移除，绝不删磁盘文件。"""
        row = self._selected_cookie_source()
        path = str(row.get("path") or "")
        if not path:
            self.settings.cookie_manager_status.setText("先在列表里点一下要移除的那一项。")
            return
        origin = str(row.get("origin") or "")
        if origin != "explicit":
            # 自动发现的来源（默认位置/配置档兜底）**不在输入框里**，删不掉：
            # 它由"框里为空"这个事实决定。这里如实说明，并给出真正能生效的做法。
            self.settings.cookie_manager_status.setText(
                f"⚠️ {path} 是**自动发现**的来源（{row.get('origin_label')}），不在输入框里、无法单独移除。\n"
                f"　　它是「框里什么都没配」时的兜底；要让它不再生效：在输入框里填上别的来源，"
                f"或到磁盘上删掉该文件（本程序**不会**替你删文件）。")
            return
        new = cookie_paths_drop(self._cookie_box_value(), path)
        self._set_cookie_box_value(new)
        self._log(f"已从 cookies 来源里移除：{path}（磁盘文件未动）", "warn")
        self.settings.cookie_manager_status.setText(
            f"已移除：{path}（**磁盘上的文件没有被删**）。"
            + ("输入框现在为空 → 引擎回到自动发现（默认位置 → 配置档）。" if not new else ""))

    def _promote_selected_cookie_source(self):
        """「设为最优先」：把选中项移到最前（只对输入框里的显式项有意义）。"""
        row = self._selected_cookie_source()
        path = str(row.get("path") or "")
        if not path:
            self.settings.cookie_manager_status.setText("先在列表里点一下要提前的那一项。")
            return
        if str(row.get("origin") or "") != "explicit":
            self.settings.cookie_manager_status.setText(
                f"{path} 是自动发现的来源（{row.get('origin_label')}），它排在所有显式来源**之后**，"
                f"本来就无法再往后。要让它优先，把它的路径填进输入框。")
            return
        cur = self._cookie_box_value()
        new = cookie_paths_add_front(cur, path)
        if new == cur:
            self.settings.cookie_manager_status.setText(f"{path} 已经在最前面了。")
            return
        self._set_cookie_box_value(new)
        self.settings.cookie_manager_status.setText(f"已把 {path} 提到最前（先到先得，它现在压得住同名旧凭据）。")

    def _clear_cookie_box(self):
        """「清空全部」：权威值清空 → 引擎回到自动发现链（默认位置 → profiles 兜底）。"""
        self._set_cookie_box_value("")
        self._log("已清空 Cookies 框 —— 引擎将自动发现（默认位置 → profiles/*/cookies.txt）", "warn")
        self.settings.cookie_manager_status.setText(
            "已清空输入框。引擎现在会**自动发现**：默认位置 → 各站配置档 profiles/*/cookies.txt。")

    def _open_selected_cookie_dir(self):
        """在资源管理器里定位选中的那一份（文件不存在就打开它的上级目录）。"""
        row = self._selected_cookie_source()
        path = str(row.get("path") or "")
        if not path:
            self.settings.cookie_manager_status.setText("先在列表里点一下要打开的那一项。")
            return
        target = Path(path)
        try:
            if target.is_file():
                subprocess.Popen(["explorer.exe", "/select,", str(target)])
            else:
                subprocess.Popen(["explorer.exe", str(target.parent)])
        except Exception as e:
            self.settings.cookie_manager_status.setText(f"打开失败：{e}")

    def _check_cookie_sources_async(self):
        """「检查有效性」：**后台线程**逐个文件核验登录凭据 → postEvent 回主线程填列表。

        **为什么必须在后台**：`_privacy_status_text()` 里有一次 **10 秒超时**的联网探测
        （`cookie_health.check_sites` → B站 nav 接口）。在 GUI 线程算 = 界面假死十秒。
        与本文件既有的 `_refresh_privacy_status_async` 同一模式（也同一理由）。

        爬取进行中**跳过**：探针要临时改进程级 `KIANA_COOKIE_FILES`，而
        `EngineBridge.start()` 也在写它 —— 两条线并发写同一全局变量，
        回滚那一下可能把引擎刚设好的值盖回去（"检查完 cookies 反而把本次爬取要用的抹了"）。
        """
        if getattr(self, "_cookie_check_busy", False):
            self.settings.cookie_manager_status.setText("⏳ 上一次检查还没跑完…")
            return
        # 爬取进行中就**不做会写环境变量的那种检查**：这条链末端的
        # `_privacy_status_text()` 要临时改**进程级** `KIANA_COOKIE_FILES`，而
        # `EngineBridge.start()` 也在写它 —— 两条线并发写同一全局变量，
        # 回滚那一下可能把引擎刚设好的值盖回去（"检查完 cookies 反而把本次爬取要用的抹了"）。
        # 与 `_refresh_privacy_status_async` 同一条纪律，**宁可少做一次检查**。
        if self._engine_running():
            self.settings.cookie_manager_status.setText(
                "⏸ 本次爬取进行中，登录态核验已跳过 —— 等这趟爬完再点「检查有效性」"
                "（核验要临时改进程级环境变量，爬取期间改它可能把本次任务要用的 cookies 抹掉，"
                "所以这里宁可少做一次检查）。")
            return
        box = self._cookie_box_value()          # 控件值在主线程取，线程里不碰 Qt

        def _work():
            try:
                rows = self._cookie_source_rows()
                text = _cookies_health_text(rows, self._privacy_status_text(box))
            except Exception as e:
                text = f"⚠️ 有效性检查失败：{type(e).__name__}: {e}"
            self._deliver_async("cookies_check", text)

        self._cookie_check_busy = True
        self.settings.cookie_manager_status.setText("⏳ 正在后台核验各来源的登录凭据（含一次 B站在线核验，最多十几秒）…")
        threading.Thread(target=_work, daemon=True).start()

    def _on_cookie_check_done(self, text: str):
        """后台核验回来了：原样显示 + 记日志。**结论里不补任何 ✅**（措辞自己带）。"""
        self._cookie_check_busy = False
        self.settings.cookie_manager_status.setText(str(text))
        self._log("── cookies 有效性检查 ──", "info")
        for line in str(text).splitlines():
            self._log("    " + line, "info")


    def _refresh_privacy_status_async(self, cookie=None, *, after_engine_start: bool = False):
        """在后台算登录态文本，算完 postEvent 回主线程 → **写日志页**（`_on_privacy_status`）。

        **为什么不能在主线程算**：`_privacy_status_text()` 里有一次**联网**探测
        （`cookie_health.check_sites` → B站 nav 接口，timeout=10s）。
        [v2.19.9] `_start()` 里那次同步调用已改掉（点「开始爬取」原来会冻最多 10 秒），
        现在两条路都走这里：`_on_login_done`（刚取完 cookies 要刷一次）
        与本函数的新调用点 `_start()`。
        文本与显示**是同一份实现**（`_privacy_status_text`），不是第二份判定。

        **[v2.19.9] 显示位置从首页那行小字改成日志页** —— 理由见 `_on_privacy_status`。

        `cookie`：要拿去探测的 Cookies 框值。**默认在主线程现取**（线程里不碰 Qt）。
        `_start()` 那条路会显式传 `cfg["cookie_file"]`：与 `EngineBridge.start()`
        写进环境变量的**是同一个字符串**，这是下面那条竞态判断成立的前提。

        `after_engine_start`：调用点是否**紧跟** `engine.start()` 之后。
        它只决定"要不要跳过探测"，因为 `_privacy_status_text()` 要临时改**进程级**
        `KIANA_COOKIE_FILES`，而 `EngineBridge.start()` 也写它 —— 两条线并发写同一个
        全局变量时，回滚那一下可能把引擎刚设好的值盖回去
        （"取完 cookies 反而把本次爬取要用的 cookies 抹了"）。于是：
          · `False`（`_on_login_done` 那条路）：用户随时可能点「开始爬取」，
            `EngineBridge.start()` 与探测**可能**并发 ⇒ 探测期间引擎在跑就跳过，
            **宁可少做一次检查**，文案如实说明跳过了、什么时候补上；
          · `True`（`_start()` 那条路）：探测在 `engine.start()` 返回**之后**才发起，
            而那个变量**只在 `EngineBridge.start()` 里写一次**、返回后整趟爬取不再动它
            ⇒ 不存在并发写者，**无需跳过**。（这里若照抄上面那条跳过，
            引擎必然在跑 ⇒ 每次点「开始爬取」都只会看到"已跳过"，结果等于没有。）
        """
        if cookie is None:
            cookie = self.home.cookie_edit.text()   # 控件值在主线程取，线程里不碰 Qt

        def _work():
            if not after_engine_start and self.engine.running:
                self._deliver_async(
                    "privacy",
                    "隐私状态: ⏸ 本次爬取进行中，登录态自检已跳过 —— 下次点「开始爬取」时会自动刷新")
                return
            try:
                text = self._privacy_status_text(cookie)
            except Exception as e:
                text = f"隐私状态: ⚠️ 自检失败（{str(e)[:60]}）"
            self._deliver_async("privacy", text)

        threading.Thread(target=_work, daemon=True).start()

    # ── [v6 P1 补] 按站身份池：导入 / 查看 ──────────────────────────────
    def _armory(self):
        """拿到身份池对象；失败返回 `(None, 错误说明)`。

        构造必须给 `(db_path, master_password)` —— 走 `armory_db_path()` **唯一实现**，
        与引擎读的是**同一个库**（否则界面显示的和引擎用的不是一回事）。
        """
        try:
            from kiana_vnext_plus.cli import _read_master_password
            from kiana_vnext_plus.cookie_armory import CookieArmory, armory_db_path
            return CookieArmory(armory_db_path(), _read_master_password()), ""
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"

    @staticmethod
    def _site_from_filename(path: str) -> str:
        """从 `www.bilibili.com_cookies (1).txt` 这类文件名里猜站点。

        只做**预填**，用户可在弹窗里改 —— 猜错了不至于把号存到错误的站点下。
        [v6] 归一化走 `cookie_armory.normalize_site` **唯一实现** ——
        早先这里自己写了一遍剥 `www.`，与 `acquire_identity` 那边一旦不一致，
        就回到"**存了却查不到**"（真机踩过：存 `bilibili.com`、查 `www.bilibili.com`）。
        """
        import re as _re
        base = os.path.basename(path or "")
        base = _re.sub(r"\.(txt|json)$", "", base, flags=_re.I)
        base = _re.sub(r"_?cookies?\s*\(\d+\)\s*$", "", base, flags=_re.I)
        base = _re.sub(r"_?cookies?$", "", base, flags=_re.I).strip(" _-")
        m = _re.search(r"([a-z0-9-]+(?:\.[a-z0-9-]+)+)", base, _re.I)
        raw = m.group(1).lower() if m else ""
        try:
            from kiana_vnext_plus.cookie_armory import normalize_site
            host = normalize_site(raw)
        except Exception:
            host = raw
        # 文件名里没有域名（如裸的 `cookies.txt`）→ 留空，让用户在弹窗里自己填
        return host if "." in host else ""

    def _import_identity(self):
        """选一个 cookies.txt → 加密入库 → 刷状态行。

        **cookie 明文绝不落日志、绝不进配置**：只经 `parse_cookie_file` 解析后
        直接交给 `CookieArmory`（Fernet 加密入库）。
        """
        from PySide6.QtWidgets import QFileDialog, QInputDialog, QMessageBox
        arm, err = self._armory()
        if arm is None:
            QMessageBox.warning(self, "身份池不可用", f"打不开身份库：{err}")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 cookies.txt", str(Path.home() / "Downloads"),
            "cookies 文件 (*.txt);;所有文件 (*)")
        if not path:
            return
        site = self._site_from_filename(path)
        site, ok = QInputDialog.getText(self, "确认站点", "这份 cookies 属于哪个站点？"
                                        "（同站多个身份才会互相轮换）", text=site)
        if not ok or not site.strip():
            return
        name, ok = QInputDialog.getText(self, "身份备注", "给这个身份起个名字"
                                        "（仅本机可见，用于区分账号）",
                                        text=Path(path).stem[:24])
        if not ok or not name.strip():
            return
        try:
            from kiana_vnext_plus.cookie_armory import parse_cookie_file
            cookie_str = parse_cookie_file(path)
            if not cookie_str:
                QMessageBox.warning(self, "文件不可用",
                                    "这个文件里没解析出任何 cookie。\n"
                                    "请确认它是 Netscape 格式的 cookies.txt。")
                return
            saved = arm.add_account(site.strip().lower(), name.strip(), cookie_str)
        except Exception as e:
            QMessageBox.warning(self, "导入失败", f"{type(e).__name__}: {e}")
            return
        if saved:
            QMessageBox.information(self, "已入库",
                                    f"✅ {name.strip()} @ {site.strip().lower()} 已加密入库。\n"
                                    "开着「按站身份池」时，抓取会按站取用它。")
        else:
            QMessageBox.warning(self, "入库失败",
                                "加密入库没成功（库里的旧记录可能占用了同名身份）。")
        # 开一下开关，让用户立刻看到效果（默认关是刻意的：这是风险敏感特性）
        if saved and not self.home.sw_armory.isChecked():
            self.home.sw_armory.setChecked(True)
        self._refresh_armory_status()

    def _show_identities(self):
        """列出库里的身份（**不含 cookie 明文**）"""
        from PySide6.QtWidgets import QMessageBox
        arm, err = self._armory()
        if arm is None:
            QMessageBox.warning(self, "身份池不可用", f"打不开身份库：{err}")
            return
        try:
            rows = arm.list_accounts()
        except Exception as e:
            QMessageBox.warning(self, "读取失败", f"{type(e).__name__}: {e}")
            return
        if not rows:
            QMessageBox.information(self, "身份池为空",
                                    "库里还没有任何身份。\n用左边的「导入身份…」加一个。")
            return
        lines = [f"{'站点':<22}{'身份':<20}健康分  冷却", "-" * 62]
        for r in rows:
            cd = f"{r['cooldown_left']}s" if r.get("cooldown_left", 0) > 0 else "—"
            lines.append(f"{str(r.get('site'))[:20]:<22}{str(r.get('name'))[:18]:<20}"
                         f"{r.get('health')!s:<8}{cd}")
        lines += ["", f"共 {len(rows)} 个身份（cookie 明文已加密，界面不显示）"]
        QMessageBox.information(self, "按站身份池", "\n".join(lines))

    # ── [v6 P1] 按站身份池：开关 / 状态行 ──
    def _on_armory_toggle(self, on):
        """开关落配置并刷新状态行。

        **默认关**（风险敏感）：开启后引擎会按站取用身份库里的 cookies 轮换，
        用的是**你的账号**跑机器量级的访问——所以要用户显式点头。
        """
        self.config["cookie_armory_enabled"] = bool(on)
        save_config(self.config)
        self._refresh_armory_status()

    def _refresh_armory_status(self):
        """状态行：**如实说库里到底有没有身份**。

        为什么必须显示：开着开关但库里空的时候，引擎会**按无 cookie 跑**——
        表面"已启用"，实际什么都没发生。第 36 轮踩过同款坑
        （`cookie_armory_enabled` 缺一跳接线 → 功能从没被启用过，界面却毫无异样）。
        这里宁可说"未配置任何身份"，也不给"看起来生效了"的错觉。
        """
        try:
            if not self.home.sw_armory.isChecked():
                self.home.armory_status.setText("按站身份池：关闭（用单份 cookies）")
                return
            from kiana_vnext_plus.cli import _read_master_password
            from kiana_vnext_plus.cookie_armory import CookieArmory, armory_db_path
            # 走**唯一**路径实现（与 `crawler` / `cli` 同一份）——
            # 否则界面数的是这个库、引擎读的是那个库，状态行就成了摆设。
            arm = CookieArmory(armory_db_path(), _read_master_password())
            rows = arm.list_accounts()
            n = len(rows)
            if n == 0:
                self.home.armory_status.setText(
                    "⚠️ 按站身份池：已开启，但**库里没有任何身份** —— "
                    "本次会按无 cookies 抓取。请先用 `cookie-add` 导入账号。")
            else:
                sites = sorted({str(r.get("site") or "?") for r in rows})
                self.home.armory_status.setText(
                    f"✅ 按站身份池：已开启，库中 {n} 个身份，覆盖 {len(sites)} 个站点")
        except Exception as e:
            # 状态行自身出错**不许静默**——否则用户以为"没提示=没问题"
            self.home.armory_status.setText(f"⚠️ 身份池状态检查失败: {e}")

    # ── [v6 M1-f] 接管已登录浏览器：开关 / 端口 / 状态行 ──
    def _on_cdp_toggle(self, on):
        """开关落配置并刷新状态行。默认关，开启需真的连得上端口才有意义。"""
        self.config["cdp_attach"] = bool(on)
        save_config(self.config)
        self._refresh_cdp_status()

    def _on_cdp_port_changed(self, port):
        self.config["cdp_port"] = int(port)
        save_config(self.config)
        if self.home.sw_cdp.isChecked():
            self._refresh_cdp_status()

    def _refresh_cdp_status(self):
        """状态行：如实显示接管到底可不可用。

        只在**开启时**探测（关闭时无需连端口）。探测是同步调用且位于 UI 线程，
        故超时压到 0.6s，避免界面卡顿。
        """
        try:
            if not self.home.sw_cdp.isChecked():
                self.home.cdp_status.setText("接管已登录浏览器：关闭（用引擎自带浏览器）")
                return
            from kiana_vnext_plus.solver_engine import probe_cdp_endpoint
            ok, msg = probe_cdp_endpoint(self.home.cdp_port_spin.value(), timeout=0.6)
            self.home.cdp_status.setText(("✅ " if ok else "⚠️ ") + msg)
        except Exception as e:
            # 状态行自身出错**不许静默**——否则用户以为"没提示=没问题"
            self.home.cdp_status.setText(f"⚠️ 接管状态检查失败: {e}")

    # ── 启动/停止/暂停/重试（v8 逻辑平移，含 cookie 传入引擎修复） ──
    def _import_url_file(self):
        """[v2.17 1-4] 从文本文件导入 URL（逐行追加到 url_edit）。

        [v2.19.9] 选文件仍在主线程（Qt 对话框必须如此），但**读取放到后台** ——
        用户完全可能选一个几万行的 URL 清单（本工程就有 `run_crawler.py -f urls.txt`
        那种用法），`read_text` 整份读在主线程上是白冻一下。读到的文本经
        `postEvent` 回主线程再追加（`_append_url_text` 碰控件，不能在线程里调）。
        """
        try:
            from PySide6.QtWidgets import QFileDialog
            path, _ = QFileDialog.getOpenFileName(
                self, "导入 URL 文件", "", "文本文件 (*.txt *.md *.csv);;所有文件 (*)")
            if not path:
                return
        except Exception as e:
            self._toast(f"导入失败: {e}", "error")
            return

        def _work():
            try:
                from pathlib import Path as _P
                text = _P(path).read_text(encoding="utf-8", errors="ignore")
                self._deliver_async("url_file", (text, path, ""))
            except Exception as e:
                self._deliver_async("url_file", (None, path, str(e)))

        threading.Thread(target=_work, daemon=True).start()

    def _on_url_file_loaded(self, payload):
        """后台读文件回来了：追加到 URL 框 + 弹原来的提示语。"""
        try:
            text, path, err = payload
        except Exception:
            text, path, err = None, "", str(payload)
        if err:
            self._toast(f"导入失败: {err}", "error")
            return
        self._append_url_text(text or "")
        self._toast(f"已导入 {path}", "success")

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
        # [v2.19.9 修复·点「开始爬取」假死最长 10 秒] 本函数原来的**第一句**是
        # `self._check_cookies()` —— 它会一路走到 `cookie_health.check_sites()`
        # → `requests.get("https://api.bilibili.com/x/web-interface/nav", timeout=10)`，
        # **在 GUI 主线程同步联网**：界面最多冻 10 秒，而且爬取要等这次探测跑完
        # 才真正启动。本文件另外两条同类路径（`_check_cookie_sources_async` /
        # `_refresh_privacy_status_async`）早就搬进后台线程并写明了"在 GUI 线程算
        # = 假死十秒"，只有这一条没改。
        # ⇒ 同步调用已删除，改成本函数**末尾**的后台探测；那个"算 + 显示"的薄壳
        # `_check_cookies()` 因此一个调用点都不剩，一并删除（见文件内说明）。
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
            # [v6 M1-f] 第③跳：接管已登录浏览器（首页开关，默认关）
            "cdp_attach": bool(h.sw_cdp.isChecked()),
            "cdp_port": int(h.cdp_port_spin.value()),
            # [v6 P1] 第③跳：按站身份池（首页开关，默认关）。
            # 第④跳在 `launcher_v8.EngineBridge` 把它带进 run_crawler 的 gcfg，
            # 第⑤跳是 `run_crawler` 写入 `GlobalConfig`，第⑥跳是 `DEFAULT_GLOBAL` 的键。
            "cookie_armory_enabled": bool(h.sw_armory.isChecked()),
            # ── [v6 P2] 第③跳：5 个缺口键（先前连这一跳都没有 → 引擎恒读默认值）──
            # `headless` 在这里做**唯一一次**反向：界面是「显示浏览器窗口」，
            # 引擎要的是 `headless`。第④⑤跳原样传递，不再翻第二次。
            "headless": not bool(h.sw_headful.isChecked()),
            "export_markdown": bool(h.sw_md.isChecked()),
            # 设置页控件挂在 `self.settings` 上（`_start` 里没有 `st` 这个局部名——
            # 我第一版写成 `st.` 被 ruff F821 当场抓住；`py_compile` 看不见这种错）
            "proxy_fetcher_enabled": bool(self.settings.sw_proxy_fetch.isChecked()),
            "proxy_source": self.settings.proxy_src_edit.text().strip(),
            "fingerprint_update_enabled": bool(self.settings.sw_fp_update.isChecked()),
        }
        self.config.update({k: cfg[k] for k in
                            ("depth", "pages", "outdir", "dl_video", "dl_image",
                             "dl_audio", "filter_words", "resolution", "subs", "thumb", "info",
                             "robots_respect")})
        self.config["sanitize"] = h.sw_sanitize.isChecked()
        self.config["filter_text"] = cfg["filter_words"]
        save_config(self.config)

        # [v6 M1-f] "不能乱用"：开启接管时**先探测端口**，不通就**不起任务**并给可读提示。
        # 绝不静默回退成"无登录态抓取"——那会表现为"页面内容不对"，用户根本查不出原因。
        if cfg["cdp_attach"]:
            from kiana_vnext_plus.solver_engine import probe_cdp_endpoint
            _ok, _msg = probe_cdp_endpoint(cfg["cdp_port"], timeout=0.8)
            if not _ok:
                self.home.cdp_status.setText(f"⚠️ {_msg}")
                self._toast(f"无法接管浏览器，任务未启动：{_msg}", "error")
                return
            self.home.cdp_status.setText(f"✅ {_msg}")

        self.engine.start(urls, cfg)
        # [v2.19.9] 启动之后**在后台**补一次登录态探测，结果写日志页。
        # **顺序是刻意的**（先启动、后探测，不是反过来）：
        #   1. 不阻塞启动 —— 判据：登录态只是**提示**，不是爬取的前置条件。
        #      `_start()` 里从来没有任何分支读探测结果；`cfg["cookie_file"]` 该给的
        #      照给，引擎自己用 cookies 跑，没登录就是 480P，但**能跑**。
        #      所以"等探测回来再启动"是白让用户等（最坏 10 秒），不可取。
        #   2. 顺手把一条老竞态关死 —— 探测要临时改进程级 `KIANA_COOKIE_FILES`，
        #      而 `EngineBridge.start()` 也写它。传进去的 `cfg["cookie_file"]`
        #      与 `EngineBridge.start()` 写进环境变量的**是同一个字符串**，
        #      且这一步在它**返回之后**执行 ⇒ 探测取到的快照就等于它写进去的值，
        #      回滚回去的还是同一个值（写 == 快照），**不存在"把本次爬取要用的
        #      cookies 抹掉"的窗口**（这正是 `after_engine_start=True` 的含义）。
        self._refresh_privacy_status_async(cfg["cookie_file"], after_engine_start=True)

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
        """把最新任务里 `status='dead'` 的页重置为 pending（下次启动同 URL 自动续爬）。

        **[v2.19.9 修复·主线程最多干等 5 秒]** 原实现在主线程 `sqlite3.connect` + UPDATE。
        `sqlite3.connect` 的默认 `timeout=5.0` —— 引擎**正持写锁**时（爬取进行中就是常态），
        这里会一直等到锁释放或 5 秒超时；界面全程不动。与 `_start()` 那次修复同类。
        现改为后台线程跑 SQL、`postEvent` 回主线程弹结论。**判据一个字没改**：
        行数与三种提示语的措辞都与原实现逐字一致，只是不占主线程。
        """
        base = self._tasks_base()

        def _work():
            try:
                import sqlite3
                projs = sorted(base.glob("cli_*"), key=lambda p: p.stat().st_mtime, reverse=True)
                if not projs:
                    self._deliver_async("retry_dead", ("未找到历史任务目录", "warning"))
                    return
                db = projs[0] / "frontier.db"
                if not db.exists():
                    self._deliver_async("retry_dead", ("最新任务无 frontier.db", "warning"))
                    return
                con = sqlite3.connect(str(db))
                try:
                    cur = con.execute(
                        "UPDATE frontier SET status='pending', retry_count=0, scheduled_at=? "
                        "WHERE status='dead'",
                        (time.time(),))
                    con.commit()
                    n = cur.rowcount
                finally:
                    con.close()             # 异常路径也要还回连接（原来没有 try/finally）
                self._deliver_async(
                    "retry_dead",
                    (f"已重置 {n} 个失败页 → 下次启动同 URL 自动续爬" if n
                     else "没有可重试的失败页",
                     "success" if n else "warning"))
            except Exception as e:
                self._deliver_async("retry_dead", (f"重试失败: {e}", "error"))

        threading.Thread(target=_work, daemon=True).start()

    def _on_retry_dead_done(self, payload):
        """后台 SQL 回来了：按原措辞弹结论（位置与同步版一致）。"""
        try:
            msg, kind = payload
        except Exception:
            msg, kind = str(payload), "info"
        self._toast(msg, kind)

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

    # [v2.19.9 删除] `_check_cookies()` —— 它是"算 + 显示"的薄壳
    # （`_privacy_status_text()` + `home.privacy_status.setText()`）。
    # 两件事同时把它变成死代码：① 首页那个控件删了；② 它**唯一**的调用点
    # `_start()` 第一句改成了后台探测（见 `_start` 顶部说明）。
    # 全仓已确认 0 个调用点。
    # **它当年防的那件事没有丢**：`_check_cookies` 的 except 分支本是"登录态自检失败
    # 不许静默"，现在这条纪律在 `_refresh_privacy_status_async._work()` 里
    # ——失败会算成 `隐私状态: ⚠️ 自检失败（…）` 并照样落到日志页，仍然可见。

    def _engine_running(self) -> bool:
        """引擎在不在跑。**只用于"要不要回滚进程级环境变量"这一个判断**。

        为什么容忍异常：本方法会在**后台线程**里被调用（探测线程），
        而此时窗对象可能正在销毁（`self.engine` 已没了）—— 那种时候"当它没在跑"，
        回滚一次环境变量不会有任何后果，比抛异常打断探测划算。
        """
        try:
            return bool(self.engine.running)
        except Exception:
            return False

    def _privacy_status_text(self, gui_cookie: str) -> str:
        """算登录态文本 —— **不碰 Qt**（所以能在后台线程里跑）。

        内容与判据**逐字照搬**原来的 `_check_cookies`（密钥是否加密 + 各站登录态），
        只是把"算"与"显示"拆开。拆的理由见 `_refresh_privacy_status_async`：
        这里有一次联网探测（timeout 10s），在主线程算就会假死最多 10 秒。

        [v2.19.9] 显示它的那个首页控件已删、`_check_cookies()` 薄壳也已删；
        本函数**必须留着** —— 设置页「检查有效性」在复用
        （`_check_cookie_sources_async` 里 `_cookies_health_text(rows, self._privacy_status_text(box))`），
        而且它是 `_refresh_privacy_status_async` 唯一的判据来源。
        **它不是"第二份判定"**：全仓算这串文本的只有这一处。
        """
        gui_cookie = str(gui_cookie or "").strip()
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
            # [v6+ 竞态修复·审查发现] 回滚前**再判一次引擎在不在跑**：
            # `EngineBridge.start()` 也写这两个变量，而这次探测最长能跑 10 秒 ——
            # 用户完全可能在这期间点「开始爬取」。那一刻 env 里是**引擎刚设好的、
            # 正确的那份**（= 首页 Cookies 框的值），我们把它盖回旧快照就等于
            # "取完 cookies 反而把本次爬取要用的 cookies 抹了"。
            # ⇒ 谁后写的谁负责：引擎在跑就**不回滚**（我们写进去的值与它同源，不冲突）。
            if not self._engine_running():
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
        # [v6 修复·顺手发现] 原来是 `data_root()` —— **该名字在本模块不存在**
        # （模块级是 `DATA_ROOT = _kcfg.data_root`，见文件第 37 行）。
        # 于是这一行**每次必抛 NameError**，被外层 `except Exception` 静默吞掉 →
        # 隐私状态行永远显示不出"密钥是否已加密"，用户看到的是一个失败的提示。
        # 这是"静默失败"家族里最典型的一种：**错在 try 里，症状在界面**。
        _bin = Path(DATA_ROOT()) / "master.key.bin"
        enc = "🔐 密钥DPAPI加密" if _bin.exists() else "⚠️ 密钥未加密"
        return f"隐私状态: {enc} · Cookies: {' '.join(bits) or '无'}"

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
                # [v2.19.9] 只读尾部：本函数被**每条进度日志**调用一次，而 stats.jsonl
                # 随爬取无限增长 —— 整份 read_text 会变成"每几秒重读一遍全历史"
                # （还在主线程上）。见 `_read_last_json_line`。
                last = _read_last_json_line(projs[0] / "stats.jsonl")
                # 用 `is not None` 而不是真值判断：合法的 `{}` 也是"读到了"
                # （真值判断会把它当成"没读到"而掉进下面的 regex 兜底，
                #  于是统计卡显示的数字与 stats.jsonl 说的不一致）。
                if last is not None:
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
        """历史任务列表刷新。

        **[v2.19.9 修复·主线程可能冻住好几秒]** 原实现在主线程对最多 60 个任务目录做
        `rglob("*")` + **逐文件 `stat()`** —— 那棵树里装的是**下载下来的视频/图片**，
        动辄上万个文件；用户每点一次「刷新」就要把整个输出目录走一遍，全程界面不动。
        与 `_run_priv_scan` 同类，只是触发更频繁（每次「开始爬取」之后也会自动刷一次）。

        现改为后台线程算、`postEvent` 回主线程填列表（同 `_refresh_privacy_status_async`）。
        `base` 仍**在主线程**取：它读 `self.config`，线程里不该碰 Qt/配置。
        """
        if getattr(self, "_tasks_busy", False):
            return                              # 连点不许排队起 N 个遍历线程
        base = self._tasks_base()
        self._tasks_busy = True
        btn = getattr(self.taskp, "refresh_btn", None)
        if btn is not None:
            btn.setEnabled(False)
        # 立刻给一句反馈：真列表要遍历完才回得来，别让用户以为"点了没反应"
        self.taskp.tasks_list.setPlainText("⏳ 正在统计历史任务（需要遍历各任务的产物目录，可能要几秒）…")

        def _work():
            try:
                items = []
                for d in sorted(base.glob("cli_*"), key=lambda p: p.stat().st_mtime,
                                reverse=True)[:60]:
                    try:
                        mt = time.strftime("%m-%d %H:%M", time.localtime(d.stat().st_mtime))
                        size_mb = sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) / 1024 / 1024
                        items.append(f"{d.name}  |  {mt}  |  {size_mb:.1f} MB")
                    except Exception:
                        continue
                text = "\n".join(items) or "暂无历史任务（点「开始爬取」创建）"
            except Exception as e:
                text = f"读取失败: {e}"         # 失败也要出结论，不许静默留个空列表
            self._deliver_async("tasks", text)

        threading.Thread(target=_work, daemon=True).start()

    def _on_tasks_done(self, text):
        """后台统计回来了：还原按钮 + 填列表（结论落在原来那个控件上，位置没变）。"""
        self._tasks_busy = False
        btn = getattr(self.taskp, "refresh_btn", None)
        if btn is not None:
            btn.setEnabled(True)
        self.taskp.tasks_list.setPlainText(str(text))

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
