"""浏览器池求解引擎

基于 Playwright/Patchright 的反爬挑战求解引擎。池化浏览器上下文，
支持指纹一致性注入、上下文预热、按代理分配、自愈机制与内存监控。

核心特性：
- 默认池大小 3，所有上下文在初始化后预热，可立即投入使用
- 每个上下文绑定独立指纹（基于代理 IP 确定性生成），并应用到
  locale / timezone / user_agent / color_scheme
- 使用 injection_scripts.build_stealth_scripts 注入 16 维隐身脚本
- 集成 source_level_stealth 源码级反检测（CDP 延迟、rebrowser-patches、协议层指纹）
- 上下文健康度追踪（成功/失败/无响应计数），无响应自动替换
- 浏览器崩溃自动重启、池耗尽紧急创建
- 周期性内存监控，超限触发上下文回收或完整重启
- 资源拦截屏蔽分析/追踪/社交/广告，但放行挑战相关资源
"""
import asyncio
import logging
import os as _os
import random
import time
from typing import Optional, Tuple

# [FIXED & MODIFIED] v2.10.5c 子进程编码：patchright driver 子进程输出用系统 GBK 解码
# 时 "UnicodeDecodeError: 0xb5" 泄漏（Windows 环境），导致 Event loop closed / 渲染链
# 静默失败。必须在导入 patchright 之前强制 UTF-8 环境（与 run_crawler locale 修复同源）。
_os.environ.setdefault("PYTHONUTF8", "1")
_os.environ.setdefault("PYTHONIOENCODING", "utf-8")
_os.environ.setdefault("PATCHRIGHT_SILENCE_MAJOR_ERRORS", "1")

# ── rebrowser-patches 环境变量必须在导入 playwright/patchright 之前设置 ──
from .source_level_stealth import (
    configure_rebrowser_env,
    build_stealth_launch_args,
    build_stealth_context_options,
    CDPDeferralManager,
    apply_cdp_fingerprint,
    stealth_navigate,
)
configure_rebrowser_env(
    runtime_fix_mode="addBinding",
    source_url="app.js",
    utility_world_name="util",
)

from .challenge_solver import UnifiedChallengeSolver
from .fingerprint_consistency import compute_fingerprint_from_ip, generate_default_fingerprint
from .injection_scripts import build_stealth_scripts
from .evasion_engine import build_evasion_scripts
from .behavioral_biometrics import build_biometrics_script
from .ultimate_evasion import build_ultimate_evasion_scripts

# 保留向后兼容的导入（外部模块可能从本模块间接引用）
from .response_adapter import ResponseAdapter  # noqa: F401
from .session_pool import SessionPool  # noqa: F401

logger = logging.getLogger(__name__)

# ── 浏览器引擎选择（优先 patchright，回退 playwright）─────────────────────
try:
    from patchright.async_api import async_playwright as pr_async
    HAS_PATCHRIGHT = True
except ImportError:
    HAS_PATCHRIGHT = False

try:
    from playwright.async_api import async_playwright as pw_async
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

# ══════════════════════════════════════════════════════════════════════
# [v6 修复] 注入通道自证：**patchright 优先，但必须先实测它真的能注入**
# ──────────────────────────────────────────────────────────────────────
# 真机根因（2026-10-01 实测：patchright 1.61.2，其内置 driver 自称 playwright-core 1.61.1）：
#   patchright 是 playwright 的"隐身补丁版"，它改写了 **Chromium 页面会话初始化**里
#   init script 的注册路径。比对两个 driver bundle：上游 playwright-core 是
#       for (const initScript of page.allInitScripts())
#         _evaluateOnNewDocument(initScript, "main", true /* runImmediately */);
#   而 patchright 换成了「按 context.initScripts / page.initScripts 各循环一次」的写法，
#   **丢掉了 runImmediately**。后果是 `context.add_init_script` / `page.add_init_script`
#   在 Chromium 上**全程静默失效**：不抛异常、CDP 命令确实下发了、浏览器也回了
#   identifier，但脚本永不运行（Chromium 内部为何因此不执行，未再下钻——不影响结论）。
#   实测 marker 探针：patchright 在第 1 次导航 / 第 2 次导航 / reload 后都读到 0；
#   同一份脚本换 playwright 立刻是 1/1/1。任何 launch 组合（默认 / channel=chromium /
#   channel=chrome / persistent context / 带不带 stealth 启动参数）都一样失效。
#   ⇒ 生产路径优先 patchright（见 `_launch_browser`），而验证路径用的 playwright，
#     所以"链修好了"在生产里**一行都没跑** —— 真机表现就是自证 5 次
#     「隐身链自证疑点（plugins=0）」。
#
# 对策：启动浏览器前用探针**实测注入通道**，探不通就回退 playwright（探针结论进程内缓存）。
#   探针只判"通道通不通"，**不判任何指纹值** —— 有头浏览器本来就报 5 个 plugin，与通道
#   无关，所以不会把"原生正常值"误判成故障，也不会覆盖原生值。
#   探针固定 headless=True：注入通道是**驱动层**行为、与渲染模式无关，而本机有真人使用，
#   一律不弹窗；有头模式下的通道可用性由 `_prewarm_page` 的**逐上下文自证**兜住。
_INJECT_PROBE_MARK = "__KIANA_INJECT_CHANNEL_OK__"
_INJECT_PROBE_SCRIPT = ("(() => { window." + _INJECT_PROBE_MARK + " = (window."
                        + _INJECT_PROBE_MARK + " || 0) + 1; })();")
# 进程内缓存：patchright 注入通道的三态结论 (True/False/None, 可读原因)。
# 浏览器实例昂贵，同一进程只探一次。
_PATCHRIGHT_CHANNEL_CACHE: Optional[Tuple[Optional[bool], str]] = None
# [v2.19.9] 「自证通过」的**稳定标记** —— 正面证据只认这个常量，不认日志措辞。
# 教训（真实踩过，commit e794712）：首次成功的日志从 DEBUG 改成 INFO、句子也重写了，
# 而断言写的是那句**旧文案** ⇒ 判据默默失效；红出来的样子像"反检测回归"，其实引擎
# 一行没坏。所以：措辞可以改，这个常量不许改；断言方 import 它，不许手抄这句话。
STEALTH_SELFCHECK_OK_MARK = "隐身链自证通过"

# 可选 psutil（用于浏览器进程内存监控）
try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False


# ── 资源拦截：拦截列表与放行列表 ────────────────────────────────────────
# 分析与追踪
_TRACKING_KEYWORDS = (
    "google-analytics.com", "googletagmanager.com", "analytics.google",
    "scorecardresearch.com", "quantserve.com", "quantcount.com",
    "mixpanel.com", "segment.io", "amplitude.com", "hotjar.com",
    "fullstory.com", "clarity.ms", "mouseflow.com", "newrelic.com",
    "pingdom.net", "matomo", "piwik", "sentry.io", "rumcdn.com",
)
# 社交媒体组件
_SOCIAL_KEYWORDS = (
    "connect.facebook.net", "facebook.com/tr", "platform.twitter.com",
    "twitter.com/i/", "platform.linkedin.com", "addthis.com",
    "sharethis.com", "disqus.com", "addtoany.com", "platform.tumblr.com",
)
# 广告
_AD_KEYWORDS = (
    "doubleclick.net", "googlesyndication.com", "googleadservices.com",
    "adservice.google.com", "amazon-adsystem.com", "adnxs.com",
    "pubmatic.com", "rubiconproject.com", "criteo.com", "taboola.com",
    "outbrain.com", "adsterra.com", "propellerads.com", "media.net",
)
_BLOCKED_HOST_KEYWORDS = _TRACKING_KEYWORDS + _SOCIAL_KEYWORDS + _AD_KEYWORDS

# 挑战相关资源（始终放行，不得拦截）
_ALLOWED_CHALLENGE_KEYWORDS = (
    # 标准 7 类挑战资源
    "challenges.cloudflare.com", "turnstile", "cloudflare",
    "cdn-cgi/challenge", "cdn-cgi/",
    "recaptcha", "grecaptcha", "google.com/recaptcha",
    "hcaptcha", "hcaptcha.com",
    "datadome", "dd-key", "datadomecdn",
    "px-captcha", "perimeterx", "pxcdn",
    "akamai", "_abck",
    # 扩展 9 类挑战资源
    "geetest", "gt.js", "captcha.geetest",
    "awswaf", "aws-waf", "challenge.js", "token.awswaf",
    "incapsula", "imperva", "reese84", "incap_ses",
    "kasada", "kp.js",
    "slider", "slide_block", "nc_iconfont",
    "captcha.php", "captcha.aspx", "kaptcha",
    "verifycode", "checkcode", "vcode",
)

# 非必要资源类型（直接拦截）
_NONESSENTIAL_RESOURCE_TYPES = ("image", "stylesheet", "font", "media")


class _ContextEntry:
    """浏览器上下文的池化包装，附带健康度与使用统计"""

    __slots__ = (
        "ctx", "page", "page_count", "proxy", "fingerprint",
        "success_count", "failure_count", "unresponsive_count",
        "last_used", "created_at",
    )

    def __init__(self, ctx, page, fingerprint, proxy=None, page_count=0):
        self.ctx = ctx
        self.page = page
        self.page_count = page_count
        self.proxy = proxy
        self.fingerprint = fingerprint
        self.success_count = 0
        self.failure_count = 0
        self.unresponsive_count = 0
        self.last_used = time.monotonic()
        self.created_at = time.monotonic()

    @property
    def is_unhealthy(self) -> bool:
        """综合判定上下文是否需要回收"""
        if self.failure_count >= 5:
            return True
        if self.unresponsive_count >= 3:
            return True
        return False


def probe_cdp_endpoint(port: int, timeout: float = 1.5):
    """探测本机 CDP 调试端口是否可用 → `(ok, 可读说明)`。

    [v6 M1-d] 用途：让"接管已登录浏览器"在**起任务之前**就能给出可读结论，
    而不是等接管失败后静默回退成无登录态抓取（用户完全看不出来）。

    **只连 127.0.0.1**（主机名写死，不接用户输入）→ 无 SSRF 面；
    路径固定 `/json/version`，是 Chrome DevTools 的标准发现端点。

    本函数**不抛异常**：任何失败都转成 `(False, 可读原因)`，调用方直接展示。
    """
    import json as _json
    import urllib.request as _ur
    _port = int(port)
    try:
        with _ur.urlopen(f"http://127.0.0.1:{_port}/json/version", timeout=timeout) as r:
            data = _json.loads(r.read().decode("utf-8", "ignore"))
        _browser = str(data.get("Browser", "")).strip() or "未知浏览器"
        return True, f"已连接 CDP :{_port}（{_browser}）"
    except Exception as e:
        return False, (f"CDP 端口 {_port} 不可用（{e!r}）——请先用 "
                       f"--remote-debugging-port={_port} 启动已登录的 Chrome，再开启本项")


async def _probe_init_script_channel(factory, args, headless: bool = True) -> Tuple[Optional[bool], str]:
    """[v6 修复] 实测 `add_init_script` 是否**真的会在页面里执行**。

    返回三态，**证据级别不同、处置也不同**（不能把"没测出来"当成"测出来是坏的"）：
      - `True`  = 实测可用（marker 出现了）；
      - `False` = **实测不可用**：CDP 命令已下发、浏览器也回了 identifier，但脚本从未执行
                  （真机 patchright 就是这一态）→ 调用方应回退到 playwright；
      - `None`  = 无法判定（探针自身失败：起不了浏览器、超时、工厂不可用……）→ 调用方
                  保持原行为，但必须把原因写进日志（有头/无头、指纹值都不参与判定）。

    只判通道、不判指纹值：本函数不看 plugins/webdriver 的数值，只看自证 marker，
    所以与有头/无头、原生 plugins 数量无关，**绝不会覆盖有头浏览器的原生值**。

    本函数**不抛异常**：任何失败都转成可读结论，由调用方决定处置。
    """
    try:
        async with factory() as pw:
            browser = await pw.chromium.launch(args=args, headless=headless, timeout=60000)
            try:
                ctx = await browser.new_context()
                await ctx.add_init_script(_INJECT_PROBE_SCRIPT)
                page = await ctx.new_page()
                page.set_default_timeout(30000)
                await page.goto("about:blank", wait_until="domcontentloaded", timeout=30000)
                got = await page.evaluate(f"() => window.{_INJECT_PROBE_MARK} || 0")
                await ctx.close()
                if int(got or 0) > 0:
                    return True, "add_init_script 已实测执行"
                return False, "add_init_script 已下发但脚本从未执行（整条隐身链不会生效）"
            finally:
                await browser.close()
    except Exception as e:
        return None, f"注入通道探针无法判定：{e!r}"


class SolverEngine:
    """单例浏览器求解引擎

    池化浏览器上下文，带页面计数轮换、指纹一致性注入、上下文预热、
    按代理分配、自愈机制、内存监控与死锁保护。
    """

    def __init__(self, pool_size=3, max_pages_per_context=30, memory_limit_mb=400,
                 session_pool=None, stealth_config=None, headless=True,
                 memory_check_interval=30, health_check_timeout=3.0,
                 two_captcha_key=None, capsolver_key=None, anti_captcha_key=None,
                 cdp_attach=False, cdp_port=9222):
        """初始化求解引擎

        Args:
            pool_size: 池中浏览器上下文数量，默认 3
            max_pages_per_context: 单个上下文最大页面使用次数，达到后轮换
            memory_limit_mb: 浏览器进程内存上限（MB），超过触发上下文回收
            session_pool: 会话池实例，用于更新 Cookie/Header
            stealth_config: 隐身配置（保留参数，向后兼容）
            headless: 是否无头模式
            memory_check_interval: 内存检查周期（秒）
            health_check_timeout: 上下文健康检查超时（秒）
            two_captcha_key: 2Captcha API 密钥（可选）
            capsolver_key: CapSolver API 密钥（可选）
            anti_captcha_key: AntiCaptcha API 密钥（可选）
        """
        self.pool_size = pool_size
        self.max_pages_per_context = max_pages_per_context
        self.memory_limit_mb = memory_limit_mb
        self.session_pool = session_pool
        self.stealth_config = stealth_config or {}
        self.headless = headless
        self.memory_check_interval = memory_check_interval
        self.health_check_timeout = health_check_timeout
        # 打码平台 API 密钥
        self.two_captcha_key = two_captcha_key
        self.capsolver_key = capsolver_key
        self.anti_captcha_key = anti_captcha_key
        # [v2.17 3.5] CDP 接管既有浏览器（实验默认关——连接用户已开调试端口的 Chrome，
        # 登录态/指纹现成；接管后浏览器生命周期归用户，close 不销毁）
        self.cdp_attach = bool(cdp_attach)
        self.cdp_port = int(cdp_port)
        self._cdp_attached = False
        # [v6 M1-d] 接管失败的**可观测**状态：原实现只写一条 warning 就静默回退到
        # 自管浏览器——用户以为在用自己的登录态，其实没有。GUI 据此给可读提示。
        self.cdp_attach_failures = 0
        self.cdp_fallback_reason = ""

        self.playwright = None
        self.browser = None
        # [v6 修复] 实际启用的浏览器引擎 + "注入通道自证"结论（GUI/日志/测试可读）。
        # 原实现只按"装了 patchright 就用 patchright"选引擎，而 patchright 的
        # add_init_script 在 Chromium 上静默失效 → 整条隐身链一行不跑（真机根因）。
        self.injection_engine = "未启动"
        self.patchright_channel_ok = True
        self.patchright_channel_reason = ""
        # [v2.19.9] 逐上下文自证的**正面结论**（至少有一个上下文自证通过）。
        # 存在的意义：让"注入真的生效了"这件事**可被断言** ——
        # 否则只能靠"日志里没出现告警"倒推，而那正是 R1 静默失效五次的成因。
        self.stealth_selfcheck_ok = False
        self._contexts: asyncio.Queue = asyncio.Queue()
        self._in_use: dict = {}  # ctx -> _ContextEntry（使用中的上下文）
        self._ready = asyncio.Event()
        self._init_lock = asyncio.Lock()
        self._restart_lock = asyncio.Lock()
        self._closing = False

        # 后台任务与重启冷却
        self._memory_task: Optional[asyncio.Task] = None
        self._last_restart = 0.0
        self._restart_cooldown = 60.0

    # ════════════════════════════════════════════════════════════════
    # 初始化与预热
    # ════════════════════════════════════════════════════════════════
    async def init(self):
        """初始化浏览器与上下文池，并对所有上下文进行预热

        创建 pool_size 个上下文，每个上下文导航到空白页并执行隐身脚本，
        使其立即可用。最后启动内存监控后台任务。
        """
        async with self._init_lock:
            if self._ready.is_set():
                return
            await self._launch_browser()

            # 创建并预热所有池上下文
            for _ in range(self.pool_size):
                try:
                    entry = await self._create_solver_context_entry()
                    await self._contexts.put(entry)
                except Exception as e:
                    logger.error(f"初始化上下文失败: {e}")
                    # 继续尝试创建剩余上下文，至少保证池非空

            if self._contexts.empty():
                raise RuntimeError("初始化失败：浏览器上下文池为空")

            self._ready.set()
            self._start_memory_monitor()
            logger.info(f"求解引擎初始化完成，池大小={self._contexts.qsize()}")

    async def _launch_browser(self):
        """启动浏览器实例（patchright 优先，**但注入通道实测通过才用**，否则回退 playwright）

        使用 source_level_stealth.build_stealth_launch_args 构建优化启动参数，
        移除所有自动化暴露标志，添加 2026 年最新验证的隐身参数。

        [v6 修复] 原实现只判"装没装 patchright"，而 patchright 的 `add_init_script` 在
        Chromium 上**静默失效** → 生产路径整条 55 维隐身链一行都不执行（真机自证
        5 次「隐身链自证疑点（plugins=0）」），而验证路径用 playwright 所以看着是好的。
        现改为：**先实测注入通道，探不通就换 playwright**（结论进程内缓存，只探一次）。
        """
        args = build_stealth_launch_args(headless=self.headless)
        # [v2.17 3.5] CDP 接管既有浏览器（连用户已开调试端口的 Chrome，登录态/指纹现成；
        # 接管后浏览器生命周期归用户，close 不销毁）
        # [v6 M1-d] 接管失败**不再静默回退**：原实现只写一条 warning 就换成自管浏览器，
        # 用户以为在用登录态、实际没有——表现为"登录后才可见的内容取不到"，极难定位。
        # 现改为 ERROR + 计数 + 保留可读原因，GUI 侧据此提示。
        if self.cdp_attach:
            try:
                _engine = (pr_async() if HAS_PATCHRIGHT else pw_async())
                self.injection_engine = "patchright(CDP接管)" if HAS_PATCHRIGHT else "playwright(CDP接管)"
                self.playwright = await _engine.start()
                self.browser = await self.playwright.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{self.cdp_port}/")
                self._cdp_attached = True
                self.cdp_fallback_reason = ""
                logger.info(f"已接管用户在开浏览器(CDP :{self.cdp_port})——浏览器生命周期归用户")
                return
            except Exception as e:
                self.cdp_attach_failures += 1
                self.cdp_fallback_reason = f"CDP :{self.cdp_port} 接管失败：{e!r}"
                self._cdp_attached = False
                logger.error(
                    f"{self.cdp_fallback_reason} —— 已回退为**自管浏览器**，本次任务"
                    f"**不带你的登录态**（登录后才可见的内容会取不到）。请确认 Chrome 以 "
                    f"--remote-debugging-port={self.cdp_port} 启动、且该端口未被占用。",
                    exc_info=True)
        # 显式传递 headless 参数，避免与 args 中的 headless 标志冲突
        factory = None
        if HAS_PATCHRIGHT:
            verdict, reason = await self._patchright_channel_ok(args)
            # 三态处置：只有**实测不可用（False）**才回退；"无法判定（None）"保持原行为。
            self.patchright_channel_ok = verdict is not False
            self.patchright_channel_reason = reason
            if verdict is not False:
                if verdict is None:
                    logger.warning(
                        f"[v6 修复] patchright 注入通道**无法判定**（{reason}）——按原行为继续"
                        f"用 patchright；每个上下文仍会做注入自证，若实测不通会打 ERROR "
                        f"说明整条链没跑（不拿『没测出来』当『测出来是坏的』）。")
                factory, name = pr_async, "patchright"
            elif HAS_PLAYWRIGHT:
                # [v6 修复] 关键回退：patchright 装了但**实测注入不了** —— 绝不"照旧用它"
                logger.error(
                    f"[v6 修复] 注入通道自证不通过：原生产路径优先 patchright，但实测其 "
                    f"add_init_script **静默失效**（{reason}）→ 整条 55 维隐身链一行都不会执行"
                    f"（真机表现：「隐身链自证疑点（plugins=0）」）。已自动改用 playwright "
                    f"启动浏览器（同一份链在 playwright 上实测生效）。根治办法是升级/"
                    f"更换 patchright 版本，或保持本回退。")
                factory, name = pw_async, "playwright"
            else:
                logger.error(
                    f"[v6 修复] 实测 patchright 的 add_init_script 不执行（{reason}），"
                    f"但本机没有 playwright 可回退 —— 仍用 patchright：**本次运行"
                    f"整条隐身链都不会生效**，请安装 playwright 或升级 patchright。")
                factory, name = pr_async, "patchright"
        elif HAS_PLAYWRIGHT:
            factory, name = pw_async, "playwright"
        else:
            raise RuntimeError("无可用浏览器引擎（请安装 patchright 或 playwright）")
        self.injection_engine = name
        self.playwright = await factory().start()
        self.browser = await self.playwright.chromium.launch(args=args, headless=self.headless)

    async def _patchright_channel_ok(self, args) -> Tuple[Optional[bool], str]:
        """[v6 修复] patchright 的注入通道是否可用（三态结论进程内缓存，只探测一次）

        只有**实测结论（True/False）**才写进缓存：`None`（探针自身失败：起不来、超时、
        工厂不可用）**不缓存**——否则一次瞬时故障会把整个进程钉在"无法判定"上，还会
        污染同进程内后续的引擎选择（如 CDP 接管失败后回退的那条路径）。
        """
        global _PATCHRIGHT_CHANNEL_CACHE
        if _PATCHRIGHT_CHANNEL_CACHE is None:
            _verdict, _reason = await _probe_init_script_channel(pr_async, args)
            if _verdict is True:
                _label = "通过"
            elif _verdict is False:
                _label = "不通过"
            else:
                _label = "无法判定"
            logger.info(f"[v6 修复] patchright 注入通道自证：{_label} —— {_reason}")
            if _verdict is not None:
                _PATCHRIGHT_CHANNEL_CACHE = (_verdict, _reason)
            return _verdict, _reason
        return _PATCHRIGHT_CHANNEL_CACHE

    async def _create_solver_context(self, proxy_url: Optional[str] = None):
        """创建单个浏览器上下文并预热

        根据 source_level_stealth 模块构建隐身上下文：
        - viewport: None（使用真实屏幕尺寸，假 viewport 本身是 bot 信号）
        - locale/timezone/UA 来自指纹
        - CDP 延迟激活管理器
        - 注入 16 维隐身脚本作为协议层补充

        Args:
            proxy_url: 代理 URL，若提供则生成对应指纹并绑定代理

        Returns:
            (context, page, fingerprint) 三元组
        """
        # 生成指纹：有代理则基于代理 IP 生成确定性指纹，否则使用默认指纹
        if proxy_url:
            fingerprint = compute_fingerprint_from_ip(proxy_url)
        else:
            fingerprint = generate_default_fingerprint()

        # 使用 source_level_stealth 构建隐身上下文选项
        context_kwargs = build_stealth_context_options(
            fingerprint, proxy_url, real_viewport=True
        )

        # [FIXED & MODIFIED] v2.10.5c 断链修复：stealth_v3 BROWSER_SPOOF 必须在新上下文
        # 创建前 merge——原来在 new_context 之后 update 永不生效。提前合并（读取失败不影响主流程）。
        # [FIXED & MODIFIED] v2.11 同源修复：viewport/locale/timezone_id/geolocation/UA 等身份
        # 字段不再被 BROWSER_SPOOF 覆盖——它们必须与指纹 JS 注入层一致（原 Shanghai 时区+1920x1080
        # 固定值 与指纹派生身份自相矛盾，本身是 bot 信号）。只保留不构成身份矛盾的设备档位字段。
        try:
            from .stealth_v3 import BROWSER_SPOOF
            for k in ('is_mobile', 'has_touch', 'device_scale_factor'):
                if k in BROWSER_SPOOF:
                    context_kwargs[k] = BROWSER_SPOOF[k]
        except Exception:
            pass

        context = None
        try:
            context = await self.browser.new_context(**context_kwargs)
            # [v6 修复] 先打**注入通道自证 marker**（独立一条 init script）：
            # 它与隐身链互不影响，预热审计据此区分两种完全不同的故障：
            #   marker 没出现 = 通道断了（引擎的 add_init_script 根本没执行 → 整条链白写）；
            #   marker 有、plugins 仍 0 = 通道通、链内容有问题。
            # 原实现只有后者可诊断，前者（真机根因）会伪装成"某个维度没兜住"。
            await context.add_init_script(_INJECT_PROBE_SCRIPT)
            # 注入完整 55 维隐身脚本链（基于 config 开关控制维度）
            stealth_scripts = self._build_full_evasion_chain(fingerprint)
            await context.add_init_script(stealth_scripts)
            # Evasion v2: WebRTC, Canvas, AudioContext, WebGL
            try:
                from .evasion_v2 import ALL_EVASION_SCRIPTS
                for script in ALL_EVASION_SCRIPTS:
                    await context.add_init_script(script)
            except Exception:
                pass
            # Stealth v3: GoogleBot-tier CDP + human behavior
            try:
                from .stealth_v3 import GOOGLEBOT_CDP_EVASION, HUMAN_BEHAVIOR_SCRIPT
                await context.add_init_script(GOOGLEBOT_CDP_EVASION)
                await context.add_init_script(HUMAN_BEHAVIOR_SCRIPT)
            except Exception:
                pass
            page = await context.new_page()
            await page.route("**/*", self._intercept_resources)

            # 预热：导航到空白页，触发隐身脚本执行，使上下文立即可用
            await self._prewarm_page(page)
            return context, page, fingerprint
        except Exception as e:
            logger.error(f"创建求解上下文失败: {e}")
            if context:
                try:
                    await context.close()
                except Exception:
                    pass
            raise

    async def _create_solver_context_entry(self, proxy_url: Optional[str] = None) -> _ContextEntry:
        """创建上下文并包装为池化条目"""
        context, page, fingerprint = await self._create_solver_context(proxy_url)
        return _ContextEntry(context, page, fingerprint, proxy=proxy_url)

    async def _prewarm_page(self, page, fingerprint: dict = None):
        """预热页面：导航至 about:blank 并执行隐身链自证审计

        [FIXED & MODIFIED] v2.10.5 P1-5 隐身链自证：原只 evaluate userAgent（装完零验证）。
        现审计 navigator.webdriver / canvas 噪声 / WebGL / 时区 / languages 是否注入生效；
        任一指标暴露自动化信号 → logger 警告（不弃用——避免重建循环，仅供排障）。

        [v6 修复] 再加一条**更根本**的自证：注入通道 marker（见 `_create_solver_context`）。
        "整条链一行都没执行"与"某个维度没兜住"是两类完全不同的故障，原审计只能用
        plugins=0 间接表达，而**有头浏览器原生就报 5 个 plugin**，通道断了也看不出来。
        """
        try:
            await page.goto("about:blank", wait_until="domcontentloaded", timeout=10000)
            # 全项审计：隐身链是否真兜住常见自动化检测
            audit = await page.evaluate("""() => {
                const c = document.createElement('canvas');
                const gl = c.getContext('webgl');
                return {
                    webdriver: navigator.webdriver,
                    vendor: navigator.vendor || '',
                    plugins_len: navigator.plugins ? navigator.plugins.length : -1,
                    webgl_vendor: gl ? gl.getParameter(gl.VENDOR) : '',
                    lang: navigator.language || '',
                    ua_len: navigator.userAgent.length,
                };
            }""")
            # [v6 修复] 注入通道自证：marker 由 `_create_solver_context` 随隐身链一起注入。
            # 它没出现 = 本引擎的 `add_init_script` 根本没执行 → 整条 55 维链一行都没跑。
            # 这一条与浏览器原生 plugins 数量无关，所以**有头模式同样有效**
            #（有头本来就报 5 个 plugin，光看 plugins 是发现不了通道断掉的）。
            _channel_mark = await page.evaluate(
                f"() => window.{_INJECT_PROBE_MARK} || 0")
            if not _channel_mark:
                logger.error(
                    f"[v6 修复] 注入通道自证失败：marker 未出现 —— 本上下文"
                    f"（引擎={self.injection_engine}）的 `add_init_script` 脚本**一行都没执行**，"
                    f"55 维隐身链（plugins/mimeTypes/webdriver/canvas 全在内）在本上下文里"
                    f"完全失效。通道探针结论：{self.patchright_channel_reason or '未记录'}")
            _flags = []
            if audit.get("webdriver") in (True, "true", "True"):
                _flags.append(f"webdriver={audit.get('webdriver')}")
            if audit.get("plugins_len", -1) <= 0 and _channel_mark:
                # 通道断了的情况上面已经单独报 ERROR，这里不重复刷同一条告警
                _flags.append(f"plugins={audit.get('plugins_len')}")
            if _flags:
                logger.warning(f"隐身链自证疑点（{','.join(_flags)}）——引擎={self.injection_engine}，"
                               f"后续若被风控可追溯该上下文")
            elif _channel_mark:
                # [v6 修复·机主明确要求「反爬要看得见的日志、输出用中文」]
                # 原来是 `logger.debug` —— **INFO 级别下完全看不见**，
                # 于是"跑通了"和"压根没跑"在日志里长得一模一样。
                # 机主那份 22 页日志里反检测字样 0 次，根因就在这。
                #
                # 但这段是**每个上下文**都跑的（上下文会轮换），全打 INFO 会刷屏
                # ⇒ **首次 INFO（把话说清楚），后续 DEBUG**。
                if not getattr(self, "_stealth_ok_logged", False):
                    self._stealth_ok_logged = True
                    logger.info(
                        "✅ %s：55 维隐身链**实测跑起来了**"
                        "（webdriver=%s、plugins=%s、引擎=%s）——"
                        "这条是**自证**结果，不是「以为配好了」",
                        STEALTH_SELFCHECK_OK_MARK,
                        audit.get("webdriver"), audit.get("plugins_len"),
                        self.injection_engine)
                else:
                    logger.debug("%s: webdriver=%s plugins=%s 引擎=%s",
                                 STEALTH_SELFCHECK_OK_MARK,
                                 audit.get("webdriver"), audit.get("plugins_len"),
                                 self.injection_engine)
                # [v2.19.9] 正面证据落到**状态**上（不靠日志文案）：测试/调用方可直接断言。
                self.stealth_selfcheck_ok = True
        except Exception as e:
            logger.debug(f"预热页面未完全就绪（可继续）: {e}")

    def _build_full_evasion_chain(self, fingerprint: dict) -> str:
        """构建完整的 55 维隐身脚本链

        根据 config 中的开关控制注入维度:
        - stealth_injection_enabled: 基础 16 维 (injection_scripts)
        - evasion_dimensions >= 35: 追加 evasion_engine 19 维 (17-35)
        - evasion_dimensions >= 43: 追加 behavioral_biometrics 8 维 (36-43)
        - ultimate_evasion_enabled: 追加 ultimate_evasion 12 维 (44-55)

        所有脚本按正确的执行顺序合并为一个 add_init_script 调用。
        """
        parts = []

        # Layer 1: 基础 16 维隐身注入 (injection_scripts)
        if self.stealth_config.get("stealth_injection_enabled", True):
            parts.append(build_stealth_scripts(fingerprint))

        # Layer 2: 极限绕过 19 维 (evasion_engine, dimensions 17-35)
        evasion_dims = self.stealth_config.get("evasion_dimensions", 55)
        if evasion_dims >= 35:
            parts.append(build_evasion_scripts(fingerprint))

        # Layer 3: 行为生物特征 8 维 (behavioral_biometrics, dimensions 36-43)
        if evasion_dims >= 43:
            parts.append(build_biometrics_script(fingerprint))

        # Layer 4: 终极绕过 12 维 (ultimate_evasion, dimensions 44-55)
        if (self.stealth_config.get("ultimate_evasion_enabled", True)
                and evasion_dims >= 55):
            parts.append(build_ultimate_evasion_scripts(fingerprint))

        return "\n".join(parts)

    # ════════════════════════════════════════════════════════════════
    # 资源拦截
    # ════════════════════════════════════════════════════════════════
    async def _abort_private(self, route):
        """[v2.19.7 安全·扫描发现] **只拒绝私网目标的轻量 route**。

        用于 render_simple 这类"不能装完整 `_intercept_resources`"的场景
        （后者会 abort 图片/样式，贴吧楼层图片被拦 → 渲染拿不到正文）。
        本回调不动资源类型策略，只做一件事：私网/保留地址一律 abort。
        覆盖页面 JS 的 fetch/XHR/WS 与所有子资源，堵住"浏览器内生请求"这条绕过面。
        """
        url = route.request.url or ""
        try:
            from .url_utils import is_private_url as _is_priv
            if url and str(url).startswith(("http://", "https://")) and _is_priv(url, dns_check=True):
                logger.warning(f"[solver] SSRF 拦截（轻量 route）：{str(url)[:60]}")
                await route.abort()
                return
        except Exception:
            pass
        await route.continue_()

    async def _intercept_resources(self, route):
        """资源拦截：屏蔽分析/追踪/社交/广告资源 + **私网目标**，放行挑战相关资源

        拦截策略：
        0. **私网/保留地址一律 abort**（[v2.19.7 安全·扫描发现] 见下）
        1. 始终放行挑战相关资源（cloudflare/recaptcha/hcaptcha/datadome 等）
        2. 拦截非必要资源类型（图片/样式/字体/媒体）
        3. 拦截已知分析、追踪、社交组件、广告脚本域名
        4. 其余资源（文档/脚本/xhr/fetch/ws）放行

        [v2.19.7 安全·扫描发现] 第 0 步是**新增的关键防线**：此前只挡顶层导航，
        而页面 JS 的 `fetch('http://192.168.1.1/')` / XHR / WebSocket 会直连内网，
        响应写进 DOM 后被 `page.content()` 抓走落盘。此处对**每个子资源请求**复检。
        为避免每子资源一次 DNS，判定结果走 `is_private_url` 自带的 host 缓存；
        仅在字面 host 命中或缓存未命中时才做解析（成本可控）。
        """
        request = route.request
        url = request.url or ""
        resource_type = request.resource_type

        # 0. [v2.19.7] 私网/保留地址：一律拒绝（含子资源/XHR/WS）
        try:
            from .url_utils import is_private_url as _is_priv
            if url and str(url).startswith(("http://", "https://")) and _is_priv(url, dns_check=True):
                logger.warning(f"[solver] SSRF 拦截：拒绝页面加载私网子资源 {str(url)[:60]}")
                await route.abort()
                return
        except Exception:
            pass  # 判定失败不阻断正常渲染（入口闸仍在）

        # 1. 始终放行挑战相关资源
        if any(kw in url for kw in _ALLOWED_CHALLENGE_KEYWORDS):
            await route.continue_()
            return

        # 2. 拦截非必要资源类型
        if resource_type in _NONESSENTIAL_RESOURCE_TYPES:
            await route.abort()
            return

        # 3. 拦截分析与追踪脚本
        lower_url = url.lower()
        if any(kw in lower_url for kw in _BLOCKED_HOST_KEYWORDS):
            await route.abort()
            return

        # 4. 其余资源放行
        await route.continue_()

    # ════════════════════════════════════════════════════════════════
    # 上下文获取与归还（自愈 + 健康度追踪）
    # ════════════════════════════════════════════════════════════════
    async def acquire(self) -> Tuple:
        """从池中获取一个健康的上下文

        包含浏览器崩溃检测、上下文健康检查与紧急创建机制。

        Returns:
            (ctx, page, page_count) 三元组
        """
        await self._ready.wait()
        if self._closing:
            raise RuntimeError("SolverEngine is closing")

        # 浏览器崩溃检测：若已断开则自动重启
        if not self._is_browser_alive():
            logger.warning("浏览器已断开，触发自动重启")
            await self._safe_restart()
            await self._ready.wait()
            if not self._is_browser_alive():
                raise RuntimeError("浏览器重启失败，无法获取上下文")

        for attempt in range(3):
            try:
                entry = await asyncio.wait_for(self._contexts.get(), timeout=30.0)
            except asyncio.TimeoutError:
                # 池耗尽：尝试紧急创建
                logger.error("获取上下文超时，池可能为空，尝试紧急创建")
                entry = await self._emergency_create()
                if entry is None:
                    raise RuntimeError("求解池耗尽且上下文持续创建失败")
                self._in_use[entry.ctx] = entry
                return entry.ctx, entry.page, entry.page_count

            # 健康检查：无响应则关闭并立即创建替换
            if not await self._is_context_healthy(entry.page):
                entry.unresponsive_count += 1
                logger.warning(
                    f"上下文无响应（第{entry.unresponsive_count}次），尝试替换")
                await self._close_entry(entry)
                replacement = await self._emergency_create()
                if replacement is not None:
                    self._in_use[replacement.ctx] = replacement
                    return replacement.ctx, replacement.page, replacement.page_count
                continue

            entry.last_used = time.monotonic()
            self._in_use[entry.ctx] = entry
            return entry.ctx, entry.page, entry.page_count

        # 多次获取均失败：紧急创建
        entry = await self._emergency_create()
        if entry is None:
            raise RuntimeError("求解池耗尽且上下文持续创建失败")
        self._in_use[entry.ctx] = entry
        return entry.ctx, entry.page, entry.page_count

    async def release(self, ctx, page, page_count, healthy: bool = True):
        """归还上下文，按使用次数与健康度决定轮换或回收

        Args:
            ctx: 浏览器上下文
            page: 上下文页面
            page_count: 已使用页面次数
            healthy: 本次使用是否健康成功（默认 True，保持向后兼容）
        """
        entry = self._in_use.pop(ctx, None)
        # 未追踪的上下文（如外部传入），仅按需关闭
        if entry is None:
            if not healthy:
                try:
                    await ctx.close()
                except Exception:
                    pass
            return

        new_count = page_count + 1
        entry.page_count = new_count
        entry.last_used = time.monotonic()

        if healthy:
            entry.success_count += 1
        else:
            entry.failure_count += 1

        need_recycle = (
            new_count >= self.max_pages_per_context
            or not healthy
            or entry.is_unhealthy
        )

        if need_recycle:
            await self._recycle_entry(entry)
        else:
            await self._contexts.put(entry)

    async def _recycle_entry(self, entry: _ContextEntry):
        """回收旧上下文并创建新上下文补充到池中

        若创建失败且池为空，触发紧急重启。
        """
        try:
            await entry.ctx.close()
        except Exception:
            pass
        try:
            new_entry = await self._create_solver_context_entry(entry.proxy)
            await self._contexts.put(new_entry)
            logger.debug("上下文已轮换")
        except Exception as e:
            logger.critical(f"上下文重建失败: {e}，池容量下降")
            if self._contexts.empty() and not self._in_use:
                logger.critical("浏览器池为空！触发紧急重启")
                asyncio.create_task(self._safe_restart())

    async def _emergency_create(self) -> Optional[_ContextEntry]:
        """紧急创建上下文（池为空时）

        若浏览器已断开，先重启再创建。

        Returns:
            新的上下文条目，或 None（创建失败）
        """
        try:
            if not self._is_browser_alive():
                await self._safe_restart()
            entry = await self._create_solver_context_entry()
            logger.warning("紧急创建上下文成功")
            return entry
        except Exception as e:
            logger.critical(f"紧急创建上下文失败: {e}")
            return None

    async def _is_context_healthy(self, page, timeout: Optional[float] = None) -> bool:
        """快速健康检查：执行一次轻量求值判定上下文是否响应"""
        t = timeout or self.health_check_timeout
        try:
            await asyncio.wait_for(page.evaluate("() => 1 + 1"), timeout=t)
            return True
        except Exception:
            return False

    def _is_browser_alive(self) -> bool:
        """检测浏览器实例是否存活"""
        try:
            return self.browser is not None and self.browser.is_connected()
        except Exception:
            return False

    async def _close_entry(self, entry: _ContextEntry):
        """安全关闭上下文条目（先关页面再关上下文，防止内存泄漏）"""
        try:
            if entry.page:
                await entry.page.close()
        except Exception:
            pass
        try:
            await entry.ctx.close()
        except Exception:
            pass

    # ════════════════════════════════════════════════════════════════
    # 求解入口
    # ════════════════════════════════════════════════════════════════
    async def run_in_page(self, url: str, init_script: str = "", eval_expr: str = "",
                          wait_ms: int = 1500, timeout_s: int = 30):
        """[v2.17 3.4] 借用浏览器上下文执行页面 JS（页内签名环境捕获 / 通用前端调用）。

        防御：url 必须过目标闸（_http_target_ok 空白名单=仅协议+私网校验——调用方
        resolver 自持平台白名单）；任何失败返回 None（不抛，调用方走诚实降级）。
        池生命周期不变：任务型借用，用完即还（复用懒初始化与 _contexts 队列）。"""
        if getattr(self, '_browser_available', True) is False:
            return None
        from .url_utils import _http_target_ok
        if not _http_target_ok(url, ()):
            logger.warning(f"run_in_page 拒绝非法 url: {str(url)[:60]}")
            return None
        if not getattr(self, '_ready', None) or not self._ready.is_set():
            try:
                await self.init()
            except Exception as e:
                logger.warning(f"run_in_page 懒初始化失败: {e}")
                return None
        entry = None
        try:
            try:
                entry = await asyncio.wait_for(self._contexts.get(), timeout=timeout_s)
            except asyncio.TimeoutError:
                logger.warning("run_in_page 上下文池取用超时")
                return None
            if init_script:
                try:
                    await entry.page.add_init_script(init_script)
                except Exception:
                    pass
            await entry.page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
            if wait_ms > 0:
                await entry.page.wait_for_timeout(wait_ms)
            return await entry.page.evaluate(eval_expr) if eval_expr else None
        except Exception as e:
            logger.debug(f"run_in_page 失败({str(url)[:40]}): {e}")
            return None
        finally:
            if entry is not None:
                try:
                    await self._contexts.put(entry)
                except Exception:
                    pass

    async def render_simple(self, url: str, extra_wait: float = 3.0) -> Tuple[Optional[str], int, dict]:
        """[FIXED & MODIFIED] v2.10.5c 普通渲染通道（论坛/JS 重站专用）

        走复刻验证过的路径：独立 context + 用户 cookies + page.goto(networkidle)，
        不做 CDP deferral/挑战检测（贴吧对 CDP 敏感——隐身链拿不到楼层，普通渲染 803KB 含正文）。
        返回 (html, status, headers)。失败返回 (None, 0, {})。

        [v2.19.6 安全·审查发现] **浏览器层 SSRF 闸（单点防御）**：
        Playwright 的 `page.goto()` 会原生跟随重定向与 JS 跳转，是绕过主通道闸的
        最短路径（实测：主通道拦截态 → 回退升级 → 浏览器跟随 302 进 169.254.169.254
        → 返回 200 + 内网正文）。此处统一拦截，覆盖所有调用方（含未来新增的）。
        """
        from .url_utils import is_private_url as _is_priv
        if not url or _is_priv(url, dns_check=True):
            logger.warning(f"[solver] SSRF 拦截：拒绝浏览器加载私网/保留地址 {str(url)[:60]}")
            return None, 0, {}
        if not getattr(self, '_browser_available', True):
            return None, 0, {}
        # [FIXED & MODIFIED] v2.10.5c 懒初始化（与 solve 一致）：未 ready 时自动 init
        if not getattr(self, '_ready', None) or not self._ready.is_set():
            try:
                await self.init()
            except Exception as e:
                logger.warning(f"render_simple 懒初始化失败: {e}")
                self._browser_available = False
                return None, 0, {}
        try:
            from .source_level_stealth import build_stealth_context_options
            from .universal_downloader import load_site_cookies
            # [FIXED & MODIFIED] v2.10.5 P0-4 render_simple 补隐身链：原裸上下文（无隐身脚本/
            # 无资源拦截/无指纹）——贴吧/知乎主力通道花 10 万字符写的隐身脚本根本没跑在上面。
            # 注入 init_script 不破坏贴吧（贴吧只对 CDP deferral 敏感，JS 层注入安全）。
            ctx = await self.browser.new_context(**build_stealth_context_options(None, None, real_viewport=True))
            try:
                # [FIXED & MODIFIED] v2.10.5 P0-4 修正2：贴吧对 add_init_script 极敏感——
                # 任何 JS 注入（55 维/基础 16 维）都会干扰楼层渲染（650KB 无正文；裸浏览器
                # 803KB 可得正文）。render_simple 保持**零注入**（仅 stealth context options +
                # 用户 cookies + 资源拦截——resource interception 不破坏 DOM）。隐身链由
                # solve/_create_solver_context 负责（验证码/挑战场景）。
                site_cookies = load_site_cookies(url)
                if site_cookies:
                    await ctx.add_cookies(site_cookies)
                page = await ctx.new_page()
                # [FIXED & MODIFIED] v2.10.5 P0-4 修正3：render_simple **不做资源拦截**——
                # _intercept_resources 会 abort 图片/样式/字体，贴吧楼层图片被拦 → 渲染状态
                # 异常（650KB 无正文；无拦截 803KB 得正文）。拦截仅用于 solve/挑战场景。
                # [v2.19.7 安全·扫描发现] 但**必须装轻量私网闸**：不装任何 route 时，
                # 页面 JS 的 fetch/XHR/WS 可直连内网并把响应写进 DOM → page.content() 落盘。
                # `_abort_private` 不动资源类型策略（不破坏贴吧渲染），只拦私网目标。
                try:
                    await page.route("**/*", self._abort_private)
                except Exception:
                    pass
                response = await page.goto(url, wait_until="networkidle", timeout=60000)
                status = response.status if response else 200
                if extra_wait > 0:
                    await page.wait_for_timeout(int(extra_wait * 1000))
                html = await page.content()
                headers = dict(response.headers) if response else {}
                # [v6 修复·R6] 把**浏览器实际落点**带回去。
                # 渲染兜底若是从短链种子进来的，页面里的相对链接会被按**短链主机**解析
                # （`b23.tv/video/BVxxx` —— b23.tv 是短链服务，下面根本没有 /video/ 路径），
                # 于是拼出一堆不存在的 URL，还会被当成视频任务去下载，存下 46 字节的错误页。
                # 用 `page.url`（导航后的真实地址）当基址即可根治。
                try:
                    headers["_final_url"] = page.url
                except Exception:
                    pass  # 拿不到落点只是解析基址退回请求 url，不影响抓取成败
                return html, status, headers
            finally:
                try:
                    await ctx.close()
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"render_simple 失败（非致命）: {e}")
            return None, 0, {}

    async def solve(self, url: str, proxy: Optional[str] = None,
                    challenge_wait: int = 30, extra_wait: float = 0,
                    wait_selector: Optional[str] = None,
                    tracker: Optional[dict] = None,
                    save_sample: Optional[str] = None) -> Tuple[Optional[str], int, dict]:
        """求解反爬挑战（v2.5.0 支持电商渲染等待）

        有代理时创建专用代理上下文（基于代理 IP 生成指纹并预热），
        使用完毕即关闭，不污染主池；无代理时从池中获取上下文。

        Args:
            url: 目标 URL
            proxy: 代理 URL（可选）
            challenge_wait: 挑战求解最大等待秒数
            extra_wait: 渲染后额外等待秒数（电商页面商品数据 XHR 返回）
            wait_selector: 渲染后等待出现的 CSS 选择器（如京东 .gl-item）
            tracker: [v2.11] 可选统计字典——求解过程中填入 challenge_type/
                    challenge_solved（captcha_bench 闭环统计用）
            save_sample: [v2.11] 可选样本目录——保存最终 HTML + 截图
                    （captcha_bench 真实挑战样本自动收集）

        Returns:
            (html, status, headers) 三元组
        """
        # [v2.19.6 安全·审查发现] 浏览器层 SSRF 闸（同 render_simple）：
        # page.goto 会跟随重定向/JS 跳转，必须在进入浏览器前拦住私网目标。
        from .url_utils import is_private_url as _is_priv
        if not url or _is_priv(url, dns_check=True):
            logger.warning(f"[solver] SSRF 拦截：拒绝浏览器求解私网/保留地址 {str(url)[:60]}")
            raise RuntimeError(f"SSRF blocked: private target {str(url)[:60]}")
        # 浏览器不可用快速失败（crawler.setup 降级时设置 _browser_available=False）
        if not getattr(self, '_browser_available', True):
            raise RuntimeError("浏览器引擎不可用（已降级 protocol-only）")
        # [FIXED & MODIFIED] v2.10.5c 懒初始化：首次求解时若未 ready 则自动 init
        # （避免 crawler.setup 强制 init 阻塞/失败拖垮整体——B站等纯下载任务无需浏览器池）
        if not getattr(self, '_ready', None) or not self._ready.is_set():
            try:
                await self.init()
            except Exception as e:
                logger.warning(f"懒初始化求解引擎失败（降级 protocol-only）: {e}")
                self._browser_available = False
                raise
        if proxy:
            # 专用代理上下文：使用完毕即关闭，避免污染主池
            entry = None
            try:
                entry = await self._create_solver_context_entry(proxy)
                return await self._do_solve(entry.page, entry.ctx, url, challenge_wait,
                                            extra_wait=extra_wait, wait_selector=wait_selector,
                                            tracker=tracker, save_sample=save_sample)
            finally:
                if entry is not None:
                    await self._close_entry(entry)
        else:
            ctx, page, page_count = await self.acquire()
            healthy = True
            try:
                return await self._do_solve(page, ctx, url, challenge_wait,
                                            extra_wait=extra_wait, wait_selector=wait_selector,
                                            tracker=tracker, save_sample=save_sample)
            except Exception:
                healthy = False
                raise
            finally:
                await self.release(ctx, page, page_count, healthy=healthy)

    async def _do_solve(self, page, ctx, url, challenge_wait, extra_wait: float = 0,
                        wait_selector: Optional[str] = None,
                        tracker: Optional[dict] = None,
                        save_sample: Optional[str] = None):
        """实际求解逻辑：隐身导航 -> 检测挑战 -> 统一求解 -> 重新加载 -> 提取结果

        使用 source_level_stealth.stealth_navigate 进行隐身导航：
        - 导航前不激活 CDP 域（避免 Network.enable 泄漏）
        - 使用 Google 搜索作为 referer
        - 导航后安全激活 CDP 域并应用协议层指纹
        - 使用 UnifiedChallengeSolver 统一检测 16 类验证码/反爬挑战
        """
        # 创建 CDP 延迟管理器（每次求解独立）
        cdp_mgr = CDPDeferralManager()
        # [FIXED & MODIFIED] v2.10.5b 论坛类（贴吧）不 attach CDP——CDP deferral 干扰
        # 贴吧 JS 楼层渲染（有 attach 拿不到楼层，无 attach 拿到）。
        _plain_nav = ('tieba.baidu.com' in url.lower()) or ('zhihu.com' in url.lower()) or ('xiaojuzi' in url.lower())
        if not _plain_nav:
            try:
                await cdp_mgr.attach(page)
            except Exception:
                pass  # CDP 附加失败不影响核心功能

        # [FIXED & MODIFIED] v2.5.4 登录态注入：cookies.txt 匹配域名的 cookies → 上下文
        # （淘宝登录墙/京东登录价/B站会员等——浏览器扩展导出一次，渲染通道自动带登录态）
        try:
            from .universal_downloader import load_site_cookies, _ensure_cookie_file
            site_cookies = load_site_cookies(url)
            if site_cookies:
                await ctx.add_cookies(site_cookies)
                _cfn = _ensure_cookie_file()
                logger.info(f"已注入站点 cookies: {len(site_cookies)} 个（{_cfn.name if _cfn else 'user cookies'}）")
        except Exception as e:
            logger.debug(f"站点 cookies 注入失败（非致命）: {e}")

        # 隐身导航：CDP 延迟 + Google referer
        # [FIXED & MODIFIED] v2.10.5 论坛/JS 重站卡住修复：domcontentloaded 时贴吧楼层
        # 数据未到（只 2.5KB 防爬首屏）→ 优先 networkidle；失败回退 domcontentloaded
        # [FIXED & MODIFIED] v2.10.5b 贴吧 CDP 隐身干扰：贴吧对 CDP 延迟敏感（隐身拿不到
        # 楼层）→ 对论坛类（tieba.baidu.com）用 use_stealth=False 普通导航（无 CDP/无 Google referer）
        try:
            if _plain_nav:
                # [FIXED & MODIFIED] v2.10.5b 论坛 plain 导航：stealth_navigate 的 CDP 延迟
                # 干扰贴吧 JS 楼层渲染 → 直接 page.goto(networkidle)，无隐身、无 CDP。
                response = await page.goto(url, wait_until="networkidle", timeout=60000)
            else:
                response = await stealth_navigate(
                    page, url, cdp_mgr, use_google_referer=True,
                    wait_until="networkidle", timeout=60000,
                )
            status = response.status if response else 200
        except Exception:
            if _plain_nav:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            else:
                response = await stealth_navigate(
                    page, url, cdp_mgr, use_google_referer=True,
                    wait_until="domcontentloaded", timeout=30000,
                )
            status = response.status if response else 200

        # ═══════════════════════════════════════════════════════════════
        # [FIXED & MODIFIED] v2.10.5c 断链修复：以下"等待/提取HTML/检测挑战/求解/返回"
        #  原先被错误缩进在 except 分支内 → 正常导航成功时函数恒返回 None（浏览器渲染/
        #  挑战求解整条链从不产出）。现改为 try 之后正常路径执行（缩进从 12 拉到 8）。
        # ═══════════════════════════════════════════════════════════════
        # [FIXED & MODIFIED] v2.5.0 电商渲染等待：等商品数据 XHR 返回后再取 HTML
        # （京东/淘宝搜索列表为 JS 动态加载，domcontentloaded 时数据未到）
        if wait_selector:
            try:
                await asyncio.wait_for(
                    page.wait_for_selector(wait_selector, state="attached"),
                    timeout=15.0,
                )
            except Exception:
                pass  # 选择器超时不影响后续
        if extra_wait > 0:
            await page.wait_for_timeout(int(extra_wait * 1000))

        html = await page.content()

        # 导航后应用 CDP 协议层指纹（非 JS 注入）
        try:
            # 从上下文获取指纹：优先从 _in_use 查找，回退到 entry 属性
            entry = self._in_use.get(ctx)
            fp = entry.fingerprint if entry else None
            if fp:
                await apply_cdp_fingerprint(page, fp, cdp_mgr)
        except Exception as e:
            logger.debug(f"CDP 指纹应用失败（非致命）: {e}")

        # [FIXED & MODIFIED] v2.11 代理信号清理 + WebRTC 欺骗接线（原死函数——
        # 走代理的上下文现在真的会清 Proxy-Connection/Via 头 + 禁用 WebRTC ICE 泄漏）
        if not _plain_nav:
            try:
                entry = self._in_use.get(ctx)
                proxy_used = (entry.proxy if entry else None)
                if proxy_used:
                    from .source_level_stealth import apply_proxy_signal_cleanup, apply_webrtc_ip_spoof
                    await apply_proxy_signal_cleanup(page, cdp_mgr)
                    await apply_webrtc_ip_spoof(page, cdp_mgr, proxy_ip="")
            except Exception as e:
                logger.debug(f"代理信号清理失败（非致命）: {e}")

        # [FIXED & MODIFIED] v2.11 iframe 挑战检测：HTML 关键词检测漏 iframe+canvas 型挑战
        # （京东 nlogin/淘宝无痕等挑战在子 frame 中）→ 聚合所有 frame 内容再检测
        _frames_html = ""
        try:
            _frames_parts = []
            for _fr in page.frames:
                try:
                    _fh = await _fr.content()
                    if _fh and len(_fh) > 200:
                        _frames_parts.append(_fh)
                except Exception:
                    pass
            if _frames_parts:
                _frames_html = "\n<!--frame-->\n".join(_frames_parts)
        except Exception:
            pass

        # 统一求解器：16 类挑战全覆盖（传入打码 API 密钥）
        solver = UnifiedChallengeSolver(
            page, challenge_wait,
            two_captcha_key=self.two_captcha_key,
            capsolver_key=self.capsolver_key,
            anti_captcha_key=self.anti_captcha_key,
        )
        challenge_type = await solver.detect(extra_html=_frames_html)
        if challenge_type:
            logger.info(f"检测到挑战 [{challenge_type}]: {url}")
            solved = await solver.solve()
            if tracker is not None:
                tracker["challenge_type"] = challenge_type
                tracker["challenge_solved"] = bool(solved)
            if solved:
                logger.info(f"挑战 [{challenge_type}] 求解成功: {url}")
            else:
                logger.warning(f"挑战 [{challenge_type}] 求解失败: {url}")
            try:
                await asyncio.wait_for(
                    page.wait_for_load_state("networkidle"),
                    timeout=30.0,
                )
            except asyncio.TimeoutError:
                logger.debug("等待 networkidle 超时，继续提取结果")
            except Exception:
                pass
            # 挑战解决后重导航还原内容（论坛类保持普通导航）
            if _plain_nav:
                response = await page.goto(url, wait_until="networkidle", timeout=30000)
            else:
                response = await stealth_navigate(
                    page, url, cdp_mgr, use_google_referer=True,
                    wait_until="networkidle", timeout=30000,
                )
            status = response.status if response else 200
            html = await page.content()

        # [FIXED & MODIFIED] v2.10.5c 人性化行为接入（trajectory_engine 导入零调用的修复）：
        # 渲染完成后执行一次鼠标移动+滚动+空闲潜伏，模拟人类浏览再取内容（降低自动化信号）。
        # [FIXED & MODIFIED] v2.11 加滚动（perform_human_scroll 原死函数接线）；click/typing
        # 保持不自动接线（随机点击可能误触页面按钮/购物车——按需显式调用）。
        if getattr(self, 'stealth_config', {}).get('use_humanization', False):
            try:
                from .trajectory_engine import (perform_human_mouse_move, perform_idle_behavior,
                                                perform_human_scroll)
                vw = self.headless and 1500 or 1920
                await perform_human_mouse_move(page, float(vw * 0.6), float(300 + (len(html) % 20) * 10))
                await perform_human_scroll(page, direction="down",
                                           total_distance=random.randint(150, 350))
                await perform_idle_behavior(page, duration=0.3)
            except Exception:
                pass

        # [FIXED & MODIFIED] v2.11 样本收集：bench 自动保存最终 HTML + 截图（真实挑战样本库）
        if save_sample:
            try:
                import pathlib as _pl
                _sd = _pl.Path(save_sample)
                _sd.mkdir(parents=True, exist_ok=True)
                (_sd / "page.html").write_text(html or "", encoding="utf-8", errors="ignore")
                await page.screenshot(path=str(_sd / "page.png"), full_page=False)
            except Exception as e:
                logger.debug(f"样本保存失败: {e}")

        cookies_list = await ctx.cookies()
        cookies_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies_list])
        if self.session_pool:
            self.session_pool.update_session(
                url, cookies=cookies_str,
                headers=dict(response.headers) if response else {})
        headers_dict = dict(response.headers) if response else {}
        # [FIXED & MODIFIED] v2.10.5c 断链修复：CDP 清理提前到正常路径 return 前执行
        # （原孤立 finally 因缩进错位导致语法错误——外层 solve() 的 try/finally 已兜底异常路径）
        try:
            await cdp_mgr.detach()
        except Exception:
            pass
        # [v6 修复·R6] 同 render_simple：把浏览器实际落点带回去当解析基址，
        # 否则短链种子的相对链接会拼成 `b23.tv/video/BV...` 这种不存在的地址。
        try:
            headers_dict["_final_url"] = page.url
        except Exception:
            pass  # 同上：落点缺失只降级解析基址，不阻断
        return html, status, headers_dict
    # ════════════════════════════════════════════════════════════════
    # 内存监控
    # ════════════════════════════════════════════════════════════════
    def _start_memory_monitor(self):
        """启动内存监控后台任务"""
        if self._memory_task is None or self._memory_task.done():
            self._memory_task = asyncio.create_task(self._memory_monitor_loop())

    def _stop_memory_monitor(self):
        """停止内存监控后台任务"""
        if self._memory_task is not None and not self._memory_task.done():
            self._memory_task.cancel()
        self._memory_task = None

    async def _memory_monitor_loop(self):
        """周期性检查浏览器进程内存，超限则触发上下文回收或完整重启"""
        try:
            while not self._closing:
                await asyncio.sleep(self.memory_check_interval)
                if self._closing or not self._is_browser_alive():
                    continue
                mem_mb = self._get_browser_memory_mb()
                if mem_mb <= 0:
                    continue

                # 超过上限：回收部分上下文以释放内存
                if mem_mb > self.memory_limit_mb:
                    logger.warning(
                        f"浏览器内存 {mem_mb:.0f}MB 超过上限 "
                        f"{self.memory_limit_mb}MB，触发上下文回收")
                    recycled = await self._recycle_pool_contexts(
                        count=min(self.pool_size, 2))
                    logger.info(f"已回收 {recycled} 个上下文以释放内存")

                # 临界内存（2 倍上限）：触发完整重启（带冷却）
                if mem_mb > self.memory_limit_mb * 2:
                    now = time.monotonic()
                    if now - self._last_restart > self._restart_cooldown:
                        self._last_restart = now
                        logger.critical(
                            f"浏览器内存 {mem_mb:.0f}MB 临界，触发完整重启")
                        await self._safe_restart()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"内存监控循环异常: {e}")

    def _get_browser_memory_mb(self) -> float:
        """获取所有浏览器子进程的 RSS 内存总和（MB）

        通过 psutil 遍历当前进程的所有子进程，筛选 chromium 进程并累加 RSS。
        无 psutil 时返回 0（禁用内存监控）。
        """
        if not HAS_PSUTIL:
            return 0.0
        total = 0
        try:
            for proc in psutil.Process().children(recursive=True):
                try:
                    name = proc.name().lower()
                    if "chrom" in name:
                        total += proc.memory_info().rss
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception:
            pass
        return total / (1024 * 1024)

    async def _recycle_pool_contexts(self, count: int = 1) -> int:
        """回收池中若干上下文并以全新上下文替换

        Args:
            count: 最多回收的上下文数量

        Returns:
            实际回收并重建的上下文数量
        """
        recycled = 0
        for _ in range(count):
            try:
                entry = self._contexts.get_nowait()
            except asyncio.QueueEmpty:
                break
            await self._close_entry(entry)
            try:
                new_entry = await self._create_solver_context_entry()
                await self._contexts.put(new_entry)
                recycled += 1
            except Exception as e:
                logger.error(f"内存回收：上下文重建失败: {e}")
        return recycled

    # ════════════════════════════════════════════════════════════════
    # 重启与关闭
    # ════════════════════════════════════════════════════════════════
    async def _safe_restart(self):
        """带保护的重启，避免并发重复重启

        使用重启锁串行化，若检测到浏览器已恢复则跳过。
        """
        async with self._restart_lock:
            if self._is_browser_alive() and self._ready.is_set():
                return  # 已被其他协程重启完成
            try:
                await self.restart()
            except Exception as e:
                logger.error(f"浏览器重启失败: {e}")

    async def restart(self):
        """关闭并重启浏览器，用于主动内存回收或崩溃恢复"""
        logger.info("正在重启浏览器...")
        await self.close()
        self._closing = False
        await self.init()

    async def close(self):
        """关闭浏览器与所有上下文，清理后台任务"""
        self._closing = True
        self._stop_memory_monitor()
        self._ready.clear()

        # 关闭所有池中上下文
        while not self._contexts.empty():
            try:
                entry = await asyncio.wait_for(self._contexts.get(), timeout=1.0)
                await self._close_entry(entry)
            except (asyncio.TimeoutError, Exception):
                break  # 超时或其他异常均退出循环

        # 关闭仍在使用中的上下文
        for entry in list(self._in_use.values()):
            await self._close_entry(entry)
        self._in_use.clear()

        # 关闭浏览器与 playwright
        if self.browser and not self._cdp_attached:
            try:
                await self.browser.close()
            except Exception:
                pass
            self.browser = None
        elif self.browser:
            self.browser = None  # CDP 接管：浏览器归用户，不销毁（仅解除引用）
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception:
                pass
            self.playwright = None
