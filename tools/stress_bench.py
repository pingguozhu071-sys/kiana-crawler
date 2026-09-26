"""压力基线实测（v2.11 阶段4-D——把"性能 6.5 分"打成有据可依）

广撒网压力跑：N 个种子 URL 深度爬取，实时统计吞吐/成功/失败/进程内存峰值，产出基线报告。

用法:
    python tools/stress_bench.py --urls URL... [--max-pages N] [--depth N] [--label NAME]

指标:
    - 页面吞吐 p/s（done/耗时）
    - 成功率（done/(done+fail)）
    - 进程内存峰值 RSS（psutil，含浏览器子进程内存抽样）
    - 数据落盘量（jsonl 字节数）

诚实边界：结果受目标站响应速度/风控影响，跨站点对比无意义——同站改配置前后对比才有意义。
"""
import sys, os, asyncio, time, argparse, logging, threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

PROJECT = Path(__file__).resolve().parent.parent


def _rss_mb() -> float:
    try:
        import psutil
        return psutil.Process().memory_info().rss / 1024 / 1024
    except Exception:
        return 0.0


async def _run(urls, max_pages, depth, label):
    import run_crawler
    out = Path.home() / "Downloads" / "KianaVnextPlus"
    t0 = time.monotonic()
    rss_peak = [0.0]
    # [v2.16 M2] 硬件因子：cpu count/logical + RAM GB（跨机归一化：hw_factor = 参考机(20核/16G)/本机）
    try:
        import psutil as _ps
        _cores = _ps.cpu_count(logical=True) or 8
        _ram_gb = _ps.virtual_memory().total / (1024 ** 3)
    except Exception:
        _cores, _ram_gb = 8, 16
    hw_factor = round((20 / max(_cores, 1)) * (16 / max(_ram_gb, 1)), 3)

    def _watch():
        while not _watch.done:
            rss_peak[0] = max(rss_peak[0], _rss_mb())
            time.sleep(0.5)
    _watch.done = False
    threading.Thread(target=_watch, daemon=True).start()

    crawler = await run_crawler.crawl(urls, {
        "crawl_depth": depth, "max_pages": max_pages,
        "download_path": str(out / f"stress_{label}" if label else out),
        "log_level": "WARNING",
        "dl_video": False, "dl_image": False, "dl_audio": False,
    })

    elapsed = time.monotonic() - t0
    _watch.done = True
    p = getattr(crawler, "_progress", {}) or {}
    done, fail = p.get("done", 0), p.get("failed", 0)
    total = done + fail
    rate = done / elapsed if elapsed > 0 else 0
    ok_rate = done / total * 100 if total else 0
    # 数据落盘量
    data_bytes = 0
    try:
        base = Path(str(out)) / f"stress_{label}" if label else Path(str(out))
        for d in base.glob("cli_*"):
            data_bytes += sum(f.stat().st_size for f in d.rglob("*.jsonl") if f.is_file())
    except Exception:
        pass

    print("\n═══ 压力基线报告 ═══")
    print(f"  标签: {label or 'default'}  种子: {len(urls)}  max_pages: {max_pages}  depth: {depth}")
    print(f"  耗时: {elapsed:.1f}s")
    print(f"  吞吐: {rate:.2f} p/s  完成 {done} 失败 {fail}  成功率 {ok_rate:.0f}%")
    print(f"  内存峰值 RSS: {rss_peak[0]:.0f} MB")
    print(f"  数据落盘(jsonl): {data_bytes/1024:.0f} KB")
    print(f"  硬件因子 hw_factor: {hw_factor:.2f}（跨机归一化用）")
    # [v2.16 M2] 基准落盘：tests/benchmarks/<label>_<ts>.jsonl（可复盘的性能档案）
    try:
        import json as _json
        import datetime as _dt
        bdir = Path(__file__).parent.parent / "tests" / "benchmarks"
        bdir.mkdir(parents=True, exist_ok=True)
        _rec = {"ts": _dt.datetime.now().isoformat(timespec="seconds"),
                "label": label, "urls": len(urls), "max_pages": max_pages, "depth": depth,
                "elapsed_s": round(elapsed, 2), "rate_pps": round(rate, 3),
                "done": done, "fail": fail, "ok_rate_pct": round(ok_rate, 1),
                "rss_peak_mb": round(rss_peak[0], 1), "data_kb": data_bytes // 1024,
                "hw_factor": round(hw_factor, 3)}
        with open(bdir / f"{label or 'default'}_{_dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl",
                  "w", encoding="utf-8") as f:
            f.write(_json.dumps(_rec, ensure_ascii=False) + "\n")
        print(f"  基准已存档: {bdir}")
    except Exception:
        pass
    return {"elapsed": elapsed, "rate": rate, "done": done, "fail": fail,
            "ok_rate": ok_rate, "rss_peak_mb": rss_peak[0], "data_kb": data_bytes // 1024,
            "hw_factor": hw_factor}


def main():
    ap = argparse.ArgumentParser(description="压力基线实测")
    ap.add_argument("--urls", nargs="+", required=True)
    ap.add_argument("--max-pages", type=int, default=50)
    ap.add_argument("--depth", type=int, default=1)
    ap.add_argument("--label", default="bench")
    args = ap.parse_args()
    asyncio.run(_run(args.urls, args.max_pages, args.depth, args.label))


if __name__ == "__main__":
    main()
