# -*- coding: utf-8 -*-
"""「同一能力多份实现」扫描器（门禁第 14 项）

**为什么要工具化**：本工程最稳定的缺陷模式已经被连续九轮验证——

| 轮 | 能力 | 发现 |
|---|---|---|
| 18 | WebRTC 泄漏面 | 5 份实现，2 份**各漏一条路径** |
| 19 | 下载会话 impersonate | 3 份拷贝，**只修了 2 份** |
| 23 | UA↔TLS | 主通道修过同源绑定，**下载链没跟** |
| 24 | 重试策略 | Redis 后端是**陈旧分叉**，落后 3 次修复 |
| 25 | 后端接口 | `push`/`pop_batch` 签名不符 → **TypeError**；缺 6 个方法 |
| 26 | 防盗链 Referer | **三种做法**，两种正好命中 403 形态 |

共同点：**同一个能力在多处各写一份**。此前每轮都是我人肉去找"下一个能力"——
**这本身就是不可靠的做法**。本扫描器把它变成可重复的检查。

**判据（按危险程度排序）**：

  ① **同名函数/方法出现在 ≥2 个模块，且形参名不一致** —— 最危险：
     调用点按 A 的签名写、跑到 B 上就是 **TypeError**（第 25 轮那两个就是这么来的）；
  ② 同名且形参一致 —— 只是**分叉风险**（第 24 轮那次；逻辑可能已经不一样了）；
  ③ 跨模块出现的同名**常量**（映射表/池）—— 第 18/26 轮那类。

用法:
  python tools/duplicate_capability_scan.py            # 核对（门禁用）
  python tools/duplicate_capability_scan.py --list      # 逐条列出
  python tools/duplicate_capability_scan.py --write-baseline
"""
import argparse
import ast
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PKG = ROOT / "kiana_vnext_plus"
BASELINE = ROOT / "tests" / "assets" / "duplicate_capability_baseline.json"

EXIT_OK = 0
EXIT_REGRESSION = 1

# 这些名字出现多次是**正常**的（协议实现、生命周期钩子等），不构成分叉信号
IGNORE_NAMES = frozenset({
    "__init__", "__aenter__", "__aexit__", "__enter__", "__exit__", "__repr__", "__str__",
    "__len__", "__eq__", "__hash__", "close", "reset", "run", "start", "stop",
    "main", "setup", "teardown", "get", "post", "put", "delete", "handler", "handle",
})

# 形参名不一致时，这些"通用后缀"差异不算危险（调用方通常不按名传）
def _params(node) -> tuple:
    a = node.args
    names = [x.arg for x in (a.posonlyargs + a.args + a.kwonlyargs)]
    if a.vararg:
        names.append("*" + a.vararg.arg)
    if a.kwarg:
        names.append("**" + a.kwarg.arg)
    return tuple(names)


def scan() -> dict:
    """→ `{名字: [{"file","line","params","kind"}]}`（只保留出现在 ≥2 个模块的名字）"""
    found = defaultdict(list)
    for p in sorted(PKG.glob("*.py")):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if n.name in IGNORE_NAMES or n.name.startswith("__"):
                    continue
                found[n.name].append({
                    "file": p.name, "line": n.lineno,
                    "params": _params(n),
                    "kind": "async" if isinstance(n, ast.AsyncFunctionDef) else "sync",
                })
    # 只保留"跨 ≥2 个模块"的
    out = {}
    for name, items in found.items():
        if len({i["file"] for i in items}) >= 2:
            out[name] = sorted(items, key=lambda x: (x["file"], x["line"]))
    return out


def classify(items) -> str:
    """三档风险。

    **判据是实测校准出来的，不是拍脑袋**：本工程真正出事的同名实现
    （`push`/`pop_batch`/`mark_failed`/`normalize_url`/`sanitize_video_filename`/
    `write_page`）**全都只出现在 2 个模块**；而出现在 ≥3 个模块的名字
    （`acquire`/`record`/`release`/`solve`/`save`）经逐条人工核对，
    **全部是无关类共享的通用动词**（信号量/限流/浏览器槽位/指标记录…），不是分叉。

    所以：

      · 出现在 **≥3 个模块** → `generic_name`（登记，不阻断）；
      · 含 `**kwargs` → `same_signature`（纯委托能接任意关键字，天然兼容，
        不处理这点会把 `write_page` 这类正常委托误报成失配——第 25 轮踩过）；
      · 其余 → 按形参是否一致分 `signature_mismatch`（**阻断**）/ `same_signature`。
    """
    if len({i["file"] for i in items}) >= 3:
        return "generic_name"
    sigs = [tuple(i["params"]) for i in items]
    if any(any(p.startswith("**") for p in s) for s in sigs):
        return "same_signature"
    return "same_signature" if len(set(sigs)) == 1 else "signature_mismatch"


def analyze() -> dict:
    return {name: {"risk": classify(items),
                   "sites": [f"{i['file']}:{i['line']}" for i in items]}
            for name, items in scan().items()}


def load_baseline() -> dict:
    try:
        data = json.loads(BASELINE.read_text(encoding="utf-8"))
        return data.get("capabilities", {}) if isinstance(data, dict) else {}
    except Exception:
        return {}


def compare(baseline: dict, current: dict) -> dict:
    """比对基线。

    三类结果：
      · `new_mismatch` —— **形参不一致**且基线里没有（最危险，调用点可能 TypeError）；
      · `new_name`     —— **全新的同名跨模块实现**（含同签名）。这条是"**让重复变成有意识的行为**"：
        重复本身不是错，但"悄悄又抄了一份"正是本工程所有缺陷的**机制**。
        新增必须登记一次（写明是与谁重复、为何不合并），此后它才进入被守护的集合；
      · `fixed_mismatch` / `gone` —— 变好的方向。
    """
    def bad(d):
        return {k for k, v in d.items() if v["risk"] == "signature_mismatch"}
    b, c = bad(baseline), bad(current)
    new_name = sorted(set(current) - set(baseline))
    return {"new_mismatch": sorted(c - b),
            "new_name": new_name,
            "fixed_mismatch": sorted(b - c),
            "gone": sorted(set(baseline) - set(current)),
            "mismatch_total": len(c), "baseline_mismatch": len(b),
            "same_sig_total": sum(1 for v in current.values() if v["risk"] == "same_signature")}


def main() -> int:
    ap = argparse.ArgumentParser(description="同一能力多份实现扫描")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--write-baseline", action="store_true")
    args = ap.parse_args()

    cur = analyze()
    mism = {k: v for k, v in cur.items() if v["risk"] == "signature_mismatch"}
    same = {k: v for k, v in cur.items() if v["risk"] == "same_signature"}

    if args.list:
        for k, v in sorted(mism.items()):
            print(f"  [形参不一致] {k}: {v['sites']}")
        for k, v in sorted(same.items()):
            print(f"  [同签名分叉] {k}: {v['sites']}")

    print(f"同名跨模块定义：{len(cur)} 个（其中**形参不一致** {len(mism)} 个，同签名 {len(same)} 个）")

    if args.write_baseline:
        BASELINE.write_text(json.dumps(
            {"_说明": ["同一能力多份实现的基线（门禁第 14 项）。",
                       "阻断两类：① **形参不一致**（调用点可能 TypeError）；",
                       "          ② **未登记的同名跨模块实现**（重复要有意识，不许悄悄又抄一份）。",
                       "同名同形参只登记（分叉风险）。合并掉一个就更新基线让它变小。",
                       "新增项必须写明「与谁重复、为何不合并」。"],
             "capabilities": cur}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"✅ 基线已写入：{BASELINE}")
        return EXIT_OK

    base = load_baseline()
    if not base:
        print("⚠️ 尚无基线——先跑 --write-baseline")
        return EXIT_OK
    cmp = compare(base, cur)
    if cmp["new_mismatch"]:
        print("❌ 新增**形参不一致**的同名实现（调用点可能 TypeError）：")
        for k in cmp["new_mismatch"]:
            print(f"   {k}: {cur[k]['sites']}")
        print("   修掉它，或确认无害后更新基线并写明理由。")
        return EXIT_REGRESSION
    if cmp["new_name"]:
        print("❌ 出现**未登记**的同名跨模块实现（重复本身不是错，但要**有意识**）：")
        for k in cmp["new_name"][:12]:
            print(f"   {k}: {cur[k]['sites']}")
        print("   合并成单一实现，或确认无害后更新基线并写明理由。")
        return EXIT_REGRESSION
    if cmp["new_name"]:
        print("❌ 出现**未登记**的同名跨模块实现（重复本身不是错，但要**有意识**）：")
        for k in cmp["new_name"][:12]:
            print(f"   {k}: {cur[k]['sites']}")
        print("   合并成单一实现，或确认无害后更新基线并写明理由。")
        return EXIT_REGRESSION
    print(f"✅ 未新增形参不一致（基线 {cmp['baseline_mismatch']} → 当前 {cmp['mismatch_total']}）"
          f"{'，且已下降 ' + str(cmp['fixed_mismatch']) if cmp['fixed_mismatch'] else ''}"
          f"；另有 {cmp['same_sig_total']} 个同签名分叉已登记")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
