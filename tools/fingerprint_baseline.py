# -*- coding: utf-8 -*-
"""结构指纹基准：离线核对 / 重建 / 在线探测（门禁第 11 项的数据源与执行体）

**离线部分（默认，无网络）**
  重算 `tests/assets/*.html` 的**结构**指纹，与入库基准
  `tests/assets/fingerprint_baseline.json` 逐一比对。它能抓到两类静默失真：
    ① 样本被替换、截断、编码改坏 → 指纹变；
    ② 指纹算法被改动 → 指纹变（结构探针的判据在无声中漂移）。
  这是 M2 的关键防线：探针的口径一旦漂移，后面所有"结构变了"的告警都不可信。

**在线部分（`--online`，需显式开启）**
  对运行期数据根里的 `probe/targets.json`（`{key: url}`，**不在仓库内**）逐条探测。
  取不到（无网络 / 被闸拦 / 全部失败）→ 退出码 **2 = 数不出来**，
  由门禁映射为 **SKIP**——**绝不冒充 PASS**。

用法:
  python tools/fingerprint_baseline.py            # 离线核对（门禁用）
  python tools/fingerprint_baseline.py --write     # 重建基准（必须写明原因！）
  python tools/fingerprint_baseline.py --online    # 加跑在线探测
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BASELINE = ROOT / "tests" / "assets" / "fingerprint_baseline.json"
ASSETS = ROOT / "tests" / "assets"

EXIT_OK = 0
EXIT_MISMATCH = 1        # 离线基准不符 → 门禁 FAIL
EXIT_CANNOT_MEASURE = 2  # 在线数不出来 → 门禁 SKIP


def _compute() -> dict:
    from kiana_vnext_plus.fingerprint_probe import structure_fingerprint
    out = {}
    for p in sorted(ASSETS.glob("*.html")):
        try:
            html = p.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            print(f"  ! 读取失败 {p.name}: {e}")
            continue
        out[p.name] = structure_fingerprint(html)
    return out


def _load_baseline() -> dict:
    try:
        data = json.loads(BASELINE.read_text(encoding="utf-8"))
        return data.get("samples", {}) if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"  ! 基准文件损坏: {e}")
        return {}


def check_offline() -> int:
    cur = _compute()
    base = _load_baseline()
    if not base:
        print("❌ 基准缺失或损坏：tests/assets/fingerprint_baseline.json")
        return EXIT_MISMATCH
    zero = [k for k, v in cur.items() if not v]
    if zero:
        print(f"❌ 有样本算不出结构指纹（解析失败/空页）: {zero}")
        return EXIT_MISMATCH
    miss = sorted(set(base) - set(cur))
    extra = sorted(set(cur) - set(base))
    drift = sorted(k for k in set(base) & set(cur) if int(base[k]) != int(cur[k]))
    if miss:
        print(f"❌ 基准里的样本已不存在: {miss}")
    if extra:
        print(f"❌ 新增样本未入基准（跑 --write 重建并写明原因）: {extra}")
    for k in drift:
        print(f"❌ 指纹漂移 {k}: 基准={base[k]} 实测={cur[k]}")
    if miss or extra or drift:
        return EXIT_MISMATCH
    print(f"✅ 结构指纹离线基准一致（{len(cur)} 个样本）")
    return EXIT_OK


def write_baseline() -> int:
    cur = _compute()
    data = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else {}
    if "_说明" not in data:
        print("❌ 基准文件缺少 _说明 段——拒绝覆盖（说明段是给后来者看的，不能丢）")
        return EXIT_MISMATCH
    data["samples"] = cur
    BASELINE.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"✅ 基准已重建（{len(cur)} 个样本）——**请在提交信息里写明为什么重建**")
    return EXIT_OK


async def _probe_all(targets: dict) -> dict:
    from curl_cffi import requests as _cq
    from kiana_vnext_plus.fingerprint_probe import StructureProbe
    probe = StructureProbe()
    session = _cq.AsyncSession(impersonate="chrome136", timeout=20)
    results = {}
    try:
        for key, url in targets.items():
            try:
                results[key] = await probe.probe(key, url, session=session)
            except Exception as e:                 # 探测本身不许中断整轮
                results[key] = {"key": key, "verdict": "probe_failed",
                                "detail": f"未捕获异常: {type(e).__name__}"}
    finally:
        try:
            await session.close()
        except Exception:
            pass
    return results


def run_online() -> int:
    from kiana_vnext_plus.config import data_root
    tf = Path(str(data_root())) / "probe" / "targets.json"
    if not tf.exists():
        print(f"⏭️ SKIP：未配置在线探针目标（{tf} 不存在）——不存在不是失败，是没配")
        return EXIT_CANNOT_MEASURE
    try:
        targets = json.loads(tf.read_text(encoding="utf-8"))
        targets = {k: v for k, v in targets.items() if isinstance(v, str) and v}
    except Exception as e:
        print(f"⏭️ SKIP：目标文件不可解析（{e}）")
        return EXIT_CANNOT_MEASURE
    if not targets:
        print("⏭️ SKIP：目标文件为空")
        return EXIT_CANNOT_MEASURE

    res = asyncio.run(_probe_all(targets))
    by = {}
    for r in res.values():
        by[r.get("verdict")] = by.get(r.get("verdict"), 0) + 1
    for k, r in sorted(res.items()):
        print(f"  {r.get('verdict'):<13} {k}: {r.get('detail','')}")

    measured = {k: v for k, v in by.items() if k != "probe_failed"}
    if not measured:
        # 全部取不到 = **数不出来**，不是"没问题"
        print("⏭️ SKIP：全部目标取不到（无网络/被拦）——**不计入判定**，也不冒充 PASS")
        return EXIT_CANNOT_MEASURE
    changed = by.get("changed", 0)
    print(f"在线探针：{by} —— 结构变化 {changed} 个（首轮**只报不阻断**，由作者决定）")
    return EXIT_OK


def main() -> int:
    ap = argparse.ArgumentParser(description="结构指纹基准核对/重建/在线探测")
    ap.add_argument("--write", action="store_true", help="重建离线基准")
    ap.add_argument("--online", action="store_true", help="加跑在线探测（需 targets.json）")
    args = ap.parse_args()

    if args.write:
        return write_baseline()
    rc = check_offline()
    if rc != EXIT_OK or not args.online:
        return rc
    return run_online()


if __name__ == "__main__":
    sys.exit(main())
