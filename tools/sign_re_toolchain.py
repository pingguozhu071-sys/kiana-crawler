"""签名逆向工具链 sign_re_toolchain.py（v2.17 S1，开发期工具——不进 Kiana 运行时）

定位：平台签名算法逆向的"开发机流水线"入口。Kiana 运行时红线不变（纯 Python 签名、
零 Node/Java 依赖）；本脚本只在开发机使用，负责：工具就绪检测、命令清单输出、
包清单（SHA256）生成、平台发版漂移检测。**本脚本不执行任何外部命令**——开发机按
打印出的清单逐条手敲（`dissect` 即输出命令 + 目录骨架；`manifest`/`drift-check`
走纯文件操作）。

工作流：
  1. dissect <apk> <platform>：检测 apktool/jadx/webcrack → 打印命令清单 → 建目录骨架
  2. 人工执行命令 + 阅读算法 → 写纯 Python + docs/algo.md + 测试向量
  3. manifest <platform>：生成包清单（SHA256）
  4. drift-check <platform> <old_manifest.json>：平台发版后 diff → 变更报告

用法示例：
  python tools/sign_re_toolchain.py dissect app.apk douyin
  python tools/sign_re_toolchain.py manifest douyin
  python tools/sign_re_toolchain.py drift-check douyin signature_extracts/douyin/manifest.json
"""
import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
EXTRACTS = PROJ / "signature_extracts"

TOOLS = {
    "apktool": "https://ibotpeaches.github.io/Apktool/（apktool.jar 放入 PATH）",
    "jadx": "https://github.com/skylot/jadx/releases（jadx.bat 放入 PATH）",
    "webcrack": "npm i -g webcrack（Node>=22，本机已有 v24）",
}


def _check_tools():
    missing = [t for t in TOOLS if not shutil.which(t)]
    if missing:
        print("[!] 缺少工具（仅开发机需要；不进 Kiana 运行时）：")
        for t in missing:
            print(f"    {t}: {TOOLS[t]}")
        print("    可继续（dissect 只建骨架+打印命令），建议装齐后手动执行")
    return missing


def _bundle_manifest(platform: str) -> dict:
    root = EXTRACTS / platform
    items = []
    for f in sorted(root.rglob("*")):
        if f.is_file() and f.suffix.lower() in (".js", ".html", ".json", ".bin"):
            items.append({
                "path": str(f.relative_to(root)).replace("\\", "/"),
                "size": f.stat().st_size,
                "sha256": hashlib.sha256(f.read_bytes()).hexdigest(),
            })
    return {"platform": platform, "generated": items}


def cmd_dissect(apk: str, platform: str):
    _check_tools()
    out = EXTRACTS / platform
    for sub in ("apk", "bundle", "deobf", "docs"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    print(f"[+] 目录骨架: {out}/（apk/bundle/deobf/docs）")
    print("[+] 待执行命令清单（开发机手动逐条执行——本脚本不 exec）：")
    print(f"    apktool d -f -o \"{out / 'apk'}\" \"{apk}\"")
    print(f"    webcrack \"{out / 'bundle'}\\<bundle.js>\" -o \"{out / 'deobf'}\"")
    print(f"    grep -ri 'encode\\|sign\\|JavascriptInterface' \"{out / 'apk'}\"")
    print("    手工产物建议：docs/algo.md（算法流程）+ 常量表 json + 测试向量 json")


def cmd_manifest(platform: str):
    if not (EXTRACTS / platform).exists():
        print(f"[!] {EXTRACTS / platform} 不存在——先 dissect")
        sys.exit(1)
    m = _bundle_manifest(platform)
    out = EXTRACTS / platform / "manifest.json"
    out.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[+] manifest 已生成: {out}（{len(m['generated'])} 个文件）")


def cmd_drift(platform: str, old_manifest: str):
    new = _bundle_manifest(platform)
    old = json.loads(Path(old_manifest).read_text(encoding="utf-8"))
    old_map = {i["path"]: i for i in old.get("generated", [])}
    new_map = {i["path"]: i for i in new["generated"]}
    added = sorted(set(new_map) - set(old_map))
    removed = sorted(set(old_map) - set(new_map))
    changed = sorted(p for p in set(old_map) & set(new_map)
                     if old_map[p]["sha256"] != new_map[p]["sha256"])
    print(f"平台 {platform} 变更报告：新增 {len(added)} / 删除 {len(removed)} / 修改 {len(changed)}")
    for p in changed:
        print(f"  ~ {p}（签名算法可能漂移！优先复核）")
    for p in added:
        print(f"  + {p}")
    for p in removed:
        print(f"  - {p}")
    if not changed and not added and not removed:
        print("  （无变化——签名版本未漂移）")


def main():
    ap = argparse.ArgumentParser(description="签名逆向工具链（开发机专用；不执行外部命令）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dissect", help="工具检测 + 命令清单 + 目录骨架")
    d.add_argument("apk")
    d.add_argument("platform")
    m = sub.add_parser("manifest", help="生成包清单（SHA256）")
    m.add_argument("platform")
    dr = sub.add_parser("drift-check", help="平台发版漂移检测")
    dr.add_argument("platform")
    dr.add_argument("old_manifest")
    args = ap.parse_args()
    if args.cmd == "dissect":
        cmd_dissect(args.apk, args.platform)
    elif args.cmd == "manifest":
        cmd_manifest(args.platform)
    elif args.cmd == "drift-check":
        cmd_drift(args.platform, args.old_manifest)


if __name__ == "__main__":
    main()
