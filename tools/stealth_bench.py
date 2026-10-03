# -*- coding: utf-8 -*-
"""反爬量化靶场：把"能不能过检测"变成**可回归的数字**（M3-c）

**为什么先做它**：方案把"第二内核"排在其后是有道理的——
先要回答一个问题：**到底有多少站点是因为内核指纹而失败的？**
如果占比很低，就没必要加一条产品线（以及那 ~300MB 二进制与一堆要重校准的阈值）。
**先量化，再决定**。

**借用边界（本项目的借鉴纪律）**：站点清单与"对照实验打分"这个**方法**是公开做法，
本项目按自己的命名与结构实现；**不复制任何上游代码、不引入其二进制、注释不点名上游**。
站点 URL 本身是事实，不是表达。

**三类结果，语义必须分清**（这是本文件最容易写错的地方）：
  · `pass`   —— 该检测站判为"正常浏览器"
  · `fail`   —— 该检测站抓到了自动化特征
  · `unknown`—— 打不开/判据取不到（**无网络、被拦、站点改版都算**）
**只有 pass/fail 参与打分**；`unknown` 一律排除。若**全部 unknown** → 退出码 2
= **数不出来**（门禁映射为 SKIP），**绝不冒充"分数 0"**——那会把"没测"说成"很差"，
反过来也会把"没测"说成"很好"。

**纪律**：得分**只许升不许降**（对齐工程"静态基线只许降"的既有做法）。

用法:
  python tools/stealth_bench.py                 # 跑一轮并与基线比（需浏览器与公网）
  python tools/stealth_bench.py --write-baseline
"""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

EXIT_OK = 0
EXIT_REGRESSION = 1
EXIT_CANNOT_MEASURE = 2

# 公开检测站清单（只借"清单与方法"，实现为本工程自写）
SITES = (
    {"key": "sannysoft", "url": "https://bot.sannysoft.com"},
    {"key": "incolumitas", "url": "https://bot.incolumitas.com"},
    {"key": "rebrowser", "url": "https://bot-detector.rebrowser.net/"},
    {"key": "browserscan", "url": "https://www.browserscan.net/bot-detection"},
    {"key": "deviceinfo", "url": "https://deviceandbrowserinfo.com/are_you_a_bot"},
    {"key": "creepjs", "url": "https://abrahamjuliot.github.io/creepjs/"},
)

PASS, FAIL, UNKNOWN = "pass", "fail", "unknown"


def baseline_path(root=None) -> str:
    d = str(root) if root else os.path.join(str(_data_root()), "bench")
    return os.path.join(d, "stealth_baseline.json")


def _data_root():
    from kiana_vnext_plus.config import data_root
    return data_root()


def summarize(results: dict) -> dict:
    """`{key: verdict}` → `{score, passed, failed, unknown, measured, total}`。

    **score 只用 pass/fail 算**：`unknown` 不进分母。
    若一个都没测出来，`score` 为 **None**（而不是 0）——"没测"与"很差"必须可区分。
    """
    passed = sum(1 for v in results.values() if v == PASS)
    failed = sum(1 for v in results.values() if v == FAIL)
    unknown = sum(1 for v in results.values() if v not in (PASS, FAIL))
    measured = passed + failed
    return {"passed": passed, "failed": failed, "unknown": unknown,
            "measured": measured, "total": len(results),
            "score": (passed / measured) if measured else None}


def compare(baseline: dict, current: dict) -> dict:
    """与基线比 → `{regressed: [...], improved: [...], score_delta, ok}`。

    **只许升不许降**：分数线下降即 `ok=False`。
    另外**逐站**报告掉了哪些站（只报总分不够——总分相同也可能是一升一降互相抵消）。
    """
    base_sites = baseline.get("sites", {}) if isinstance(baseline, dict) else {}
    regressed = sorted(k for k, v in current.items()
                       if base_sites.get(k) == PASS and v == FAIL)
    improved = sorted(k for k, v in current.items()
                      if base_sites.get(k) == FAIL and v == PASS)
    bscore, cscore = baseline.get("score"), summarize(current)["score"]
    delta = None
    if isinstance(bscore, (int, float)) and isinstance(cscore, (int, float)):
        delta = round(cscore - bscore, 4)
    ok = True
    if regressed:
        ok = False
    if delta is not None and delta < 0:
        ok = False
    return {"regressed": regressed, "improved": improved,
            "score_delta": delta, "ok": ok}


def run_bench(probe) -> dict:
    """对清单逐站调用 `probe(url) -> verdict`。单站异常**不中断整轮**，记为 unknown。

    [v6 更正] 我加 `--kernel` 时曾把它改成 `run_bench(probe, kernel)` 并调
    `probe(url, kernel)` —— 那**打破了本函数的契约**：既有调用方（含测试）
    传进来的探针只接 `url`，于是 `TypeError` 被下面的 `except` 吞成 unknown，
    两个既有测试当场变红（`test_collects_verdicts` / `test_probe_exception_...`）。
    **正确做法**：本函数**不该知道内核**这回事 —— 由 `main()` 用闭包把 kernel 注入进去。
    参数少一个，契约反而更稳。
    """
    results = {}
    for site in SITES:
        try:
            v = probe(site["url"])
        except Exception:
            v = UNKNOWN
        results[site["key"]] = v if v in (PASS, FAIL) else UNKNOWN
    return results


def _default_probe(url: str, kernel: str = "patchright") -> str:
    """用**指定内核**打开检测站并读判据。

    ⚠️ **本函数需要浏览器与公网**（真跑那一轮才是它的验证）。
    `kernel`：`patchright`（现状基线）或 `camoufox`（第二内核）——
    两者**只差内核**，其余探测逻辑逐字相同，所以分数差异可归因到内核。
    """
    import asyncio

    async def _collect(pg) -> str:
        await pg.goto(url, wait_until="domcontentloaded", timeout=30000)
        await pg.wait_for_timeout(8000)          # 让检测脚本跑完
        text = (await pg.content()).lower()
        bad = ("webdriver", "automation detected", "you are a bot", "headless")
        good = ("you are human", "normal", "not detected")
        if any(g in text for g in good):
            return PASS
        if any(x in text for x in bad):
            return FAIL
        return UNKNOWN

    async def _go_patchright():
        from patchright.async_api import async_playwright
        async with async_playwright() as p:
            b = await p.chromium.launch(headless=False)
            try:
                return await _collect(await b.new_page())
            finally:
                await b.close()

    async def _go_camoufox():
        from camoufox.async_api import AsyncCamoufox
        # `geoip=True`：按出口 IP 自动匹配 locale/时区/地理（这正是 camoufox 的卖点之一）
        async with AsyncCamoufox(headless=False, geoip=True) as b:
            return await _collect(await b.new_page())

    try:
        if kernel == "camoufox":
            return asyncio.run(_go_camoufox())
        return asyncio.run(_go_patchright())
    except Exception as e:
        import sys as _s
        print(f"    （{kernel} 起浏览器失败: {type(e).__name__}: {str(e)[:90]}）", file=_s.stderr)
        return UNKNOWN


def _load(path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(path, data) -> bool:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        return True
    except Exception as e:
        print(f"⚠️ 基线写入失败: {e}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="反爬量化靶场（得分只许升不许降）")
    ap.add_argument("--write-baseline", action="store_true", help="把本轮结果记为基线")
    ap.add_argument("--path", default=None, help="基线文件路径（默认运行期数据根）")
    # [v6 P3] 内核可切换 —— 原来写死 patchright，"换了 camoufox 有没有变好"没法回答。
    # 两次跑**只差这一个变量**，其余探测逻辑逐字相同，所以分数差异可归因到内核。
    ap.add_argument("--kernel", choices=("patchright", "camoufox"), default="patchright",
                    help="用哪个浏览器内核跑（默认 patchright=现状；camoufox=第二内核）")
    args = ap.parse_args()

    path = args.path or baseline_path()
    if args.kernel != "patchright":
        # 换内核时默认另存一份基线，**不覆盖**原基线
        # （否则"换了内核分数掉了"会把原基线冲掉，再也比不出退步）
        path = path.replace(".json", f".{args.kernel}.json")
    print(f"内核：{args.kernel}    基线文件：{path}")
    print("⚠️ 会弹出浏览器窗口（检测站需要真实渲染环境）")
    # [v6 更正] 用闭包把 kernel 注入，而不是把它塞进 run_bench 的签名
    results = run_bench(lambda u: _default_probe(u, args.kernel))
    s = summarize(results)
    for k, v in sorted(results.items()):
        print(f"  {v:<8} {k}")

    if s["measured"] == 0:
        # 全部取不到 = **数不出来**，不是"0 分"
        print("⏭️ SKIP：全部站点未取到判据（无浏览器/无网络/被拦）——**不计入评分**，"
              "也不冒充 PASS 或 FAIL")
        return EXIT_CANNOT_MEASURE

    print(f"\n得分 {s['passed']}/{s['measured']} = {s['score']:.2f}"
          f"（unknown {s['unknown']} 个已排除）")

    if args.write_baseline:
        _save(path, {"sites": results, "score": s["score"], "kernel": args.kernel})
        print(f"✅ 基线已写入：{path}")
        return EXIT_OK

    base = _load(path)
    if not base:
        print(f"⚠️ 尚无基线（{path}）——先跑 --write-baseline 建立基线")
        return EXIT_OK
    cmp = compare(base, results)
    print(f"对比基线：Δ得分={cmp['score_delta']} "
          f"退步={cmp['regressed'] or '无'} 进步={cmp['improved'] or '无'}")
    if not cmp["ok"]:
        print("❌ 反爬得分**退步**了（本工程只许升不许降）——查指纹/启动参数是否被改动")
        return EXIT_REGRESSION
    print("✅ 得分未退步")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
