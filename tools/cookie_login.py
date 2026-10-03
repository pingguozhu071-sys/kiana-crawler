# -*- coding: utf-8 -*-
"""拿登录态的命令行入口：**打开专用浏览器 → 手动登录 → cookies.txt 自动就位**。

用法（在仓库根目录跑）：

    python tools/cookie_login.py --list-sites                 # 看支持哪些站
    python tools/cookie_login.py --site bilibili --dry-run     # 只打印路径，不开浏览器
    python tools/cookie_login.py --site bilibili               # ← 主角：弹窗口，登录，关掉
    python tools/cookie_login.py --status                      # 看各站配置档现状
    python tools/cookie_login.py --site bilibili --export-only # 从已存配置档重导 cookies.txt
    python tools/cookie_login.py --merge                       # 把各站 cookies.txt 合成一份

为什么是 `tools/` 而不是接进 `kiana_vnext_plus/cli.py`：
`cli.py` 是**引擎任务管理**子命令（status/export/errors/rule-*/cookie-add），
本脚本是**用户一次性的登录动作**，且它会**弹一个有头浏览器** ——
把它塞进引擎 CLI 会让"跑个 status 就可能弹窗"这种事发生。放在 `tools/` 与
`yt_download.py` / `live_regression.py` 这些"用户本地手动用具"同类；
真正的模块逻辑在 `kiana_vnext_plus/cookie_profile.py`（GUI 下一轮直接 import 那个模块）。

## 退出码（`release_check` 那套语义）

    0 = 拿到并写出了 cookies.txt
    1 = 跑完了，但没拿到登录态（没登录成功 / 配置档里本来就没有）
    2 = 用法或环境错误（站名不认识、没装浏览器引擎、参数组合非法）
"""
import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Windows 控制台默认 GBK，本脚本要打印中文路径与站点名 —— 不重设编码会
# UnicodeEncodeError（或在某些终端下变成乱码）。与 run_crawler / cookie_health
# 的同款做法一致（`cookie_health.py:161`）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

from kiana_vnext_plus import cookie_profile as cp  # noqa: E402


def _fmt_path(p) -> str:
    return str(p) if p else "—"


def cmd_list_sites() -> int:
    """打印登记表。**同时打出可用站名**，因为 `--site` 的报错信息也依赖这张表。"""
    print("支持的站点（--site 可传短名或域名）：\n")
    print(f"  {'短名':<12} {'站点':<14} {'域名':<34} 登录页")
    for opt in cp.site_options():
        print(f"  {opt['site_key']:<12} {opt['label']:<14} "
              f"{','.join(opt['domains']):<34} {opt['login_url']}")
    print(f"\n配置档根目录：{cp.profiles_root()}")
    return 0


def cmd_status() -> int:
    """只看磁盘上的配置档现状（**不开浏览器**）—— 排查"到底有没有登录过"用。"""
    print(f"配置档根目录：{cp.profiles_root()}\n")
    print(f"  {'短名':<12} {'cookies.txt':<13} {'storage_state':<15} 路径")
    for opt in cp.site_options():
        key = opt["key"]
        pdir = cp.profile_dir(key)
        ck = pdir / "cookies.txt"
        st = cp.storage_state_path(key)
        state = cp.load_storage_state(key)
        n_cookie = len(cp.state_cookies(state)) if state else 0
        # 只报"有没有"和条数，**绝不打印任何 cookie 值**
        ck_txt = f"有({n_cookie}条)" if ck.exists() else "无"
        st_txt = f"有({n_cookie}条)" if st.exists() else "无"
        print(f"  {opt['site_key']:<12} {ck_txt:<13} {st_txt:<15} {pdir}")
    return 0


def cmd_merge() -> int:
    """把各站 cookies.txt 合成一份（yt-dlp / `_ensure_cookie_file` 只吃一个文件）。"""
    files = []
    for opt in cp.site_options():
        p = cp.profile_dir(opt["key"]) / "cookies.txt"
        if p.exists():
            files.append(p)
    if not files:
        print("没有任何配置档导出过 cookies.txt —— 先跑 --site <站名> 登录一次")
        return 1
    out = cp.profiles_root() / "cookies.all.txt"
    text, n = cp.merge_cookie_files(files, out=out)
    print(f"已合并 {len(files)} 份 → {out}（{n} 条 cookie）")
    for f in files:
        print(f"  · {f}")
    print("\n把它交给引擎：")
    print(f'  set "KIANA_COOKIE_FILE={out}"')
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="cookie_login",
        description="用持久化浏览器配置档获取登录态（替代手动导出 cookies.txt）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--site", default=None,
                    help="站点：短名（bilibili/douyin/tieba/kuaishou/xiaohongshu/wechat_mp）"
                         "或域名（www.bilibili.com）")
    ap.add_argument("--url", default=None, help="覆盖默认登录页")
    ap.add_argument("--timeout", type=float, default=None,
                    help="最长等待秒数（默认一直等，直到你关掉浏览器窗口）")
    ap.add_argument("--wait-login", action="store_true",
                    help="登录凭据一出现就自动收工关窗口（默认：等你手动关窗口）")
    ap.add_argument("--out", default=None, help="cookies.txt 落盘路径（默认写到该站配置档目录里）")
    ap.add_argument("--export-only", action="store_true",
                    help="不开浏览器，直接从已存配置档重新导出 cookies.txt")
    ap.add_argument("--list-sites", action="store_true", help="列出支持的站点")
    ap.add_argument("--status", action="store_true", help="看各站配置档现状（不开浏览器）")
    ap.add_argument("--merge", action="store_true", help="把各站 cookies.txt 合成一份")
    ap.add_argument("--dry-run", action="store_true",
                    help="只解析并打印路径/登录页，不开浏览器（自测用）")
    args = ap.parse_args(argv)

    # 这三个是"独立动作"，不需要 --site
    if args.list_sites:
        return cmd_list_sites()
    if args.status:
        return cmd_status()
    if args.merge:
        return cmd_merge()
    if not args.site:
        ap.error("必须给 --site（或用 --list-sites / --status / --merge）")

    # ── 站名解析失败 = 用法错误（退出码 2），且把可用站点列出来 ──
    try:
        key = cp.resolve_site(args.site)
    except ValueError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 2
    meta = cp.SITE_PROFILES[key]
    # ⚠️ `profile_dir(create=True)` 是**有副作用**的调用，不能放在 `--dry-run` 分支之前
    # 无条件执行 —— `--dry-run` 承诺"只看不碰"（它是我们唯一能离线自测的路径，
    # 有副作用就等于自测本身会改环境）。
    create = not (args.dry_run or args.export_only)
    pdir = cp.profile_dir(key, create=create)
    out = Path(args.out) if args.out else pdir / "cookies.txt"

    if args.dry_run:
        # 自测路径：**完整走一遍解析与路径计算，只是不开浏览器**。
        # 没有这条，验证"CLI 能不能独立跑通"就必须真弹窗。
        print(f"站点      : {meta['label']}（{key} → 配置档目录名 {meta['site_key']}）")
        print(f"配置档    : {pdir}")
        print(f"登录页    : {args.url or meta['login_url']}")
        print(f"cookies   : {out}")
        print(f"state     : {cp.storage_state_path(key)}")
        print(f"浏览器引擎: patchright={cp.HAS_PATCHRIGHT} playwright={cp.HAS_PLAYWRIGHT}")
        print("（--dry-run：不启动浏览器）")
        return 0

    if args.export_only:
        got = cp.export_profile_cookies(key, out=out)
        if got is None:
            print(f"配置档里没有可用 cookie：{cp.storage_state_path(key)}\n"
                  f"先跑一次 `python tools/cookie_login.py --site {args.site}` 登录。",
                  file=sys.stderr)
            return 1
        print(f"已导出 → {got}")
        return 0

    if not (cp.HAS_PATCHRIGHT or cp.HAS_PLAYWRIGHT):
        print("没装浏览器引擎：patchright 与 playwright 都 import 不到。\n"
              "本工程栈里应当有 patchright；没有就先装它。", file=sys.stderr)
        return 2

    print(f"即将打开 {meta['label']} 的专用浏览器窗口（配置档：{pdir}）")
    print("登录完成后**直接关掉那个窗口**，cookies 就会自动写出来。")
    res = asyncio.run(cp.open_profile_for_login(
        key, url=args.url, timeout_s=args.timeout,
        wait_for_login=args.wait_login, out=out))

    print(f"\n状态      : {res['status']}"
          f"{'（提前检测到登录态）' if res['login_detected'] else ''}")
    print(f"cookie 数 : {res['cookie_count']}（localStorage origin {res['origins']} 个）")
    print(f"cookies   : {_fmt_path(res['cookies_path'])}")
    print(f"storage   : {_fmt_path(res['state_path'])}")
    if res["note"]:
        print(f"说明      : {res['note']}")

    if not res["ok"]:
        return 1
    print("\n交给引擎（二选一）：")
    print(f'  set "KIANA_COOKIE_FILE={res["cookies_path"]}"')
    print(f"  或把 {res['cookies_path']} 填进 GUI 的 cookies 路径框")
    return 0


if __name__ == "__main__":
    sys.exit(main())
