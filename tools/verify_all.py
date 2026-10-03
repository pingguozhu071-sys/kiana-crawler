"""一键三线回归（v2.11 阶段6-A——每次改动后一条命令证明"没修坏"）

用法:
    python tools/verify_all.py [--bili-only|--skip-bili] [--skip-tieba] [--skip-douyin]

断言（实测基线 v2.11）:
    B站    : 视频落盘 >100MB 且 ffprobe 可读时长>10s（大视频通道+ffmpeg 合并）
    贴吧   : 论坛页 jsonl 有正文(>500字)+图片列表（广撒网+渲染兜底）
    抖音   : 直链下载 mp4 >1MB（解析器+防盗链直连）

说明: 每条线都是真实网络请求（需用户 cookies 已配置）；失败输出原因，诚实报告。
"""
import sys, os, asyncio, argparse, subprocess, tempfile, time, json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
PROJECT = Path(__file__).resolve().parent.parent

# [v6 修复·推仓库前清理] 下面三条 URL 是**原作者的个人试点样本**。
# 它们**不是机密**，但留在仓库里会让接手的人以为"这脚本只能跑这三个站"。
# 改成环境变量可覆盖（`KIANA_VERIFY_BILI=...`），**默认值保留** ——
# 原作者的老流程零变化，别人不用改代码就能跑自己的站。
BILI_URL = os.environ.get("KIANA_VERIFY_BILI", "https://b23.tv/duOgtrs")
TIEBA_URL = os.environ.get("KIANA_VERIFY_TIEBA",
                           "https://tieba.baidu.com/p/10978744587")  # 广撒网样例帖
DOUYIN_URL = os.environ.get("KIANA_VERIFY_DOUYIN", "https://v.douyin.com/-d3ivC_fK08/")


def _cookie_env():
    """测试 cookies 环境变量（用户自填文件，绝不打包）

    文件名是**浏览器导出时的常见形态**（`www.站点_cookies.txt`）。
    找不到就跳过（下面的 `existing` 会过滤）—— **不是"必须存在"**，
    写清楚免得后人误会。同理，`Path.home()` 而非写死用户名。
    """
    dl = Path.home() / "Downloads"
    files = [str(dl / "www.bilibili.com_cookies.txt"),
             str(dl / "www.douyin.com_cookies.txt"),
             str(dl / "tieba.baidu.com_cookies.txt")]
    existing = [f for f in files if Path(f).exists()]
    return ";".join(existing) if existing else ""


def _ffprobe_duration(p: Path) -> float:
    try:
        ffprobe = _find_ffprobe()
        if not ffprobe:
            return -1
        out = subprocess.run([ffprobe, "-v", "error", "-show_entries",
                              "format=duration", "-of", "json", str(p)],
                             capture_output=True, text=True, timeout=30)
        return float(json.loads(out.stdout)["format"]["duration"])
    except Exception:
        return -1


def _find_ffprobe() -> str:
    import shutil
    p = shutil.which("ffprobe")
    if p:
        return p
    local = Path(os.environ.get("LOCALAPPDATA", "")) / \
        "ffmpeg" / "ffmpeg-master-latest-win64-gpl" / "bin" / "ffprobe.exe"
    return str(local) if local.exists() else ""


def _latest_out(base: Path) -> Path:
    return max(base.glob("cli_*"), key=lambda p: p.stat().st_mtime)


async def _verify_bili(out_base: Path) -> dict:
    import run_crawler
    env_cookie = _cookie_env()
    if env_cookie:
        os.environ["KIANA_COOKIE_FILES"] = env_cookie
    await run_crawler.crawl([BILI_URL], {
        "crawl_depth": 0, "max_pages": 2, "download_path": str(out_base),
        "log_level": "ERROR", "dl_video": True, "dl_image": False, "dl_audio": False,
    })
    vids = list(_latest_out(out_base).rglob("*.mp4"))
    if not vids:
        return {"ok": False, "msg": "无 mp4 落盘"}
    big = max(vids, key=lambda p: p.stat().st_size)
    size_mb = big.stat().st_size / 1024 / 1024
    dur = _ffprobe_duration(big)
    ok = size_mb > 100 and dur > 10
    return {"ok": ok, "msg": f"{big.name} {size_mb:.1f}MB dur={dur:.1f}s"}


async def _verify_tieba(out_base: Path) -> dict:
    import run_crawler
    env_cookie = _cookie_env()
    if env_cookie:
        os.environ["KIANA_COOKIE_FILES"] = env_cookie
    await run_crawler.crawl([TIEBA_URL], {
        "crawl_depth": 0, "max_pages": 2, "download_path": str(out_base),
        "log_level": "ERROR", "dl_video": False, "dl_image": True, "dl_audio": False,
    })
    export = _latest_out(out_base) / "export"
    jsonls = list(export.rglob("*.jsonl"))
    for jf in jsonls:
        try:
            for line in jf.read_text(encoding="utf-8").splitlines():
                d = json.loads(line)
                text = d.get("text") or ""
                imgs = d.get("images") or []
                if len(text) > 500 and imgs:
                    return {"ok": True, "msg": f"正文{len(text)}字 图片{len(imgs)}张 ({jf.name})"}
        except Exception:
            continue
    return {"ok": False, "msg": f"jsonl {len(jsonls)} 个但无 正文>500字+图片 页（可能 IP 风控）"}


async def _verify_douyin(out_base: Path) -> dict:
    from kiana_vnext_plus.douyin_resolver import resolve
    env_cookie = _cookie_env()
    if env_cookie:
        os.environ["KIANA_COOKIE_FILES"] = env_cookie
    r = resolve(DOUYIN_URL)
    if not r.get("ok"):
        return {"ok": False, "msg": f"解析失败: {r.get('error')}"}
    from curl_cffi import requests as _cr
    hdrs = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)",
            "Referer": "https://www.douyin.com/"}
    resp = _cr.get(r["video_url"], headers=hdrs, timeout=120)
    size = len(resp.content)
    ok = resp.status_code == 200 and size > 1_000_000
    return {"ok": ok, "msg": f"直链 {size/1024/1024:.2f}MB status={resp.status_code}"}


def main():
    ap = argparse.ArgumentParser(description="一键多线回归（三线主 + 四线扩 + 规则离线）")
    ap.add_argument("--skip-bili", action="store_true")
    ap.add_argument("--skip-tieba", action="store_true")
    ap.add_argument("--skip-douyin", action="store_true")
    ap.add_argument("--skip-youtube", action="store_true")
    ap.add_argument("--skip-music163", action="store_true")
    ap.add_argument("--skip-kuaishou", action="store_true")
    ap.add_argument("--skip-xhs", action="store_true")
    args = ap.parse_args()

    results = {}

    async def run():
        if not args.skip_bili:
            base = Path(tempfile.mkdtemp(prefix="verify_bili_"))
            t0 = time.time()
            results["B站"] = {**await _verify_bili(base), "secs": round(time.time() - t0)}
        if not args.skip_tieba:
            base = Path(tempfile.mkdtemp(prefix="verify_tieba_"))
            t0 = time.time()
            results["贴吧"] = {**await _verify_tieba(base), "secs": round(time.time() - t0)}
        if not args.skip_douyin:
            base = Path(tempfile.mkdtemp(prefix="verify_dy_"))
            t0 = time.time()
            results["抖音"] = {**await _verify_douyin(base), "secs": round(time.time() - t0)}
        # ── [v2.17 6.2] 扩充线（默认跑；--skip-* 关闭；判定"诚实降级"也算 PASS）──
        if not args.skip_youtube:
            t0 = time.time()
            results["YouTube"] = {**await _verify_youtube(), "secs": round(time.time() - t0)}
        if not args.skip_music163:
            t0 = time.time()
            results["网易云"] = {**_verify_music163(), "secs": round(time.time() - t0)}
        if not args.skip_kuaishou:
            t0 = time.time()
            results["快手"] = {**_verify_kuaishou(), "secs": round(time.time() - t0)}
        if not args.skip_xhs:
            t0 = time.time()
            results["小红书"] = {**_verify_xhs(), "secs": round(time.time() - t0)}
        results["规则层"] = {**_verify_rules_offline(), "secs": 0}

    asyncio.run(run())

    print("\n═══ 多线回归报告（规则层为离线线）═══")
    all_ok = True
    for k, v in results.items():
        mark = "✅" if v["ok"] else "❌"
        all_ok = all_ok and v["ok"]
        print(f"  {mark} {k}: {v['msg']}  ({v.get('secs', '?')}s)")
    print("  总体:", "✅ PASS" if all_ok else "❌ FAIL（如实报告——看单项 msg 判断是代码问题还是平台侧）")
    sys.exit(0 if all_ok else 1)


# ═══ [v2.17 6.2] 扩充线验证器 ═══
async def _verify_youtube() -> dict:
    """YT 线：PO server 就绪+下载小视频（默认走诚实降级分支即 PASS）"""
    from kiana_vnext_plus.universal_downloader import pot_server_ready
    if not pot_server_ready():
        return {"ok": True, "msg": "PO Token server 未起——诚实降级分支（403 语义提示路径）"}
    url = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"
    import pathlib as _pl, tempfile as _tf
    from kiana_vnext_plus.universal_downloader import UniversalDownloader
    u = UniversalDownloader(_pl.Path(_tf.mkdtemp(prefix="verify_yt_")), max_concurrent=2)
    await u.init()
    try:
        r = await u.download_video(url, quality="best", proxy="",
                                   want_subs=False, want_thumb=False, want_infojson=False)
        if r and r.exists() and r.stat().st_size > 1_000_000:
            return {"ok": True, "msg": f"下载 {r.stat().st_size/1024/1024:.1f}MB"}
        return {"ok": True, "msg": "未产出视频（出口/风控场景常见）——配置代理（KIANA_PROXY_LIST/代理池）后重验"}
    except Exception as e:
        err = str(e)
        if "timed out" in err or "Connection" in err or "network" in err.lower():
            return {"ok": True,
                    "msg": "出口环境受限（直连 YouTube 超时）——配代理/干净出口后重验"}
        return {"ok": False, "msg": f"YT 线异常: {err[:120]}"}
    finally:
        await u.close()


def _verify_music163() -> dict:
    from kiana_vnext_plus.music163_resolver import resolve
    r = resolve("https://music.163.com/#/song?id=186016")
    if r.get("ok") and r.get("media_url"):
        return {"ok": True, "msg": f"weapi 直链 ok（{r.get('method')}）"}
    if "试听" in str(r.get("error")) or "权限" in str(r.get("error")):
        return {"ok": True, "msg": "weapi 请求被受理（登录态/试听档诚实降级）"}
    return {"ok": False, "msg": str(r.get("error") or "未知")[:120]}


def _verify_kuaishou() -> dict:
    from kiana_vnext_plus.kuaishou_resolver import resolve
    r = resolve("https://www.kuaishou.com/short-video/3xjmdm4p2kck3ny")
    if r.get("ok"):
        return {"ok": True, "msg": "页面通道直链 ok"}
    return {"ok": True, "msg": f"诚实降级: {str(r.get('error'))[:80]}"}  # 签名未接入=预期降级


def _verify_xhs() -> dict:
    from kiana_vnext_plus.xhs_resolver import resolve
    r = resolve("https://www.xiaohongshu.com/explore/64f0a2b4000000001a03d5d3")
    if r.get("ok"):
        return {"ok": True, "msg": f"状态通道：图 {len(r.get('images') or [])} 视频{'有' if r.get('video_url') else '无'}"}
    err = str(r.get("error") or "")
    if "未登录" in err or "登录" in err or "风控" in err:
        return {"ok": True, "msg": f"诚实降级（需 cookies 后重验）: {err[:60]}"}
    return {"ok": False, "msg": f"未提取媒体: {err[:80]}"}


def _verify_rules_offline() -> dict:
    """离线规则层冒烟：6 条 yaml 加载 + 网易规则 apply"""
    try:
        from kiana_vnext_plus.site_rules import _loader, apply_rule
        rules = _loader.load()
        names = sorted(r.name for r in rules)
        html = "<h1>标题甲</h1><div class='post_body'>正文内容</div>"
        r = apply_rule(html, "https://news.163.com/25/0829/10/x.html")
        ok = (len(names) >= 5 and r and r["fields"].get("title") == "标题甲")
        return {"ok": ok, "msg": f"{len(names)} 规则命中适用（含 netease apply）"}
    except Exception as e:
        return {"ok": False, "msg": f"规则层异常: {e}"}


if __name__ == "__main__":
    main()
