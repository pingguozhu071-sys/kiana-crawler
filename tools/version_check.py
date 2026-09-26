"""v2.16 M7 版本检查器：对比本地版本与 manifest 声明的版本（提示,不自动更新）

用法: python tools/version_check.py [--manifest PATH]

manifest 默认 <程序目录>/VERSION.json：{"latest": "2.16.0", "released": "2026-08-29"}
（无外网依赖；用户/发布方更新 manifest 即可提示新版；本工具绝不自动升级。）
"""
import sys, os, json
from pathlib import Path

LOCAL_VERSION = "2.17.0"
MANIFEST_NAME = "VERSION.json"


def _local():
    try:
        import kiana_vnext_plus
        return getattr(kiana_vnext_plus, "__version__", None) or LOCAL_VERSION
    except Exception:
        return LOCAL_VERSION


def _manifest(path=None):
    """读 VERSION.json（固定目录：程序目录 或 工程根；不存在返回 None）"""
    cands = []
    if path:
        cands.append(Path(path))
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        cands.append(Path(meipass) / MANIFEST_NAME)
    cands.append(Path(__file__).resolve().parent.parent / MANIFEST_NAME)
    for p in cands:
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return None
    return None


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Kiana 版本检查器（本地 manifest）")
    ap.add_argument("--manifest", default="", help="VERSION.json 路径（默认自动查找）")
    args = ap.parse_args()
    local = _local()
    m = _manifest(args.manifest or None)
    print(f"本地版本: {local}")
    if m is None or not m.get("latest"):
        print("未找到 VERSION.json（无发布方声明）——保持当前")
        return
    latest = str(m["latest"]).lstrip("v")
    if latest != local:
        print(f"manifest 最新: {latest}  → 有新版可升级（需手动安装,本工具不自动更新）")
        print(f"  发布时间: {m.get('released', '?')}")
    else:
        print("✅ 已是最新版")


if __name__ == "__main__":
    main()
