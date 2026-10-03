# -*- coding: utf-8 -*-
"""静默失败扫描：让"例外必须写理由注释"这条纪律**可核对**（门禁第 12 项）

**为什么需要它**：`03-终检与闭环方案` 第一组写了一条判据——
"无新增 `except: pass` / 无日志的静默失败分支（例外必须写理由注释）"。
但这条**此前无法核对**：全仓实测有 200+ 处 `except ...: pass`，肉眼扫不出来，
而工程文档里却按"只有两处合法例外"的语气在写。

**本扫描器只做一件事，但做死**：找出**热路径文件**里"`except` 体只有 `pass`、
且**没有任何 `#` 注释**"的位置，并与锁定基线比对——**只许降不许升**
（对齐既有的 ruff/mypy 基线做法）。

两类位置被区别对待：

| 形态 | 判定 |
|---|---|
| `except X: pass  # 说明` | ✅ **合法**——写清理由的忽略是工程实践，不是债 |
| `except X: pass` | ❌ 计入基线——静默吞掉异常，出事时没有任何线索 |

**为什么只扫热路径**：抓取/网络/安全/身份这几条链上的静默失败会直接变成
"用户看到的现象和真实原因不一致"（工程最贵的一类）；其余位置的清理是独立决策，
不在这里顺手做（修一件事只修那件事）。

用法:
  python tools/silent_failure_scan.py                 # 核对（门禁用）
  python tools/silent_failure_scan.py --list          # 逐条列出
  python tools/silent_failure_scan.py --write-baseline
"""
import argparse
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BASELINE = ROOT / "tests" / "assets" / "silent_failure_baseline.json"

EXIT_OK = 0
EXIT_REGRESSION = 1

# 热路径：抓取 / 网络 / 安全 / 身份。这几条链上的静默失败最贵。
HOT_FILES = (
    "kiana_vnext_plus/page_processor.py",
    "kiana_vnext_plus/protocol_engine.py",
    "kiana_vnext_plus/media_downloader.py",
    "kiana_vnext_plus/universal_downloader.py",
    "kiana_vnext_plus/m3u8_downloader.py",
    "kiana_vnext_plus/url_utils.py",
    "kiana_vnext_plus/frontier.py",
    "kiana_vnext_plus/redis_frontier.py",
    "kiana_vnext_plus/engine_router.py",
    "kiana_vnext_plus/solver_engine.py",
    "kiana_vnext_plus/concurrency.py",
    "kiana_vnext_plus/cookie_armory.py",
    "kiana_vnext_plus/cookie_health.py",
    "kiana_vnext_plus/identity_session.py",
    "kiana_vnext_plus/sanitizer.py",
)


def _has_reason(lines, start: int, end: int) -> bool:
    """`except` 到 `pass` 之间（含同行）是否出现过 `#` 注释。

    AST 不留注释，故回源看文本——这是本扫描器唯一需要的文本级判断。
    """
    for i in range(max(start - 1, 0), min(end, len(lines))):
        if "#" in lines[i]:
            return True
    return False


def scan_file(path: Path):
    """返回该文件里"只 pass 且无理由注释"的位置列表 `[( lineno, exc_type )]`。"""
    try:
        src = path.read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(src)
    except Exception:
        return []
    lines = src.splitlines()
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for h in node.handlers:
            if len(h.body) != 1 or not isinstance(h.body[0], ast.Pass):
                continue
            et = ast.unparse(h.type) if h.type else "BARE"
            if not _has_reason(lines, h.lineno, h.body[0].lineno):
                out.append((h.lineno, et))
    return out


def scan(root: Path = ROOT) -> dict:
    """→ `{相对路径: [[行号, 异常类型], ...]}`；只含**无理由**的 pass-only 处理器。"""
    res = {}
    for rel in HOT_FILES:
        p = Path(root) / rel
        if not p.exists():
            continue
        hits = scan_file(p)
        if hits:
            res[rel.replace("\\", "/")] = [[n, t] for n, t in hits]
    return res


def load_baseline() -> dict:
    try:
        data = json.loads(BASELINE.read_text(encoding="utf-8"))
        return data.get("files", {}) if isinstance(data, dict) else {}
    except Exception:
        return {}


def compare(baseline: dict, current: dict) -> dict:
    """逐文件比数量。**新增文件**或**数量上升**都算退步；下降记为好。"""
    worse, better = [], []
    for f, hits in current.items():
        base = len(baseline.get(f, []))
        if len(hits) > base:
            worse.append((f, base, len(hits)))
        elif len(hits) < base:
            better.append((f, base, len(hits)))
    for f, hits in baseline.items():
        if f not in current:                     # 整个文件的静默失败被清干净了
            better.append((f, len(hits), 0))
    return {"worse": sorted(worse), "better": sorted(better),
            "total": sum(len(v) for v in current.values()),
            "baseline_total": sum(len(v) for v in baseline.values())}


def main() -> int:
    ap = argparse.ArgumentParser(description="热路径静默失败扫描（只许降不许升）")
    ap.add_argument("--list", action="store_true", help="逐条列出")
    ap.add_argument("--write-baseline", action="store_true")
    args = ap.parse_args()

    cur = scan()
    total = sum(len(v) for v in cur.values())
    if args.list:
        for f, hits in sorted(cur.items()):
            for n, t in hits:
                print(f"  {f}:{n}  ({t})")
    print(f"热路径里「只 pass 且无理由注释」的处理器：**{total}** 处"
          f"（覆盖 {len(cur)} 个文件）")

    if args.write_baseline:
        BASELINE.write_text(json.dumps(
            {"_说明": ["热路径静默失败基线（门禁第 12 项）。",
                       "只许降不许升：修掉一处就更新本文件，让它变小。",
                       "要新增例外，请在 except 行或 pass 行写上 `#` 理由注释——",
                       "写清理由的忽略是工程实践，不是债；不写理由的才是。"],
             "files": {f: [n for n, _ in hits] for f, hits in cur.items()}},
            ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"✅ 基线已写入：{BASELINE}")
        return EXIT_OK

    base = load_baseline()
    if not base:
        print("⚠️ 尚无基线——先跑 --write-baseline")
        return EXIT_OK
    cmp = compare(base, cur)
    if cmp["worse"]:
        print("❌ 静默失败**新增**（热路径不允许）：")
        for f, b, n in cmp["worse"]:
            print(f"   {f}: {b} → {n}")
        print("   修掉，或在 except/pass 行补 `# 理由` 注释。")
        return EXIT_REGRESSION
    print(f"✅ 未新增（基线 {cmp['baseline_total']} → 当前 {cmp['total']}）"
          f"{'，且已下降 ' + str(cmp['better']) if cmp['better'] else ''}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
