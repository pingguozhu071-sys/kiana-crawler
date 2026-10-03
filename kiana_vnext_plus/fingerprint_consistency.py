"""高级浏览器指纹生成器

基于代理 IP 生成确定性、一致性的浏览器指纹。
覆盖 Canvas/WebGL/Audio/Font/Navigator/Screen/Timezone 等全维度。
"""
import hashlib
import random
import re


# ── GPU 厂商/渲染器组合（28 组，覆盖 2023-2025 全主流显卡）────────────────
GPU_PROFILES = [
    # Intel 集显
    ("Intel Inc.", "Intel(R) Iris(R) Xe Graphics"),
    ("Intel Inc.", "Intel(R) UHD Graphics 630"),
    ("Intel Inc.", "Intel(R) Iris(R) Plus Graphics 645"),
    ("Intel Inc.", "Intel(R) HD Graphics 530"),
    ("Intel Inc.", "Intel(R) UHD Graphics 770"),
    ("Intel Inc.", "Intel(R) UHD Graphics 730"),
    ("Intel Inc.", "Intel(R) Iris(R) Xe MAX Graphics"),
    ("Intel Inc.", "Intel(R) Arc(TM) A380 Graphics"),
    ("Intel Inc.", "Intel(R) Arc(TM) A770 Graphics"),
    # NVIDIA 30/40 系列
    ("NVIDIA Corporation", "NVIDIA GeForce RTX 3060/PCIe/SSE2"),
    ("NVIDIA Corporation", "NVIDIA GeForce RTX 3070/PCIe/SSE2"),
    ("NVIDIA Corporation", "NVIDIA GeForce RTX 3080/PCIe/SSE2"),
    ("NVIDIA Corporation", "NVIDIA GeForce RTX 4060/PCIe/SSE2"),
    ("NVIDIA Corporation", "NVIDIA GeForce RTX 4060 Ti/PCIe/SSE2"),
    ("NVIDIA Corporation", "NVIDIA GeForce RTX 4070/PCIe/SSE2"),
    ("NVIDIA Corporation", "NVIDIA GeForce RTX 4070 Ti/PCIe/SSE2"),
    ("NVIDIA Corporation", "NVIDIA GeForce RTX 4080/PCIe/SSE2"),
    ("NVIDIA Corporation", "NVIDIA GeForce RTX 5060/PCIe/SSE2"),
    ("NVIDIA Corporation", "NVIDIA GeForce GTX 1650/PCIe/SSE2"),
    ("NVIDIA Corporation", "NVIDIA GeForce GTX 1660 Ti/PCIe/SSE2"),
    # AMD 显卡
    ("AMD", "AMD Radeon RX 6600 XT"),
    ("AMD", "AMD Radeon RX 7600"),
    ("AMD", "AMD Radeon RX 7800 XT"),
    ("AMD", "AMD Radeon(TM) Graphics"),
    ("AMD", "AMD Radeon Pro 5500M OpenGL Engine"),
    ("ATI Technologies Inc.", "AMD Radeon HD 7950"),
    # Apple GPU
    ("Apple", "Apple M1"),
    ("Apple", "Apple M2"),
]

# ── 屏幕分辨率组合 ──────────────────────────────────────────────────
SCREEN_RESOLUTIONS = [
    (1920, 1080), (2560, 1440), (1366, 768), (1536, 864),
    (1440, 900), (1680, 1050), (2560, 1080), (3440, 1440),
    (1280, 720), (1600, 900),
]

# ── 时区与地理区域映射 ─────────────────────────────────────────────
TIMEZONE_MAP = {
    "US": ("America/New_York", -5, "en-US"),
    "US-CA": ("America/Los_Angeles", -8, "en-US"),
    "US-CENTRAL": ("America/Chicago", -6, "en-US"),
    "UK": ("Europe/London", 0, "en-GB"),
    "DE": ("Europe/Berlin", 1, "de-DE"),
    "FR": ("Europe/Paris", 1, "fr-FR"),
    "JP": ("Asia/Tokyo", 9, "ja-JP"),
    "CN": ("Asia/Shanghai", 8, "zh-CN"),
    "HK": ("Asia/Hong_Kong", 8, "zh-HK"),
    "SG": ("Asia/Singapore", 8, "en-SG"),
    "AU": ("Australia/Sydney", 10, "en-AU"),
    "CA": ("America/Toronto", -5, "en-CA"),
}

# ── Chrome 版本范围（120-138，覆盖 2024-2025 全版本）──────────────────
CHROME_VERSIONS = list(range(120, 139))


def clamp_screen_and_window(sw: int, sh: int, win_w=None, win_h=None):
    """[v2.17 E-P1-6] 屏幕/任务栏/窗口一致性 clamp（参考 Camoufox pythonlib 纯 Python
    逻辑——MIT 部分，适配本工程实现）：可用高度必须小于屏高（任务栏 40-72px），
    窗口尺寸必须 <= 屏幕（防"Windows UA + 任务栏截掉后 avail>screen"式不可能组合）。
    返回 (avail_h, win_w, win_h)。"""
    avail_h = sh - min(max(int(sh * 0.06), 40), 72)  # 任务栏占屏高 6% 区间的保底取值
    if win_h is None:
        win_h = avail_h
    if win_w is None:
        win_w = int(sw * (0.9 if win_h == avail_h else 0.95))
    win_w = min(win_w, sw)
    win_h = min(win_h, avail_h)
    return avail_h, win_w, win_h

# ── HTTP/2 指纹模板（SETTINGS 帧参数，用于 Akamai HTTP/2 指纹检测）─────────
HTTP2_FINGERPRINT_TEMPLATES = [
    # [HEADER_TABLE_SIZE, ENABLE_PUSH, MAX_CONCURRENT_STREAMS, INITIAL_WINDOW_SIZE, MAX_FRAME_SIZE, MAX_HEADER_LIST_SIZE]
    [65536, 1, 1000, 6291456, 16384, 262144],
    [65536, 0, 1000, 6291456, 16384, 262144],
    [4096, 1, 100, 6291456, 16384, 65536],
    [65536, 1, 256, 6291456, 16384, 262144],
    [65536, 0, 1000, 15728640, 16384, 262144],
]

# ── userAgentData 高熵值品牌组合池 ──────────────────────────────────
UA_DATA_BRAND_POOLS = [
    [("Chromium", None), ("Google Chrome", None), ("Not;A=Brand", "24")],
    [("Chromium", None), ("Google Chrome", None), ("Not.A/Brand", "99")],
    [("Chromium", None), ("Google Chrome", None), ("Not)A=Brand", "8")],
    [("Chromium", None), ("Google Chrome", None), ("Not/A)Brand", "24")],
    [("Chromium", None), ("Not;A=Brand", "24"), ("Google Chrome", None)],
]

# ── 硬件并发数选项 ─────────────────────────────────────────────────
HARDWARE_CONCURRENCY = [4, 6, 8, 8, 12, 12, 16, 16, 20, 24]

# ── 设备内存选项（GB）──────────────────────────────────────────────
DEVICE_MEMORY = [4, 8, 8, 16, 16, 32]

# ── 系统平台 ──────────────────────────────────────────────────────
PLATFORMS = [
    "Win32",
    "Win32",
    "Win32",
    "MacIntel",
    "Linux x86_64",
]

# ── 字体列表池（每个会话随机选一个子集）──────────────────────────────
FONT_POOL = [
    "Arial", "Arial Black", "Arial Narrow", "Calibri", "Cambria",
    "Cambria Math", "Candara", "Comic Sans MS", "Consolas", "Constantia",
    "Corbel", "Courier", "Courier New", "Ebrima", "Franklin Gothic Medium",
    "Gabriola", "Gadugi", "Georgia", "Impact", "Javanese Text",
    "Leelawadee UI", "Lucida Console", "Lucida Sans Unicode", "MS Gothic",
    "MS PGothic", "MS Sans Serif", "MS Serif", "MV Boli", "Malgun Gothic",
    "Microsoft Sans Serif", "MingLiU-ExtB", "Mongolian Baiti",
    "Nirmala UI", "Palatino Linotype", "Segoe Print", "Segoe Script",
    "Segoe UI", "Segoe UI Emoji", "Segoe UI Historic", "Segoe UI Symbol",
    "SimSun", "Sitka Small", "Sylfaen", "Tahoma", "Times New Roman",
    "Trebuchet MS", "Verdana", "Webdings", "Wingdings", "Yu Gothic",
]


def _seed_rng(seed_hex: str) -> random.Random:
    """从十六进制种子创建确定性随机数生成器"""
    seed_int = int(seed_hex[:8], 16)
    return random.Random(seed_int)


# ── 本机会话标识与本地地理（[v6 修复] 见 _pick_geo 的说明）─────────────
LOCAL_SESSION = "default_local_session"
# 无代理直连时的默认地理。取 CN 是因为本工程的运行环境在中国大陆：
# **诚实**地报本机地理，比伪装成一个和出口 IP 不符的外国更安全。
LOCAL_GEO = "CN"


def _pick_geo(proxy_url: str) -> str:
    """从代理 URL 推断地理区域。

    [v6 修复·真机实测发现] **原实现用子串匹配**：
        for code in TIMEZONE_MAP:
            if code.lower() in proxy_url.lower():   # ← 子串
                return code
    后果：本机会话的哨兵串 `"default_local_session"` 里同时含有
      **`DE`**(**de**fault) / **`AU`**(def**au**lt) / **`CA`**(lo**ca**l)
    → 循环取第一个 → **恒判为德国（DE）**（注释却写着"默认美国"，与实际不符）。

    于是**每一次不走代理的爬取**都会伪装成：
      `de-DE` 语言 + `Europe/Berlin` 时区 + 德语 Linux UA ——
    而真实出口是**中国大陆 IP**。风控只要交叉比对 IP 地理与浏览器语言/时区，
    **当场识破**（比"不伪装"更糟：真实浏览器不会自相矛盾）。
    实测证据：`compute_fingerprint_from_ip("default_local_session")`
      → geo=DE / lang=de-DE / tz=Europe/Berlin / platform=Linux x86_64。

    修法两条：
      ① **本机会话不再伪装外国** —— 直接返回本机地理（LOCAL_GEO）；
      ② 代理场景改为 **token 边界匹配**，`"default"` 里的 `de` 不会再被当德国。
    """
    lower = (proxy_url or "").strip().lower()
    # ① 本机会话（无代理直连）：用本机真实地理
    if not lower or lower == LOCAL_SESSION or "local" in _tokens_of(lower):
        return LOCAL_GEO
    # ② 代理：按分隔符切词后**整词**匹配（不再子串匹配）
    tokens = _tokens_of(lower)
    for code in TIMEZONE_MAP:
        if code.lower() in tokens:
            return code
    return "US"


def _tokens_of(s: str) -> set:
    """把 URL/串按非字母数字切开，供**整词**匹配（避免 de 命中 default）"""
    return {t for t in re.split(r"[^a-z0-9]+", s) if t}


def _is_local_session(proxy_url: str) -> bool:
    """是否"无代理直连本机"会话（哨兵串 / 空 / 含 local 词）"""
    lower = (proxy_url or "").strip().lower()
    return (not lower) or lower == LOCAL_SESSION or "local" in _tokens_of(lower)


def _local_platform() -> str:
    """本机真实 `navigator.platform` 对应的取值。

    与 geo 同样的道理：**本机会话不该伪装**。伪装成别的操作系统只会
    和真实系统特征（字体、时区、系统字体栅格）产生矛盾。
    """
    import sys
    if sys.platform.startswith("win"):
        return "Win32"
    if sys.platform == "darwin":
        return "MacIntel"
    return "Linux x86_64"


def compute_fingerprint_from_ip(proxy_url: str) -> dict:
    """从代理 IP 生成一致的浏览器全维度指纹

    返回包含以下维度的指纹字典：
    - canvas_noise: Canvas 指纹噪声种子 (1-5)
    - webgl_vendor: WebGL GPU 厂商
    - webgl_renderer: WebGL 渲染器名称
    - webgl_version: WebGL 版本字符串
    - webgl_shading_language_version: GLSL 版本
    - user_agent: 完整 User-Agent
    - chrome_version: Chrome 主版本号
    - screen_width / screen_height: 屏幕分辨率
    - avail_width / avail_height: 可用屏幕区域
    - color_depth: 颜色深度
    - pixel_ratio: 设备像素比
    - timezone: IANA 时区
    - timezone_offset: UTC 偏移（小时）
    - language: 浏览器语言
    - languages: 语言列表
    - platform: navigator.platform
    - hardware_concurrency: CPU 线程数
    - device_memory: 设备内存 (GB)
    - fonts: 字体列表
    - audio_noise: AudioContext 噪声偏移
    - geo_code: 地理区域代码
    - do_not_track: DNT 设置
    - max_touch_points: 最大触控点数
    """
    # None 安全防护
    if proxy_url is None:
        proxy_url = "default_local_session"
    seed = hashlib.sha256(proxy_url.encode()).hexdigest()
    rng = _seed_rng(seed)

    # 地理区域
    geo_code = _pick_geo(proxy_url)
    tz_name, tz_offset, lang = TIMEZONE_MAP.get(geo_code, TIMEZONE_MAP["US"])

    # GPU 配置
    gpu_idx = int(seed[2:4], 16) % len(GPU_PROFILES)
    vendor, renderer = GPU_PROFILES[gpu_idx]

    # Chrome 版本 —— [FIXED & MODIFIED] v2.10.5 UA/TLS 同源绑定：
    # 主版本号必须由 pick_tls_impersonate 选定的 TLS 伪装版本决定
    # （原 chrome_major 独立随机 → UA 说 Chrome/138 实际 TLS=chrome120，JA3/UA 交叉比对
    # 自曝——反爬第一自杀行为；200 次采样 94% 失配）。删除 Edge UA 分支（TLS 池无 Edge 目标）。
    _tls = pick_tls_impersonate(proxy_url)
    _tls_ver = int("".join(ch for ch in _tls if ch.isdigit()))  # chrome120→120
    chrome_major = _tls_ver if _tls_ver >= 100 else rng.choice(CHROME_VERSIONS)
    chrome_full = f"{chrome_major}.0.{rng.randint(6000, 6999)}.{rng.randint(10, 200)}"

    # 屏幕
    sw, sh = rng.choice(SCREEN_RESOLUTIONS)
    # [v2.17 E-P1-6] 任务栏/窗口 clamp（原 avail_h 随机取 40-72 但窗口尺寸不联动——
    # 可能出现 avail>screen 或窗口>屏幕的不可能组合）
    avail_h, win_w, win_h = clamp_screen_and_window(sw, sh)
    color_depth = rng.choice([24, 24, 24, 30, 32])
    pixel_ratio = rng.choice([1, 1, 1, 1.25, 1.5, 2])

    # Navigator 属性
    # [v6 修复] 本机会话**平台要跟真实机器一致**：原来无条件 `rng.choice(PLATFORMS)`，
    # 而 `default_local_session` 这个种子偏偏抽到 `Linux x86_64` —— 于是本机是 Windows、
    # 浏览器却自称 Linux。与 geo 同一条道理：**本机会话应当诚实，不该伪装**。
    platform = _local_platform() if _is_local_session(proxy_url) else rng.choice(PLATFORMS)
    hw_conc = rng.choice(HARDWARE_CONCURRENCY)
    dev_mem = rng.choice(DEVICE_MEMORY)

    # 字体列表（从池中随机选 28-40 个）
    font_count = rng.randint(28, 40)
    fonts = rng.sample(FONT_POOL, min(font_count, len(FONT_POOL)))

    # Canvas 噪声（1-5，不是简单的 XOR）
    canvas_noise = rng.randint(1, 5)

    # Audio 噪声（微小偏移，0.0001-0.001）
    audio_noise = rng.uniform(0.0001, 0.001)

    # DNT
    dnt = rng.choice(["1", "1", "null", "unspecified"])

    # 触控点
    max_touch = rng.choice([0, 0, 0, 1, 5, 10])

    # CSS 媒体查询偏好
    color_scheme = rng.choice(["light", "light", "light", "dark"])
    reduced_motion = rng.choice(["no-preference", "no-preference", "no-preference", "reduce"])
    contrast_pref = rng.choice(["no-preference", "no-preference", "more"])

    # 全局隐私控制
    global_privacy_control = rng.choice([False, False, False, True])

    # 窗口位置（screenX/screenY）
    screen_x = rng.randint(0, max(0, sw - 800))
    screen_y = rng.randint(0, max(0, avail_h - 600))
    avail_left = 0
    avail_top = 0

    # HTTP/2 指纹模板
    http2_fp = rng.choice(HTTP2_FINGERPRINT_TEMPLATES)

    # userAgentData 品牌组合
    ua_brands_pool = rng.choice(UA_DATA_BRAND_POOLS)

    # [FIXED & MODIFIED] v2.10.5 UA/TLS 同源：单一 Chrome UA（主版本 = TLS 版本，与 TLS 池一致
    # 的 chrome120/123/124/131/133a/136）。删除 Edge 双模板分支——TLS 池没有 Edge 目标，
    # Edge UA 配 Chrome TLS 同样自曝。
    ua = (
        f"Mozilla/5.0 ({'Windows NT 10.0; Win64; x64' if 'Win' in platform else 'Macintosh; Intel Mac OS X 10_15_7' if 'Mac' in platform else 'X11; Linux x86_64'}) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{chrome_major}.0.0.0 Safari/537.36"
    )

    return {
        "canvas_noise": canvas_noise,
        "canvas_noise_seed": int(seed[:8], 16),
        "webgl_vendor": vendor,
        "webgl_renderer": renderer,
        "webgl_version": "WebGL 1.0 (OpenGL ES 2.0 Chromium)",
        "webgl_shading_language_version": "WebGL GLSL ES 1.0 (OpenGL ES GLSL ES 1.0 Chromium)",
        "webgl_unmasked_vendor": vendor,
        "webgl_unmasked_renderer": renderer,
        "user_agent": ua,
        "chrome_version": chrome_major,
        "chrome_full_version": chrome_full,
        "screen_width": sw,
        "screen_height": sh,
        "avail_width": win_w,
        "avail_height": avail_h,
        "window_width": win_w,
        "window_height": win_h,
        "color_depth": color_depth,
        "pixel_ratio": pixel_ratio,
        "timezone": tz_name,
        "timezone_offset": tz_offset,
        "language": lang,
        "languages": [lang, lang.split("-")[0]],
        "platform": platform,
        "hardware_concurrency": hw_conc,
        "device_memory": dev_mem,
        "fonts": fonts,
        "audio_noise": audio_noise,
        "geo_code": geo_code,
        "do_not_track": dnt,
        "max_touch_points": max_touch,
        # ── 新增极限绕过维度 ──
        "color_scheme": color_scheme,
        "reduced_motion": reduced_motion,
        "contrast": contrast_pref,
        "global_privacy_control": global_privacy_control,
        "screen_x": screen_x,
        "screen_y": screen_y,
        "avail_left": avail_left,
        "avail_top": avail_top,
        "http2_settings": http2_fp,
        "ua_data_brands": ua_brands_pool,
    }


def generate_default_fingerprint() -> dict:
    """生成一个默认指纹（无代理时使用）"""
    return compute_fingerprint_from_ip("default_local_session")


class FingerprintGenerator:
    """[FIXED & MODIFIED] v2.10.5c 断链修复：crawler.py 一直 try 导入本类 → 类缺失被
    except 吞掉 → self._fp_gen 恒为 None → 指纹地理/时区一致性从未生效。
    本类封装 generate_default_fingerprint + pick_tls_impersonate，供给引擎各处的
    getattr 兼容调用（页面处理器/求解器按需取指纹）。"""

    def __init__(self, proxy_url: str = "default_local_session"):
        self.proxy_url = proxy_url

    def generate(self, proxy_url: str = None) -> dict:
        """生成全维度指纹（带确定性种子——同代理同指纹，跨请求一致）"""
        url = proxy_url or self.proxy_url
        return compute_fingerprint_from_ip(url)

    def tls(self, proxy_url: str = None) -> str:
        """返回该代理应使用的 TLS 伪装版本"""
        return pick_tls_impersonate(proxy_url or self.proxy_url)


# ── TLS 指纹轮换池（[v2.17 E-P1-5] 升级到 curl_cffi 0.16.0 内置最新 chrome 段——chrome136 实测可用。
# 仅收 chrome 系：UA 同源绑定（chrome_major 从 _tls 数字取）对非 chrome 前缀（safari/firefox）
# 会生成 "Chrome/2601" 式失配 UA——safari/firefox 目标待 UA 分支适配后再入池）──
TLS_IMPERSONATE_POOL = [
    "chrome136",
    "chrome131",
    "chrome124",
    "chrome120",
    "chrome116",
]


def pick_tls_impersonate(proxy_url: str) -> str:
    """根据代理 URL 确定性选择 TLS 指纹版本"""
    seed = hashlib.sha256(proxy_url.encode()).hexdigest()
    idx = int(seed[6:8], 16) % len(TLS_IMPERSONATE_POOL)
    return TLS_IMPERSONATE_POOL[idx]


def user_agent_for(impersonate: str) -> str:
    """按指定的 TLS 伪装版本生成**同源**的桌面 Chrome UA。

    [v6 修复] 下载链此前各自硬编码 UA：`Chrome/120.0`，甚至**漏掉版本号的截断串**
    （`…AppleWebKit/537.36` 后直接结束）——而它们的会话 impersonate 是 `chrome136`。
    UA 说 120、JA3/JA4 说 136，正是本文件 195 行那条注释认定的
    "**反爬第一自杀行为**"；截断串更严重：**没有任何真实浏览器会发那种形态**。
    主通道早在 v2.10.5 就做了 UA/TLS 同源绑定，下载链这次补上。

    解析不出主版本时回退 136（与 `TLS_IMPERSONATE_POOL[0]` 对齐），
    绝不生成"无版本号"的 UA。
    """
    digits = "".join(ch for ch in str(impersonate or "") if ch.isdigit())
    major = int(digits) if digits.isdigit() and int(digits) >= 100 else 136
    return ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36")
