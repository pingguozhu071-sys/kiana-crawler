"""隐身注入档位 A/B 实验（v2.11 D——站点×档位矩阵，用数据定每站注入强度）

对同一站点分别跑 裸渲染(render_simple) 与 全链求解(solve) 两种档位，
对比 status / HTML 长度，产出「站点×档位」矩阵。

已知事实（v2.10.5 实测）：贴吧 solve 全链 403/7KB vs render_simple 803KB——
JS 重站注入越裸越好；反爬重站（电商搜索等）则可能相反。

用法:
    python tools/stealth_ab.py --urls URL... [--runs N] [--wait W]
"""
import sys, os, asyncio, argparse, logging
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

DEFAULT_URLS = [
    "https://tieba.baidu.com/f?kw=%E9%AD%94%E6%B3%95%E5%B0%91%E5%A5%B3",
    "https://www.zhihu.com/explore",
    "https://www.bilibili.com/",
]


async def _run_mode(solver, url, mode, runs, wait):
    sizes, statuses, oks = [], [], 0
    for _ in range(runs):
        try:
            if mode == "simple":
                html, st, _ = await solver.render_simple(url, extra_wait=3.0)
            else:
                html, st, _ = await solver.solve(url, challenge_wait=25, extra_wait=3.0)
            sizes.append(len(html or ""))
            statuses.append(st)
            oks += 1 if (st < 400 and html and len(html) > 5000) else 0
        except Exception as e:
            statuses.append(-1)
            print(f"    {mode} 异常: {type(e).__name__}")
        await asyncio.sleep(wait)
    mean_size = sum(sizes) / len(sizes) if sizes else 0
    return {"ok": oks, "runs": runs, "mean_size": int(mean_size),
            "statuses": sorted(set(statuses))}


async def _ab(solver, url, runs, wait):
    print(f"\n═══ {url[:70]} ═══")
    simple = await _run_mode(solver, url, "simple", runs, wait)
    full = await _run_mode(solver, url, "solve", runs, wait)
    print(f"  [裸渲染 simple] 可达 {simple['ok']}/{simple['runs']}  均长 {simple['mean_size']}"
          f"  status={simple['statuses']}")
    print(f"  [全链 solve ] 可达 {full['ok']}/{full['runs']}  均长 {full['mean_size']}"
          f"  status={full['statuses']}")
    if simple["ok"] > full["ok"]:
        verdict = "★ 该站用裸渲染档（render_simple）"
    elif full["ok"] > simple["ok"]:
        verdict = "★ 该站用全链档（solve）"
    else:
        verdict = "★ 两档一致（按反爬需求选择 solve 挑战求解档）"
    print(f"  {verdict}")
    return {"url": url, "simple": simple, "full": full, "verdict": verdict}


async def _main(urls, runs, wait):
    from kiana_vnext_plus.config import GlobalConfig
    from kiana_vnext_plus.solver_engine import SolverEngine
    from omegaconf import OmegaConf
    gcfg = GlobalConfig(OmegaConf.create({"log_level": "INFO"}))
    solver = SolverEngine(pool_size=1, max_pages_per_context=3,
                          memory_limit_mb=4096, headless=True)
    await solver.init()
    matrix = []
    for url in urls:
        matrix.append(await _ab(solver, url, runs, wait))
    print("\n═══ 站点×档位矩阵 ═══")
    for m in matrix:
        print(f"  {urlparse(m['url']).netloc:<28} {m['verdict']}")


def main():
    ap = argparse.ArgumentParser(description="隐身注入档位 A/B 实验")
    ap.add_argument("--urls", nargs="+", default=None)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--wait", type=float, default=4.0)
    args = ap.parse_args()
    asyncio.run(_main(args.urls or DEFAULT_URLS, args.runs, args.wait))


if __name__ == "__main__":
    main()
