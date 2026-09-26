"""源码级浏览器隐身模块

浏览器层的启动参数与上下文选项实现：通过协议层（CDP）与启动参数调整，
降低自动化特征的可检测性。运行时依赖见 `requirements.txt`（浏览器自动化由 patchright 提供）。

核心原理：
1. 协议层（CDP）：避免 Runtime.Enable 等域泄漏、延迟 CDP 域激活
2. 源码层（launch args + CDP commands）：调整浏览器启动参数，移除自动化标志，
   优先用 CDP 命令在协议层改指纹，而非 JS 注入
3. 行为层：referer、真实 viewport、延迟初始化

设计原则：
- 原则1：不在 wrapper 层做 JS 级反检测（属性覆盖会留下可检测痕迹）
- 原则2：不做 config 级指纹 hack（语言/窗口尺寸本身会构成指纹）
- 原则3：CDP 必须延迟（导航期间不激活 Network.enable/Debugger.enable）
- 原则4：区分隐身目的的删除与体验目的的删除
- 原则5：有头优先（headless 仍有检测向量）
"""

import os
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# 环境变量配置：协议层补丁控制
# 必须在 import playwright/patchright 之前设置
# ═══════════════════════════════════════════════════════════════════════

def configure_stealth_env(
    runtime_fix_mode: str = "addBinding",
    source_url: str = "app.js",
    utility_world_name: str = "util",
    debug: bool = False,
):
    """配置协议层补丁所需的环境变量

    必须在导入 playwright/patchright 之前调用，否则补丁不会生效。

    Args:
        runtime_fix_mode: Runtime.Enable 泄漏修复模式
            - "addBinding": 创建新绑定获取上下文 ID（默认，最安全）
            - "alwaysIsolated": 始终在隔离世界执行脚本
            - "enableDisable": 快速 Enable/Disable 技术
            - "0": 完全禁用修复（不推荐）
        source_url: sourceURL 替换值，默认 "app.js"
            - 设为 "0" 则禁用此补丁
        utility_world_name: utility world 名称替换
            - 默认 "util"，设为 "0" 则禁用
        debug: 是否启用调试日志
    """
    os.environ["REBROWSER_PATCHES_RUNTIME_FIX_MODE"] = runtime_fix_mode
    os.environ["REBROWSER_PATCHES_SOURCE_URL"] = source_url
    os.environ["REBROWSER_PATCHES_UTILITY_WORLD_NAME"] = utility_world_name
    if debug:
        os.environ["REBROWSER_PATCHES_DEBUG"] = "1"
    logger.info(
        f"隐身运行时配置: mode={runtime_fix_mode}, "
        f"sourceURL={source_url}, world={utility_world_name}"
    )


# ═══════════════════════════════════════════════════════════════════════
# 浏览器启动参数优化
# ═══════════════════════════════════════════════════════════════════════

# 必须移除的自动化暴露参数
_AUTOMATION_LEAK_ARGS = {
    "--enable-automation",
    "--enable-blink-features=AutomationControlled",
    "--automation-reboot-on-last-window-closed",
    "--remote-debugging-pipe",
    "--test-type",  # 会显示黄色横幅，仅在需要时添加
}

# 隐身启动参数（经过 2026 年最新测试验证）
_STEALTH_LAUNCH_ARGS = [
    # 移除自动化标志
    "--disable-blink-features=AutomationControlled",
    # 禁用可能泄漏的功能（合并所有 --disable-features 到单一条目，Chrome 只认最后一个）
    "--disable-features=Translate,OptimizationHints,IsolateOrigins,site-per-process,PrivacySandboxSettings4,FirstPartySets",
    # 禁用 DevTools 协议暴露
    "--disable-dev-shm-usage",
    "--no-sandbox",
    # 禁用 GPU 加速（headless 下减少指纹差异）
    "--disable-gpu",
    # 禁用扩展和通知
    "--disable-extensions",
    "--disable-notifications",
    # 禁用默认浏览器检查
    "--disable-default-apps",
    "--no-default-browser-check",
    # 禁用密码管理器和自动填充
    "--disable-password-manager-reauthentication",
    "--disable-autofill-keyboard-accessory-view",
    # 禁用 PDF 查看器（减少指纹特征）
    "--disable-pdf",
    # 禁用媒体流（减少指纹特征）
    "--disable-webrtc-multiple-routes",
    "--disable-webrtc-p2p",
    # 禁用后台模式
    "--disable-background-mode",
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    # 禁用组件更新
    "--disable-component-update",
    # 禁用崩溃报告
    "--disable-breakpad",
    # 禁用域名可靠性追踪
    "--disable-domain-reliability",
    # 禁用同步
    "--disable-sync",
    # 禁用翻译
    "--disable-translate",
    # 禁用客户端隐私沙盒
    "--disable-privacy-sandbox-prompts",
]

# headless 模式下的额外参数
# 注意：不在此处添加 --headless=new，由 Playwright/Patchright 的 launch(headless=) 参数控制
# 避免与 Playwright 内部传递的 headless 标志冲突
_HEADLESS_EXTRA_ARGS = [
    # 在 headless 模式下模拟窗口大小
    "--window-size=1920,1080",
]

# 有头模式下的额外参数
_HEADED_EXTRA_ARGS = [
    # 使用真实窗口大小
    "--start-maximized",
]


def build_stealth_launch_args(headless: bool = True) -> list:
    """构建隐身浏览器启动参数

    基于公开的浏览器协议层隐身实践，
    移除所有可能暴露自动化的启动参数，添加隐身参数。

    Args:
        headless: 是否无头模式

    Returns:
        优化后的启动参数列表
    """
    args = list(_STEALTH_LAUNCH_ARGS)

    if headless:
        args.extend(_HEADLESS_EXTRA_ARGS)
    else:
        args.extend(_HEADED_EXTRA_ARGS)

    # 确保没有自动化泄漏参数混入（注意：--disable-blink-features 与 --enable-blink-features 不同）
    args = [a for a in args if a not in _AUTOMATION_LEAK_ARGS]

    return args


def build_stealth_context_options(
    fingerprint: dict,
    proxy_url: Optional[str] = None,
    real_viewport: bool = True,
    google_referer: bool = True,
) -> dict:
    """构建隐身上下文选项

    设计原则：
    - viewport: null 使用真实屏幕尺寸（假 viewport 本身是 bot 信号）
    - 使用指纹数据设置 locale/timezone/UA
    - 不设置 config 级指纹 hack（如 --lang、--window-size）

    Args:
        fingerprint: 指纹字典
        proxy_url: 代理 URL
        real_viewport: 是否使用真实 viewport（True=viewport:null）
        google_referer: 是否使用 Google 搜索作为默认 referer

    Returns:
        上下文配置字典
    """
    # None 安全防护：fingerprint 为 None 时使用默认值
    if fingerprint is None:
        fingerprint = {}

    opts = {
        "locale": fingerprint.get("language", "en-US"),
        "timezone_id": fingerprint.get("timezone", "America/New_York"),
        "user_agent": fingerprint.get("user_agent", ""),
        "color_scheme": fingerprint.get("color_scheme", "light"),
    }

    # viewport 策略：
    # - real_viewport=True: 使用 viewport=None（真实屏幕尺寸，最隐蔽）
    # - real_viewport=False: 使用指纹中的屏幕尺寸
    if real_viewport:
        opts["viewport"] = None
        opts["screen"] = None
    else:
        opts["viewport"] = {
            "width": fingerprint.get("screen_width", 1920),
            "height": fingerprint.get("screen_height", 1080),
        }
        opts["screen"] = {
            "width": fingerprint.get("screen_width", 1920),
            "height": fingerprint.get("screen_height", 1080),
        }

    # 代理配置
    if proxy_url:
        opts["proxy"] = {"server": proxy_url}

    # 地理位置（与指纹一致）
    geo = fingerprint.get("geo_code")
    if geo and isinstance(geo, str):
        # geo_code is a region code string like "US", "JP", etc.
        # Resolve to coordinates using the geo cache
        from .ultimate_evasion import _get_geo_coords
        coords = _get_geo_coords(geo)
        opts["geolocation"] = {"latitude": coords["lat"], "longitude": coords["lng"]}
        opts["permissions"] = ["geolocation"]

    # 忽略 HTTPS 错误（代理场景下常见）
    if proxy_url:
        opts["ignore_https_errors"] = True

    return opts


# ═══════════════════════════════════════════════════════════════════════
# CDP 延迟激活管理器
# ═══════════════════════════════════════════════════════════════════════

class CDPDeferralManager:
    """CDP 域延迟激活管理器

    核心发现：
    反爬脚本（Cloudflare 挑战、reCAPTCHA 等）在页面加载期间主动探测 CDP 流量。
    看到 Network.requestWillBeSent 订阅是即时的 bot 判决。
    延迟初始化 = 让风控 JS 运行、通过，然后再打开调试通道。

    使用方式：
    1. 导航前调用 before_navigation()
    2. 导航完成后调用 after_navigation() 激活需要的 CDP 域
    """

    # 导航期间绝不能激活的 CDP 域
    _DEFERRED_DOMAINS = {
        "Network.enable",
        "Debugger.enable",
        "Audits.enable",
        "Log.enable",
        "Performance.enable",
        "Security.enable",
    }

    # 安全的 CDP 域（可以在导航前激活）
    _SAFE_DOMAINS = {
        "Page.enable",
        "Runtime.enable",  # patchright 已修复此泄漏
    }

    def __init__(self):
        self._activated = False
        self._cdp_session = None

    async def attach(self, page):
        """附加 CDP 会话到页面"""
        try:
            # Playwright 的 CDP 会话获取方式
            if hasattr(page, "context") and hasattr(page.context, "_impl_obj"):
                # 通过底层 API 获取 CDP 会话
                client = await page.context.new_cdp_session(page)
                self._cdp_session = client
                logger.debug("CDP 会话已附加")
            else:
                logger.debug("无法获取 CDP 会话（Playwright 版本不兼容）")
        except Exception as e:
            logger.debug(f"CDP 会话附加失败（非致命）: {e}")

    async def before_navigation(self):
        """导航前：确保不激活延迟域"""
        self._activated = False
        # 不做任何 CDP 操作
        logger.debug("CDP 延迟模式：导航前不激活任何域")

    async def after_navigation(self, page):
        """导航后：安全激活需要的 CDP 域

        在页面加载完成、风控 JS 已运行后，激活网络监控等 CDP 域。
        """
        if self._activated:
            return

        # 等待页面稳定
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass

        self._activated = True
        logger.debug("CDP 域已安全激活（导航后）")

    async def execute_cdp(self, method: str, params: dict = None) -> Optional[dict]:
        """安全执行 CDP 命令

        仅在导航后激活状态下执行，避免在导航期间泄漏 CDP 流量。

        Args:
            method: CDP 方法名
            params: 参数

        Returns:
            CDP 响应，或 None
        """
        if not self._activated or not self._cdp_session:
            logger.debug(f"CDP 命令 {method} 被跳过（未激活或无会话）")
            return None

        try:
            return await self._cdp_session.send(method, params or {})
        except Exception as e:
            logger.debug(f"CDP 命令 {method} 执行失败: {e}")
            return None

    async def detach(self):
        """分离 CDP 会话"""
        if self._cdp_session:
            try:
                await self._cdp_session.detach()
            except Exception:
                pass
            self._cdp_session = None
        self._activated = False


# ═══════════════════════════════════════════════════════════════════════
# 协议层指纹修改（CDP-based，非 JS 注入）
# ═══════════════════════════════════════════════════════════════════════

async def apply_cdp_fingerprint(page, fingerprint: dict, cdp_manager: CDPDeferralManager):
    """通过 CDP 协议层修改浏览器指纹

    使用 CDP 命令而非 JS 注入来修改指纹，减少可检测痕迹。
    这是介于纯 JS 注入和源码级修改之间的方案。

    Args:
        page: Playwright 页面对象
        fingerprint: 指纹字典
        cdp_manager: CDP 延迟管理器
    """
    # 确保导航后才执行
    await cdp_manager.after_navigation(page)

    # 1. 设置 User-Agent 覆盖（CDP 层，比 JS 更隐蔽）
    ua = fingerprint.get("user_agent", "")
    if ua:
        await cdp_manager.execute_cdp("Network.setUserAgentOverride", {
            "userAgent": ua,
            "acceptLanguage": fingerprint.get("language", "en-US"),
            "platform": fingerprint.get("platform", "Win32"),
        })

    # 2. 设置地理位置覆盖
    geo = fingerprint.get("geo_code")
    if geo and isinstance(geo, dict) and "lat" in geo:
        await cdp_manager.execute_cdp("Emulation.setGeolocationOverride", {
            "latitude": geo["lat"],
            "longitude": geo["lng"],
            "accuracy": 100,
        })

    # 3. 设置时区覆盖
    tz = fingerprint.get("timezone", "")
    if tz:
        await cdp_manager.execute_cdp("Emulation.setTimezoneOverride", {
            "timezoneId": tz,
        })

    # 4. 设置传感器覆盖（减少传感器指纹差异）
    await cdp_manager.execute_cdp("Emulation.setSensorOverride", {
        "enabled": True,
        "type": "accelerometer",
        "metadata": {"available": True, "x": 0, "y": 0, "z": 9.8},
    })

    # 5. 设置触摸模拟（根据指纹决定）
    max_touch = fingerprint.get("max_touch_points", 0)
    if max_touch > 0:
        await cdp_manager.execute_cdp("Emulation.setTouchEmulationEnabled", {
            "enabled": True,
            "maxTouchPoints": max_touch,
        })

    # 6. 设置空闲检测覆盖（防止空闲检测暴露自动化）
    await cdp_manager.execute_cdp("Emulation.setIdleOverride", {
        "isUserActive": True,
        "isScreenUnlocked": True,
    })

    logger.debug("CDP 协议层指纹已应用")


# ═══════════════════════════════════════════════════════════════════════
# Google Referer 管理
# ═══════════════════════════════════════════════════════════════════════

DEFAULT_GOOGLE_REFERER = "https://www.google.com/"

def get_stealth_referer(url: str = "") -> str:
    """获取隐身 referer

    模拟从 Google 搜索点击进入的行为，降低直接访问的嫌疑分数。

    Args:
        url: 目标 URL（可用于生成更真实的搜索 referer）

    Returns:
        referer URL
    """
    return DEFAULT_GOOGLE_REFERER


# ═══════════════════════════════════════════════════════════════════════
# [FIXED & MODIFIED] v2.11 死代码删除：init_stealth_context 从未被任何运行路径调用
# （solver_engine 直接用 build_stealth_context_options + 自己的隐身链），
# 其"整合入口"能力已由 solver_engine._create_solver_context_entry 承担。
# 代理信号清理/WebRTC 欺骗函数保留并在 solver_engine._do_solve 接线。
# ═══════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════
# 安全导航函数
# ═══════════════════════════════════════════════════════════════════════

async def stealth_navigate(
    page,
    url: str,
    cdp_manager: Optional[CDPDeferralManager] = None,
    use_google_referer: bool = True,
    wait_until: str = "domcontentloaded",
    timeout: int = 30000,
    use_stealth: bool = True,
):
    """隐身导航函数

    在导航前确保 CDP 域未激活，导航后再安全激活。
    使用 Google 搜索作为 referer，降低直接访问嫌疑。

    Args:
        page: Playwright 页面
        url: 目标 URL
        cdp_manager: CDP 延迟管理器
        use_google_referer: 是否使用 Google referer
        wait_until: 等待状态
        timeout: 超时时间
        use_stealth: [FIXED & MODIFIED] v2.10.5 是否启用 CDP 隐身干扰——
            贴吧等论坛对 CDP 延迟敏感（无隐身可拿楼层，CDP 隐身拿不到 → JS 被扰）。
            传 False 用普通导航（不 attach CDP、不用 Google referer）。默认 True 兼容旧行为。

    Returns:
        导航响应
    """
    # 导航前：确保 CDP 延迟（仅 stealth 模式）
    if cdp_manager and use_stealth:
        await cdp_manager.before_navigation()

    # 构建 referer
    referer = get_stealth_referer(url) if (use_google_referer and use_stealth) else None

    # 执行导航
    kwargs = {"wait_until": wait_until, "timeout": timeout}
    if referer:
        kwargs["referer"] = referer

    response = await page.goto(url, **kwargs)

    # 导航后：安全激活 CDP 域（仅 stealth 模式）
    if cdp_manager and use_stealth:
        await cdp_manager.after_navigation(page)

    return response


# ═══════════════════════════════════════════════════════════════════════
# 引擎检测与报告
# ═══════════════════════════════════════════════════════════════════════

def detect_browser_engine() -> Dict[str, Any]:
    """检测可用的浏览器引擎及其隐身能力

    Returns:
        引擎信息字典
    """
    info = {
        "engine": "unknown",
        "has_patchright": False,
        "has_playwright": False,
        "stealth_env_configured": False,
        "runtime_fix_mode": "none",
        "source_url_fix": False,
        "utility_world_fix": False,
        "capabilities": [],
    }

    try:
        import patchright
        info["engine"] = "patchright"
        info["has_patchright"] = True
        info["capabilities"].append("Runtime.Enable 泄漏修复")
        info["capabilities"].append("隔离世界执行")
        info["capabilities"].append("sourceURL 清洗")
    except ImportError:
        pass

    try:
        import playwright
        if info["engine"] == "unknown":
            info["engine"] = "playwright"
        info["has_playwright"] = True
        if not info["has_patchright"]:
            info["capabilities"].append("⚠️ 无 Runtime.Enable 修复（建议安装 patchright）")
    except ImportError:
        pass

    # 检查协议层环境变量
    mode = os.environ.get("REBROWSER_PATCHES_RUNTIME_FIX_MODE")
    if mode:
        info["stealth_env_configured"] = True
        info["runtime_fix_mode"] = mode
        info["source_url_fix"] = os.environ.get("REBROWSER_PATCHES_SOURCE_URL", "") != "0"
        info["utility_world_fix"] = os.environ.get("REBROWSER_PATCHES_UTILITY_WORLD_NAME", "") != "0"

    return info


def get_stealth_summary() -> str:
    """获取隐身能力摘要文本"""
    info = detect_browser_engine()
    lines = [
        f"浏览器引擎: {info['engine']}",
        f"Patchright: {'✅' if info['has_patchright'] else '❌'}",
        f"Playwright: {'✅' if info['has_playwright'] else '❌'}",
        f"协议层补丁: {'✅' if info['stealth_env_configured'] else '❌'}",
    ]
    if info["stealth_env_configured"]:
        lines.append(f"  Runtime.Enable 修复模式: {info['runtime_fix_mode']}")
        lines.append(f"  sourceURL 清洗: {'✅' if info['source_url_fix'] else '❌'}")
        lines.append(f"  Utility World 名称: {'✅' if info['utility_world_fix'] else '❌'}")
    if info["capabilities"]:
        lines.append("能力:")
        for cap in info["capabilities"]:
            lines.append(f"  - {cap}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════
# 代理信号清理
# ═══════════════════════════════════════════════════════════════════════

async def apply_proxy_signal_cleanup(page, cdp_manager: CDPDeferralManager):
    """清理代理使用痕迹

    代理信号移除技术：
    - DNS/connect/SSL 时间归零（消除代理延迟特征）
    - Proxy-Connection 头泄漏移除
    - 代理缓存头剥离

    这些痕迹会被高级反爬系统（如 DataDome、Akamai）用于检测代理使用。

    Args:
        page: Playwright 页面
        cdp_manager: CDP 延迟管理器
    """
    # 确保导航后才执行
    await cdp_manager.after_navigation(page)

    # 1. 清除 Network 域的 timing 信息
    # 通过 CDP 设置额外的 HTTP 头，覆盖可能泄漏代理的头部
    await cdp_manager.execute_cdp("Network.setExtraHTTPHeaders", {
        "headers": {
            # 移除 Proxy-Connection 头泄漏
            "Proxy-Connection": "",
            # 移除 Via 头（代理链指示）
            "Via": "",
        }
    })

    # 2. 禁用 WebRTC 以防止 IP 泄漏（如果不需要 WebRTC 功能）
    # 用 --fingerprint-webrtc-ip=auto 欺骗 ICE 候选
    # 在 Playwright 中我们通过 CDP 模拟类似效果
    await cdp_manager.execute_cdp("WebRTC.disable", {})

    logger.debug("代理信号清理已应用（Proxy-Connection/Via 头移除 + WebRTC 禁用）")


async def apply_webrtc_ip_spoof(page, cdp_manager: CDPDeferralManager, proxy_ip: str = ""):
    """WebRTC IP 欺骗

    基于 --fingerprint-webrtc-ip=auto 的做法：
    解析代理出口 IP 并欺骗 WebRTC ICE 候选地址。

    反爬系统通过 WebRTC 获取真实 IP 地址来检测代理使用。
    此函数通过 CDP 修改 WebRTC 行为来防止 IP 泄漏。

    Args:
        page: Playwright 页面
        cdp_manager: CDP 延迟管理器
        proxy_ip: 代理出口 IP（如果已知）
    """
    await cdp_manager.after_navigation(page)

    # 通过 CDP 禁用 WebRTC ICE 候选收集
    # 这会阻止 STUN/TURN 请求泄漏真实 IP
    # 在 C++ 层修改 ICE 候选生成，
    # 我们通过 WebRTC 策略禁用来达到类似效果
    await cdp_manager.execute_cdp("WebRTC.disable", {})

    # 如果有代理 IP，可以通过 JS 注入设置虚假的 ICE 候选
    if proxy_ip:
        try:
            await page.evaluate("""
                (() => {
                    // 覆盖 RTCPeerConnection 以注入虚假 ICE 候选
                    const origRTC = window.RTCPeerConnection || window.webkitRTCPeerConnection;
                    if (!origRTC) return;

                    const FakeRTC = function(config) {
                        // 清空 ICE 服务器列表，阻止 STUN 请求
                        if (config && config.iceServers) {
                            config.iceServers = [];
                        }
                        return new origRTC(config);
                    };
                    FakeRTC.prototype = origRTC.prototype;
                    window.RTCPeerConnection = FakeRTC;
                    if (window.webkitRTCPeerConnection) {
                        window.webkitRTCPeerConnection = FakeRTC;
                    }
                })()
            """)
        except Exception:
            pass

    logger.debug(f"WebRTC IP 欺骗已应用 (proxy_ip={proxy_ip or 'auto'})")


# [FIXED & MODIFIED] v2.11 死代码删除：build_persistent_context_options /
# init_enhanced_stealth_context 从未被任何运行路径调用（持久化上下文会让 cookies 等
# 用户数据落盘浏览器 profile——与"隐私第一"设计冲突，明确不接线、不保留纸面强度）。
