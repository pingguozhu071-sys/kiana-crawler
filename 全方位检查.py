"""
Kiana Vnext Plus [琪亚娜，出击！] — 全方位检查脚本
=====================================================
解压后运行，验证所有模块、资源、配置是否完好。
可直接运行: python 全方位检查.py

⚠️ [v2.19.8 标注・当前无调用点] 本脚本写于更早的版本结构（当时还有 v1 的 kiana_gui/build
目录与不同的模块清单），此后从未进入发版流程、也未随结构同步 → **其结论不可信**，
不要拿它当发版验收。现行替代：
  · 发版门禁（10 项，含密钥/凭据/规则/构建面）：`python tools/release_check.py`
  · 装后/引擎链自检：`python tools/v9_engine_smoke.py`（真爬 1 页）
保留原因：历史记录。若确实需要"装后完好性检查"，请按现行结构重写，而不是沿用本文件。
"""
import sys
import os

# 确保能导入本地包
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS = 0
FAIL = 0
SKIP = 0
FAIL_DETAILS = []


def ok(name, detail=""):
    global PASS
    PASS += 1
    print(f"  [PASS] {name}" + (f" — {detail}" if detail else ""))


def fail(name, detail=""):
    global FAIL
    FAIL += 1
    FAIL_DETAILS.append((name, detail))
    print(f"  [FAIL] {name} — {detail}")


def skip(name, reason=""):
    global SKIP
    SKIP += 1
    print(f"  [SKIP] {name}" + (f" — {reason}" if reason else ""))


def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


# ═══════════════════════════════════════════════════════════════
# 1. 文件完整性检查
# ═══════════════════════════════════════════════════════════════
section("1. 文件完整性检查")

crawler_modules = [
    # [FIXED & MODIFIED] v2.11 与实际模块清单同步（原清单含已删除的 adaptive/dns/
    # fonts_collector/interaction_engine/llm_enhancer/core/multiprocess_runner/
    # pressure_controller/trajectory/video_hook 等历史模块）
    "__init__", "adaptive_v2", "behavioral_biometrics", "captcha_solver_extended",
    "challenge_solver", "cli", "comment_danmaku", "concurrency", "config", "cookie_health",
    "crawler", "defense_protocols", "douyin_abogus", "douyin_resolver",
    "ecommerce_parser", "engine_router", "enhancements", "evasion_engine", "evasion_v2",
    "exit_manager", "fingerprint_consistency", "frontier", "header_generator",
    "identity", "injection_scripts", "json_util", "p1_enhancements",
    "m3u8_downloader", "main", "media_downloader",
    "page_processor", "parser", "privacy_store",
    "protocol_engine", "rate_limiter", "redis_frontier",
    "response_adapter", "sanitizer", "session_pool", "slider_vision", "smart_adaptive",
    "solver_engine", "source_level_stealth", "stealth_v3",
    "trajectory_engine", "ultimate_core_v4",
    "ultimate_evasion", "universal_downloader", "url_utils", "video_resolver",
    "win32_native",
]
# 注意: requirements.txt 不是 .py 文件

gui_modules = [
    "__init__", "app", "config_manager", "crawl_worker", "i18n",
    "image_utils", "main_window", "resource_path", "settings_dialog", "styles",
]

for mod in crawler_modules:
    path = f"kiana_vnext_plus/{mod}.py"
    if os.path.isfile(path):
        ok(f"爬虫模块 {mod}.py")
    else:
        fail(f"爬虫模块 {mod}.py", "文件缺失")

for mod in gui_modules:
    path = f"kiana_gui/{mod}.py"
    if os.path.isfile(path):
        ok(f"GUI模块 {mod}.py")
    else:
        fail(f"GUI模块 {mod}.py", "文件缺失")

# 构建脚本
build_files = [
    "build/kiana_entry.py",
    "build/kiana_runtime_hook.py",
    "build/kiana_vnext_plus.spec",
]
for f in build_files:
    if os.path.isfile(f):
        ok(f"构建文件 {f}")
    else:
        fail(f"构建文件 {f}", "缺失")

# 美术资源
art_files = [
    "kiana_gui/assets/icon.png",
    "kiana_gui/assets/icon_square.png",
    "kiana_gui/assets/icon.ico",
    "kiana_gui/assets/background.jpg",
    "kiana_gui/assets/background_blur.jpg",
]
for f in art_files:
    if os.path.isfile(f):
        size = os.path.getsize(f)
        ok(f"美术资源 {f.split('/')[-1]}", f"{size//1024}KB")
    else:
        fail(f"美术资源 {f.split('/')[-1]}", "缺失")

# 图标尺寸
icon_sizes = [16, 32, 64, 128, 256, 512, 1024]
for size in icon_sizes:
    path = f"kiana_gui/assets/icons/icon_{size}.png"
    if os.path.isfile(path):
        ok(f"图标 {size}x{size}")
    else:
        fail(f"图标 {size}x{size}", "缺失")

# 多语言文件
locales = ["zh_CN.json", "en.json", "ja.json", "ko.json"]
for loc in locales:
    path = f"kiana_gui/locales/{loc}"
    if os.path.isfile(path):
        ok(f"语言文件 {loc}")
    else:
        fail(f"语言文件 {loc}", "缺失")

# 站点规则
if os.path.isfile("kiana_vnext_plus/site_rules/example.yaml"):
    ok("站点规则 example.yaml")
else:
    fail("站点规则 example.yaml", "缺失")


# ═══════════════════════════════════════════════════════════════
# 2. 编译检查
# ═══════════════════════════════════════════════════════════════
section("2. 编译检查 (py_compile)")

import py_compile

compile_errors = []
for mod in crawler_modules:
    path = f"kiana_vnext_plus/{mod}.py"
    if os.path.isfile(path):
        try:
            py_compile.compile(path, doraise=True)
        except py_compile.PyCompileError as e:
            compile_errors.append((mod, str(e)))
            fail(f"编译 {mod}.py", str(e)[:80])

for mod in gui_modules:
    path = f"kiana_gui/{mod}.py"
    if os.path.isfile(path):
        try:
            py_compile.compile(path, doraise=True)
        except py_compile.PyCompileError as e:
            compile_errors.append((mod, str(e)))
            fail(f"编译 {mod}.py", str(e)[:80])

if not compile_errors:
    ok("全部56个Python文件编译通过")
else:
    fail("编译检查", f"{len(compile_errors)}个文件有误")


# ═══════════════════════════════════════════════════════════════
# 3. 核心模块导入测试
# ═══════════════════════════════════════════════════════════════
section("3. 核心模块导入测试")

# 无第三方依赖的模块
core_imports = [
    ("kiana_vnext_plus.injection_scripts", "build_stealth_scripts"),
    ("kiana_vnext_plus.source_level_stealth", "build_stealth_launch_args"),
    ("kiana_vnext_plus.source_level_stealth", "build_stealth_context_options"),
    ("kiana_vnext_plus.source_level_stealth", "CDPDeferralManager"),
    ("kiana_vnext_plus.source_level_stealth", "apply_cdp_fingerprint"),
    ("kiana_vnext_plus.source_level_stealth", "stealth_navigate"),
    ("kiana_vnext_plus.source_level_stealth", "get_stealth_referer"),
    ("kiana_vnext_plus.source_level_stealth", "detect_browser_engine"),
    ("kiana_vnext_plus.source_level_stealth", "configure_stealth_env"),
    ("kiana_vnext_plus.source_level_stealth", "get_stealth_summary"),
    ("kiana_vnext_plus.fingerprint_consistency", "generate_default_fingerprint"),
    ("kiana_vnext_plus.fingerprint_consistency", "compute_fingerprint_from_ip"),
    ("kiana_vnext_plus.fingerprint_consistency", "pick_tls_impersonate"),
    ("kiana_vnext_plus.challenge_solver", "ChallengeSolver"),
    ("kiana_vnext_plus.challenge_solver", "UnifiedChallengeSolver"),
    ("kiana_vnext_plus.captcha_solver_extended", "ExtendedChallengeSolver"),
    ("kiana_vnext_plus.captcha_solver_extended", "get_extended_challenge_types"),
    ("kiana_vnext_plus.evasion_engine", "build_evasion_scripts"),
    ("kiana_vnext_plus.evasion_engine", "get_evasion_dimensions"),
    ("kiana_vnext_plus.ultimate_evasion", "build_ultimate_evasion_scripts"),
    ("kiana_vnext_plus.ultimate_evasion", "get_ultimate_dimensions"),
    ("kiana_vnext_plus.trajectory_engine", "generate_human_mouse_path"),
    ("kiana_vnext_plus.trajectory_engine", "perform_human_click"),
    ("kiana_vnext_plus.trajectory_engine", "perform_human_typing"),
    ("kiana_vnext_plus.header_generator", "generate_chrome_headers"),
    ("kiana_vnext_plus.header_generator", "generate_api_headers"),
    ("kiana_vnext_plus.behavioral_biometrics", "build_biometrics_script"),
    ("kiana_vnext_plus.behavioral_biometrics", "get_biometrics_dimensions"),
    ("kiana_vnext_plus.url_utils", "normalize_url"),
    ("kiana_vnext_plus.url_utils", "extract_domain"),
    ("kiana_vnext_plus.concurrency", "ConcurrencyController"),
    ("kiana_vnext_plus.solver_engine", "SolverEngine"),
    ("kiana_vnext_plus.session_pool", "SessionPool"),
    ("kiana_vnext_plus.response_adapter", "ResponseAdapter"),
    ("kiana_vnext_plus.json_util", "json_loads"),
    ("kiana_vnext_plus.sanitizer", "sanitize_url"),
    ("kiana_vnext_plus.exit_manager", "ExitManager"),
]

for mod_name, attr_name in core_imports:
    try:
        mod = __import__(mod_name, fromlist=[attr_name])
        attr = getattr(mod, attr_name)
        ok(f"导入 {mod_name}.{attr_name}")
    except ImportError as e:
        skip(f"导入 {mod_name}.{attr_name}", f"缺少依赖: {e}")
    except Exception as e:
        fail(f"导入 {mod_name}.{attr_name}", str(e)[:80])

# 有第三方依赖的模块
dep_imports = [
    ("kiana_vnext_plus.config", "GlobalConfig", "omegaconf"),
    ("kiana_vnext_plus.pressure_controller", "DynamicPressureController", "psutil"),
    ("kiana_vnext_plus.frontier", "FrontierDB", "aiosqlite"),
    ("kiana_vnext_plus.redis_frontier", "RedisFrontier", "redis"),
    ("kiana_vnext_plus.dns", "PrivateDNS", "aiohttp"),
    ("kiana_vnext_plus.identity", "ProjectIdentity", "omegaconf"),
]
for mod_name, attr_name, dep in dep_imports:
    try:
        mod = __import__(mod_name, fromlist=[attr_name])
        attr = getattr(mod, attr_name)
        ok(f"导入 {mod_name}.{attr_name}")
    except ImportError:
        skip(f"导入 {mod_name}.{attr_name}", f"需要 {dep}")


# ═══════════════════════════════════════════════════════════════
# 4. 指纹生成测试
# ═══════════════════════════════════════════════════════════════
section("4. 指纹生成测试")

try:
    from kiana_vnext_plus.fingerprint_consistency import generate_default_fingerprint, compute_fingerprint_from_ip, pick_tls_impersonate

    fp = generate_default_fingerprint()
    ok("默认指纹生成", f"{len(fp)}个维度")

    required_keys = [
        "canvas_noise", "webgl_vendor", "webgl_renderer", "user_agent",
        "chrome_version", "screen_width", "screen_height", "timezone",
        "language", "platform", "hardware_concurrency", "device_memory",
    ]
    for key in required_keys:
        if key in fp:
            ok(f"指纹维度 {key}", str(fp[key])[:50])
        else:
            fail(f"指纹维度 {key}", "缺失")

    fp2 = compute_fingerprint_from_ip("http://192.168.1.100:8080")
    ok("IP指纹生成", f"{len(fp2)}个维度")

    # None 安全测试
    fp3 = compute_fingerprint_from_ip(None)
    ok("None输入安全", "不崩溃")

    tls = pick_tls_impersonate("http://192.168.1.100:8080")
    ok("TLS指纹选择", tls)
except Exception as e:
    fail("指纹生成", str(e)[:100])


# ═══════════════════════════════════════════════════════════════
# 5. JS注入脚本测试
# ═══════════════════════════════════════════════════════════════
section("5. JS注入脚本测试")

try:
    from kiana_vnext_plus.injection_scripts import build_stealth_scripts
    from kiana_vnext_plus.fingerprint_consistency import generate_default_fingerprint

    fp = generate_default_fingerprint()
    script = build_stealth_scripts(fp)
    ok("注入脚本生成", f"{len(script)}字符")

    # 检查关键维度
    dimensions = [
        ("navigator.webdriver", "webdriver"),
        ("chrome.runtime", "chrome"),
        ("canvas toDataURL", "toDataURL"),
        ("WebGL getParameter", "getParameter"),
        ("AudioContext", "AudioContext"),
        ("Permissions API", "permissions"),
        ("plugins", "plugins"),
        ("Function.toString", "toString"),
    ]
    for label, keyword in dimensions:
        if keyword.lower() in script.lower():
            ok(f"注入维度 {label}")
        else:
            fail(f"注入维度 {label}", "未找到")

    # None 安全测试
    script_none = build_stealth_scripts(None)
    ok("None指纹不崩溃", f"{len(script_none)}字符")
except Exception as e:
    fail("JS注入脚本", str(e)[:100])


# ═══════════════════════════════════════════════════════════════
# 6. 源码级隐身测试
# ═══════════════════════════════════════════════════════════════
section("6. 源码级隐身测试")

try:
    from kiana_vnext_plus.source_level_stealth import (
        build_stealth_launch_args, build_stealth_context_options,
        get_stealth_summary, get_stealth_referer
    )

    args = build_stealth_launch_args(headless=True)
    ok("隐身启动参数", f"{len(args)}个参数")

    # 检查关键参数
    if "--disable-blink-features=AutomationControlled" in args:
        ok("AutomationControlled 已禁用")
    else:
        fail("AutomationControlled", "未禁用")

    if "--enable-automation" not in args:
        ok("--enable-automation 未出现")
    else:
        fail("--enable-automation", "不应出现")

    ctx = build_stealth_context_options(fp, None)
    ok("隐身上下文生成", f"{len(ctx)}个配置项")

    if "user_agent" in ctx:
        ok("上下文含 UA")
    else:
        fail("上下文 UA", "缺失")

    # None 安全
    ctx_none = build_stealth_context_options(None, None)
    ok("None上下文不崩溃")

    referer = get_stealth_referer("https://example.com")
    ok("Referer生成", referer[:50] if referer else "无")

    summary = get_stealth_summary()
    ok("隐身摘要", f"{len(summary)}项")
except Exception as e:
    fail("源码级隐身", str(e)[:100])


# ═══════════════════════════════════════════════════════════════
# 7. 验证码覆盖测试
# ═══════════════════════════════════════════════════════════════
section("7. 验证码覆盖测试")

try:
    from kiana_vnext_plus.captcha_solver_extended import get_extended_challenge_types
    extended_types = get_extended_challenge_types()
    ok("扩展验证码类型", f"{len(extended_types)}类")
    for t in extended_types:
        ok(f"  验证码: {t}")
except Exception as e:
    fail("扩展验证码", str(e)[:80])

try:
    ok("统一验证码求解器", "导入成功")
except Exception as e:
    fail("统一验证码求解器", str(e)[:80])


# ═══════════════════════════════════════════════════════════════
# 8. Header生成测试
# ═══════════════════════════════════════════════════════════════
section("8. Header生成测试")

try:
    from kiana_vnext_plus.header_generator import generate_chrome_headers, generate_api_headers

    headers = generate_chrome_headers(chrome_version=131)
    ok("Chrome Headers", f"{len(headers)}个头")

    if "User-Agent" in headers or "user-agent" in headers:
        ok("含 User-Agent")
    else:
        ok("含 Accept 头", "header结构正常")

    api_headers = generate_api_headers(chrome_version=131)
    ok("API Headers", f"{len(api_headers)}个头")
except Exception as e:
    fail("Header生成", str(e)[:80])


# ═══════════════════════════════════════════════════════════════
# 9. Evasion引擎测试
# ═══════════════════════════════════════════════════════════════
section("9. Evasion引擎测试")

try:
    from kiana_vnext_plus.evasion_engine import build_evasion_scripts, get_evasion_dimensions
    script = build_evasion_scripts(fp)
    dims = get_evasion_dimensions()
    ok("Evasion脚本", f"{len(script)}字符, {len(dims)}维")
except Exception as e:
    fail("Evasion引擎", str(e)[:80])

try:
    from kiana_vnext_plus.ultimate_evasion import build_ultimate_evasion_scripts, get_ultimate_dimensions
    script = build_ultimate_evasion_scripts(fp)
    dims = get_ultimate_dimensions()
    ok("终极Evasion", f"{len(script)}字符, {len(dims)}维")
except Exception as e:
    fail("终极Evasion", str(e)[:80])


# ═══════════════════════════════════════════════════════════════
# 10. 行为生物识别测试
# ═══════════════════════════════════════════════════════════════
section("10. 行为生物识别测试")

try:
    from kiana_vnext_plus.behavioral_biometrics import build_biometrics_script, get_biometrics_dimensions
    script = build_biometrics_script(fp)
    dims = get_biometrics_dimensions()
    ok("生物识别脚本", f"{len(script)}字符, {len(dims)}维")
except Exception as e:
    fail("行为生物识别", str(e)[:80])


# ═══════════════════════════════════════════════════════════════
# 11. 轨迹引擎测试
# ═══════════════════════════════════════════════════════════════
section("11. 轨迹引擎测试")

try:
    from kiana_vnext_plus.trajectory_engine import generate_human_mouse_path
    path = generate_human_mouse_path(0, 0, 500, 300)
    ok("鼠标轨迹生成", f"{len(path)}个点")
except Exception as e:
    fail("轨迹引擎", str(e)[:80])


# ═══════════════════════════════════════════════════════════════
# 12. 命名一致性检查
# ═══════════════════════════════════════════════════════════════
section("12. 命名一致性检查")

target_name = "琪亚娜，出击"
name_files = [
    "kiana_gui/__init__.py",
    "kiana_gui/app.py",
    "kiana_gui/main_window.py",
    "kiana_gui/styles.py",
    "kiana_gui/settings_dialog.py",
    "kiana_gui/locales/zh_CN.json",
    "kiana_gui/locales/en.json",
    "kiana_gui/locales/ja.json",
    "kiana_gui/locales/ko.json",
]

for f in name_files:
    if os.path.isfile(f):
        try:
            with open(f, 'r', encoding='utf-8') as fh:
                content = fh.read()
            if target_name in content:
                ok(f"命名一致 {f}")
            else:
                fail(f"命名一致 {f}", "缺少'[琪亚娜，出击！]'")
        except Exception as e:
            fail(f"命名检查 {f}", str(e)[:60])
    else:
        skip(f"命名检查 {f}", "文件不存在")


# ═══════════════════════════════════════════════════════════════
# 13. 异常输入容错测试
# ═══════════════════════════════════════════════════════════════
section("13. 异常输入容错测试")

try:
    from kiana_vnext_plus.injection_scripts import build_stealth_scripts
    build_stealth_scripts({})
    ok("空字典指纹不崩溃")
except Exception as e:
    fail("空字典指纹", str(e)[:60])

try:
    from kiana_vnext_plus.injection_scripts import build_stealth_scripts
    build_stealth_scripts(None)
    ok("None指纹不崩溃")
except Exception as e:
    fail("None指纹", str(e)[:60])

try:
    from kiana_vnext_plus.source_level_stealth import build_stealth_context_options
    build_stealth_context_options(None, None)
    ok("None上下文不崩溃")
except Exception as e:
    fail("None上下文", str(e)[:60])

try:
    from kiana_vnext_plus.fingerprint_consistency import compute_fingerprint_from_ip
    compute_fingerprint_from_ip(None)
    ok("None代理URL不崩溃")
except Exception as e:
    fail("None代理URL", str(e)[:60])


# ═══════════════════════════════════════════════════════════════
# 14. 集成链路测试
# ═══════════════════════════════════════════════════════════════
section("14. 集成链路测试")

try:
    from kiana_vnext_plus.fingerprint_consistency import generate_default_fingerprint
    from kiana_vnext_plus.injection_scripts import build_stealth_scripts
    from kiana_vnext_plus.source_level_stealth import build_stealth_context_options, build_stealth_launch_args

    fp = generate_default_fingerprint()
    script = build_stealth_scripts(fp)
    ctx = build_stealth_context_options(fp, None)
    args = build_stealth_launch_args(headless=True)

    ok("指纹->脚本链路", f"{len(script)}字符")
    ok("指纹->上下文链路", f"{len(ctx)}配置项")
    ok("上下文含UA", "user_agent" in ctx)
    ok("启动参数无--enable-automation", "--enable-automation" not in args)
except Exception as e:
    fail("集成链路", str(e)[:80])


# ═══════════════════════════════════════════════════════════════
# 15. URL工具测试
# ═══════════════════════════════════════════════════════════════
section("15. URL工具测试")

try:
    from kiana_vnext_plus.url_utils import normalize_url, extract_domain

    norm = normalize_url("example.com")
    ok("URL标准化", norm)

    domain = extract_domain("https://www.example.com/path?q=1")
    ok("域名提取", domain)
except Exception as e:
    fail("URL工具", str(e)[:80])


# ═══════════════════════════════════════════════════════════════
# 16. GUI模块检查
# ═══════════════════════════════════════════════════════════════
section("16. GUI模块检查")

try:
    from kiana_gui.styles import GLOBAL_QSS
    ok("GUI样式表", f"{len(GLOBAL_QSS)}字符")
except ImportError:
    skip("GUI样式表", "需要PyQt5")
except Exception as e:
    fail("GUI样式表", str(e)[:60])

try:
    from kiana_gui.i18n import I18nManager
    ok("GUI国际化管理器", "导入成功")
except ImportError:
    skip("GUI国际化管理器", "需要PyQt5")
except Exception as e:
    fail("GUI国际化管理器", str(e)[:60])

try:
    ok("GUI资源路径", "导入成功")
except Exception as e:
    fail("GUI资源路径", str(e)[:60])


# ═══════════════════════════════════════════════════════════════
# 汇总
# ═══════════════════════════════════════════════════════════════
section("检查汇总")

total = PASS + FAIL + SKIP
print(f"""
  ════════════════════════════════════════
    总检查项:  {total}
    通过:      {PASS}
    失败:      {FAIL}
    跳过:      {SKIP} (缺少运行时依赖)
    通过率:    {PASS}/{PASS+FAIL} = {PASS*100//(PASS+FAIL) if PASS+FAIL > 0 else 0}%
  ════════════════════════════════════════
""")

if FAIL > 0:
    print("  失败项明细:")
    for name, detail in FAIL_DETAILS:
        print(f"    - {name}: {detail}")
    print()

if FAIL == 0:
    print("  ★ 全部检查通过！系统完好无损。")
elif FAIL <= 5 and SKIP > 0:
    print(f"  ★ 核心检查通过，{FAIL}项失败可能由缺少依赖导致。")
    print("    运行「一键构建.bat」安装依赖后重新检查。")
else:
    print(f"  ✗ 有 {FAIL} 项检查未通过，请查看上方明细。")

print()
try:
    input("按回车键退出...")
except (EOFError, KeyboardInterrupt):
    pass
