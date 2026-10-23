"""按需安装浏览器组件（安装包已不再随包 Chromium）。

为什么要这一步
==============
v2.19.11 起浏览器**不进安装包**。原因不是口味，是硬上限：NSIS 单次输入数据上限
是 **2^31 字节（2,147,483,648）**，2026-10-23 实测 makensis 处理到
**2,147,173,959** 字节即报

    Internal compiler error #12345: error mmapping datablock to 148126.

崩掉；32 位 NSIS 3.12 与 64 位 makensis（PE machine=0x8664）同一个样。
两棵 onedir 有 45.5% 字节重复（去重见 `tools/pack_installer_payload.py`），
去重后并集仍 2.125 GiB，只有把同源的 chromium + headless_shell 移出包，
载荷才降到 **1.453 GiB**（余量 0.547 GiB）。

装到哪儿、怎么被发现
====================
默认装到 `%LOCALAPPDATA%\\ms-playwright` —— 这正是
`run_crawler._detect_browser_path()` 候选链的第 3 档，装完即被认出，不需要改配置。
要装到别处就 `--dest`，再自行设 `PLAYWRIGHT_BROWSERS_PATH`。

用法
====
    python tools/install_browsers.py                  # 装到 %LOCALAPPDATA%\\ms-playwright
    python tools/install_browsers.py --check          # 只探测，不下载
    python tools/install_browsers.py --check --dest X # 探查指定目录
    python tools/install_browsers.py --driver "D:\\...\\_internal\\playwright\\driver"

打包版怎么用
============
安装目录里的 `_internal\\playwright\\driver` 就是 playwright 的 driver（含 node.exe 与
`package\\cli.js`）。把这个目录用 `--driver` 指过去即可；不带 `--driver` 时本工具
会自动依次找 `sys._MEIPASS`、可执行文件旁的 `_internal`、以及 pip 装的 playwright。

退出码：0 成功/已就绪；2 找不到可用的 driver（环境问题）；1 下载失败。
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence

# 默认安装目标（与 run_crawler._detect_browser_path 的候选同源）
def default_dest() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"


def default_packages() -> list[str]:
    """要装的浏览器包名。两种都要：有头渲染与 headless 采集各用一套。"""
    return ["chromium", "chromium-headless-shell"]


def is_driver(path: Path) -> bool:
    """判断一个目录是不是 playwright/patchright 的 driver（node.exe + package/cli.js）。"""
    path = Path(path)
    return (path / "node.exe").is_file() and (path / "package" / "cli.js").is_file()


def driver_candidates(explicit: str | Path | None = None) -> list[Path]:
    """driver 目录的候选链（顺序即优先级）。**不检查存在性**，便于单测。"""
    out: list[Path] = []
    if explicit:
        out.append(Path(explicit))

    env = os.environ.get("KIANA_PLAYWRIGHT_DRIVER", "")
    if env:
        out.append(Path(env))

    # 打包版：PyInstaller 解包目录
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        for name in ("playwright", "patchright"):
            out.append(Path(meipass) / name / "driver")

    # 打包版：可执行文件旁边（onedir）
    exe_dir = Path(sys.executable).parent
    for name in ("playwright", "patchright"):
        out.append(exe_dir / "_internal" / name / "driver")

    # 源码环境：pip 装的那份
    for mod_name in ("playwright", "patchright"):
        try:
            spec = importlib.util.find_spec(mod_name)
        except (ImportError, ValueError):
            spec = None
        if spec and spec.origin:
            out.append(Path(spec.origin).parent / "driver")
    return out


def find_driver(explicit: str | Path | None = None) -> Path | None:
    """返回第一个真实可用的 driver 目录；都没有则 None。"""
    for cand in driver_candidates(explicit):
        if is_driver(cand):
            return cand
    return None


def find_installed(dest: Path) -> list[str]:
    """`dest` 下已有的浏览器目录名（chromium-XXXX 这种）。"""
    dest = Path(dest)
    if not dest.is_dir():
        return []
    return sorted(p.name for p in dest.glob("chromium*") if p.is_dir())


def build_command(driver: Path, packages: Sequence[str]) -> list[str]:
    """拼命令行：`node.exe package/cli.js install <pkgs...>`。"""
    return [
        str(Path(driver) / "node.exe"),
        str(Path(driver) / "package" / "cli.js"),
        "install",
        *packages,
    ]


def install(driver: Path, dest: Path, packages: Sequence[str], *, timeout: int = 1800) -> int:
    """真的跑下载。返回子进程退出码（0 = 成功）。"""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    # 两个引擎都**显式**指向 dest：不能 setdefault —— 父进程里可能已经有一个
    # PLAYWRIGHT_BROWSERS_PATH（`tools/_tools_isolation.py` 就会设），保留旧值会让
    # 下载落到 A、运行时去 B 找，装完等于没装。
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(dest)
    env["PATCHRIGHT_BROWSERS_PATH"] = str(dest)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    cmd = build_command(driver, packages)
    print(f"[install-browsers] 目标目录: {dest}")
    print(f"[install-browsers] 执行: {' '.join(cmd)}")
    try:
        return subprocess.call(cmd, env=env, timeout=timeout)
    except FileNotFoundError:
        print(f"X driver 里的 node.exe 跑不起来: {cmd[0]}", file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired:
        print(f"X 下载超时（{timeout}s）—— 网络问题请配代理后重试", file=sys.stderr)
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="按需安装 Chromium（安装包已不随包浏览器）",
    )
    ap.add_argument("--driver", default=None, help="playwright/patchright 的 driver 目录")
    ap.add_argument("--dest", default=None, help="安装到哪；留空就用 %%LOCALAPPDATA%%/ms-playwright")
    ap.add_argument("--check", action="store_true", help="只探测，不下载")
    ap.add_argument(
        "--packages",
        default=",".join(default_packages()),
        help="要装的包名，逗号分隔（默认 chromium,chromium-headless-shell）",
    )
    args = ap.parse_args(argv)

    dest = Path(args.dest) if args.dest else default_dest()
    have = find_installed(dest)
    driver = find_driver(args.driver)

    print(f"[install-browsers] 目标目录: {dest}")
    print(f"[install-browsers] 已装: {', '.join(have) if have else '（无）'}")
    print(f"[install-browsers] driver: {driver if driver else '（未找到）'}")

    if args.check:
        return 0 if (have and driver) else (2 if not driver else 0)

    if not have and driver is None:
        print(
            "X 找不到 playwright/patchright 的 driver —— 既没法下载，装完也没法用。\n"
            "  打包版请指到安装目录：--driver \"<安装目录>\\_internal\\playwright\\driver\"\n"
            "  源码环境请先 pip install playwright（或 patchright）",
            file=sys.stderr,
        )
        return 2
    if driver is None:
        print("! 浏览器已在，但没找到 driver（采集时仍会需要它）", file=sys.stderr)
        return 0

    packages = [p for p in args.packages.split(",") if p]
    return install(driver, dest, packages)


if __name__ == "__main__":
    sys.exit(main())
