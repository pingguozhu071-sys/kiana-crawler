# -*- coding: utf-8 -*-
"""`tools/` 脚本的**默认安全**数据隔离（v2.19.9）。

## 为什么需要它（真事故，不是防御性编程）

`tests/conftest.py` 那套 autouse 护栏**只在 pytest 里生效**。`tools/` 下的脚本是
`python tools/xxx.py` 直接执行的，护栏根本不在场，于是它们会真的动机主的数据：

  · **`KianaV9()` 构造期**就有写路径 —— `launcher_v9.py` 的启动迁移钩子见到
    `launcher_config.json` 里还有明文密钥，会写 `secrets/*.bin` 并**改写真配置**；
  · `_start()` / `_wp_update()` → `save_config()` → 真 `launcher_config.json`；
  · `EngineBridge.start()` → 进程内 `run_crawler.crawl()` → 真 `http_cache/`
    （`crawler.py` 的 `HttpCache` **没有 cache_dir 注入点**，只能靠重定向数据根拦住）。

## 为什么是 `LOCALAPPDATA`

它是**唯一**能同时钉住两样东西的钩子：
  · `launcher_v8.CONFIG_FILE` —— 模块顶部**导入期**求值（`_launcher_config_path()`）；
  · `kiana_vnext_plus.config.data_root()` —— **调用期**求值。

⚠️ 因此 `activate()` **必须写在脚本最顶上、任何 launcher 导入之前**：
导入之后再改环境变量，`CONFIG_FILE` 早就固化到机主真路径上了。
（`KIANA_PORTABLE=1` 不够用：它只影响 `data_root()`，管不到 `CONFIG_FILE`；
而且非冻结时它把数据根指到**仓库里**的 `KianaData/`，那是污染仓库，不是隔离。
`KIANA_DATA_ROOT` 这个变量在全仓**根本不存在**，别以为有。）

## 用法

    from _tools_isolation import activate, add_real_flag
    ISOLATED_ROOT = activate()          # ← 必须在 import launcher_* 之前

要**真的**对机主数据根跑（真机冒烟），显式加 `--real`，或在环境里设
`KIANA_TOOLS_REAL=1`。那时本模块**原样不动**任何环境变量，只打印一条风险提示。

## 副作用与取舍（写清楚，别让下一个人以为是白捡的）

重定向 `LOCALAPPDATA` 会连带影响 `run_crawler._detect_browser_path()` —— 它是从
`%LOCALAPPDATA%\\ms-playwright` 找 Chromium 的。所以本模块**先**把探到的真实浏览器
路径写进 `PLAYWRIGHT_BROWSERS_PATH` / `PATCHRIGHT_BROWSERS_PATH`（那两个变量在
`run_crawler.py:92-93` 是 `setdefault`，**先设的赢**），**再**重定向数据根。
这样"隔离数据"与"还能起浏览器"可以兼得。
"""
import atexit
import os
import pathlib
import shutil
import sys
import tempfile

__all__ = ["activate", "add_real_flag", "want_real", "isolated_root", "REAL_FLAG", "REAL_ENV"]

REAL_FLAG = "--real"
REAL_ENV = "KIANA_TOOLS_REAL"
KEEP_ENV = "KIANA_TOOLS_KEEP_DATA"

# 与 `run_crawler._detect_browser_path()` 的候选**同源**（少了 _MEIPASS 那条：
# 那条只在打包内成立，而 tools/ 脚本永远是源码模式跑的）。
_BROWSER_VARS = ("PLAYWRIGHT_BROWSERS_PATH", "PATCHRIGHT_BROWSERS_PATH")
_BROWSER_SUBDIRS = (
    ("ms-playwright",),
    ("Hermes Agent CN Desktop", "data", "hermes-home", "cache", "ms-playwright"),
)

_activated_root = None


def want_real(argv=None) -> bool:
    """要不要对**机主真实数据根**跑。默认 False（= 隔离）。"""
    args = list(sys.argv[1:] if argv is None else argv)
    if REAL_FLAG in args:
        return True
    return os.environ.get(REAL_ENV, "") == "1"


def isolated_root():
    """已激活的隔离根；没激活（或跑的是 --real）时返回 None。"""
    return _activated_root


def add_real_flag(ap) -> None:
    """给脚本自己的 argparse 加上 `--real`（只为让它在 `--help` 里**可见**）。

    `activate()` 是直接扫 `sys.argv` 的（它跑在 argparse 之前），所以**加不加这个
    参数，`--real` 都能用**；这里加是为了"帮助里必须写明风险"这条要求 ——
    一个藏起来的逃生舱等于没有逃生舱。
    """
    ap.add_argument(
        REAL_FLAG, action="store_true",
        # ⚠️ argparse 的 help 文本会做 `%` 插值，裸写 %LOCALAPPDATA% 会抛
        # "badly formed help string"。所以这里必须写成 %%LOCALAPPDATA%%。
        help="⚠️ 危险：对**机主真实数据根**运行（真读真写 "
             "%%LOCALAPPDATA%%\\KianaVnextPlus：launcher_config.json、secrets/、"
             "http_cache/、profiles/、cookies.txt）。默认是**隔离模式**"
             "（临时数据根，跑完即删，不碰机主任何数据）。"
             "只有明确要对真机做冒烟/回归时才加这个开关。")


def _find_real_browser_dir(localappdata: str):
    """在**真实** LOCALAPPDATA 下找 Chromium 目录。找不到返回 None。"""
    if not localappdata:
        return None
    for parts in _BROWSER_SUBDIRS:
        cand = pathlib.Path(localappdata).joinpath(*parts)
        try:
            if cand.is_dir() and any(cand.glob("chromium*")):
                return cand
        except OSError:
            continue
    return None


def activate(script: str, *, argv=None) -> "pathlib.Path | None":
    """把数据根重定向到临时目录（默认）；`--real` 时什么都不做。

    返回隔离根（`--real` 时返回 None）。**必须**在 import 任何 launcher 模块之前调用。
    重复调用是幂等的（第二次直接返回同一个根，不会又造一个新的临时目录）。
    """
    global _activated_root

    if want_real(argv):
        print(f"[{script}] ⚠️ --real：本次将**直接使用机主真实数据根** "
              f"{os.environ.get('LOCALAPPDATA', '')}\\KianaVnextPlus"
              f"（会读写 launcher_config.json / secrets/ / http_cache/ / profiles/）。"
              f"去掉 --real 即为隔离模式。", file=sys.stderr)
        return None

    if _activated_root is not None:              # 幂等
        return _activated_root

    real_local = os.environ.get("LOCALAPPDATA", "")
    # ① 先把真实浏览器路径固化好 —— 必须在改 LOCALAPPDATA **之前**（见模块 docstring）
    preserved = []
    if real_local:
        for var in _BROWSER_VARS:
            if os.environ.get(var):
                continue                          # 用户自己指定过了，不覆盖
            found = _find_real_browser_dir(real_local)
            if found is not None:
                os.environ[var] = str(found)
                preserved.append(f"{var}={found}")

    # ② 再重定向数据根。LOCALAPPDATA 一改，CONFIG_FILE（导入期）与 data_root()
    #    （调用期）**同时**落进临时目录。
    root = pathlib.Path(tempfile.mkdtemp(prefix="kiana_tools_isolated_"))
    local = root / "LocalAppData"
    (local / "KianaVnextPlus").mkdir(parents=True, exist_ok=True)
    os.environ["LOCALAPPDATA"] = str(local)
    # 明确**不要**继承机主的 KIANA_PORTABLE：非冻结时它把数据根指到仓库里的
    # KianaData/，那是污染仓库而不是隔离。这里必须清掉。
    os.environ.pop("KIANA_PORTABLE", None)
    # cookies 来源同理：不继承，免得脚本悄悄拿机主真登录态去真爬
    os.environ.pop("KIANA_COOKIE_FILES", None)
    os.environ.pop("KIANA_COOKIE_FILE", None)

    _activated_root = root

    if os.environ.get(KEEP_ENV, "") == "1":
        keep_note = f"（{KEEP_ENV}=1 → 保留，便于事后翻看）"
    else:
        atexit.register(shutil.rmtree, root, True)
        keep_note = f"（跑完自动删；要留着看就设 {KEEP_ENV}=1）"

    print(f"[{script}] 🔒 隔离模式：数据根已重定向到 {root}\\LocalAppData\\KianaVnextPlus "
          f"{keep_note}\n"
          f"          机主的真实数据根**一个字节都不会动**。"
          f"要真的对真机跑，加 --real（见 --help，有风险说明）。"
          + ("\n          已保留浏览器路径：" + "; ".join(preserved) if preserved else ""),
          file=sys.stderr)
    return root
