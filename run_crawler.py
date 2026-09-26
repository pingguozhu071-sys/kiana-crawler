"""Kiana Vnext Plus — CLI Crawler v2
Usage: python run_crawler.py <URL> [-d depth] [-m max_pages] [-o output]
"""
import sys
import os
import asyncio
import time
import argparse
import json
from pathlib import Path

ENGINE = Path(__file__).parent.resolve()
if ENGINE.exists():
    sys.path.insert(0, str(ENGINE))

# [FIXED & MODIFIED] v2.11 KIANA_CRYPTO_KEY 机制整体删除（identity.py Fernet 链零消费者）。

# [FIXED & MODIFIED] v2.6.8 强制 UTF-8 locale（安装版独立进程默认 gbk → yt-dlp 调 ffmpeg 合并时
# subprocess 读 ffmpeg 输出（B站 dash 流 metadata 含 UTF-8 中文）→ gbk 解码崩溃 → 合并失败 →
# 分离流被清理 → 作者"空文件夹"根因。Windows 不支持 C.UTF-8，用 en_US.UTF-8（已验证 setlocale 成功）。
import locale as _locale
try:
    _locale.setlocale(_locale.LC_CTYPE, 'en_US.UTF-8')
except Exception:
    pass
import os as _os
_os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
# [FIXED & MODIFIED] v2.6.10 终极保险：monkey-patch subprocess 默认编码为 utf-8——
# 无论 locale 是否生效，text 模式 subprocess（ffmpeg 合并等）一律 utf-8 解码，杜绝 gbk 崩溃。
import subprocess as _subprocess
_orig_popen_init = _subprocess.Popen.__init__
def _popen_init_utf8(self, *args, **kwargs):
    if kwargs.get('text') or kwargs.get('universal_newlines'):
        kwargs.setdefault('encoding', 'utf-8')
        kwargs.setdefault('errors', 'replace')
    _orig_popen_init(self, *args, **kwargs)
_subprocess.Popen.__init__ = _popen_init_utf8

def _load_user_env():
    """从 Windows 用户注册表加载环境变量（进程环境缺失时兜底，如从 GUI 启动）"""
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Environment") as key:
                for name in ("DEEPSEEK_API_KEY", "PLAYWRIGHT_BROWSERS_PATH",
                             "PATCHRIGHT_BROWSERS_PATH"):
                    if name not in os.environ:
                        try:
                            val, _ = winreg.QueryValueEx(key, name)
                            if val:
                                os.environ[name] = str(val)
                        except OSError:
                            pass
        except Exception:
            pass

_load_user_env()

def _detect_browser_path() -> str:
    """自动探测可用的浏览器安装路径（打包内 _MEIPASS→环境变量→标准路径→Hermes cache）
    [FIXED & MODIFIED] v2.16 阶段1 可移植性：优先用打包内的 browsers/（换机无需手动安装）"""
    from pathlib import Path as _P
    meipass = getattr(sys, "_MEIPASS", "")
    candidates = [
        str(_P(meipass) / "browsers") if meipass else "",
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""),
        os.environ.get("PATCHRIGHT_BROWSERS_PATH", ""),
        str(_P(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"),
        # [FIXED & MODIFIED] F5：硬编码本机绝对路径改为动态拼 LOCALAPPDATA（用户名变化后仍可用）
        str(_P(os.environ.get("LOCALAPPDATA", "")) / "Hermes Agent CN Desktop" / "data" / "hermes-home" / "cache" / "ms-playwright"),
    ]
    for p in candidates:
        if p and _P(p).is_dir() and any(_P(p).glob("chromium*")):
            if meipass and p == candidates[0]:
                print("[Kiana] 使用打包内 Chromium（换机即用浏览器渲染）")
            return p
    return ""

bp = _detect_browser_path()
if bp:
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", bp)
    os.environ.setdefault("PATCHRIGHT_BROWSERS_PATH", bp)

_defaults = {
    "crawl_depth": 5, "max_pages": 500,
    "download_path": str(Path.home() / "Downloads" / "KianaVnextPlus"),
    "log_level": "INFO",
}

BAR_W = 30
def bar(pct, w=BAR_W):
    f = int(w * pct / 100)
    return "=" * f + "-" * (w - f)


def _maybe_llm_enhancer(cfg):
    """[v2.16.1] LLM 客户端条件构建（默认关）：llm_enabled 且 Key/地址齐全才构建；
    返回 None 时引擎不调 LLM（开关关闭=零开销，与旧行为一致）。"""
    if not cfg.get("llm_enabled"):
        return None
    key = str(cfg.get("llm_key") or "").strip()
    base = str(cfg.get("llm_api_base") or "").strip()
    model = str(cfg.get("llm_model") or "").strip()
    if not (key and base):
        return None
    try:
        from kiana_vnext_plus.llm_client import LLMClient
        return LLMClient({"format": "openai", "base_url": base, "api_key": key,
                          "model": model or "deepseek-v4-flash"})
    except Exception as e:
        print(f"[warn] LLM 客户端构建失败: {e}")
        return None

def page_timeout_override(cfg: dict) -> dict:
    """[v2.19.8] CLI/GUI 的 `page_timeout` → GlobalConfig 覆盖项。

    **只在该键被显式指定时**才返回覆盖字典：键不存在 = 沿用 `config.DEFAULT_GLOBAL`
    的唯一默认源（300s）。这样既让 `--page-timeout 0`（关闭看门狗）与配置文件里的
    `page_timeout` 真正生效，又不会在别处硬编码第二个默认值。
    非法值（非整数）按"未指定"处理并告警——不让一个手滑的字符串把引擎的兜底关掉。
    """
    if not cfg or "page_timeout" not in cfg:
        return {}
    try:
        return {"page_timeout": max(0, int(cfg["page_timeout"]))}
    except (TypeError, ValueError):
        print(f"[warn] page_timeout 非法值已忽略（沿用配置默认）: {cfg.get('page_timeout')!r}")
        return {}


async def crawl(urls, cfg, on_engine=None):
    t0 = time.monotonic()
    from kiana_vnext_plus.config import GlobalConfig
    from kiana_vnext_plus.identity import ProjectIdentity
    from kiana_vnext_plus.crawler import Crawler
    from omegaconf import OmegaConf
    import hashlib

    pid = "cli_" + hashlib.sha256(urls[0].encode()).hexdigest()[:8]
    dl = Path(cfg["download_path"])
    dl.mkdir(parents=True, exist_ok=True)

    gcfg = GlobalConfig(OmegaConf.create({
        "log_level": cfg["log_level"], "download_path": str(dl),
        "privacy_sanitize": not cfg.get("no_sanitize", False),
        "video_download_enabled": cfg.get("dl_video", True),
        "image_download_enabled": cfg.get("dl_image", True),
        "audio_download_enabled": cfg.get("dl_audio", False),
        # [v2.16.1] 副产物开关 GUI 化（原 config 死键，GUI 一直无入口）
        "download_subtitles": cfg.get("subs", True),
        "download_thumbnail": cfg.get("thumb", True),
        "write_info_json": cfg.get("info", True),
        # [v2.17 E-P1] robots 合规 / sitemap 播种 / 爬行策略（默认关/bfs=既有行为）
        "robots_respect": cfg.get("robots_respect", False),
        "sitemap_discover": cfg.get("sitemap_discover", False),
        "crawl_strategy": str(cfg.get("crawl_strategy", "bfs")),
        # [v2.17 E-P2] 身份捆绑轮换（默认关；开启时出口+cookie 打包虚拟用户）
        "identity_bundle": cfg.get("identity_bundle", False),
        # [FIXED & MODIFIED] v2.17 稳定性门禁：显式键表缺 llm_budget_month → GUI 预算
        # 恒落 DEFAULT（500）不生效；补传导（crawler 端 llm_enrich 消费）
        "llm_budget_month": int(cfg.get("llm_budget_month", 500)),
        # [v2.17 B4b] 证据驱动动态优先级（默认关）
        "dynamic_priority": cfg.get("dynamic_priority", False),
        # [v2.19.7 安全·扫描发现] 打码密钥透传（GUI→EngineBridge→此处→crawler→SolverEngine）。
        # 此前这条链在 EngineBridge 翻译表断掉（GUI 三框能填能存但引擎恒空），且密钥以明文
        # 存 launcher_config.json；现由 launcher_v8.load_captcha_keys 从 DPAPI 密文读取后传入。
        "captcha_api_keys": cfg.get("captcha_api_keys") or {},
        # [v2.19.8 修复] 单页看门狗：**只显式指定时**才进键表。原表里根本没有这个键，
        # 于是 `--page-timeout 0` 与配置文件里的 page_timeout 全部失效，引擎恒用
        # DEFAULT_GLOBAL 的 300s（"关不掉看门狗"）。
        # 不写死默认值是为了保留 config.DEFAULT_GLOBAL 作为唯一默认源。
        **page_timeout_override(cfg),
    }))

    # [FIXED & MODIFIED] v2.11 产物保鲜：历史任务目录自动清理（默认 7 天/50GB，config 可调；
    # 正在运行（<2h 新建）的目录永不触碰）
    try:
        from kiana_vnext_plus.enhancements import prune_task_dirs
        prune_task_dirs(dl,
                        keep_days=int(gcfg.get("task_retention_days", 7)),
                        max_gb=float(gcfg.get("task_retention_max_gb", 50)))
    except Exception:
        pass

    project = ProjectIdentity(pid, base_dir=dl, ephemeral=True)
    project.config = OmegaConf.merge(project.config, {
        "limits": {"max_depth": cfg["crawl_depth"], "max_pages": cfg["max_pages"]},
        "download_path": str(dl),
        # [v2.16.1] 画质档 GUI 化（preferred_resolution 原只是配置/CLI 死键）
        "video_settings": {"preferred_resolution": cfg.get("resolution", "highest")},
    })

    c = Crawler(project, gcfg, worker_id="cli")
    # [FIXED & MODIFIED] v2.11 引擎引用回调：GUI 进程内引擎原在 run_until_complete 返回后
    # 才拿到 crawler 对象——运行期间恒 None，「停止」从未走过软停止路径。on_engine 在
    # 创建后立即交出引用（暂停/继续/软停止全部依赖它）。
    if on_engine is not None:
        try:
            on_engine(c)
        except Exception:
            pass
    # 内容过滤白名单（来自 launcher 设置）
    if cfg.get("filter_words"):
        c._crawl_filters = {"keywords": [w.strip() for w in cfg["filter_words"].split(",") if w.strip()]}
    # [v2.16.1] LLM 恢复接入（默认关——GUI 设置页开关；Key/地址不齐不构建，静默跳过）
    c._llm_enhancer = _maybe_llm_enhancer(cfg)
    await c.setup()

    # [FIXED & MODIFIED] v2.10.5c 贴吧/论坛快速路径：setup 的浏览器池预热太慢会把贴吧
    # 拖到超时，而贴吧静态必 403（防爬首屏）→ 用轻量单浏览器 render_simple 直接渲染拿正文，
    # 结果与主循环等价（render_simple 已验证 803KB 贴吧正文）。仅当种子含贴吧 URL 时触发。
    try:
        _forum_urls = [u for u in urls if any(k in u.lower() for k in ('tieba.baidu.com', 'zhihu.com', 'xiaojuzi'))]
        if _forum_urls and getattr(c.solver, '_browser_available', True) and hasattr(c.solver, 'render_simple'):
            from kiana_vnext_plus.parser import extract_metadata
            from kiana_vnext_plus.sanitizer import sanitize_text
            import json as _json
            print(f"  [论坛快速路径] 检测到 {len(_forum_urls)} 个论坛链接，浏览器渲染采集…")
            for _fu in _forum_urls:
                _html, _st, _ = await c.solver.render_simple(_fu, extra_wait=4.0)
                if _html and len(_html) > 20000:
                    # [FIXED & MODIFIED] v2.10.5 P2-10 论坛快速路径脱敏对齐主路径：先在原始 HTML
                    # 上 sanitize_text（description/author/images 派生自已脱敏数据——原仅脱 text 后
                    # 提取，description/author 绕过 → 实测 jsonl 有 email 命中的 gap）
                    if getattr(c, '_sanitize_enabled', True):
                        _html = sanitize_text(_html)
                    _data = extract_metadata(_html, _fu)
                    _data['url'] = _fu
                    _data['rendered'] = True
                    # [FIXED & MODIFIED] v2.10.5c 贴吧图片过滤：extract_metadata 会收框架图标
                    # /头像（home_icon/portrait/pc-main-core），只保留帖子真图（tiebapic/himg/img）
                    _data['images'] = [u for u in (_data.get('images') or [])
                                       if any(k in u.lower() for k in ('tiebapic.baidu.com', 'himg.bdimg.com', 'imgsrc.baidu.com'))
                                       or 'forum/w=' in u.lower()]
                    from urllib.parse import urlparse as _up
                    _dom = (_up(_fu).netloc or 'unknown').replace('www.', '')
                    # [FIXED & MODIFIED] v2.10.6 B4 落盘统一：改走 exporter.add_jsonl（与主路径
                    # 同格式同位置 export/data/<域名>/<时间戳>.jsonl——原自写文件绕 exporter）
                    try:
                        c.exporter.add_jsonl(_dom, _data)
                    except Exception:
                        _out = project.export_dir / 'data' / _dom
                        _out.mkdir(parents=True, exist_ok=True)
                        from datetime import datetime as _dt
                        with open(_out / (_dt.now().strftime('%Y%m%d_%H%M%S') + '.jsonl'), 'a', encoding='utf-8') as _f:
                            _f.write(_json.dumps(_data, ensure_ascii=False) + '\n')
                    print(f"  [论坛快速路径] ✅ {_fu[:45]} → title={str(_data.get('title',''))[:30]} text_len={len(_data.get('text',''))}")
                else:
                    print(f"  [论坛快速路径] ⚠️ {_fu[:45]} 渲染失败 (status={_st})")
    except Exception as _fe:
        print(f"  [论坛快速路径] 异常: {_fe}（继续走主流程）")

    print(f"\n{'='*60}")
    print(f"  Kiana Vnext Plus | URL: {urls[0][:45]}")
    print(f"  depth={cfg['crawl_depth']}  max={cfg['max_pages']}  out={dl}")
    print(f"{'='*60}\n")

    # [FIXED & MODIFIED] v2.14 阶段4 日志落盘：主 CLI 入口原零 logging 配置——引擎
    # logger.info（抓取链路关键日志）整批被丢弃，logs/ 目录永远是空的（与使用说明
    # 矛盾）。现挂 RotatingFileHandler（10MB×5）兑现承诺；级别接 log_level 死键。
    try:
        import logging as _logging
        from logging.handlers import RotatingFileHandler as _RFH
        _lv = getattr(cfg, "get", lambda k, d=None: None)  # cfg 是 engine_cfg dict
        _lvl_name = str(cfg.get("log_level", "INFO")).upper()
        _lvl = getattr(_logging, _lvl_name, _logging.INFO)
        _log_dir = dl / "logs"
        _log_dir.mkdir(parents=True, exist_ok=True)
        _fh = _RFH(str(_log_dir / "crawl.log"), maxBytes=10 * 1024 * 1024,
                   backupCount=5, encoding="utf-8")
        _fh.setFormatter(_logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        # [v2.19 安全 P0] 文件 handler 必须挂脱敏 Filter——否则含 ?token= 的 URL /
        # API Key 形态会明文落盘 crawl.log（原 Filter 挂在 root logger 上对子 logger 无效）
        try:
            from kiana_vnext_plus.config import attach_log_sanitizer
            attach_log_sanitizer(_fh)
        except Exception:
            pass
        _root = _logging.getLogger()
        _root.setLevel(_lvl)
        if not any(isinstance(h, _RFH) for h in _root.handlers):
            _root.addHandler(_fh)
    except Exception:
        pass

    last_titles = []
    async def heartbeat():
        while True:
            await asyncio.sleep(1.5)
            p = getattr(c, "_progress", {})
            elapsed = time.monotonic() - t0
            d, f2, pn = p.get("done", 0), p.get("failed", 0), p.get("pending", 0)
            tot = p.get("total", 0)
            pct = d / tot * 100 if tot else 0
            speed = d / elapsed if elapsed > 0 else 0
            print(f"  [{elapsed:5.0f}s] {bar(pct)} {pct:5.1f}%  done={d} fail={f2} pend={pn}  {speed:.1f}p/s")
            # [FIXED & MODIFIED] v2.14 心跳降本：原 dl.rglob("*.json") 全产物树递归（含视频/图片
            # 目录，文件多时数百 ms 且阻塞 loop）→ 限定 data 子目录 + 挪线程池
            try:
                jf_list = await asyncio.to_thread(
                    lambda: sorted((dl / "export" / "data").rglob("*.json"),
                                   key=lambda x: x.stat().st_mtime, reverse=True)[:8])
            except Exception:
                continue
            for jf in jf_list[:5]:
                if jf.name not in last_titles:
                    try:
                        # [FIXED & MODIFIED] F13：open 加 with（原 json.load(open()) 句柄泄漏）
                        with open(jf, encoding="utf-8") as _jf:
                            jd = json.load(_jf)
                        t = jd.get("title", "").strip()
                        if t and len(t) > 2:
                            print(f"    [+] {t[:55]}")
                            last_titles.append(jf.name)
                    except Exception: pass

    ht = asyncio.create_task(heartbeat())
    try:
        await c.run(urls)
    except KeyboardInterrupt:
        print("\n[Interrupted]")
        c._should_stop = True
    finally:
        ht.cancel()
        try: await ht
        except asyncio.CancelledError: pass
        # 兜底关闭引擎资源（进程内模式必需：aiohttp session 等），幂等
        try:
            await c._graceful_shutdown()
        except Exception:
            pass

    elapsed = time.monotonic() - t0
    p = getattr(c, "_progress", {})
    all_json = list(dl.rglob("*.json"))
    print(f"\n{'='*60}")
    print(f"  Done in {elapsed:.0f}s | {p.get('done',0)} pages | {len(all_json)} JSON files")
    print(f"{'='*60}\n")
    return c  # 供 GUI 进程内引擎软停止（c._should_stop）

def main():
    # Windows: Proactor 事件循环比默认 Selector 性能高 20-30%（大量 socket 并发时）
    # [FIXED & MODIFIED] v2.9.0 Python 3.14+ 默认即 Proactor——显式设置触发
    # DeprecationWarning（3.16 起 slated for removal）——仅 <3.14 才手动设置
    if sys.platform == "win32" and sys.version_info < (3, 14):
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        except Exception as e:
            print(f"[warn] Proactor policy unavailable: {e}")

    ap = argparse.ArgumentParser(description="Kiana Vnext Plus CLI Crawler")
    ap.add_argument("urls", nargs="*", help="URL(s)")
    ap.add_argument("-f", "--file", help="URL file")
    ap.add_argument("-d", "--depth", type=int, help="Depth")
    ap.add_argument("-m", "--max-pages", type=int, help="Max pages")
    ap.add_argument("-o", "--output", help="Output directory")
    ap.add_argument("--no-sanitize", action="store_true",
                    help="关闭内容脱敏（保留手机号/邮箱/IP 原始数据）")
    ap.add_argument("--no-video", action="store_true", help="不下载视频")
    ap.add_argument("--no-image", action="store_true", help="不下载图片")
    ap.add_argument("--dl-audio", action="store_true", help="下载音频")
    ap.add_argument("--filter", default="", help="内容过滤白名单关键词（逗号分隔）")
    ap.add_argument("--cookie-file", default="", action="store",
                    help="用户自填 cookies.txt 路径（分号分隔多文件；不打包进程序）")
    ap.add_argument("--no-llm", action="store_true", help="禁用 LLM 智能增强（已移除，兼容保留）")
    ap.add_argument("--sitemap", action="store_true",
                    help="[v2.17] 开启 sitemap 种子播种（站点地图自动发现并批量入队）")
    ap.add_argument("--robots", action="store_true",
                    help="[v2.17] 开启 robots.txt 合规（链接入队前按域判定）")
    ap.add_argument("--strategy", choices=("bfs", "dfs", "bff"), default="bfs",
                    help="[v2.17] 爬行策略：bfs 广度(默认)/dfs 深度近似/bff 优先值降序")
    ap.add_argument("--dynamic-priority", action="store_true",
                    help="[v2.17 B4b] 证据驱动动态优先级（规则命中提前/空壳后排，默认关）")
    ap.add_argument("--page-timeout", type=int, default=None,
                    help="[v2.17 3-A] 单页处理超时看门狗（秒；0=关闭；超时击杀并标重试；"
                         "不传则用配置默认 300s）")
    args = ap.parse_args()

    cfg = dict(_defaults)
    # [FIXED & MODIFIED] v2.16 -d 0 语义：原 `if args.depth:` 对 0 为假 → 深度 0（只爬种子）
    # 被静默忽略成默认深度 5（实测 -d 0 跟出 98 个子任务）
    if args.depth is not None: cfg["crawl_depth"] = args.depth
    if args.max_pages is not None and args.max_pages > 0: cfg["max_pages"] = args.max_pages
    if args.output: cfg["download_path"] = args.output
    if args.no_sanitize: cfg["no_sanitize"] = True
    if args.no_video: cfg["dl_video"] = False
    if args.no_image: cfg["dl_image"] = False
    if args.dl_audio: cfg["dl_audio"] = True
    if args.filter: cfg["filter_words"] = args.filter
    if args.cookie_file:
        os.environ["KIANA_COOKIE_FILES"] = args.cookie_file
        os.environ.pop("KIANA_COOKIE_FILE", None)
    if args.no_llm: cfg["llm_enabled"] = False
    # [v2.17 E-P1] sitemap/robots 开关透传（默认关）
    if args.sitemap: cfg["sitemap_discover"] = True
    if args.robots: cfg["robots_respect"] = True
    cfg["crawl_strategy"] = args.strategy  # bfs 默认（既有行为）
    # [v2.17 B4b] 证据驱动动态优先级（默认关）
    if args.dynamic_priority:
        cfg["dynamic_priority"] = True
    # [v2.19.8 修复·作者发现的漂移] 原为 `if args.page_timeout > 0`：帮助文本承诺"0=关闭"，
    # 但显式传 0 时键根本没进 cfg（argparse 默认也是 0，两者不可区分）→ 引擎读到的是
    # DEFAULT_GLOBAL 的 300s，**看门狗关不掉**（声明与行为不符）。
    # 现：argparse 默认改 None（=未指定，沿用配置默认），显式传值（含 0）一律透传。
    if args.page_timeout is not None:
        cfg["page_timeout"] = max(0, int(args.page_timeout))

    urls = list(args.urls)
    # [FIXED & MODIFIED] v2.6.0 URL 清洗：分享文本整段粘贴时提取 http(s) URL（与 GUI 同逻辑）
    import re as _re
    def _clean_urls(raw_list):
        out = []
        for line in raw_list:
            line = str(line).strip()
            if not line:
                continue
            m = _re.search(r'https?://[^\s\u4e00-\u9fff]+', line)
            if m:
                out.append(m.group(0).rstrip('.,;:!?。，；：！？)】》"\'' ))
            elif '://' in line:
                out.append(line)
        return out
    urls = _clean_urls(urls)
    if args.file:
        fp = Path(args.file)
        if fp.exists():
            # [FIXED & MODIFIED] F13：open 加 with（句柄泄漏）
            with open(fp, encoding="utf-8") as _uf:
                urls.extend(_clean_urls(_uf.readlines()))

    if not urls:
        print("Usage: python run_crawler.py <URL> [-d depth] [-m max] [-o dir]")
        sys.exit(1)

    asyncio.run(crawl(urls, cfg))

if __name__ == "__main__":
    main()
