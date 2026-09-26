# -*- mode: python ; coding: utf-8 -*-
import os as _os
import shutil
from pathlib import Path as _P
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

# [FIXED & MODIFIED] v2.14 阶段4 供应链：动态解析路径（原硬编码本机用户名+
# Python 版本绝对路径——换机/升 Python 构建必炸）。照抄 universal_downloader
# 的动态定位模式。
def _find_ddddocr_dir():
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

# [v2.18.3] 修隐患：原 _find_ffmpeg 与本文件下方用 `os.environ`，但文件只导入了
# `import os as _os`（依赖 PyInstaller 执行环境恰好注入 os，换版本即崩）——统一为 _os。
# 原 `import os as _os`（此处）已上移至文件顶部。
_dd = _find_ddddocr_dir()
_ff = _find_ffmpeg("ffmpeg.exe")
_fp = _find_ffmpeg("ffprobe.exe")

hiddenimports = []
hiddenimports += collect_submodules('kiana_vnext_plus')
hiddenimports += collect_submodules('qfluentwidgets')
wp_datas = [('assets', 'assets'), ('run_crawler.py', '.')]
# [v2.16.1] 站点规则层进包（GUI 进程内引擎同样消费规则层——原缺失导致安装版规则恒空）
wp_datas.append(('rules/sites', 'rules/sites'))
if _dd:
    wp_datas.append((_dd, 'ddddocr'))
wp_binaries = []
if _ff and _fp:
    wp_binaries = [(_ff, 'bin'), (_fp, 'bin')]
wp_datas += collect_data_files('qfluentwidgets')

# [v2.16 阶段1 可移植性] Chromium 随包（GUI 用 patchright 渲染同样需要）
# [v2.19] 版本号改通配扫描：原硬编码 chromium-1228（浏览器升级后 spec 静默不入包，
#        而 一键构建.bat 用通配报"已存在"——两处行为不一致）
_mpw = _P(_os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
_browser_picked = []
if _mpw.is_dir():
    for _pat in ("chromium-*", "chromium_headless_shell-*"):
        _cands = sorted([d for d in _mpw.glob(_pat) if d.is_dir()],
                        key=lambda p: p.stat().st_mtime, reverse=True)
        if _cands:
            _srcdir = _cands[0]          # 取最新修订号
            wp_datas.append((str(_srcdir), f"browsers/{_srcdir.name}"))
            _browser_picked.append(_srcdir.name)
            print(f"[spec] 打入浏览器: {_srcdir.name}")
if not _browser_picked:
    print("[spec] !! 未找到 ms-playwright Chromium——浏览器渲染/验证码能力将不可用")

# [v2.16 阶段1 可移植性] YT PO Token 三件套随包
# [v2.18.3] 定位增强：环境变量优先，其次本机稳定候选（vendor 目录，防 TEMP 被系统清理）
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

_psrc = next((c for c in _pot_candidates() if (c / "build" / "main.js").exists()), None)
if _psrc:
    wp_datas.append((str(_psrc), "pot_server/server"))
    _deno = next((c for c in _deno_candidates() if c.exists()), None)
    if _deno:
        wp_datas.append((str(_deno), "pot_server"))
    print(f"[spec] 打入 PO Token: {_psrc}")
else:
    # [v2.19] 缺件**真报错**（原仅 print 后照常出包，与 docs/BUILD.md 承诺的
    # "任一为空 = 构建失败"不符——会导致静默产出没有 YT 支持的包）。
    # 逃生舱：显式设 KIANA_SKIP_POT=1 可跳过（自用测试构建用）。
    if _os.environ.get("KIANA_SKIP_POT", "") == "1":
        print("[spec] !! 跳过 PO Token（KIANA_SKIP_POT=1）——YT 下载将不可用")
    else:
        raise SystemExit(
            "[spec] 构建中止：未找到 PO Token server（build/main.js）。\n"
            "        请检查 %LOCALAPPDATA%\\KianaVnextPlus\\vendor\\bgutil-pot\\server，\n"
            "        或设 POT_SERVER_DIR 环境变量；确要跳过请设 KIANA_SKIP_POT=1。")

a = Analysis(
    ['launcher_v9.py'],
    pathex=[],
    binaries=wp_binaries,
    datas=wp_datas,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # [FIXED & MODIFIED] v2.11 excludes：本机 site-packages 里的无关大件（torch 4.2GB/
    # transformers/scipy/frida/PyQt5 等）会被 collect_submodules 间接拖入——引擎零引用，
    # 全部排除（v2.10.6 实测 3.1GB→364MB 的同一手段）
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

# [v2.16 M5] Launcher 转 onedir（Chromium 随包 ~771MB 超 NSIS onefile 压缩限制）
# 安装器 File /r dist\KianaLauncher\*（与 KianaCrawler onedir 同思路）
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='KianaLauncher',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
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
    name='KianaLauncher',
)
