"""Kiana 测试共享 fixture（v2.15 阶段1 工程卫生）

此前 17 个测试文件各自为政（env/目录/清理逻辑重复）——统一到 conftest。
"""
import hashlib
import importlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("KIANA_CRYPTO_KEY", "kv_test")

import pytest


@pytest.fixture
def tmp_frontier(tmp_path):
    """独立临时 FrontierDB（init_async 完成、用后自清）
    [FIXED & MODIFIED] v2.17 稳定性门禁：原实现对 init/close 各起一个 asyncio.run——
    aiosqlite 连接跨 loop 关闭；且 teardown 用 except: pass 吞错 → worker 线程/连接
    静默泄漏（长套件累积）。现改同一 loop 内 init+close，并显式关 loop。"""
    import asyncio
    from kiana_vnext_plus.frontier import FrontierDB

    loop = asyncio.new_event_loop()
    db = FrontierDB(str(tmp_path / "frontier.db"))
    loop.run_until_complete(db.init_async())
    yield db
    try:
        loop.run_until_complete(db.close())
    except Exception:
        pass
    finally:
        loop.close()


# ══════════════════════════════════════════════════════════════════════
# [v2.19.9] 数据隔离护栏：跑测试**一个字节**都不许写进机主的真实数据根
#
# 真实事故（不是假设）：`tests/test_cookie_sources_manager.py` 被执行时，把机主真实的
#   %LOCALAPPDATA%\KianaVnextPlus\launcher_config.json
# 里的 `cookie_file` 抹成了 `""` —— 那是机主刚登录好的 B站 cookies 路径。
#
# 根因：`launcher_v8.py:45  CONFIG_FILE = _launcher_config_path()` 是**导入期**求值的
# 模块常量。测试里那句 `monkeypatch.setenv("LOCALAPPDATA", tmp_path)` 只影响"之后现算
# 路径"的代码（`data_root()` 就是现算的，所以它有效），**对导入期常量无效** ——
# `save_config()` 于是每次都写向机主的真配置。
#
# 只把这个测试修好没有意义：下一个人写新测试照样会踩。所以这里做**两层**，
# 让"默认安全"取代"记得打桩"：
#   ① 预防（运行在每个测试之前，测试作者什么都不用写）：
#      把 `launcher_v8` / `launcher_v9` 的 `CONFIG_FILE` 强行指到本测试的 tmp_path。
#   ② 兜底（运行在每个测试之后）：机主真实数据根下那批**不可再生**的文件
#      （配置 / cookies / 登录态 / 密钥 / 身份池）前后各取一次指纹，
#      变了就**大声失败**并指名道姓是哪个测试干的。
#
# 为什么两层缺一不可：① 只挡**已知**机制（快、无副作用）；② 挡**未知**机制
# （比如将来有人绕开 CONFIG_FILE 直接拼 `%LOCALAPPDATA%` 字面量）。
# 没有 ②，下一个新写法又会是一次静默的数据损坏 —— 而静默正是这次事故最坏的部分。
# ══════════════════════════════════════════════════════════════════════

# 用 **conftest 导入时** 的环境算机主真实数据根，并固化成常量：
# 这样即使某个测试 monkeypatch 了 LOCALAPPDATA，这里仍然指向机主真目录
# （护栏本身绝不能被测试自己的隔离手段带偏）。conftest 是本目录最早被导入的
# 文件，此刻环境还是干净的。
_REAL_LOCALAPPDATA = os.environ.get("LOCALAPPDATA") or str(Path.home())
_REAL_DATA_ROOT = (
    Path(__file__).resolve().parent.parent / "KianaData"
    if os.environ.get("KIANA_PORTABLE", "") == "1"
    else Path(_REAL_LOCALAPPDATA) / "KianaVnextPlus"
)

# 只圈**丢了就找不回来**的东西。不圈 http_cache / samples / bench 这类可再生的
# 派生数据：它们被写坏只是"脏"，不是"丢"；把它们圈进来只会让护栏变慢、变吵，
# 而吵的护栏会被人加 `--deselect` 绕过 —— 那就白做了。
_IRREPLACEABLE = (
    "launcher_config.json",
    "launcher_config.json.bak",
    "launcher_config.json.v2197-backup",
    "cookies.txt",                        # 默认位置的登录态
    "master.key.bin",                     # 丢了 → secrets/ 里的密文全部解不开
    "armory.db",                          # 按站身份池（导入过的账号）
    "secrets/*",
    "profiles/*/cookies.txt",             # 各站配置档的登录态
    "profiles/*/storage_state.json",
)


def _fingerprint_real_data() -> dict:
    """{相对路径: "size:mtime_ns:sha256"} —— 机主真实数据根下那批不可再生文件的指纹。

    带上 size/mtime 是为了让失败信息直接可读（"从 1174 字节变成 1101 字节"比
    两串哈希好懂得多）；带上 sha256 是为了抓"大小没变但内容被改写"。
    """
    out = {}
    for pat in _IRREPLACEABLE:
        for p in _REAL_DATA_ROOT.glob(pat):
            if not p.is_file():
                continue
            rel = str(p.relative_to(_REAL_DATA_ROOT))
            try:
                b = p.read_bytes()
                out[rel] = f"{len(b)}:{p.stat().st_mtime_ns}:{hashlib.sha256(b).hexdigest()}"
            except OSError as e:            # 读不到也要留痕（被独占锁住本身就是异常）
                out[rel] = f"unreadable:errno={e.errno}"
    return out


def _shape_of_real_data_root() -> tuple:
    """真实数据根的**结构**：(深度≤2 的目录集合, 顶层条目名集合)。

    为什么光有上面的文件指纹不够：`mkdir` 不碰任何文件的内容/mtime，
    所以"用默认 root 去建一个配置档目录"的测试会**静默造出真实目录**，
    而文件指纹纹丝不动。把结构的"有没有"也比一遍就堵上了。
    这里只比名字、不算哈希 —— 便宜（本机约 40+35 个目录项）。
    """
    if not _REAL_DATA_ROOT.is_dir():
        return frozenset(), frozenset()
    dirs, top = set(), set()
    try:
        for entry in os.scandir(_REAL_DATA_ROOT):
            top.add(entry.name)
            if not entry.is_dir():
                continue
            dirs.add(entry.name)
            try:
                for sub in os.scandir(entry.path):
                    if sub.is_dir():
                        dirs.add(f"{entry.name}/{sub.name}")
            except OSError:
                pass
    except OSError:
        pass
    return frozenset(dirs), frozenset(top)


def _pin_launcher_config(fake: Path, monkeypatch) -> list:
    """把两个 launcher 模块的 `CONFIG_FILE` 都指到 `fake`，返回真被打桩的模块名。

    ⚠️ **两个都必须打**，这是本护栏最容易做错的一步：
    `launcher_v9.py:86` 写的是 `from launcher_v8 import CONFIG_FILE, ...` ——
    `import` 搬的是**值**（同一个 Path 对象的引用），`launcher_v9.CONFIG_FILE` 是
    **另一个名字绑定**。只打 `launcher_v8.CONFIG_FILE` 的话，v9 里
    `load_config()/save_config()` 读的是它自己的那个全局，**照旧指向真实路径**。
    （实测：打桩 v8 之后 `launcher_v9.CONFIG_FILE` 仍指向机主真配置。）

    这里**主动 import** 而不是只扫 `sys.modules`：若将来有人把
    `import launcher_v9` 写在测试函数体内（惰性导入），fixture 跑的时候它还没进
    `sys.modules`，只扫缓存就会漏过 —— 模块随后会带着**真实路径**完成导入，洞照旧。
    两个模块合计约 0.5s，且导入期不写任何文件（实测），这个代价换"默认安全"值。
    """
    pinned = []
    for name in ("launcher_v8", "launcher_v9"):
        mod = sys.modules.get(name)
        if mod is None:
            try:
                mod = importlib.import_module(name)
            except Exception:
                continue                    # 无 Qt / 无 qfluentwidgets：那两个测试本来也会 skip
        monkeypatch.setattr(mod, "CONFIG_FILE", fake, raising=False)
        pinned.append(name)
    return pinned


@pytest.fixture(autouse=True)
def never_touch_the_real_data_root(tmp_path, monkeypatch):
    """**全仓库 autouse**：每个测试前后都不许动机主的真实数据。

    autouse 是刻意的 —— 这条护栏的价值全在"不需要任何人记得它存在"。
    要新增一个测试却什么都不做，就已经是安全的。
    """
    fake_cfg = tmp_path / "launcher_config.json"
    _pin_launcher_config(fake_cfg, monkeypatch)

    before_files, before_shape = _fingerprint_real_data(), _shape_of_real_data_root()
    yield
    after_files, after_shape = _fingerprint_real_data(), _shape_of_real_data_root()

    if after_files == before_files and after_shape == before_shape:
        return

    changed = sorted(
        k for k in set(before_files) | set(after_files)
        if before_files.get(k) != after_files.get(k))
    new_dirs = sorted(after_shape[0] - before_shape[0])
    gone_dirs = sorted(before_shape[0] - after_shape[0])
    new_top = sorted(after_shape[1] - before_shape[1])

    parts = []
    if changed:
        parts.append("   被改写的文件:\n" + "\n".join(
            f"     · {k}\n       之前: {before_files.get(k, '<不存在>')}\n"
            f"       之后: {after_files.get(k, '<不存在>')}" for k in changed))
    if new_dirs or gone_dirs or new_top:
        parts.append(
            "   结构变化（`mkdir` 不改任何文件的内容/mtime，所以只比文件指纹会漏掉它）:\n"
            + (f"     新建目录: {new_dirs}\n" if new_dirs else "")
            + (f"     消失目录: {gone_dirs}\n" if gone_dirs else "")
            + (f"     新建顶层条目: {new_top}\n" if new_top else ""))

    raise AssertionError(
        "❌ 这个测试改写了机主的**真实**数据（已破坏，不会自动还原）！\n"
        f"   数据根: {_REAL_DATA_ROOT}\n"
        + "\n".join(parts) +
        "\n   为什么必须当失败处理：这些文件里是机主的登录态/密钥，"
        "丢了就找不回来 —— 静默写坏的代价远大于一条红测试。\n"
        "   怎么修：① 用 `tmp_path`，别把数据根指到真实目录；"
        "② 若被测代码在**导入期**就把路径固化成了模块常量"
        "（`launcher_v8.py:45` 的 `CONFIG_FILE` 就是这样），"
        "那么 `monkeypatch.setenv(\"LOCALAPPDATA\", ...)` 对它**无效**，"
        "必须在构造被测对象**之前** `monkeypatch.setattr(模块, \"CONFIG_FILE\", tmp_path / ...)`；"
        "③ 注意 `launcher_v9.CONFIG_FILE` 是 `from launcher_v8 import` 拷来的"
        "**另一个**名字绑定，两个都要打；"
        "④ 构造 `Crawler` 时 `HttpCache` 若不传 `cache_dir` 就会去建真实的 "
        "`<data_root>/http_cache` —— 显式传 `cache_dir=tmp_path/...`。\n"
        "   另注：若此刻正好有 Kiana 程序在运行并保存配置，也会触发本条 —— "
        "关掉程序后重跑即可确认。"
    )
