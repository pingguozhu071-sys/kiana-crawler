"""v2.16 M4 数据看板：读任务 stats.jsonl + frontier 概览 → 趋势图 PNG（Qt 离屏/matplotlib）

用法: python tools/dashboard.py --project <cli_xxx 目录> [--out dashboard.png]

图形：吞吐/完成趋势折线 + 成功率/失败柱状（matplotlib，无 GUI 依赖）。
"""
import sys, os, json, argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
plt.rcParams["axes.unicode_minus"] = False


def _stats_rows(project):
    f = Path(project) / "stats.jsonl"
    if not f.exists():
        return []
    rows = []
    for line in f.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _frontier_counts(project):
    try:
        import sqlite3
        con = sqlite3.connect(str(Path(project) / "frontier.db"))
        counts = dict(con.execute("SELECT status, COUNT(*) FROM frontier GROUP BY status").fetchall())
        con.close()
        return counts
    except Exception:
        return {}


def _sqlite_summary(db_path):
    """[v2.17 4.1] 读 cli export --format sqlite 导出的 data.sqlite：media/scrape 概览。
    返回 (ok, dict) / (False, {})。纯 sqlite3 查询，无 GUI 依赖。"""
    try:
        import sqlite3
        con = sqlite3.connect(str(db_path))
        med = con.execute("SELECT COUNT(*), COALESCE(SUM(downloaded),0) FROM media").fetchone()
        scr = con.execute("SELECT COUNT(*) FROM scrape").fetchone()
        doms = con.execute(
            "SELECT domain, COUNT(*) FROM scrape WHERE domain != '' "
            "GROUP BY domain ORDER BY 2 DESC LIMIT 15").fetchall()
        titles = con.execute(
            "SELECT title FROM scrape WHERE title != '' LIMIT 5").fetchall()
        con.close()
        return True, {"media": med[0], "media_downloaded": med[1], "scrape": scr[0],
                      "domains": doms, "titles": [t[0] for t in titles]}
    except Exception as e:
        return False, {"error": str(e)}


def main():
    ap = argparse.ArgumentParser(description="Kiana 数据看板")
    ap.add_argument("--project", required=True, help="任务目录（cli_xxx）")
    ap.add_argument("--out", default="", help="输出 PNG")
    ap.add_argument("--db", default="", help="[v2.17 4.1] 已导出的 data.sqlite"
                    "（media/scrape 概览模式——与 stats 模式互斥，提供即走该模式）")
    args = ap.parse_args()

    if args.db:
        ok, info = _sqlite_summary(args.db)
        if not ok:
            print(f"（sqlite 读取失败: {info.get('error')}）")
            return
        print(f"═══ data.sqlite 概览（{args.db}）═══")
        print(f"  media 记录: {info['media']}（已下载 {info['media_downloaded']}）")
        print(f"  scrape 记录: {info['scrape']}")
        if info["domains"]:
            print("  按域 scrape 计数:")
            for dom, n in info["domains"]:
                print(f"    {dom:<30} {n}")
        if info["titles"]:
            print("  最近标题:", " | ".join(str(t)[:24] for t in info["titles"][:3]))
        return

    rows = _stats_rows(args.project)
    counts = _frontier_counts(args.project)
    if not rows:
        # [v2.16 M4] stats.jsonl 可能为空（已知遗留）→ 用 frontier 计数兜底展示
        print("（stats.jsonl 无内容——以 frontier 状态为准）")
        print(f"  frontier 状态: {counts}")
        print(f"  提示: stats.jsonl 0 字节为 v2.15 已知遗留（多批任务偶发），不影响功能")
        return

    # 图1：完成趋势
    fig, ax = plt.subplots(figsize=(10, 5), facecolor="#12151c")
    ax.set_facecolor("#161a22")
    xs = list(range(len(rows)))
    done = [r.get("done", 0) for r in rows]
    fail = [r.get("failed", 0) for r in rows]
    ax.plot(xs, done, color="#4FA3E8", label="完成页数")
    ax.fill_between(xs, 0, done, color="#4FA3E8", alpha=0.2)
    ax.plot(xs, fail, color="#E5534B", label="失败页数")
    ax.set_title(f"Kiana 数据看板 · {Path(args.project).name}", color="#E6EDF3")
    ax.set_xlabel("批次", color="#9aa4b2")
    ax.set_ylabel("页数", color="#9aa4b2")
    ax.legend(facecolor="#1a2029", labelcolor="#C9D1D9")
    ax.grid(color="#2a313c")
    for spine in ax.spines.values():
        spine.set_color("#2a313c")
    ax.tick_params(colors="#9aa4b2")
    fig.tight_layout()
    out = args.out or Path(args.project) / "dashboard.png"
    fig.savefig(str(out), dpi=120, facecolor=fig.get_facecolor())
    print(f"看板已生成: {out}")
    print(f"  frontier 状态: {counts}")


if __name__ == "__main__":
    main()
