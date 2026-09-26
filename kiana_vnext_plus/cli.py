import asyncio
import argparse
import logging
import json
import os
from .identity import ProjectIdentity
# [FIXED & MODIFIED] v2.11 run 子命令（multiprocess_runner）已移除：多进程模式从未打包
# （spec excludes multiprocessing + cli 不进安装包），且 Windows SQLite 多写者共享同一
# frontier.db 无写者协调——诚实下线。单进程异步引擎已覆盖 20 核（crawler 全局并发自适应）。
# 保留 status/export/retry-dead 三个任务管理子命令。


def _read_master_password() -> str:
    """读取 master_password —— CookieArmory 的 Fernet 密钥源。

    [v2.19.7 安全·扫描发现] 原实现自建了一套密钥读取，有两个洞：
      1. 硬编码 `%LOCALAPPDATA%\\KianaVnextPlus`——便携模式（KIANA_PORTABLE=1）下密钥
         在 exe 同级 KianaData/，这里找不到 → 静默跳到下一档；
      2. 兜底是**公开字面量** `"kiana-fallback"`——任何看过源码的人都持有该"密钥"，
         这些机器上弹药库（全量 cookie）的 Fernet 加密等于没加密。
    现在统一走 `GlobalConfig`：同一份 data_root() 定位、DPAPI 解密、首次运行随机生成，
    与引擎/GUI 完全同源；公开兜底删除，取不到就明确失败，而不是悄悄降级成弱密钥。
    """
    try:
        from .config import GlobalConfig   # 顺带装配日志脱敏 Filter（config 模块级副作用）
        _pw = str(GlobalConfig().cfg.master_password or "").strip()
        if _pw and _pw != "your_strong_password":
            return _pw
    except Exception as e:
        logging.getLogger(__name__).warning(f"master 密钥经 config 读取失败，尝试环境变量: {e}")
    _env = os.environ.get("KIANA_MASTER_PASSWORD", "").strip()
    if _env:
        return _env
    raise SystemExit(
        "❌ 取不到 master_password：数据根不可用且未设置 KIANA_MASTER_PASSWORD。\n"
        "   拒绝回退到弱兜底密钥（那会让弹药库加密形同虚设）。请检查数据目录权限后重试。")


def main():
    # [v2.19.7 安全·扫描发现] CLI 链路此前**从不 import config**，而日志脱敏 Filter 的装配
    # 是 config 的模块级副作用（_install_handler_autosanitize 包装 logging.Handler.__init__
    # 给每个未来 handler 自动挂 _SanitizeLogFilter）→ 命令行跑 status/export 时完整 URL
    # （含 token/session/key）原样进 stdout。这里显式导入，让脱敏在 basicConfig 之前就位。
    try:
        from . import config as _cfg  # noqa: F401  （导入即装配脱敏）
    except Exception as _e:  # 脱敏装不上不该阻断 CLI，但必须留痕
        print(f"⚠️ 日志脱敏装配失败（继续运行）: {_e}")
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(processName)s %(name)s: %(message)s',
    )
    parser = argparse.ArgumentParser(description="Kiana Ultimate Hybrid Crawler")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status").add_argument("--project", required=True)

    export_parser = subparsers.add_parser("export")
    export_parser.add_argument("--project", required=True)
    # [v2.17 4.1] sqlite 导出后端（media/scrape 两表，本地面板/跨域汇总）
    export_parser.add_argument("--format", choices=["jsonl", "csv", "xlsx", "sqlite"], default="jsonl")

    retry_parser = subparsers.add_parser("retry-dead")
    retry_parser.add_argument("--project", required=True)

    # [FIXED & MODIFIED] v2.13 阶段3 errors 只写不读 → 失败分析视图
    errors_parser = subparsers.add_parser("errors", help="查看失败/错误档案（聚合分析）")
    errors_parser.add_argument("--project", required=True)
    errors_parser.add_argument("--limit", type=int, default=50)

    # [FIXED & MODIFIED] v2.13 阶段5 Cookie 弹药库管理（多账号加密入库+健康轮换）
    ca_add = subparsers.add_parser("cookie-add", help="账号 cookies 加密入库")
    ca_add.add_argument("--site", required=True, help="站点标识（如 bilibili）")
    ca_add.add_argument("--name", required=True, help="账号名（如 acc1）")
    ca_add.add_argument("--file", required=True, help="Netscape cookies.txt 路径")
    ca_list = subparsers.add_parser("cookie-list", help="查看弹药库账号健康状态")
    ca_list.add_argument("--site", default=None)

    # [v2.16 M6] rule-new：生成站点规则骨架（零代码接入新站点）
    rn = subparsers.add_parser("rule-new", help="生成站点规则骨架")
    rn.add_argument("--domain", required=True, help="域名（如 example.com）")
    # [v2.17 5.2] rule-validate / rule-test：规则文件校验与本地样例试跑
    rv = subparsers.add_parser("rule-validate", help="校验站点规则文件（YAML+必需键）")
    rv.add_argument("file", help="规则文件路径（rules/sites/*.yaml）")
    rt = subparsers.add_parser("rule-test", help="本地 HTML 样例测试规则抽取")
    rt.add_argument("file", help="规则文件路径")
    rt.add_argument("--url", required=True, help="样例页面 URL（match 判定用）")
    rt.add_argument("--html", default=None, help="本地 HTML 样例路径；缺省仅语法校验")

    args = parser.parse_args()

    if args.command == "status":
        import aiosqlite

        async def show():
            db_path = ProjectIdentity(args.project).get_db_path()
            async with aiosqlite.connect(db_path) as db:
                cursor = await db.execute("SELECT status, COUNT(*) FROM frontier GROUP BY status")
                async for row in cursor:
                    print(f"{row[0]}: {row[1]}")

        asyncio.run(show())

    elif args.command == "export":
        import aiosqlite

        async def do_export():
            project = ProjectIdentity(args.project)
            output = project.export_dir / f"data.{args.format}"
            async with aiosqlite.connect(project.get_db_path()) as db:
                cursor = await db.execute("SELECT data_json FROM extracted")
                rows = await cursor.fetchall()
                data = []
                for r in rows:
                    try:
                        data.append(json.loads(r[0]))
                    except (json.JSONDecodeError, TypeError):
                        continue
                if args.format == "jsonl":
                    with open(output, 'w', encoding='utf-8') as f:
                        for item in data:
                            f.write(json.dumps(item, ensure_ascii=False) + "\n")
                elif args.format == "csv" and data:
                    import csv
                    with open(output, 'w', newline='', encoding='utf-8') as f:
                        writer = csv.DictWriter(f, fieldnames=list(data[0].keys()))
                        writer.writeheader()
                        writer.writerows(data)
                elif args.format == "xlsx":
                    from .enhancements import DataExporter
                    n = DataExporter(project.export_dir).export_xlsx(output, data)
                    if n < 0:
                        print("xlsx 导出失败（详见日志）")
                        return
                elif args.format == "sqlite":
                    from .enhancements import DataExporter
                    n = DataExporter(project.export_dir).export_sqlite(output, data)
                    if n < 0:
                        print("sqlite 导出失败（详见日志）")
                        return
                print(f"exported to {output}")

        # [FIXED & MODIFIED] v2.17 冒烟抓到：export 分支定义 do_export 后从未 asyncio.run
        # ——整个 export 子命令死代码（exit=0 无输出无文件，老库也从未真正导出过）。
        # 补调用；防回归断言见 tests/test_v2162_data_rules.py
        asyncio.run(do_export())

    elif args.command == "cookie-add":
        from .cookie_armory import CookieArmory, parse_cookie_file
        db_path = ProjectIdentity(args.project).get_db_path()
        master = _read_master_password()
        cookie_str = parse_cookie_file(args.file)
        if not cookie_str:
            print("（cookies 文件为空或格式不识别）")
            return
        armory = CookieArmory(db_path, master)
        ok = armory.add_account(args.site, args.name, cookie_str)
        print(f"✅ 账号 {args.name}@{args.site} 已加密入库" if ok else "❌ 入库失败")

    elif args.command == "cookie-list":
        from .cookie_armory import CookieArmory
        db_path = ProjectIdentity(args.project).get_db_path()
        armory = CookieArmory(db_path, _read_master_password())
        rows = armory.list_accounts(args.site)
        if not rows:
            print("（弹药库为空——用 cookie-add 入库）")
            return
        print(f"{'站点':<14}{'账号':<16}{'健康分':<8}{'冷却':<10}{'成/败'}")
        for r in rows:
            cd = f"{r['cooldown_left']}s" if r["cooldown_left"] > 0 else "-"
            print(f"{r['site']:<14}{r['name']:<16}{r['health']:<8}{cd:<10}{r['success']}/{r['fail']}")

    elif args.command == "retry-dead":
        import time
        import aiosqlite

        async def retry():
            db_path = ProjectIdentity(args.project).get_db_path()
            async with aiosqlite.connect(db_path) as db:
                await db.execute(
                    "UPDATE frontier SET status='pending', retry_count=0, scheduled_at=? WHERE status='dead'",
                    (time.time(),))
                await db.commit()
            print("dead tasks reset")

        asyncio.run(retry())

    elif args.command == "errors":
        from .frontier import FrontierDB

        async def show_errors():
            db = FrontierDB(ProjectIdentity(args.project).get_db_path())
            await db.init_async()
            rows = await db.get_recent_errors(limit=args.limit)
            if not rows:
                print("（无错误记录）")
                return
            # 按类型聚合
            agg = {}
            for r in rows:
                agg[r["error_type"]] = agg.get(r["error_type"], 0) + 1
            print("═══ 错误类型分布 ═══")
            for k, v in sorted(agg.items(), key=lambda kv: -kv[1]):
                print(f"  {k}: {v}")
            print(f"\n═══ 最近 {len(rows)} 条 ═══")
            for r in rows:
                ts = time.strftime("%m-%d %H:%M", time.localtime(r["timestamp"])) if r["timestamp"] else "?"
                url = (r["normalized_url"] or "?")[:70]
                # [v2.17 4.3] platform/code 列（结构化审计"哪个平台什么码"）
                _tag = (r.get("platform") or "") + ("/" + str(r.get("code")) if r.get("code") else "")
                _t = f" [{_tag}]" if _tag else ""
                print(f"  [{ts}]{_t} {r['error_type']}  {url}")
                if r["error_message"]:
                    print(f"         {r['error_message'][:90]}")

        asyncio.run(show_errors())

    elif args.command == "rule-new":
        # [v2.16 M6] 生成站点规则骨架（site_rules.new_rule_template）
        try:
            import pathlib as _p
            from .site_rules import new_rule_template
            rules_dir = _p.Path(__file__).parent.parent / "rules" / "sites"
            rules_dir.mkdir(parents=True, exist_ok=True)
            out = rules_dir / f"{args.domain}.yaml"
            if out.exists():
                print(f"⚠️ {out.name} 已存在（覆盖写）")
            out.write_text(new_rule_template(args.domain), encoding="utf-8")
            print(f"✅ 规则骨架已生成: {out}")
            print("  编辑选择器后保存即生效（热加载）")
        except Exception as e:
            print(f"❌ 生成失败: {e}")

    elif args.command == "rule-validate":
        from .site_rules import validate_rule_file
        ok, msgs = validate_rule_file(args.file)
        for m in msgs:
            print(("❌ " if not ok else "✅ ") + m)
        if ok:
            print("规则文件有效（保存即生效，热加载）")

    elif args.command == "rule-test":
        import pathlib as _p
        from .site_rules import test_rule_url
        _html = ""
        if args.html:
            _html = _p.Path(args.html).read_text(encoding="utf-8-sig", errors="ignore")
        out = test_rule_url(args.url, _html, args.file)
        if not out["ok"]:
            for m in out["messages"]:
                print("❌ " + m)
            print("规则校验失败")
            return
        for m in out["messages"]:
            print("✅ " + m)
        if out["result"] is None:
            return
        import json as _j
        r = out["result"]
        print(f"抽取结果: name={r['name']} fields={list(r['fields'])} "
              f"images={len(r['images'])} list_items={len(r['list_items'])} "
              f"next_url={r['next_url']}")
        if r["fields"]:
            print(_j.dumps(r["fields"], ensure_ascii=False)[:500])


if __name__ == "__main__":
    main()
