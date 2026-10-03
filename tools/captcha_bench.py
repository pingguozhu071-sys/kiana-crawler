"""验证码/反爬挑战闭环实测（v2.11 重构——原 v2.10.6 A3 单 URL 版升级）

用法:
    python tools/captcha_bench.py --urls URL1 URL2 ... [--runs N] [--wait W]
        [--mode solve|simple] [--tune]

功能:
    1. 多站清单、逐 URL 统计：可达率 / 挑战类型分布 / 求解成功率（激活原死字段）
    2. 真实样本自动收集：每轮 page.html + page.png 存到项目内固定目录
       tests/assets/captcha_samples/<时间戳>/（挑战样本库；样本由 SolverEngine 落盘，
       路径为项目内固定根 + 纯时间戳，零用户/URL 派生组件）
    3. --tune 网格搜索滑块阈值（slider_vision.SLIDER_PARAMS 运行时更新），输出对比表
    4. 结果仅 stdout 报告（不落盘——实测数据与样本目录打印在控制台，可复盘）

诚实边界：若站点对当前 IP 做全站风控（无挑战直接 403/风控页），通过率为平台侧限制，
工具照实报告——不是代码缺陷（v2.10.6 实测贴吧 IP 临时风控即此情形）。
"""
import sys, os, asyncio, argparse, logging, datetime
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

# 样本库固定根目录（项目内；样本子目录 = 纯时间戳，路径无任何用户/URL 派生组件）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLES_ROOT = PROJECT_ROOT / "tests" / "assets" / "captcha_samples"

# 默认站点清单（真实挑战高发站）
DEFAULT_URLS = [
    "https://tieba.baidu.com/f?kw=%E9%AD%94%E6%B3%95%E5%B0%91%E5%A5%B3",
    "https://www.zhihu.com/explore",
    "https://www.toutiao.com/",
]

# 滑块阈值网格（template 模板匹配相关性 × edge 边缘匹配相关性）
TUNE_GRID = [
    {"template_threshold": 0.55, "edge_threshold": 0.40},
    {"template_threshold": 0.60, "edge_threshold": 0.45},
    {"template_threshold": 0.65, "edge_threshold": 0.50},
]


def _tag(url: str) -> str:
    try:
        return (urlparse(url).netloc or "site").replace("www.", "").replace(".", "_")[:40]
    except Exception:
        return "site"


def _new_sample_dir() -> str:
    """新建样本子目录：<SAMPLES_ROOT>/<纯时间戳>（路径零用户/URL 派生组件）"""
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    d = SAMPLES_ROOT / ts
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


async def _once(solver, url: str, mode: str, wait: float, tag: str) -> dict:
    tracker = {}
    try:
        if mode == "simple":
            html, st, _ = await solver.render_simple(url, extra_wait=3.0)
        else:
            sample = _new_sample_dir()
            html, st, _ = await solver.solve(url, challenge_wait=25, extra_wait=3.0,
                                             tracker=tracker, save_sample=sample)
        ok = bool(st < 400 and html and len(html) > 5000)
        return {"ok": ok, "status": st, "size": len(html or ""),
                "challenge": tracker.get("challenge_type"),
                "solved": tracker.get("challenge_solved")}
    except Exception as e:
        return {"ok": False, "status": 0, "size": 0, "error": type(e).__name__}
    finally:
        await asyncio.sleep(wait)


async def _bench_urls(solver, urls, runs, wait, mode):
    rows = []
    for url in urls:
        ok_n = fail_n = 0
        challenges = {}
        for i in range(runs):
            r = await _once(solver, url, mode, wait, _tag(url))
            if r["ok"]:
                ok_n += 1
            else:
                fail_n += 1
            if r.get("challenge"):
                k = r["challenge"]
                challenges.setdefault(k, [0, 0])
                challenges[k][0] += 1
                if r.get("solved"):
                    challenges[k][1] += 1
            print(f"  [{i+1}/{runs}] {url[:60]} → {'✅' if r['ok'] else '❌'}"
                  f" status={r['status']} len={r['size']}"
                  + (f" 挑战={r['challenge']} 求解={'Y' if r.get('solved') else 'N'}"
                     if r.get('challenge') else "")
                  + (f" ({r['error']})" if r.get('error') else ""))
        rate = ok_n / runs * 100 if runs else 0
        print(f"  ══ {url[:60]}: 可达 {ok_n}/{runs} ({rate:.0f}%) 达标={'YES✅' if rate >= 70 else 'NO❌'}")
        for k, (seen, solved) in challenges.items():
            print(f"       挑战[{k}] 出现 {seen} 次 求解成功 {solved} ({solved/seen*100:.0f}%)")
        rows.append({"url": url, "mode": mode, "runs": runs, "ok": ok_n,
                     "rate_pct": round(rate, 1), "challenges": str(challenges)})
    return rows


async def _tune(solver, url, runs, wait):
    from kiana_vnext_plus.slider_vision import set_slider_params, get_slider_params
    orig = get_slider_params()
    print(f"\n═══ 滑块阈值网格搜索: {url[:60]} ═══")
    table = []
    try:
        for combo in TUNE_GRID:
            set_slider_params(**combo)
            ok_n = 0
            for i in range(runs):
                r = await _once(solver, url, "solve", wait, _tag(url))
                ok_n += 1 if r["ok"] else 0
            rate = ok_n / runs * 100 if runs else 0
            table.append((combo, rate))
            print(f"  template={combo['template_threshold']} edge={combo['edge_threshold']}"
                  f" → 可达 {ok_n}/{runs} ({rate:.0f}%)")
    finally:
        set_slider_params(**orig)
    best = max(table, key=lambda t: t[1]) if table else None
    if best:
        print(f"  ★ 最优组合: template={best[0]['template_threshold']} "
              f"edge={best[0]['edge_threshold']} ({best[1]:.0f}%)")
    print("  （网格搜索只对滑块阈值生效；无滑块挑战的站点各组合结果应一致——诚实基线）")


def main():
    ap = argparse.ArgumentParser(description="验证码闭环实测")
    ap.add_argument("--urls", nargs="+", default=None, help="目标 URL 列表（默认贴吧/知乎/头条）")
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--wait", type=float, default=5.0)
    ap.add_argument("--mode", choices=("solve", "simple"), default="solve")
    ap.add_argument("--tune", action="store_true", help="网格搜索滑块阈值（对 --urls 第一个 URL）")
    args = ap.parse_args()

    urls = args.urls or DEFAULT_URLS

    from kiana_vnext_plus.config import GlobalConfig
    from kiana_vnext_plus.solver_engine import SolverEngine
    from omegaconf import OmegaConf

    # [v6] 同 stealth_ab：**不是死代码**——`GlobalConfig.__init__` 首跑会生成/加载
    # `master.key.bin`。去掉赋值只为消除 ruff F841，调用本身要保留。
    GlobalConfig(OmegaConf.create({"log_level": "INFO"}))
    solver = SolverEngine(pool_size=1, max_pages_per_context=3,
                          memory_limit_mb=4096, headless=True)

    async def _main():
        await solver.init()
        if args.tune:
            await _tune(solver, urls[0], args.runs, args.wait)
        rows = await _bench_urls(solver, urls, args.runs, args.wait, args.mode)
        print("\n═══ 汇总 ═══")
        for r in rows:
            print(f"  {r['url'][:60]}  mode={r['mode']}  可达 {r['ok']}/{r['runs']}"
                  f" ({r['rate_pct']}%)  挑战分布: {r['challenges']}")
        print(f"\n样本目录根: {SAMPLES_ROOT}")

    asyncio.run(_main())


if __name__ == "__main__":
    main()
