# -*- mode: python ; coding: utf-8 -*-
import os as _os
import shutil
from pathlib import Path as _P
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_submodules

# [FIXED & MODIFIED] v2.14 阶段4 供应链：动态解析路径（原硬编码 miku0 用户名+
# Python314 绝对路径——换机构建必炸；与 KianaLauncher.spec 同款修复）
def _find_ddddocr_dir():
    # [v6] **优先工程内的 vendor 副本**（机主要求"打包也打进去"），
    # 没有才退回 site-packages。否则 spec 会把 site-packages 那份打进包，
    # 而运行时又从 vendor 导入 → 两份可能版本不一致。
    _v = _P(SPECPATH) / "vendor" / "ddddocr"
    if _v.is_dir():
        return str(_v)
    try:
        import ddddocr
        return str(_P(ddddocr.__file__).parent)
    except Exception:
        return ""

def _find_ffmpeg(name):
    p = shutil.which(name)
    if p:
        return p
    local = _P(_os.environ.get("LOCALAPPDATA", "")) / "ffmpeg" / "ffmpeg-master-latest-win64-gpl" / "bin" / name
    return str(local) if local.exists() else ""

_dd = _find_ddddocr_dir()
_ff = _find_ffmpeg("ffmpeg.exe")
_fp = _find_ffmpeg("ffprobe.exe")

datas = [('assets', 'assets')]
# [v2.16.1] 站点规则层进包（v2.15 后规则层从未打入——安装版 RuleLoader 恒空，功能静默失效）
datas.append(('rules/sites', 'rules/sites'))
if _dd:
    datas.append((_dd, 'ddddocr'))
binaries = []
if _ff and _fp:
    binaries = [(_ff, 'bin'), (_fp, 'bin')]
hiddenimports = []
datas += collect_data_files('trafilatura')
hiddenimports += collect_submodules('kiana_vnext_plus')

# ── [v6] 内置第三方部件（`vendor/`）：camoufox 隐身内核 + ddddocr 验证码 ──────────
# 机主要求"打包也打进去"——装完即用，不依赖用户自己 pip。
# ⚠️ 源码**不入 git**（见 `vendor/README.md`）；但**要打进 exe**——两者不冲突。
# `SPECPATH` 是 PyInstaller 注入的全局（spec 所在目录）。
_SPEC_DIR = _P(SPECPATH)
_vendor_dir = _SPEC_DIR / "vendor"
_pathex = []
_vendor_pkgs = []
if _vendor_dir.is_dir():
    for _pkg in ("camoufox", "ddddocr"):
        if (_vendor_dir / _pkg).is_dir():
            _vendor_pkgs.append(_pkg)
            try:
                hiddenimports += collect_submodules(_pkg)
            except Exception as _e:
                print(f"[spec] collect_submodules({_pkg}) 失败: {_e}")
    # vendor 优先：让 PyInstaller 从工程里那份解析，而不是 site-packages
    if _vendor_pkgs:
        _pathex.append(str(_vendor_dir))
    print(f"[spec] vendor 部件入包: {_vendor_pkgs or '（无）'}")
else:
    print("[spec] ⚠️ 未找到 vendor/ —— camoufox/ddddocr 不会被打进包")

# [v2.16 阶段1 可移植性] Chromium 随包：把本机 ms-playwright 的 chromium 与 headless_shell
# 打进 <datadir>/browsers/ —— 换机后无需手动 `patchright install chromium`（约690MB）。
# 仅在构建机存在对应目录时打入；_detect_browser_path 优先读 _MEIPASS/browsers。
_mpw = _P(_os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
# [v2.19] 版本号通配（原硬编码 chromium-1228——浏览器升级后静默不入包）
_browser_picked = []
if _mpw.is_dir():
    for _pat in ("chromium-*", "chromium_headless_shell-*"):
        _cands = sorted([d for d in _mpw.glob(_pat) if d.is_dir()],
                        key=lambda p: p.stat().st_mtime, reverse=True)
        if _cands:
            _srcdir = _cands[0]
            datas.append((str(_srcdir), f"browsers/{_srcdir.name}"))
            _browser_picked.append(_srcdir.name)
            print(f"[spec] 打入浏览器: {_srcdir.name}")
if not _browser_picked:
    print("[spec] !! 未找到 ms-playwright Chromium——浏览器渲染能力将不可用")

# [v2.16 阶段1 可移植性] YT PO Token 三件套随包
# [v2.18.3] 定位增强：环境变量优先，其次本机稳定候选（vendor 目录，防 TEMP 被系统清理）
#   POT_SERVER_DIR = bgutil server 目录（含 build/main.js）；DENO_EXE = deno.exe 路径
# 运行时 universal_downloader.pot_server_ensure 按环境变量定位，打包后读 _MEIPASS/pot_server。
def _pot_candidates():
    _c = []
    _e = _os.environ.get("POT_SERVER_DIR", "")
    if _e:
        _c.append(_P(_e))
    _la = _os.environ.get("LOCALAPPDATA", "")
    if _la:
        _c.append(_P(_la) / "KianaVnextPlus" / "vendor" / "bgutil-pot" / "server")
    _c.append(_P(_os.path.expanduser("~")) / "AppData" / "Local"
              / "KianaVnextPlus" / "vendor" / "bgutil-pot" / "server")
    return _c

def _deno_candidates():
    _c = []
    _e = _os.environ.get("DENO_EXE", "")
    if _e:
        _c.append(_P(_e))
    _la = _os.environ.get("LOCALAPPDATA", "")
    if _la:
        _c.append(_P(_la) / "KianaVnextPlus" / "vendor" / "deno.exe")
    return _c

_pot = next((c for c in _pot_candidates() if (c / "build" / "main.js").exists()), None)
if _pot:
    datas.append((str(_pot), "pot_server/server"))
    _deno = next((c for c in _deno_candidates() if c.exists()), None)
    if _deno:
        datas.append((str(_deno), "pot_server"))
    print(f"[spec] 打入 PO Token: {_pot}")
else:
    # [v2.19] 缺件真报错（原仅 print——静默产出无 YT 支持的包）；KIANA_SKIP_POT=1 可跳过
    if _os.environ.get("KIANA_SKIP_POT", "") == "1":
        print("[spec] !! 跳过 PO Token（KIANA_SKIP_POT=1）——YT 下载将不可用")
    else:
        raise SystemExit(
            "[spec] 构建中止：未找到 PO Token server（build/main.js）。\n"
            "        请检查 %LOCALAPPDATA%\\KianaVnextPlus\\vendor\\bgutil-pot\\server，\n"
            "        或设 POT_SERVER_DIR 环境变量；确要跳过请设 KIANA_SKIP_POT=1。")


a = Analysis(
    ['run_crawler.py'],
    pathex=_pathex,   # [v6] 含 vendor/（内置第三方部件）
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'torch', 'torchvision', 'torchaudio', 'transformers', 'scipy', 'sympy',
        'frida', 'PyQt5', 'pandas', 'matplotlib', 'IPython', 'jupyter',
        'numpy.f2py', 'tkinter', 'test', 'unittest', 'pydoc_data',
        'onnxruntime', 'ctranslate2', '_maxminddb_geolite2', 'av',
        'multiprocessing', 'concurrent.futures.process',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='KianaCrawler',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets\\icon.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='KianaCrawler',
)
