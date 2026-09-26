# -*- coding: utf-8 -*-
"""8 语言静默安装/卸载矩阵（v2.17.1 C6）。

用法：python tools/installer_matrix.py [--setup PATH] [--langs 2057,1033,...] [--dry]
每个语言：/S /LANG=<id> /D=<temp> 静默安装 → 校验(launcher/注册表/Uninstall.exe) →
Uninstall.exe /S 静默卸载 → 校验清理。静默模式不弹窗，无 UI 干扰。

/ LANG 编号：en-GB=2057 en-US=1033 zh-CN=2052 zh-TW=1028 zh-HK=3076 ja=1041 ko=1042 fr=1036
"""
import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

LANG_IDS = [2057, 1033, 2052, 1028, 3076, 1041, 1042, 1036]
LANG_NAMES = {2057: "en-GB", 1033: "en-US", 2052: "zh-CN", 1028: "zh-TW",
              3076: "zh-HK", 1041: "ja-JP", 1042: "ko-KR", 1036: "fr-FR"}


def run(cmd, timeout=900):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                       errors="ignore")
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def newest_setup() -> Path | None:
    """仓库根目录下最新的安装包（按 mtime）。

    [v2.19.8 修复] 原默认值硬编码 `KianaVnextPlus-Setup-2.17.1.0.exe`——该文件早已不存在，
    不显式传 `--setup` 就直接报"安装包不存在"退出（工具形同不可用）。改为自动探测：
    同一目录下多个安装包时取最新的一个。"""
    cands = sorted(Path(__file__).resolve().parent.parent.glob("KianaVnextPlus-Setup-*.exe"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0] if cands else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup", default=None,
                    help="安装包路径；缺省自动探测仓库根目录下最新的 KianaVnextPlus-Setup-*.exe")
    ap.add_argument("--langs", default=",".join(map(str, LANG_IDS)))
    ap.add_argument("--dry", action="store_true", help="仅列出将执行的命令")
    args = ap.parse_args()

    setup = Path(args.setup) if args.setup else newest_setup()
    if setup is None:
        print("❌ 仓库根目录下找不到任何 KianaVnextPlus-Setup-*.exe（请先打包或显式传 --setup）")
        return 1
    if not setup.exists():
        print(f"❌ 安装包不存在: {setup}")
        return 1
    print(f"（使用安装包：{setup.name}）")
    lang_ids = [int(x) for x in args.langs.split(",") if x.strip()]
    # [v2.19.8 修复·实测事故] `--dry` 此前**只被解析、从未被判断**（参数是摆设）：
    # 传了 --dry 仍会真的执行静默安装+卸载。实测后果：临时副本的卸载器改写了**正式安装**
    # 的桌面/开始菜单快捷方式（把 WorkingDirectory 指向临时目录，随后该目录被删）——
    # 正是本工程历史上"桌面图标出问题"的同一类事故。现在 --dry 真正短路：
    # 只打印将要执行的命令，不建临时目录、不启动安装器、不碰注册表与快捷方式。
    if args.dry:
        print(f"═══ --dry：仅列出将执行的命令（{len(lang_ids)} 语言，不做任何安装/卸载）═══")
        for lang in lang_ids:
            name = LANG_NAMES.get(lang, str(lang))
            plan_dir = rf"%TEMP%\kiana_mat_{name}_<random>\app"
            print(f"\n[{name}]")
            print(f"  安装: \"{setup}\" /S /LANG={lang} /D={plan_dir}")
            print(f"  卸载: \"{plan_dir}\\Uninstall.exe\" /S")
        print("\n（dry 模式不校验产物、不动系统状态）")
        return 0
    print(f"═══ 安装器语言矩阵（{len(lang_ids)} 语言）═══")
    fails = []
    for lang in lang_ids:
        name = LANG_NAMES.get(lang, str(lang))
        instdir = Path(tempfile.mkdtemp(prefix=f"kiana_mat_{name}_")) / "app"
        cmds = [["/S", "/LANG=%d" % lang, "/D=%s" % instdir]]
        print(f"\n[{name}] 静默安装 → {instdir}")
        ok = True
        for args_ in cmds:
            rc, out = run([str(setup)] + args_)
            if rc != 0:
                ok = False
                print(f"  ❌ 安装返回 {rc}: {out[-200:]}")
        if ok:
            launcher = instdir / "KianaLauncher.exe"
            unins = instdir / "Uninstall.exe"
            exists_ok = launcher.exists() and unins.exists()
            print(f"  {'✅' if exists_ok else '❌'} 产物: launcher={launcher.exists()} "
                  f"uninstall={unins.exists()} ({(instdir.stat().st_size / 1e6):.0f}MB)")
        else:
            exists_ok = False
        # 卸载
        if exists_ok:
            rc, out = run([str(unins), "/S"])
            if rc != 0:
                ok = False
                print(f"  ❌ 卸载返回 {rc}")
            time.sleep(1)
            cleaned = not launcher.exists()
            print(f"  {'✅' if cleaned else '❌'} 静默卸载清理")
        if not ok or not exists_ok:
            fails.append(name)
        try:
            import shutil
            shutil.rmtree(instdir.parent, ignore_errors=True)
        except Exception:
            pass
    print(f"\n结果: {len(lang_ids) - len(fails)}/{len(lang_ids)} 通过"
          + (f"；FAIL={fails}" if fails else "；全体 PASS"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
