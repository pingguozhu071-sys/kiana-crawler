@echo off
chcp 65001 >nul 2>&1
title Kiana Vnext Plus - 一键构建工具 v2.10.5
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ==================================================
echo   Kiana Vnext Plus
echo   Windows 一键构建工具 (v2.10.5)
echo   PyInstaller 双 exe + NSIS 安装器
echo ==================================================
echo.

REM === [1/7] 检查 Python ===
echo [1/7] 检查 Python 环境...
python --version >nul 2>&1
if !ERRORLEVEL! neq 0 (
    echo   X 未找到 Python! 请先安装 Python 3.11+
    echo   下载地址: https://www.python.org/downloads/
    pause
    exit /b 1
)
echo   OK Python 可用

REM === [2/7] 检查关键依赖 ===
echo [2/7] 检查关键依赖...
python -c "import PySide6, yt_dlp, curl_cffi, omegaconf, aiofiles, ddddocr, trafilatura, openpyxl, qfluentwidgets" >nul 2>&1
if !ERRORLEVEL! neq 0 (
    echo   ! 依赖缺失，按 requirements.txt 锁定版安装...
    REM [FIXED & MODIFIED] v2.14 供应链统一：单一清单（去 --upgrade 防升级炸包）
    pip install -r kiana_vnext_plus\requirements.txt
    REM [v2.19.3] 开发/测试依赖独立清单（pytest/ruff/mypy/pyinstaller/pip-audit）
    pip install -r requirements-dev.txt
    if !ERRORLEVEL! neq 0 (
        echo   X 依赖安装失败! 请手动检查网络/pip
        pause
        exit /b 1
    )
    echo   OK 依赖安装完成
) else (
    echo   OK 依赖齐全
    REM [v2.19.3] 运行时依赖齐全时也确保测试工具在位（第 4b 步要跑 pytest）
    python -c "import pytest, ruff" >nul 2>&1
    if !ERRORLEVEL! neq 0 (
        echo   ! 测试工具缺失，按 requirements-dev.txt 安装...
        pip install -r requirements-dev.txt
    )
)

REM === [3/7] 检查 Chromium 浏览器 ###
echo [3/7] 检查 Chromium 浏览器...
set "PW_CACHE=%LOCALAPPDATA%\ms-playwright"
REM [FIXED & MODIFIED] v2.14 通配修订号（原硬编码 chromium-1228，浏览器升级后每次构建误报缺失）
set "CHROMIUM_FOUND="
for /d %%D in ("!PW_CACHE!\chromium-*") do set "CHROMIUM_FOUND=%%D"
if defined CHROMIUM_FOUND (
    echo   OK Chromium 已存在
) else (
    echo   ! 未找到 ms-playwright Chromium (不影响纯 CLI 下载, 影响浏览器渲染/验证码)
    echo   ! 可选: python -m patchright install chromium
)

REM === [4/7] 语法检查 ===
echo [4/7] 语法检查...
REM [FIXED & MODIFIED] v2.18 语法检查入口对齐 spec（GUI 真入口 launcher_v9.py——原 glob v8 旧壳，检查了个寂寞）
python -c "import py_compile, glob, sys; files=glob.glob('launcher_v9.py')+glob.glob('run_crawler.py')+glob.glob('kiana_vnext_plus/*.py'); [py_compile.compile(f, doraise=True) for f in files]; print('OK 语法检查通过')" >nul 2>&1
if !ERRORLEVEL! neq 0 (
    echo   X 语法检查失败!
    pause
    exit /b 1
)
echo   OK 语法检查通过

REM === [4c/7] 静态检查（ruff/mypy 警告不阻断，进构建）===
echo [4c/7] 静态检查 (ruff/mypy)...
python -m ruff check . >nul 2>&1
if !ERRORLEVEL! neq 0 (
    echo   ! ruff 报告问题（不阻断，手动 python -m ruff check . 查看）
)
python -m mypy kiana_vnext_plus --ignore-missing-imports >nul 2>&1
if !ERRORLEVEL! neq 0 (
    echo   ! mypy 报告问题（不阻断，手动 python -m mypy kiana_vnext_plus 查看）
)
echo   OK 静态检查完成

REM === [4b/7] 行为测试（漏回归防线）===
echo [4b/7] 行为测试 (pytest)...
python -m pytest tests -q >nul 2>&1
if !ERRORLEVEL! neq 0 (
    echo   X 测试失败! 中止构建（回归未过不得打包）
    pause
    exit /b 1
)
echo   OK 行为测试通过 (100+)

REM === [4d/7] PO Token 组件（打包必备，漏设环境变量则 YT 下载静默失效）===
echo [4d/7] PO Token 组件...
REM [v2.18.3] 自动定位 vendor 目录（原需手动 export，漏设即静默丢组件）
set "POT_SERVER_DIR=%LOCALAPPDATA%\KianaVnextPlus\vendor\bgutil-pot\server"
set "DENO_EXE=%LOCALAPPDATA%\KianaVnextPlus\vendor\deno.exe"
if exist "%POT_SERVER_DIR%\build\main.js" (
    echo   OK PO Token server 已定位
) else (
    echo   ! 未找到 PO Token server: %POT_SERVER_DIR%
    echo   ! YT 下载将不可用（其余功能不受影响，可继续构建）
)
if exist "%DENO_EXE%" (
    echo   OK deno.exe 已定位
) else (
    echo   ! 未找到 deno.exe: %DENO_EXE%
)

REM === [5/7] PyInstaller 打包 (双 exe) ###
echo [5/7] PyInstaller 打包 (需要5-15分钟)...
echo.
echo   --- KianaLauncher (GUI, onedir) ---
python -m PyInstaller --noconfirm --clean KianaLauncher.spec
if !ERRORLEVEL! neq 0 (
    echo   X Launcher 打包失败!
    pause
    exit /b 1
)
echo   OK KianaLauncher.exe 打包完成

echo.
echo   --- KianaCrawler (onedir, CLI) ---
python -m PyInstaller --noconfirm --clean KianaCrawler.spec
if !ERRORLEVEL! neq 0 (
    echo   X Crawler 打包失败!
    pause
    exit /b 1
)
echo   OK KianaCrawler.exe 打包完成

REM === [6/7] NSIS 安装器 ===
echo [6/7] NSIS 安装器...
REM [FIXED & MODIFIED] v2.11 PATH 兜底：新终端 PATH 常缺 NSIS → 探测标准安装路径
set "MAKENSIS=makensis"
where makensis >nul 2>&1
if !ERRORLEVEL! neq 0 (
    if exist "%ProgramFiles(x86)%\NSIS\makensis.exe" (
        set "MAKENSIS=%ProgramFiles(x86)%\NSIS\makensis.exe"
    ) else if exist "%ProgramFiles%\NSIS\makensis.exe" (
        set "MAKENSIS=%ProgramFiles%\NSIS\makensis.exe"
    ) else (
        echo   ! 找不到 makensis, 跳过 NSIS (dist 已生成, 可直接使用)
        goto :CHECK
    )
)
echo   --- 生成安装包 ---
"!MAKENSIS!" kiana_setup.nsi
if !ERRORLEVEL! neq 0 (
    echo   X makensis 失败 (脚本必须 UTF-8 BOM)
    pause
    exit /b 1
)
echo   OK NSIS 安装包生成完成

:CHECK
REM === [7/7] 检查输出 ===
echo [7/7] 检查输出...
echo.
echo   dist\KianaLauncher.exe   (GUI 单文件)
echo   dist\KianaCrawler\       (CLI onedir)
echo   KianaVnextPlus-Setup-*.exe (安装包, 若 makensis 可用)
echo.
echo ==================================================
echo   构建完成! 要发布: 运行 kiana_setup.nsi 生成安装包
echo ==================================================
pause
