@echo off
REM [v2.19.8 fix] Three hardcoded/dead items removed (would break on another machine
REM or after a Python upgrade):
REM   1) set PYTHON=...\Python314\python.exe  -> now uses `python` from PATH
REM   2) PLAYWRIGHT/PATCHRIGHT_BROWSERS_PATH hardcoded -> set below only if the dir exists
REM   3) set KIANA_CRYPTO_KEY=kv              -> that mechanism was removed entirely
REM NOTE: keep this file ASCII-only. cmd parses .bat bytes with the OEM codepage, so
REM       non-ASCII comments can be split into bogus commands (实测踩过).
REM This is the owner's convenience launcher; the shipped CLI is dist\KianaCrawler\KianaCrawler.exe
setlocal

if exist "%LOCALAPPDATA%\ms-playwright" (
    set "PLAYWRIGHT_BROWSERS_PATH=%LOCALAPPDATA%\ms-playwright"
    set "PATCHRIGHT_BROWSERS_PATH=%LOCALAPPDATA%\ms-playwright"
)

set "SCRIPT_DIR=%~dp0"

:menu
cls
echo ============================================================
echo    Kiana Vnext Plus - CLI Crawler
echo ============================================================
echo.
set /p url="Enter URL: "
if "%url%"=="" goto menu

echo.
echo Crawling...
echo.
python "%SCRIPT_DIR%run_crawler.py" %url%
echo.
echo ============================================================
echo Done! Press any key to crawl another URL...
pause >nul
goto menu
