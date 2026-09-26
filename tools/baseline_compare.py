"""v2.16 M2 性能基准对比：读两份 stress_bench 基准 JSONL → 回归报告

用法:
    python tools/baseline_compare.py --old <旧基准.jsonl> --new <新基准.jsonl> [--threshold 0.20]

输出: 吞吐/延迟/成功率变化 %；吞吐下降超过 threshold 打 ❌（回归门禁）。
"""
import sys, os, json, argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _load(p):
    with open(p, encoding="utf-8") as f:
        return json.loads(f.readline().strip())  # 每文件一条记录


def main():
    ap = argparse.ArgumentParser(description="性能基准对比")
    ap.add_argument("--old", required=True, help="旧基准 jsonl")
    ap.add_argument("--new", required=True, help="新基准 jsonl")
    ap.add_argument("--threshold", type=float, default=0.20, help="吞吐下降告警阈值(默认0.20)")
    args = ap.parse_args()

    old = _load(args.old)
    new = _load(args.new)
    print("═══ 性能回归报告 ═══")
    print(f"  旧: {args.old}  ({old.get('ts','')})")
    print(f"  新: {args.new}  ({new.get('ts','')})")
    if old.get("label") != new.get("label"):
        print("  ⚠️ 两次基准 label 不同——非同一场景，对比仅供参考")
    d_rate = (new["rate_pps"] - old["rate_pps"]) / old["rate_pps"] * 100 if old["rate_pps"] else 0
    d_succ = new["ok_rate_pct"] - old["ok_rate_pct"]
    d_rss = (new["rss_peak_mb"] - old["rss_peak_mb"])
    print(f"  吞吐: {old['rate_pps']} → {new['rate_pps']} p/s  ({d_rate:+.1f}%)")
    print(f"  成功率: {old['ok_rate_pct']:.0f}% → {new['ok_rate_pct']:.0f}%  ({d_succ:+.1f}%)")
    print(f"  RSS: {old['rss_peak_mb']:.0f} → {new['rss_peak_mb']:.0f} MB  ({d_rss:+.0f})")
    print(f"  hw_factor: 旧{old.get('hw_factor','?')} 新{new.get('hw_factor','?')} (跨机归一化参考)")
    regress = d_rate < -args.threshold * 100
    print(f"  回归判定(吞吐降>{args.threshold*100:.0f}%):", "❌ 回归! 需排查" if regress else "✅ 无回归")
    sys.exit(1 if regress else 0)


if __name__ == "__main__":
    main()
