# -*- coding: utf-8 -*-
"""把验收文档里「引擎行数/模块数」同步成实测值。

**为什么需要它**：`tests/test_doc_claims.py` 强制
`docs/工程全景介绍-对外评审版.md` 里的引擎代码行数**精确等于实测**。
而**每改一次引擎代码，这个数就变** → 那条测试必红。

原来的流程因此变成：
    改代码 → 跑全量（3 分钟）→ 文档守卫红 → 手改数字 → **再跑一遍全量** → 提交
每轮白烧一次全量测试的时间。**本工具把"改数字"这一步提到跑测试之前**，
于是全量只需跑一次。

用法（改完引擎代码后、跑测试之前）：
    python tools/sync_doc_counts.py            # 同步
    python tools/sync_doc_counts.py --check    # 只检查，不同步（红了就退出码 1）

退出码：
    0  文档已一致（同步模式即「已同步成功」）
    1  文档数字过期 / 同步后仍不一致（既有语义，勿改）
    3  验收文档不在本仓库里 —— 公开快照的正常状态，**未读写任何文件**
       （不用 2：argparse 把「命令行用法错误」占了，避免两种含义撞车）

[v6 补缺口] 现在**行数与模块数都同步**（此前模块数漏了，
每加一个模块都要手工改文档，否则 `test_doc_claims` 红）。

⚠️ **只改「引擎 XX 行 / XX 个模块」那两个数**，绝不碰启动器的 `1,717` / `1,348` ——
早先我用 `replace("24,xxx", ...)` 图省事，把启动器行数也一起替换了，
被 `test_doc_claims` 当场抓住。
"""
import argparse
import os
import re
import sys
from pathlib import Path

# ─────────────────────────────────────────────────────────────
# [v2.19.9 修复] 中文 Windows 控制台是 GBK(cp936)，而本工具会 print ✅/❌ ——
# GBK **编码不了** U+2705/U+274C，于是 `UnicodeEncodeError` 当场崩（实测复现）：
#     UnicodeEncodeError: 'gbk' codec can't encode character '\u2705'
# 以前靠调用方自己设 `PYTHONIOENCODING=utf-8` 绕过 —— 那是"要求人记得加参数"，
# 与"默认就能用"的纪律相反（用户也确实被这个坑绊过）。这里在脚本内部把它修掉：
# 显式把两个流改成 UTF-8，并留 `errors="replace"` 作第二道保险 —— 万一某个流
# 不支持 reconfigure，也只是把个别符号降级成 `?`，而不是整个工具崩掉。
# ─────────────────────────────────────────────────────────────
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "kiana_vnext_plus"
DOC = ROOT / "docs" / "工程全景介绍-对外评审版.md"

# [v2.19.9 修复] 该文档**不随公开仓库发布**（内部验收材料，只在私有工程里）。
# 下面两个常量服务于 main() 顶部的守卫：打印用相对路径（不吐本机绝对路径），
# 退出码 3 = 「目标文档缺失」——与「不一致(1)」「用法错误(argparse 的 2)」区分开。
DOC_REL = "docs/工程全景介绍-对外评审版.md"
EXIT_NO_DOC = 3

# 引擎行数在文档里的写法：`| 引擎包 | 70 个模块、24,821 行（...）|`
#
# ⚠️ 两个正则**只匹配数字本身**，后缀用**后顾断言**（`(?=...)`）——
# 早先写成 `(\d[\d,]*) 行(后缀)` 再整体替换，会把「 行」甚至「引擎」吃掉，
# 把文档改坏（两次踩坑：先吃掉「 行」，再吃掉「引擎」）。
_ENGINE_LINES = re.compile(r"\d[\d,]*(?= 行[（(]`?kiana_vnext_plus)")
# 兜底：可维护性那节的「70 个模块、24,821 行引擎」
_ENGINE_LINES2 = re.compile(r"\d[\d,]*(?= 行引擎)")
# [v6 补缺口] **模块数**此前**不同步** —— 文档写「70 个模块」，而每加一个模块
# 实测就变，`test_doc_claims::test_engine_module_and_line_count` 必红，
# 只能手工改（这一轮我就手工改了两次）。
# 与行数同理：正则**只圈数字**，后缀「 个模块」原样保留。
_ENGINE_MODULES = re.compile(r"\d+(?= 个模块)")


def measure() -> tuple:
    n = tot = 0
    for dirpath, dirnames, filenames in os.walk(PKG):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for f in filenames:
            if f.endswith(".py"):
                n += 1
                tot += sum(1 for _ in (Path(dirpath) / f).read_text(
                    encoding="utf-8", errors="ignore").splitlines())
    return n, tot


# ── 启动器行数：文档里形如 "`launcher_v9.py` 1,717 行" ──────────────────
# [v6 补充] 原来本工具**只管引擎行数**，于是每改一次 GUI，
# `test_doc_claims::test_launcher_line_counts` 就红一次 —— 又得手动改。
# 现在一并管：文档数字与实测**一起**同步，跑测试前执行一次即可。
_LAUNCHERS = ("launcher_v9.py", "launcher_v8.py")


def measure_launchers() -> dict:
    out = {}
    for rel in _LAUNCHERS:
        p = ROOT / rel
        if p.exists():
            out[rel] = sum(1 for _ in p.read_text(
                encoding="utf-8", errors="ignore").splitlines())
    return out


def launcher_numbers_in_doc() -> dict:
    """{文件名: 文档里写的那个数}（每个文件可能有多个写法，取集合）"""
    text = DOC.read_text(encoding="utf-8")
    out = {}
    for rel in _LAUNCHERS:
        pat = re.compile(re.escape(rel) + r"`?\s*(\d[\d,]*) 行")
        out[rel] = {int(m.replace(",", "")) for m in pat.findall(text)}
    return out


def current_in_doc() -> set:
    text = DOC.read_text(encoding="utf-8")
    found = set()
    for pat in (_ENGINE_LINES, _ENGINE_LINES2):
        for m in pat.finditer(text):
            # 正则只圈住数字（无捕获组）→ 用 group(0)
            found.add(m.group(0))
    return found


# ── [v6 补缺口] 仓库规模类数字（提交数 / tag 数 / 测试文件数）──────────────
# 为什么不早做：这三类数字**每提交一次就变**，而文档里的值只靠人记得去改。
# 后果实测：`test_doc_claims::test_documented_commit_count_not_absurdly_stale`
# 的判据是"文档值 ≥ 实际的 80%"——**只要一直不改，迟早会翻过阈值**。
# 本轮就是这样翻的：实际 319 提交，文档还写 254，而 319×0.8 = 255.2 —— **差 1.2**。
# 既然本工具的存在意义就是"把改数字提到跑测试之前"，这三类也该归它管。
def _git(*a) -> str:
    import subprocess
    try:
        return subprocess.run(["git", *a], cwd=ROOT, capture_output=True,
                              text=True, timeout=20).stdout.strip()
    except Exception:
        return ""


def measure_repo() -> dict:
    """提交数 / tag 总数 / 两类 tag 数 / 测试文件数（全部可机械求得）。"""
    tags = [t for t in _git("tag").splitlines() if t.strip()]
    c = _git("rev-list", "--count", "HEAD")
    tests_dir = ROOT / "tests"
    return {
        "commits": int(c) if c.isdigit() else 0,
        "tags": len(tags),
        "final_tags": len([t for t in tags if t.startswith("v") and t.endswith("-final")]),
        "ckpt_tags": len([t for t in tags if t.startswith("checkpoint-")]),
        "test_files": len(list(tests_dir.glob("test_*.py"))) if tests_dir.is_dir() else 0,
    }


_REPO_SUBS = (
    (re.compile(r"\d+(?= 次提交)"), "commits"),
    (re.compile(r"\d+(?= 个 tag（含)"), "tags"),
    (re.compile(r"\d+(?= 个 `v\*-final` 发布 tag)"), "final_tags"),
    (re.compile(r"\d+(?= 个 `checkpoint-\*` 回滚点)"), "ckpt_tags"),
    (re.compile(r"\d+(?= 个测试文件)"), "test_files"),
)


def sync_repo_numbers(text: str, m: dict) -> str:
    """把仓库规模类数字写回文档（**只圈数字**，后缀原样保留）。"""
    for rx, key in _REPO_SUBS:
        text = rx.sub(lambda _m, v=m[key]: str(v), text)
    return text


def modules_in_doc() -> set:
    """文档里写的模块数（可能多处）。"""
    try:
        text = DOC.read_text(encoding="utf-8")
    except OSError:
        return set()
    return {int(m) for m in _ENGINE_MODULES.findall(text)}


def main() -> int:
    ap = argparse.ArgumentParser(description="同步验收文档里的引擎规模数字")
    ap.add_argument("--check", action="store_true", help="只检查不同步（不一致则退出 1）")
    args = ap.parse_args()

    # ── [v2.19.9 修复] 验收文档**不随公开仓库发布**，缺了必须**明说** ──────────
    # 它是内部验收材料，只在私有工程里；公开快照的 `docs/` 下没有它。
    # 此前本函数一进来就读它，于是全新 clone 上跑本工具**必崩**（实测）：
    #     FileNotFoundError: .../docs/工程全景介绍-对外评审版.md
    # 本工具存在的唯一目的，就是把实测数字写回**那份文档**；没有它就没有同步目标。
    # 于是：说清原因 → 退出码 3（可脚本判定）→ **一个文件都不碰**。
    # 既不崩，也不假装「已同步」（静默 return 0 会让调用方以为写成功了）。
    if not DOC.exists():
        print(f"未同步：{DOC_REL} 不在本仓库里 —— 没有可同步的目标。")
        print("  该文档是内部验收材料，不随公开快照发布（公开 docs/ 下没有它）。")
        print("  本次未读写任何文件；退出码 3 = 「目标文档缺失」，不是崩溃、也不是成功。")
        return EXIT_NO_DOC

    n, tot = measure()
    want = f"{tot:,}"
    have = current_in_doc()
    lw = measure_launchers()
    lhave = launcher_numbers_in_doc()

    mhave = modules_in_doc()
    repo = measure_repo()
    print(f"实测：{n} 个模块 / {tot:,} 行")
    print(f"仓库：{repo['commits']} 提交 / {repo['tags']} tag"
          f"（final {repo['final_tags']} / checkpoint {repo['ckpt_tags']}）"
          f" / {repo['test_files']} 个测试文件")
    print(f"文档：{sorted(have) or '（没找到）'}；模块数 {sorted(mhave) or '（没找到）'}")
    for rel, real in lw.items():
        ok = lhave.get(rel) == {real}
        print(f"  {rel}: 实测 {real:,} 行 / 文档 {sorted(lhave.get(rel) or []) or '（没找到）'}"
              f"  {'✅' if ok else '❌'}")

    engine_ok = have == {want}
    launch_ok = all(lhave.get(rel) == {real} for rel, real in lw.items())
    mod_ok = mhave == {n}

    _txt = DOC.read_text(encoding="utf-8")
    repo_ok = sync_repo_numbers(_txt, repo) == _txt

    if engine_ok and launch_ok and mod_ok and repo_ok:
        print("✅ 已一致")
        return 0
    if args.check:
        if not engine_ok:
            print(f"❌ 引擎行数不一致 —— 应改为 {want}")
        if not mod_ok:
            print(f"❌ 引擎模块数不一致 —— 应改为 {n}")
        _t = DOC.read_text(encoding="utf-8")
        if sync_repo_numbers(_t, repo) != _t:
            print("❌ 仓库规模类数字过期（提交/tag/测试文件数）—— 跑同步即可")
        for rel, real in lw.items():
            if lhave.get(rel) != {real}:
                print(f"❌ {rel} 行数不一致 —— 应改为 {real:,}")
        print("跑 `python tools/sync_doc_counts.py` 修")
        return 1

    text = DOC.read_text(encoding="utf-8")
    # 引擎：正则只圈住数字，后缀原样保留 —— 回填时**不要**再补任何后缀
    text = _ENGINE_LINES.sub(lambda m: want, text)
    text = _ENGINE_LINES2.sub(lambda m: want, text)
    text = _ENGINE_MODULES.sub(lambda m: str(n), text)   # [v6] 模块数一并同步
    text = sync_repo_numbers(text, repo)                 # [v6] 仓库规模类数字
    # 启动器：整段（`文件名` N 行）一起替换，数字用千分位
    for rel, real in lw.items():
        text = re.sub(re.escape(rel) + r"`?\s*\d[\d,]* 行",
                      f"{rel}` {real:,} 行", text)
    DOC.write_text(text, encoding="utf-8", newline="\n")

    after_engine = current_in_doc()
    after_launch = launcher_numbers_in_doc()
    if after_engine != {want}:
        print(f"❌ 引擎同步后仍不一致：{sorted(after_engine)}")
        return 1
    for rel, real in lw.items():
        if after_launch.get(rel) != {real}:
            print(f"❌ {rel} 同步后仍不一致：{sorted(after_launch.get(rel) or [])}")
            return 1
    print(f"✅ 已同步：引擎 {want} 行；" +
          "；".join(f"{rel} {real:,} 行" for rel, real in lw.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
