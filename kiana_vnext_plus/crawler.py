import asyncio
import uuid
import logging
import os
import re
import time
from typing import List
from .config import GlobalConfig
from .identity import ProjectIdentity
from .frontier import FrontierDB
from .concurrency import ConcurrencyController
from .session_pool import SessionPool
from .protocol_engine import ProtocolEngine
from .solver_engine import SolverEngine
from .engine_router import EngineRouter
from .media_downloader import MediaDownloader
from .m3u8_downloader import M3U8Downloader, sanitize_video_filename
from .universal_downloader import UniversalDownloader
from .smart_adaptive import SmartAdaptiveController
from .page_processor import PageProcessor
from .exit_manager import ExitManager
from .defense_protocols import ShieldSoldier, SentinelBrain, AmeNoHabakiri, OracleBrain

logger = logging.getLogger(__name__)


def _safe_int_cfg(val, default: int) -> int:
    """[v2.18 P2-13] GUI/配置传来的数值可能是 "30.5"/"abc"——int() 直接 ValueError
    会打穿 run()。非法值回落默认（浮点字符串取整）。"""
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return default


def _resolve_crawl_strategy(strategy, dynamic_priority) -> str:
    """[v2.17 0-4] 策略决议（纯函数）：B4b 证据按 priority ASC 语义（命中-1=提前），
    bff 按 priority DESC 出队——两者同开语义完全反转，开启动态优先级即强制 bfs。"""
    s = str(strategy or "bfs").lower()
    if dynamic_priority and s == "bff":
        return "bfs"
    return s


def _quality_for(pref) -> str:
    """[FIXED & MODIFIED] v2.11 画质档接线（identity preferred_resolution 死配置 → yt-dlp fmt 链）
    [FIXED & MODIFIED] v2.16.1 4K/2K 无声修复：原 2160/1440 映射为纯 bestvideo[height<=N]
    ——fmt_chain 首项直接选中单流（无音轨，不触发合并）；改 bestvideo+bestaudio 组合，
    yt-dlp 自动取音轨并 ffmpeg 合并（低于 N 时自然降档，画质上限语义不变）"""
    try:
        p = str(pref or "highest").lower()
    except Exception:
        return "best"
    return {
        "highest": "best", "best": "best",
        "2160": "bestvideo[height<=2160]+bestaudio", "4k": "bestvideo[height<=2160]+bestaudio",
        "1440": "bestvideo[height<=1440]+bestaudio", "2k": "bestvideo[height<=1440]+bestaudio",
        "1080": "best[height<=1080]", "720": "best[height<=720]",
        "480": "best[height<=480]",
    }.get(p, "best")


def append_stats_line(stats_path, done, failed, pending, total, batch_size,
                      videos=None, images=None, bytes_total=None, final=False) -> bool:
    """stats.jsonl 追加一行结构化进度（v2.15 阶段5 信号源；返回是否写入成功）
    [v2.17 4.2] videos/images/bytes 扩展键（媒体完成数/图片数/累计字节）——消费方
    （launcher _parse_progress / dashboard）只读已知键，新增键可选，向后兼容。
    默认 0：保持行结构完整（图表消费方无需判空）。
    [v2.18 P3-1] final=True 标记任务收尾快照（消费方可区分中间态/终态）。"""
    try:
        import json as _json
        _line = {"ts": time.time(),
                 "done": done, "failed": failed,
                 "pending": pending, "total": total,
                 "batch": batch_size,
                 "videos": int(videos or 0), "images": int(images or 0),
                 "bytes": int(bytes_total or 0)}
        if final:
            _line["final"] = True
        with open(stats_path, "a", encoding="utf-8") as _sf:
            _sf.write(_json.dumps(_line, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def _extract_bvid(url: str):
    """[FIXED & MODIFIED] v2.11 从 URL 提取 B站 BV/av 号（b23.tv 短链无法静态提取→跳过，诚实降级）"""
    try:
        m = re.search(r'BV[0-9A-Za-z]{10}', url)
        if m:
            return m.group(0)
        m = re.search(r'av(\d+)', url, re.I)
        if m:
            return f"av{m.group(1)}"
    except Exception:
        pass
    return None

# [FIXED & MODIFIED] v2.6.11 真实视频文件判定：>1MB + 视频扩展名 + 非 HTML 头
# （防 fallback download_direct 把页面 HTML 当视频写文件 → 误标 completed → 空文件夹）
# [v2.17 稳定性门禁] 清理豁免后缀：副产物（字幕/封面/info）与 yt-dlp 断点续传文件
# （曾误删 .part/.ytdl → 大文件下载永远从头开始；列表提取为常量供单测锁定）
KEEP_SUFFIXES = ('.vtt', '.srt', '.json', '.webp', '.jpg', '.png', '.ass', '.lrc',
                 '.part', '.ytdl')


def _purge_non_video(_vdir):
    """清理目录下非视频垃圾文件（HTML/0字节/分离流残留）；KEEP_SUFFIXES 豁免不删"""
    try:
        for _f in _vdir.rglob("*"):
            if _f.is_file() and not _is_real_video_file(_f):
                if _f.suffix.lower() in KEEP_SUFFIXES:
                    continue
                _f.unlink()
    except Exception:
        pass


def _collect_proxy_nodes(proxy_list) -> list:
    """[v2.17 稳定性门禁] proxy_list → [(url, country|None)] 节点列表。
    Mapping 识别：OmegaConf DictConfig 是 Mapping 而非 dict 子类——原 isinstance(item, dict)
    让带 country 的 dict 型配置节点被静默丢弃；且空串节点原样入池（已过滤）。纯函数供单测。"""
    from collections.abc import Mapping as _Mapping
    nodes = []
    for item in proxy_list or []:
        if isinstance(item, str):
            _u = item.strip()
            if _u:
                nodes.append((_u, None))
        elif isinstance(item, _Mapping):
            try:
                _u = str(item["url"]).strip()
                if _u:
                    nodes.append((_u, item.get("country")))
            except Exception:
                continue
    return nodes


def _is_real_video_file(p) -> bool:
    try:
        if not p.is_file():
            return False
        if p.stat().st_size < 1_000_000:
            return False
        if p.suffix.lower() not in ('.mp4', '.mkv', '.webm', '.flv', '.mov', '.avi', '.m4a'):
            return False
        with open(p, 'rb') as _f:
            _head = _f.read(256)
        _h = _head.lstrip().lower()
        if _h[:15].startswith(b'<!doctype') or _h[:5] == b'<html' or _h[:5] == b'<!doc':
            return False
        return True
    except Exception:
        return False


def _batch_cap(max_pages, done, hard_cap=50):
    """[v2.19.9] 单批取任务数的上限 —— 必须受**总页数上限**约束。

    **为什么需要它**：`max_pages` 是在**每个任务开始前**才查的（`page_processor`），
    而 `pop_batch` 一次最多租出 50 个任务、随即被**并发启动** ⇒ 那 50 个查到的是
    **同一个"还没超"的计数** ⇒ **全部照跑**。真机实测：`-m 3` 跑了 **10 页**
    （种子的 9 个链接在同一批里租出）—— 使用者设的页数上限**拦不住一个批**。

    额度用完（`room <= 0`）时**故意返回满额**：这一批会被逐条判为"超上限" →
    记 `skipped` 并移出 pending，正是我们要的**快速排空**；收窄到 0 反而会让
    `pop_batch` 恒空、`pending` 永不清零 ⇒ **又变成空转**（本工程刚修过同一形状）。
    """
    room = int(max_pages) - int(done)
    if room <= 0:
        return int(hard_cap)
    return max(1, min(int(hard_cap), room))


class Crawler:
    """主爬虫引擎：协调前沿队列、引擎路由、并发控制、防御协议"""

    def __init__(self, project: ProjectIdentity, global_cfg: GlobalConfig, worker_id=None):
        """[FIXED & MODIFIED] v2.10.6 B2 拆分：原 165 行单体初始化拆为 6 个 _init_* 私有方法
        （纯代码挪移，属性名与顺序完全保持——getattr 反向引用不破）"""
        self.project = project
        self.cfg = global_cfg
        self.worker_id = worker_id or str(uuid.uuid4())[:8]
        # [v2.17 4.2] 页内成图累计计数（stats.jsonl images 键；单事件循环内自增无需锁）
        self._stats_images = 0

        self._init_frontier()
        self._init_network()
        self._init_downloaders()
        self._init_adaptive()
        self._init_enhancements()
        self._init_processor()

    def _init_frontier(self):
        # 前沿队列
        if self.cfg.frontier_backend == "redis":
            # [FIXED & MODIFIED] v2.11 惰性导入（redis 不在构建依赖，换机构建硬 import 必炸）
            from .redis_frontier import RedisFrontier
            self.frontier = RedisFrontier(self.cfg.redis_url, FrontierDB(self.project.get_db_path()))
        else:
            self.frontier = FrontierDB(self.project.get_db_path())

    def _init_network(self):
        # 出口管理
        self.exit_mgr = ExitManager(self.project.config)
        self.exit_mgr.forbid_direct = self.cfg.forbid_direct

        # 会话池（强化版 SessionPool，早期创建供 ProtocolEngine/SolverEngine/EngineRouter 使用）
        self.session_pool = SessionPool()

        # 协议引擎
        self.protocol = ProtocolEngine(
            impersonate=self.cfg.protocol_engine_impersonate,
            session_pool=self.session_pool,
            # [FIXED & MODIFIED] v2.10.5 P2-13 接线 max_retries（config"max_retries":5 是死配置，
            # ProtocolEngine 一直用默认 3）——用户激进风格可配到 5
            max_retries=self.cfg.get("max_retries", 3),
        )

        # 求解引擎: 合并全局配置中的隐身设置到 stealth_config
        stealth_cfg = dict(self.project.config.get("stealth_config", {}))
        # 从全局配置继承隐身相关开关（项目级配置优先）
        for key in ("ultimate_evasion_enabled", "trajectory_engine_enabled",
                     "stealth_injection_enabled", "evasion_dimensions",
                     "use_humanization"):
            if key not in stealth_cfg:
                val = getattr(self.cfg, key, None)
                if val is not None:
                    stealth_cfg[key] = val

        self.solver = SolverEngine(
            pool_size=self.cfg.browser_pool_size,
            max_pages_per_context=self.cfg.browser_max_pages_per_context,
            memory_limit_mb=self.cfg.browser_memory_limit_mb,
            session_pool=self.session_pool,
            stealth_config=stealth_cfg,
            headless=self.cfg.get("headless", True),
            # [FIXED & MODIFIED] v2.10.5 P1-6 打码 API 接线：从 captcha_api_keys 读密钥传给
            # SolverEngine（reCAPTCHA v2/hCaptcha/GeeTest-API 分支此前全哑，因密钥从不传入）
            two_captcha_key=self.cfg.get("captcha_api_keys", {}).get("twocaptcha") or None,
            capsolver_key=self.cfg.get("captcha_api_keys", {}).get("capsolver") or None,
            anti_captcha_key=self.cfg.get("captcha_api_keys", {}).get("anticaptcha") or None,
            # [v2.17 3.5] CDP 接管既有浏览器（实验默认关）
            cdp_attach=bool(self.cfg.get("cdp_attach", False)),
            cdp_port=int(self.cfg.get("cdp_port", 9222)),
        )

        # 引擎路由
        self.router = EngineRouter(
            self.protocol, self.solver, self.session_pool, self.exit_mgr,
            self.cfg.challenge_max_wait,
        )

        # 并发控制
        # [FIXED & MODIFIED] v2.11 硬件自适应基线：global_concurrency 占位默认 200 从未按
        # 机器校准（16GB/20 核实机 → 实际并发受 per-domain 与浏览器池限制）。用户未显式
        # 配置（==占位 200）时按 CPU 核数自适应：max(8, min(32, cores*2))。
        _gc = self.cfg.get("global_concurrency", 200)
        if not _gc or int(_gc) >= 200:
            try:
                import psutil as _ps
                _cores = _ps.cpu_count(logical=True) or 8
                _gc = max(8, min(32, _cores * 2))
                logger.info(f"硬件自适应全局并发: {_gc} ({_cores} 逻辑核)")
            except Exception:
                _gc = 24
        self.concurrency = ConcurrencyController(
            global_max=int(_gc),
            per_domain_default=self.project.config.limits.get("rate_limit_per_domain", 20),  # [FIXED & MODIFIED] v2.10.5 P1-7 单域并发默认 2→20（配 20 核激进）
        )
        # 限流引擎（Token Bucket + 全局/域名双档 + 指数退避 + 速率边界探测）
        try:
            from .rate_limiter import RateLimiter
            # [FIXED & MODIFIED] v2.11 数值同源：rate_limit_per_domain 只存在于项目层配置
            # （GlobalConfig 无此键 → 恒落 10.0，与 ConcurrencyController 取到的 20 双源矛盾）
            _per_domain = self.project.config.limits.get("rate_limit_per_domain", 20)
            self.rate_limiter = RateLimiter(
                global_rate=self.cfg.get("rate_limit_global", 100.0),
                global_burst=200.0,
                domain_rate=_per_domain,
                domain_burst=max(_per_domain, 20.0),
            )
        except Exception:
            self.rate_limiter = None

        # 防御协议初始状态
        self._shielded_domains = set()
        self._defense_mode = getattr(self.cfg, 'defense_mode', 'shield')
        self.shield = None
        self.sentinel = None
        self.habakiri = None
        self.oracle = None

        # 指纹生成器
        try:
            from .fingerprint_consistency import FingerprintGenerator
            self._fp_gen = FingerprintGenerator()
        except Exception:
            self._fp_gen = None

        # 行为/下载开关
        self._should_stop = False
        self._paused = False  # [FIXED & MODIFIED] v2.11 GUI 暂停语义（run 主循环等待）
        self._emit_bg: set = set()  # [v2.18 P2-3] fire-and-forget 任务强引用集
        self._sanitize_enabled = bool(self.cfg.get("privacy_sanitize", True))
        # [v2.16.1] SSRF 域名解析校验开关接线（配置可关；默认开）
        # [v2.19.7 安全·扫描发现] 这个开关是"逃生门"：关掉后主通道不再做域名解析校验，
        # 域名指向 127.0.0.1/169.254.169.254 的 URL 会被放行。保留它（有人 DNS 把外部
        # 域名解析到内网段时否则完全抓不动），但**必须留痕**——静默关闭等于没人知道防护
        # 已经失效；同时声明下载通道仍强制校验（safe_get 显式 dns_check=True）。
        try:
            from . import url_utils as _uu
            _uu.DNS_CHECK_ENABLED = bool(self.cfg.get("private_dns_resolve_check", True))
            if not _uu.DNS_CHECK_ENABLED:
                logger.warning(
                    "⚠️ SSRF 域名解析校验已被配置关闭（private_dns_resolve_check=false）："
                    "主通道不再拦截「域名解析到内网」的 URL。下载通道仍强制校验。")
        except Exception:
            pass
        # [v2.17 E-P1-1] robots 合规开关（默认关；开启后链接入队经 robots_policy 判定）
        self._robots_respect = bool(self.cfg.get("robots_respect", False))
        # [v2.17 E-P2] 身份捆绑池（默认关——开启时出口+cookie 捆绑虚拟用户）
        self._init_identity_bundle()
        # [v2.17 E-P1-2] sitemap 种子播种（默认关；开启后种子域自动发现站点地图批量入队）
        self._sitemap_discover = bool(self.cfg.get("sitemap_discover", False))
        # [v2.17 E-P1-4] 爬行策略（bfs 默认=既有行为/dfs 深度近似/bff 优先值降序）
        self._crawl_strategy = str(self.cfg.get("crawl_strategy", "bfs")).lower()
        # [v2.17 B4b] 证据驱动动态优先级（默认关——规则命中提前/空壳后排）
        self._dynamic_priority = bool(self.cfg.get("dynamic_priority", False))
        # [v2.17 0-6] smart_adaptive 调参循环保留开关（默认关=v2+autoscale 唯一调参源；
        # 恢复旧三方互搏需显式开启——不推荐，仅取证用）
        self._smart_adaptive_tuning = bool(self.cfg.get("smart_adaptive_tuning", False))
        # [FIXED & MODIFIED] v2.17 0-4 兼容纠偏：dynamic_priority 与 bff 语义反转——强制 bfs
        if self._dynamic_priority and self._crawl_strategy == "bff":
            self._crawl_strategy = _resolve_crawl_strategy(self._crawl_strategy,
                                                          self._dynamic_priority)
            logger.warning("dynamic_priority 与 bff 语义互斥（bff=优先级降序出队）——强制 bfs")
        self._dl_video = bool(self.cfg.get("video_download_enabled", True))
        self._dl_image = bool(self.cfg.get("image_download_enabled", True))
        self._dl_audio = bool(self.cfg.get("audio_download_enabled", False))

    def _init_downloaders(self):
        # 媒体下载
        self.media = MediaDownloader(self.project.video_dir, max_concurrent=6)  # [FIXED & MODIFIED] v2.10.5 P0-2 图片/直连并发 3→6
        self.m3u8_downloader = M3U8Downloader(
            concurrency=self.cfg.m3u8_concurrency,
            proxy_func=self._get_proxy_for_domain,
            gpu_acceleration=self.cfg.get("gpu_acceleration", True),
        )
        self.downloader = UniversalDownloader(self.project.export_dir, max_concurrent=10)

        # Production enhancements
        from .enhancements import DataExporter,DataValidator
        self.exporter = DataExporter(self.project.export_dir / "data")
        # [FIXED & MODIFIED] v2.11 Markdown 快照开关（副产物层）
        self._export_markdown = bool(self.cfg.get("export_markdown", True))
        self.validator = DataValidator()

    def _init_adaptive(self):
        # Adaptive v2: multi-factor self-tuning + 系统压力分级（替代旧 DynamicPressureController）
        from .adaptive_v2 import AdaptiveControllerV2,DataCleaner
        self.adaptive_v2 = AdaptiveControllerV2(
            min_con=self.cfg.get("min_concurrency", 20),
            max_con=self.cfg.get("max_concurrency", 100),
            concurrency=self.concurrency,
            config=self.cfg,
            solver=self.solver,
        )
        self.cleaner = DataCleaner()
        # 自适应控制
        self.adaptive = SmartAdaptiveController(
            target_sr=self.cfg.get("adaptive_target_success_rate", 0.95),
            interval=self.cfg.get("adaptive_adjust_interval", 10),
            min_concurrency=self.cfg.get("adaptive_min_concurrency", 3),
            max_concurrency=self.cfg.get("adaptive_max_concurrency", 100),
        )
        self._progress = {'done': 0, 'failed': 0, 'pending': 0, 'total': 0,
                              'skipped': 0}   # [v6] 上限跳过的任务另计，别混进 failed
        # [v2.19.9] 每域视频上限的告警**每域只说一次**：超限的视频可能有很多个，
        # 逐个告警会把日志刷满（本工程吃过日志噪音的亏）。
        self._video_cap_warned: set = set()

    def _init_enhancements(self):
        # [FIXED & MODIFIED] IntelligentParser 已移除（LLM 模块删除的连带——死代码，从不被调用）
        self.ai_parser = None
        # [FIXED & MODIFIED] LLM 增强器已整体移除（国内网络延迟 300-500ms/超时阻塞每页处理，用户确认删除）
        self._llm_enhancer = None
        # P0 Ultimate Core v4
        from .ultimate_core_v4 import (AutoscaledPool, FullSourceExtractor)
        self.autoscale_pool = AutoscaledPool(min_con=10, max_con=300)
        self.link_extractor = FullSourceExtractor()
        # P1 Enhancements
        from .p1_enhancements import (Router,ItemPipeline,PageClassifier,HttpCache,SignalBus)
        self.url_router = Router()   # URL 模式路由（p1；不覆盖核心 EngineRouter）
        self.pipeline = ItemPipeline()
        self.page_classifier = PageClassifier()
        self.http_cache = HttpCache(ttl=_safe_int_cfg(self.cfg.get("http_cache_ttl", 3600), 3600))
        self.signals = SignalBus()

    def _init_processor(self):
        self.processor = PageProcessor(self)
        # [v6 M1-c] 身份弹药库（默认关）——必须在 processor 建好之后挂
        self._init_cookie_armory()
        # 修复：存储所有后台任务的引用，确保关闭时能正确取消
        self._monitor_task = None
        self._adaptive_task = None
        self._pressure_task = None
        self._proxy_sync_task = None
        self._recycle_task = None
        self._autoscale_task = None

        # 压力/自适应控制器（adaptive_v2 替代旧 DynamicPressureController，接口兼容）
        self.pressure_controller = self.adaptive_v2

    def _emit_signal(self, event: str, **kwargs):
        """同步安全地发射信号事件（无运行循环时静默跳过）"""
        if not getattr(self, 'signals', None):
            return
        try:
            loop = asyncio.get_event_loop()
            if loop and loop.is_running():
                _t = loop.create_task(self.signals.emit(event, **kwargs))
                # [v2.18 P2-3] 持强引用（无引用任务可被 GC 静默吞掉）
                self._emit_bg.add(_t)
                _t.add_done_callback(self._emit_bg.discard)
        except Exception:
            pass

    def _init_cookie_armory(self):
        """[v6 M1-c] 按站身份弹药库接线 —— **默认关，零影响**。

        开启后：每页抓取前 `acquire_identity(域)` 取一个按站账号（cookie 经
        `install_cookie_lease` 注入），抓完按结果 `report`（成功回血 / 限流冷却 /
        网络问题**不惩罚**）。未开启时 `processor.cookie_armory` 保持 None，
        抓取路径与本特性引入前**完全一致**。

        **失败必须可读**：取不到主密码等情况明确降级为"本次不启用"并告警，
        不静默、也不假装启用（后者会让用户以为登录态生效了）。

        注：accounts 表落在 `project.get_db_path()`（与 frontier 同库，v6 已收敛
        schema）。Redis 后端时身份记账仍走本地库——身份是**本机凭据**，不外发。
        """
        self.processor.cookie_armory = None
        if not self.cfg.get("cookie_armory_enabled", False):
            return
        try:
            from .cli import _read_master_password
            from .cookie_armory import CookieArmory, armory_db_path
            # [v6 修复] 原来用 `self.project.get_db_path()` —— 那是**任务级**路径
            # （`<输出目录>\cli_<URL哈希>\frontier.db`，随首个 URL 变），
            # 而 `cli cookie-add` 存到的是**另一个**库 ⇒ 存的号爬取永远读不到、
            # 每个新 URL 还是一个空库。改走 `armory_db_path()` 单一实现（用户级固定位置）。
            self.processor.cookie_armory = CookieArmory(
                armory_db_path(), _read_master_password())
            logger.info("身份弹药库已接线（按站账号 acquire/report 生效）")
        except Exception as e:
            logger.warning(f"身份弹药库启用失败，本次任务按无身份运行: {e}")

    def _init_identity_bundle(self):
        """[v2.17 E-P2] 身份捆绑池（默认关）：出口代理列表 + cookie 组打包为"虚拟用户"。
        开启时出口/cookie 主链路接会话池（被封锁整包退役换新）；关闭时完全走原
        exit_mgr/原 cookie 通道（零影响）。cookie 组来源与 cookie_health 同源：
        KIANA_COOKIE_FILES（分号分隔）→ KIANA_COOKIE_FILE → 默认 cookies.txt。"""
        self._identity_pool = None
        if not self.cfg.get("identity_bundle"):
            return
        try:
            from .identity_session import SessionPool, load_cookie_groups
            proxies = []
            plist = self.project.config.get("proxy_list") or []
            if isinstance(plist, str):
                plist = plist.splitlines()
            for p in list(plist):  # OmegaConf ListConfig 非 list——统一 list() 兜底
                if p and isinstance(p, str) and p.strip():
                    proxies.append(p.strip())
            for p in str(os.environ.get("KIANA_PROXY_LIST", "")).splitlines():
                if p.strip():
                    proxies.append(p.strip())
            # cookie 组：与 cookie_health 相同的解析路径
            cookie_files = []
            envs = str(os.environ.get("KIANA_COOKIE_FILES", ""))
            if envs:
                # [v2.18 P2-7] 统一走 parse_cookie_file_list：换行分隔/注释/引号原来静默失效
                from .cookie_utils import parse_cookie_file_list
                cookie_files = parse_cookie_file_list(envs)
            elif os.environ.get("KIANA_COOKIE_FILE"):
                cookie_files = [str(os.environ["KIANA_COOKIE_FILE"])]
            else:
                _default = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                        "KianaVnextPlus", "cookies.txt")
                if os.path.exists(_default):
                    cookie_files = [_default]
            bundles = load_cookie_groups(cookie_files)
            if not bundles:
                bundles = [{"__default__": True}]  # 至少一组占位（无 cookie 时仅捆绑出口）
            self._identity_pool = SessionPool(proxies=proxies, cookie_bundles=bundles)
            try:
                # 请求头注入挂钩：_build_headers 按 URL 域从活跃会话取 Cookie（仅在
                # session_pool 旧通道未给 Cookie 时生效——用户自填 cookies 优先级不变）
                self.protocol.identity_pool_provider = self._identity_pool
            except Exception as e:
                # 原为 `except Exception: pass`——挂不上则整个身份注入**静默失效**
                logger.warning(f"身份注入挂钩挂载失败（本次任务将不带身份 cookie）: {e}")
            logger.info(f"身份捆绑池已建（{len(proxies)} 出口 / {len(bundles)} cookie 组）"
                        f"——封锁整包退役开关生效")
        except Exception as e:
            logger.warning(f"身份捆绑池构建失败（回退出口管理器）: {e}")
            self._identity_pool = None

    async def _proxy_sync_once(self):
        """[v2.17 3-C] 单轮代理源同步：拉取→校验→注池；上一轮注入节点先清（防积累）。
        只清本模块注入过的（_proxy_injected 集合按 url 记忆——手动配置不受影响）。"""
        try:
            from .proxy_fetcher import parse_proxy_source, fetch_all
            src = str(self.cfg.get("proxy_source", "") or "")
            if not src:
                return 0
            ok, failed = await fetch_all(parse_proxy_source(src))
            _new = {p["url"] for p in ok}
            stale = (getattr(self, "_proxy_injected", set()) or set()) - _new
            em = getattr(self, "exit_mgr", None)
            if em is not None and stale:
                for u in stale:
                    em.nodes.pop(u, None)
                em._active_nodes = [n for n in em._active_nodes if n.proxy_url not in stale]
            for p in ok:
                if em is not None:
                    em.add_node(p["url"], geo_country=p.get("country"))
            self._proxy_injected = _new
            logger.info(f"代理源同步: 拉取 {len(ok)}（失败源 {failed}）→ 注池")
            return len(ok)
        except Exception as e:
            logger.warning(f"代理源同步失败: {e}")
            return 0

    async def _proxy_sync_loop(self):
        """[v2.17 3-C] 周期同步循环（proxy_fetcher_enabled 时由 run() 启动；关停随任务取消）"""
        while not self._should_stop:
            await self._proxy_sync_once()
            try:
                await asyncio.sleep(float(self.cfg.get("proxy_fetch_interval", 600)))
            except Exception:
                await asyncio.sleep(600)

    async def _polite_delay(self, domain):
        """[v2.17 1-5] robots Crawl-delay 尊重（仅 robots_respect 开启时生效——默认零影响）：
        按域记录上次请求时刻，未达 crawl-delay 则等待。合规任务/海外站开 --robots 即生效。"""
        if not self._robots_respect:
            return
        try:
            from .robots_policy import crawl_delay_for
            delay = crawl_delay_for(domain)
            if delay <= 0:
                return
            # [v2.19 P1 竞态修复] 原实现"读 last → await sleep → 写 now"跨 await 读-改-写：
            # 同域多协程可同时读到同一 last，await 后双双写入 → Crawl-delay 被击穿
            # （落在合规维度，性质更重）。改为**预约式占位**——同步块内把下一次允许
            # 时刻预定为"本轮基准 + delay"，再 await 自己该等的时间；无需锁且严格串行。
            _gate = getattr(self, "_robots_last_req", None)
            if _gate is None:
                _gate = {}
                self._robots_last_req = _gate
            _now = time.time()
            _next = max(float(_gate.get(domain, 0.0)), _now)
            _gate[domain] = _next + delay          # 同步占位（本块内无 await）
            _wait = _next - _now
            if _wait > 0:
                await asyncio.sleep(_wait)
        except Exception:
            pass

    async def _acquire_identity(self, domain):
        """[v2.17 E-P2] 主链路出口获取：(proxy, session|None)。
        身份捆绑开启 → 会话池出口（无出口会话时 proxy="" 但 session 仍用于 cookie/反馈）；
        关闭 → ("", None)（调用方走原 exit_mgr）。"""
        if getattr(self, "_identity_pool", None) is None:
            return "", None
        try:
            sess = self._identity_pool.get(domain or "default")
            if sess is not None:
                return sess.proxy or "", sess
        except Exception:
            pass
        return "", None

    def _identity_feedback(self, domain, session, fail=False, status=None, err=None):
        """[v2.17 E-P2] 请求结果为风控信号 → mark_bad（连续 2 次整包退役）；成功 → mark_good
        （清零坏计数）。decide_bad 保守：超时/连接错不计封锁，免误退役健康出口。"""
        if session is None or getattr(self, "_identity_pool", None) is None:
            return
        try:
            from .identity_session import decide_bad
            if decide_bad(bool(fail), status=status, err=err or ""):
                self._identity_pool.mark_bad(domain or session.domain, session)
            elif not fail:
                self._identity_pool.mark_good(domain or session.domain, session)
        except Exception:
            pass

    async def _get_proxy_for_domain(self, domain=None):
        # [v2.17 E-P2] 身份捆绑开启时：优先会话池出口（封锁整包退役语义）；否则原通道
        _proxy, _sess = await self._acquire_identity(domain or "default")
        if _proxy:
            return _proxy
        return await self.exit_mgr.acquire_for_domain(domain or 'default')

    async def setup(self):
        logger.info(f"Worker {self.worker_id} starting setup...")

        # Windows native optimizations (GC, IO, CPU affinity)
        self._orig_power_plan = None
        try:
            from .win32_native import apply_all_optimizations, memory_recycle_loop, ensure_high_performance_power_plan
            # [FIXED & MODIFIED] v2.10.5 P2-12 避免电源计划双调用：apply 内部不再切电源
            # （init_power_plan=False），统一由外层 ensure 切并保存句柄 → shutdown 能真实恢复
            apply_all_optimizations(self.cfg.cfg, init_power_plan=False)
            # 爬虫运行期间切高性能电源计划（结束时自动恢复）
            self._orig_power_plan = ensure_high_performance_power_plan()
        except Exception as e:
            logger.debug(f"Win32 native optimizations skipped: {e}")

        await self.frontier.init_async()
        await self.protocol.init()
        # [FIXED & MODIFIED] v2.10.4 隐身模块移除：不再自动探测/注入 127.0.0.1:7897 代理
        # ——端口探测误报（本机其他软件占用 7897 时误判 SakuraCat 已开启）导致没开 VPN
        # 也走坏代理 → 大量失败重试。现在一律直连：用户要隐身时自行开启全局代理（TUN
        # 全局路由），引擎无需感知代理存在。
        logger.info("隐身旁路：引擎直连模式（隐身请由用户自行开启全局代理）")
        # 智能解析器会话初始化（异步）
        if getattr(self, 'ai_parser', None):
            pass  # [FIXED & MODIFIED] ai_parser 已移除（原 init 调用于此删除）
        # 浏览器求解引擎：失败时 graceful 降级（protocol-only 模式），不中断爬虫
        # [FIXED & MODIFIED] v2.10.5c 懒初始化：不在 setup 强制 init——浏览器池预热会阻塞
        # 纯下载类任务（B站视频等）；solver.solve/render_simple 已内置首次调用自动 init。
        if getattr(self.cfg, 'lazy_solver_init', True):
            logger.info("求解引擎懒初始化模式（首次渲染时自动启动浏览器池）")
        else:
            try:
                await self.solver.init()
            except Exception as e:
                # [FIXED & MODIFIED] v2.10.5c 打印完整堆栈（原来只 warning 无原因——
                # solver.init 失败原因被隐藏 → 渲染/验证码通道静默禁用，贴吧等 JS 重站拿不到正文）
                import traceback as _tb
                logger.warning(f"求解引擎不可用（降级 protocol-only）: {e}\n{_tb.format_exc()[-800:]}")
                try:
                    if getattr(self.solver, 'playwright', None):
                        await self.solver.playwright.stop()
                except Exception:
                    pass
                self.solver._browser_available = False
        await self.media.init_session()
        await self.m3u8_downloader.init_session()
        await self.downloader.init()

        # 加载代理列表
        proxy_list = self.project.config.get("proxy_list", [])
        # [v2.17 稳定性门禁] Mapping 识别 + 空串过滤（见 _collect_proxy_nodes 注释）
        for _node_url, _node_country in _collect_proxy_nodes(proxy_list):
            self.exit_mgr.add_node(_node_url, geo_country=_node_country)
        # [FIXED & MODIFIED] v2.10.5 P1-9 KIANA_PROXY_LIST 热加载：环境变量（分号/换行/逗号分隔）
        # 补充代理池（原唯一来源 config proxy_list → 代理一断海外站全灭）
        try:
            _env_proxies = os.environ.get("KIANA_PROXY_LIST", "")
            for _p in _env_proxies.replace(",", ";").replace("\n", ";").split(";"):
                _p = _p.strip()
                if _p.startswith(("http://", "https://", "socks")):
                    self.exit_mgr.add_node(_p)
                    from .sanitizer import sanitize_proxy
                    logger.info(f"KIANA_PROXY_LIST 注入代理: {sanitize_proxy(_p)}")
        except Exception:
            pass

        if self.cfg.forbid_direct and not self.exit_mgr.nodes:
            logger.warning("forbid_direct=True but no exits configured")

        # 初始化防御协议
        if self._defense_mode == 'shield':
            self.shield = ShieldSoldier()
            self.shield.mount(self)
            self.sentinel = SentinelBrain(self.shield, self.cfg.master_password)
            await self.sentinel.authorize(self.cfg.master_password)
            # [FIXED & MODIFIED] v2.10.5 P1-8 启动防御恢复循环：原 start_recovery_loop 零调用 →
            # 域名升 RED/YELLOW 后冷却到点也永不降级（整场坐冷板凳）。启动后每 15s 检查恢复。
            try:
                await self.shield.start_recovery_loop()
            except Exception:
                # [FIXED & MODIFIED] v2.14 except 治理：静默吞掉会让恢复循环未启动
                # （RED/YELLOW 冷却到点永不降级——正是本代码要修的病无声复发）
                logger.exception("防御恢复循环启动失败（冷却恢复将不可用）")
        elif self._defense_mode == 'habakiri':
            self.habakiri = AmeNoHabakiri()
            self.habakiri.mount(self)
            self.oracle = OracleBrain(self.habakiri, self.cfg.master_password)
            await self.oracle.authorize(self.cfg.master_password)

        # 启动后台任务
        self._monitor_task = asyncio.create_task(self._video_download_worker())
        self._pressure_task = asyncio.create_task(self.pressure_controller.run())
        # AutoscaledPool 多因子自适应（CPU/内存/错误率综合调优，与压力控制器互补）
        if getattr(self, 'autoscale_pool', None):
            try:
                # [FIXED & MODIFIED] v2.10.5 P1-7 AutoscaledPool 接线：调优结果写回全局信号量
                # （此前 _tune 只改自身 → 幻影池；record 也从未被调）
                _pool = self.autoscale_pool
                def _apply(new_max):
                    try:
                        _t = asyncio.create_task(self.concurrency.adjust_global(new_max, source="autoscale"))
                        # [v2.18 P2-3] 持强引用
                        self._emit_bg.add(_t)
                        _t.add_done_callback(self._emit_bg.discard)
                    except Exception:
                        pass
                _pool.set_apply(_apply)
                self._autoscale_task = asyncio.create_task(_pool.run(interval=3.0))
            except Exception:
                self._autoscale_task = None
        # Windows 内存回收后台循环
        recycle_interval = self.cfg.get("memory_recycle_interval", 300)
        self._recycle_task = asyncio.create_task(memory_recycle_loop(recycle_interval))

        # 发射生命周期信号
        self._emit_signal("crawler_started", worker_id=self.worker_id)

        # 修复：设置协议引擎并启动健康检查（原遗漏导致功能未启用）
        self.exit_mgr.set_protocol_engine(self.protocol)
        if self.exit_mgr.nodes:
            await self.exit_mgr.start_health_check(interval=60)

        self._progress = {'done': 0, 'failed': 0, 'pending': 0, 'total': 0,
                              'skipped': 0}   # [v6] 上限跳过的任务另计，别混进 failed
        # [v2.19.9] 每次爬取重新计：每域上限的告警在**本次任务**里只出现一次
        self._video_cap_warned = set()
        # [v2.17 E-P1-5] 在线指纹更新（默认关；开启后任务启动时尝试一次 curl-cffi
        # FingerprintManager.update_fingerprints——免升级拉最新指纹；失败仅日志不阻塞）
        # [v6 修复·真机实测] 那个端点 **`api.impersonate.pro` 已经失效**：
        # 域名还能解析（141.193.154.70），但 TLS 证书与主机名不匹配
        # ⇒ `SSL: no alternative certificate subject name matches target hostname`。
        # 这是**上游服务的问题，不是本工程的 bug**，可原来的日志长得像我们的错
        # （一句干巴巴的 curl 报错），排查时白费功夫。现在把"是什么情况"说清楚。
        try:
            if self.cfg.get("fingerprint_update_enabled"):
                from curl_cffi import FingerprintManager
                _n = FingerprintManager().update_fingerprints()
                logger.info(f"curl_cffi 在线指纹已更新（{_n} 项）")
        except Exception as _fe:
            _msg = str(_fe)
            if "certificate" in _msg.lower() or "SSL" in _msg:
                logger.warning(
                    "curl_cffi 在线指纹更新跳过：上游端点 `api.impersonate.pro` 的 TLS 证书"
                    "与主机名不匹配（该服务已不在那里）。**这是 curl-cffi 上游的事，不是本工程的问题**，"
                    "抓取照常。要关掉这条尝试：设置页把「指纹库自动更新」关掉"
                    "（`fingerprint_update_enabled=False`）。")
            else:
                logger.warning(f"curl_cffi 在线指纹更新失败（不影响运行）: {_fe}")
        logger.info("Setup complete.")

    async def run(self, seed_urls: List[str]):
        # Checkpoint resume
        from .enhancements import CrawlCheckpoint
        checkpoint = CrawlCheckpoint(self.project.dir / "checkpoint.json")
        state = checkpoint.load()
        if state and state.get('pending',0) > 0:
            # [v2.17 B1c] 诚实化：checkpoint 为统计参照（真实续爬由 frontier 持久化
            # 保证——seed 用 force=True 重推、未完成任务留 pending/retry 表）
            logger.info(f"checkpoint 统计参照: done={state.get('done',0)} pending={state.get('pending',0)}（真实续爬由 frontier 持久化保证）")
        pcount = 0
        # [v2.17 2-B] LLM 链接打分（默认关）：种子级一次批处理 → 覆盖默认 priority
        # (越小越优先; 失败/未配置诚实降级为默认 1)
        _llm_seed_scores = {}
        if self.cfg.get("llm_link_scoring", False):
            try:
                from .llm_client import LLMClient, async_llm_score_links
                _c = LLMClient({
                    "format": str(self.cfg.get("llm_format", "openai")),
                    "base_url": str(self.cfg.get("llm_api_base", "")),
                    "api_key": str(self.cfg.get("llm_key", "")),
                    "model": str(self.cfg.get("llm_model", "")),
                })
                _m = max(1, _safe_int_cfg(self.cfg.get("llm_link_budget", 64), 64))
                _llm_seed_scores = await async_llm_score_links(
                    _c, [u for u in seed_urls], budget_links=_m)
                logger.info(f"LLM 链接打分: {len(_llm_seed_scores)} 条（预算 {_m}）")
            except Exception as e:
                logger.warning(f"LLM 链接打分失败（降级默认打分）: {e}")
        # [FIXED & MODIFIED] v2.6.2 种子类型检测（用户直链输入的核心通道——任意直链自动分流）：
        #   视频/音频/m3u8 → yt-dlp；图片 → 图片通道；其他文件(pdf/zip等) → 文件通道
        #   未识别的普通 URL → 正常爬取解析（全方位数据采集本职）
        _MEDIA_EXT = ('.mp4', '.mkv', '.webm', '.mov', '.avi', '.flv', '.ts',
                      '.mp3', '.flac', '.wav', '.aac', '.ogg', '.m4a')
        _IMAGE_EXT = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.svg', '.avif', '.ico')
        _FILE_EXT = ('.pdf', '.zip', '.rar', '.7z', '.doc', '.docx', '.xls', '.xlsx',
                     '.ppt', '.pptx', '.txt', '.epub', '.apk', '.exe', '.json', '.csv')
        for url in seed_urls:
            # [v2.19 安全 P0] 种子闸：种子可能来自文件导入/剪贴板/远程 feed，来源不可信；
            # 私网/环回/保留地址一律不入队（dns_check=False 轻量判定，避免逐条 DNS 阻塞）。
            try:
                from .url_utils import is_private_url as _is_priv_seed
                if _is_priv_seed(str(url), dns_check=False):
                    logger.warning(f"种子拦截（SSRF）：私网/保留地址已跳过 {str(url)[:60]}")
                    continue
            except Exception:
                pass
            _u = url.lower()
            is_m3u8 = '.m3u8' in _u or '/hls/' in _u
            is_media = any(_u.rstrip('/').endswith(ext) for ext in _MEDIA_EXT)
            is_image = any(_u.rstrip('/').endswith(ext) for ext in _IMAGE_EXT)
            is_file = any(_u.rstrip('/').endswith(ext) for ext in _FILE_EXT)
            # [FIXED & MODIFIED] v2.9.2 B站视频页种子直连下载：bilibili.com/video/BVxxx 直接
            # 入 yt-dlp 队列（自带 B站 extractor + cookies → 最高画质）——完全不 fetch 页面，
            # 绕过 GeeTest 风控（用户 GUI 实测：B站视频页走页面解析 → 美国代理 IP 触发
            # geetest 挑战 → 0 pages。直连 + 跳过解析 = 视频页唯一正确链路）
            # [FIXED & MODIFIED] v2.10.0 番剧修复：bilibili.com/bangumi/play/ep 也直连入队
            # （用户 GUI 输番剧链接爬不到——原检测只认 /video/——番剧走页面解析必风控失败）
            # [FIXED & MODIFIED] v2.10.1 多平台视频检测：不再只认 B站——抖音/快手/小红书/
            # YouTube/腾讯/爱奇艺/优酷/西瓜/微博视频等常见平台链接直接入 yt-dlp 队列
            # （yt-dlp 原生支持 1000+ 站点——用户"只能爬 B站"根因就是检测只认 B站）
            # [FIXED & MODIFIED] v2.10.2 全平台扩充：全流媒体/全多媒体平台（音视频全覆盖）
            is_bili_video = ('bilibili.com/video/' in _u) or ('bilibili.com/bangumi/' in _u) or \
                            ('b23.tv/' in _u and ('BV' in _u or 'ep' in _u)) or \
                            ('b23.tv/' in _u)  # [FIXED & MODIFIED] v2.10.5c b23.tv 短链必是 B站视频/番剧（duOgtrs 等纯短码不含 BV/ep，原判断漏检→被当普通网页抓失败）
            _VIDEO_PLATFORMS = (
                # 国内短视频/直播
                'douyin.com', 'v.douyin.com', 'iesdouyin.com',        # 抖音
                'kuaishou.com', 'gifshow.com', 'kwai.com',            # 快手
                'xiaohongshu.com', 'xhslink.com',                     # 小红书
                'huoshan.com', 'pipixia.com',                         # 火山/皮皮虾
                'huya.com', 'douyu.com', 'bilibili.com',              # 虎牙/斗鱼/B站
                'yy.com', 'zhanqi.tv', 'longzhu.com',                 # YY/战旗/龙珠
                # 国内长视频
                'v.qq.com', 'i.qq.com',                               # 腾讯视频
                'iqiyi.com', 'youku.com', 'ixigua.com',               # 爱奇艺/优酷/西瓜
                'mgtv.com', 'tv.sohu.com', 'le.com', 'pptv.com',      # 芒果/搜狐/乐视/PPTV
                'acfun.cn', 'tv.cctv.com', 'cntv.cn',                 # ACFUN/央视
                'weibo.com', 'miaopai.com', 'yidianzixun.com',        # 微博/秒拍/一点
                # 音频/播客
                'music.163.com', 'y.qq.com', 'kg.qq.com', 'kugou.com', # 网易云/QQ音乐/酷狗
                'ximalaya.com', 'qingting.fm', 'podcasts.google.com',  # 喜马拉雅/蜻蜓
                'spotify.com', 'open.spotify.com', 'soundcloud.com',    # Spotify/SoundCloud
                # 海外视频
                'youtube.com', 'youtu.be', 'tiktok.com', 'vm.tiktok.com',
                'instagram.com', 'twitter.com', 'x.com', 'facebook.com',
                'reddit.com', 'vimeo.com', 'dailymotion.com', 'twitch.tv',
                'bilibili.tv', 'pinterest.com', 'linkedin.com', 'vk.com',
                'rumble.com', 'odysee.com', 'bitchute.com',            # Rumble/Odysee
            )
            # [FIXED & MODIFIED] v2.10.6 C2 平台误匹配：原 `any(k in _u)` 子串匹配导致
            # qq.com/tencent.com/weibo.com 等普通页面（QQ邮箱/腾讯官网/微博主页）被判为视频站
            # 强行入 yt-dlp 队列。改为**域名精确匹配**（主机名 == 域 或 是其子域）。
            is_video_platform = False
            if not (is_image or is_file or is_m3u8 or is_media):
                try:
                    from urllib.parse import urlparse as _up3
                    _host = (_up3(url).netloc or "").lower().split(":")[0]
                    if _host:
                        is_video_platform = any(
                            _host == d or _host.endswith("." + d) for d in _VIDEO_PLATFORMS)
                except Exception:
                    pass
            # [FIXED & MODIFIED] v2.17 0-2 路由精度：B站域内仅视频/番剧路径才走 yt-dlp——
            # 专栏(/read/cv)、动态(t.bilibili.com)、空间页等被无差别推下载队列（必失败、
            # 占 video_downloads 记录）后还需重走页面通道。非视频路径 → 普通页面解析。
            if is_video_platform and _host and "bilibili.com" in _host:
                is_video_platform = any(h in _u for h in (
                    "/video/", "/bangumi/", "/medialist/", "/play/", "/festival/",
                    "/cheese/", "/list/"))
            is_video_platform = is_video_platform or is_bili_video
            if is_image:
                try:
                    # [v6 修复] 原来传的是 `referer=url`（图片自己的地址）——于是 Referer
                    # 头变成**CDN 自己的主机**（如 i0.hdslb.com），而工程注释写明
                    # "CDN 校验主站 Referer，无/错 Referer 直接 403"；同时 v2.4.1 想要的
                    # "CDN 图归到对应主站目录"也没生效。改用唯一实现推导。
                    from .url_utils import referer_for as _referer_for
                    await self.downloader.download_image(url, referer=_referer_for(url))
                    logger.info(f"种子为图片直链，已下载: {url[:60]}")
                except Exception as e:
                    logger.debug(f"image seed: {e}")
                continue
            if is_file:
                try:
                    await self.downloader.download_file(url)
                    logger.info(f"种子为文件直链，已下载: {url[:60]}")
                except Exception as e:
                    logger.debug(f"file seed: {e}")
                continue
            if is_m3u8 or is_media:
                try:
                    from urllib.parse import urlparse as _up
                    _dom = (_up(url).netloc or 'direct').replace('www.', '')
                    await self.frontier.add_video_download(url, _dom)
                    logger.info(f"种子为媒体直链，直接入下载队列: {url[:60]}")
                except Exception as e:
                    logger.debug(f"media seed enqueue: {e}")
                continue
            # [本轮修复·真机日志] **媒体流分片**（`.m4s` 等）既不是页面、也不是可独立
            # 交付的媒体文件 —— 判据见 `url_utils.is_media_stream_url`（形态判定，
            # 不是域名黑名单）。不拦的话它会掉进下面最后那条 `frontier.push` 变成
            # **页面任务**：白抓一轮、必然 403、再白起一次浏览器渲染兜底。
            # ⚠️ 只拦分片后缀：`.mp4`/`.ts`/`.m3u8` 的直链种子走上面的媒体分支，
            # "给一个直链就直接下"这条能力**一个字没动**。
            from .url_utils import is_media_stream_url as _is_stream_seg
            if _is_stream_seg(url):
                logger.warning(f"种子是媒体流分片（不是页面，也不是完整媒体文件），"
                               f"已跳过不入队: {url[:70]}")
                continue
            # [v2.17 1-2] RSS/Atom 订阅源种子：形态判定 → 解析条目逐条入队（depth=0,
            # 血缘 parent_hash=feed 源 url；失败诚实降级为普通页面通道）
            from .feed_source import looks_like_feed, collect_feed
            if looks_like_feed(url):
                _entries = await collect_feed(url)
                if _entries:
                    from .url_utils import url_hash as _uh_fn
                    from .url_utils import is_private_url as _is_priv
                    # [v2.19 安全 P0] 入队闸：feed 是**远端内容**，恶意 feed 可注入内网
                    # 地址（配合主通道无闸即构成完整 SSRF 链）。dns_check=False 为轻量
                    # 字面判定——入队高频，避免同步 DNS 阻塞。
                    _kept = 0
                    for _e in _entries:
                        try:
                            if not _e or _is_priv(str(_e), dns_check=False):
                                continue
                        except Exception:
                            continue
                        await self.frontier.push(_e, depth=0, priority=1, force=True,
                                                 parent_hash=_uh_fn(url))
                        _kept += 1
                    logger.info(f"RSS 种子入队: {_kept}/{len(_entries)} 条（{url[:50]}）")
                    continue
                logger.info(f"feed 无条目——降级普通页面通道: {url[:50]}")
            if is_video_platform:
                # [FIXED & MODIFIED] v2.10.2 视频平台页双通道：视频入下载队列（yt-dlp）
                # + 页面继续入解析队列（封面/标题/简介/相关推荐链接/图片全采集）——
                # 用户实测"B站链接只有视频没封面/相关链接"根因：v2.9.2 直连下载跳过了
                # 页面解析。国内站直连规则已保证 B站页面 fetch 不走代理（无 GeeTest）
                #
                # [v6 修复·用户实测发现] 原此处**没有 `_dl_video` 守卫** —— 于是
                # `--no-video` / GUI 关掉视频下载时，**种子这条捷径照样下视频**：
                # 实测跑一个 B站种子，明传 `--no-video`，仍产出 126MB 的 mp4。
                # 页面解析通道不受影响（封面/标题/相关链接照收），只跳过视频入队。
                if self._dl_video:
                    try:
                        from urllib.parse import urlparse as _up
                        # [v6 修复·真机实测] **必须清洗**：媒体直链常来自带端口的 CDN
                        # （实测 `xy111x6x14x207xy.mcdn.bilivideo.cn:8082`），
                        # 而 Windows 目录名不许有 `:` ⇒ `mkdir` 直接抛
                        # `[WinError 267] 目录名称无效`，整条视频下不动。
                        # 走 `url_utils.safe_filename` —— 那是本工程的**统一入口**
                        # （别处早已用它，唯独这条视频路径漏了）。
                        from .url_utils import safe_filename as _sfn
                        _dom = _sfn((_up(url).netloc or 'direct').replace('www.', ''))
                        # [v6 修复·真机实测] **短链种子不再单独入视频队列**。
                        # 真机日志（同一视频、两个 URL）：
                        #   `B站视频入队: https://b23.tv/ybyASFu`                     ← 种子路径
                        #   `B站视频入队: https://www.bilibili.com/video/BV1obZjBSEpT` ← 页面解析
                        # ⇒ 队列里两条 ⇒ **整个视频下两遍**（实测两个 82MB 文件：
                        #   白耗 82MB 带宽 + 164MB 磁盘，日志还重复报"下载完成"）。
                        # 我先前修的是"**同一个 URL** 重复入队"，管不到"同视频不同 URL"。
                        #
                        # 为什么可以安全跳过：紧接着就把该 URL **入了页面解析队列**，
                        # 页面解析拿到的是**规范 URL**（带 BV 号）—— 那条更好：
                        # 既不会拼出短链主机的假 URL（见 R6），去重也有稳定的键。
                        from .url_utils import is_shortener_url
                        if is_shortener_url(url):
                            logger.info(
                                f"种子是短链 → 跳过视频入队（页面解析会用规范 URL 入队）: "
                                f"{url[:55]}")
                        else:
                            await self.frontier.add_video_download(url, _dom)
                            logger.info(f"种子为视频平台页 → 入下载队列: {url[:55]}")
                    except Exception as e:
                        logger.debug(f"video platform enqueue: {e}")
                else:
                    logger.info(f"已按「不下载视频」跳过视频入队，仅入页面解析队列: {url[:55]}")
                await self.frontier.push(url, depth=0, priority=1, force=True)
                logger.info(f"种子为视频平台页 → 同时入页面解析队列（封面/链接/图片采集）: {url[:55]}")
                continue
            # [v2.17 2-B] LLM 打分覆盖优先级；默认 1（既有行为）
            await self.frontier.push(url, depth=0,
                                     priority=_llm_seed_scores.get(url, 1), force=True)
            # [FIXED & MODIFIED] 种子强制重新爬（无视历史 done）
        # [v2.17 E-P1-2] sitemap 种子播种（默认关；开启后对种子域自动发现并解析站点地图、
        # 批量入队 priority=0——站内全站地址一次拿到，广撒网任务的正解）
        if getattr(self, "_sitemap_discover", False):
            try:
                from urllib.parse import urlparse as _up1
                from .enhancements import discover_sitemap, parse_sitemap
                from .url_utils import is_private_url
                _domains = set()
                for _su in seed_urls:
                    try:
                        _h = (_up1(_su).hostname or "").lower()
                        if _h:
                            _domains.add(_h)
                    except Exception:
                        continue
                for _d in _domains:
                    if is_private_url(f"https://{_d}/"):
                        continue
                    _maps = await discover_sitemap(_d)
                    _cnt = 0
                    for _m in (_maps or [])[:5]:
                        for _pu in (await parse_sitemap(_m))[:300]:
                            try:
                                if is_private_url(_pu):
                                    continue
                                await self.frontier.push(_pu, depth=0, priority=0)
                                _cnt += 1
                            except Exception:
                                continue
                    if _cnt:
                        logger.info(f"sitemap 播种({_d}): {_cnt} 条站点地图 URL 入队")
                    else:
                        logger.info(f"sitemap 未发现/为空({_d})")
            except Exception as _se:
                logger.warning(f"sitemap 播种失败: {_se}")
        await self.frontier.flush()
        # [FIXED & MODIFIED] v2.17 0-6 自适应唯一化：smart_adaptive 的调参循环停启——
        # 此前 smart_adaptive(10s)+AdaptiveControllerV2(5s)+AutoscaledPool 三方同时
        # adjust_global 同一信号量（策略互相抵消，min-wins 掩盖冲突）；保留 adaptive
        # record() 供观测/冷却映射，调参源唯一化为 pressure_controller(v2)+autoscale。
        self._adaptive_task = None
        if not getattr(self, "_smart_adaptive_tuning", None):
            logger.info("smart_adaptive 调参循环已停（v2+autoscale 唯一调参源）")
        # [v2.17 3-C] 代理源周期同步（默认关；proxy_source 配置后自动注池/换批）
        self._proxy_sync_task = None
        if self.cfg.get("proxy_fetcher_enabled", False):
            self._proxy_sync_task = asyncio.create_task(self._proxy_sync_loop())
            logger.info("代理源同步循环已启动（proxy_fetcher_enabled）")
        # 修复：降低 flush 频率（仅 batch 不为空时 flush，避免空转 join）
        empty_polls = 0
        try:
            while not self._should_stop:
                # [FIXED & MODIFIED] v2.11 GUI 暂停语义：pause() 置 _paused → 主循环在此
                # 等待（已取出的批照常处理完，不半途搁置任务）；resume() 清零继续取批。
                while self._paused and not self._should_stop:
                    await asyncio.sleep(0.5)
                batch = None
                # [v2.18 P2-10] flusher 争 WAL 写锁时 pop_batch 偶发 "database is locked"——
                # 旧实现直接炸出 run() 终止整场。退避重试 2 次（busy_timeout 已 30s，
                # 这里兜的是极端并发写窗口），仍失败再抛。
                for _attempt in range(3):
                    try:
                        # [v2.19.9 修复] 批量必须受**总页数上限**约束 —— 原为硬编码 50，
                        # 而 `max_pages` 是每个任务开始前才查的（`page_processor`）⇒ 一批
                        # 50 个任务并发启动会把使用者设的上限冲过去（最多 49 页）。
                        # 真机实测：`-m 3` 跑了 **10 页**。
                        # 语义与两个边界（额度用完**取满**、至少 1）见 `_batch_cap` docstring。
                        batch = await self.frontier.pop_batch(
                            _batch_cap(self.project.config.limits.max_pages,
                                       await self.frontier.count_done_total()),
                            worker_id=self.worker_id,
                            strategy=getattr(self, "_crawl_strategy", "bfs"))  # [FIXED & MODIFIED] v2.10.5 P1-7 批上限 10→50（原 10 卡死 20 核并发）#[v2.17 E-P1-4] 爬行策略透传（bfs/dfs/bff）
                        break
                    except Exception as _pe:
                        if _attempt == 2:
                            raise
                        logger.warning(f"pop_batch 暂时性失败({_attempt+1}/2): {_pe} —— 退避重试")
                        await asyncio.sleep(0.5 * (_attempt + 1))
                if not batch:
                    counts = await self.frontier.get_counts()
                    if counts.get('pending', 0) + counts.get('retry', 0) == 0:
                        break
                    empty_polls += 1
                    await self.frontier.flush()  # [FIXED & MODIFIED] 空 poll 立即 flush：写队列未落库的
                    # retry/pending 任务不落库 → pop_batch 永远空 → 空转 40s（B站实测）
                    await asyncio.sleep(1)
                    continue
                empty_polls = 0
                tasks = [asyncio.create_task(self.processor.process_job(job)) for job in batch]
                # [v2.17 3-A] 任务级看门狗：page_timeout>0 时单页处理超时 → kill+标 retry
                # （防单页挂起冻住批循环；默认 0=关闭零影响）
                _pt = _safe_int_cfg(self.cfg.get("page_timeout", 0), 0)
                if _pt > 0:
                    async def _guarded(task, job):
                        try:
                            await asyncio.wait_for(asyncio.shield(task), timeout=_pt)
                        except asyncio.TimeoutError:
                            task.cancel()
                            # [v2.18 P1-2] cancel 后必须 await 原任务真正退出：
                            # 旧版直接 mark_failed 留下孤儿协程（shutdown 期间继续写已
                            # close 的资源），且与原任务自身的落库顺序竞态
                            _outcome = await asyncio.gather(task, return_exceptions=True)
                            _r = _outcome[0] if _outcome else None
                            if isinstance(_r, asyncio.CancelledError):
                                logger.warning(f"页面处理超时({_pt}s)已击杀: {job.get('normalized_url', '')[:70]}")
                                await self.frontier.mark_failed(job.get('url_hash', ''), retry=True)
                                await self.frontier.write_error(
                                    job.get('url_hash', ''), "PAGE_TIMEOUT",
                                    f"single page exceeded {_pt}s")
                            elif isinstance(_r, BaseException):
                                # 击杀前已自行失败：异常路径在任务内已落库，不重复标
                                logger.warning(f"页面处理超时({_pt}s)后以异常退出: {job.get('normalized_url', '')[:70]}")
                            else:
                                # 超时瞬间恰好完成：结果有效，保留（避免 done→retry 重复爬整页）
                                logger.info(f"页面处理超时({_pt}s)但恰好完成，保留结果: {job.get('normalized_url', '')[:70]}")
                            raise
                    _results = await asyncio.gather(
                        *[_guarded(t, j) for t, j in zip(tasks, batch)],
                        return_exceptions=True)
                else:
                    _results = await asyncio.gather(*tasks, return_exceptions=True)
                # [FIXED & MODIFIED] v2.14 return_exceptions：单任务抛异常不再让 gather 上抛
                # （原其余 49 个协程变孤儿被 shutdown 半途击杀），失败详情在任务内已落 errors 表
                _errs = sum(1 for r in _results if isinstance(r, BaseException))
                if _errs:
                    logger.warning(f"批次内 {_errs}/{len(tasks)} 个任务异常（详见 errors 表）")
                # [v2.13] 批间拟人延迟：对数正态分布（原 uniform 的对称分布在统计检测下
                # 呈"机器味"；真人浏览间隔是右偏长尾——多数快、偶尔拖长）
                import random as _rnd
                try:
                    from .trajectory_engine import lognormal_delay
                    await asyncio.sleep(lognormal_delay(0.35, sigma=0.7, min_v=0.10))
                except Exception:
                    await asyncio.sleep(_rnd.uniform(0.15, 0.7))
                # 每批处理完才 flush，减少写队列压力
                await self.frontier.flush()
                # [FIXED & MODIFIED] v2.15 阶段5 结构化进度信号：stats.jsonl 持久化
                # （GUI 原靠正则反解析 stdout——脆弱耦合；stats 文件是可观测性数据源）
                # [FIXED & MODIFIED] v2.16 修复：原块 time.time() 在无模块级 import time 时
                # 恒 NameError → 文件恒 0 字节（v2.15 遗留）；计数改采 frontier 权威值
                # （_progress 的 pending/total 从不更新 → GUI 进度条恒跳 100%）。
                try:
                    _c = await self.frontier.get_counts()
                    _done, _failed = _c.get("done", 0), _c.get("failed", 0)
                    _pending = _c.get("pending", 0) + _c.get("retry", 0)
                    _total = _done + _failed + _pending + _c.get("dead", 0)
                    # [v2.17 4.2] videos/bytes 采 frontier 权威值（video_downloads completed
                    # 计数+file_size 累计）；images 为页内成图累计计数（避免每批全表扫描）
                    _vs = await self.frontier.get_video_stats()
                    if not append_stats_line(self.project.dir / "stats.jsonl",
                                             _done, _failed, _pending, _total, len(tasks),
                                             videos=_vs.get("completed", 0),
                                             images=getattr(self, "_stats_images", 0),
                                             bytes_total=_vs.get("bytes", 0)):
                        logger.warning("stats.jsonl 写入失败")
                except Exception as _se:
                    logger.warning(f"stats.jsonl 写入失败: {_se}")
        except asyncio.CancelledError:
            logger.info("Cancelled")
        finally:
            # [FIXED & MODIFIED] v2.6.4 等视频 worker 收尾——用户"空文件夹"真正的根因：
            # 页面少时主循环立即 break → shutdown 杀 worker → pending 视频从未下载
            # （vxQTGNB 成功是因为页面多时 worker 轮询期间恰好下载完成，纯运气）
            try:
                import time as _t
                _deadline = _t.monotonic() + 150
                while _t.monotonic() < _deadline:
                    _pv = await self.frontier.get_pending_videos(1)
                    if not _pv:
                        break
                    await asyncio.sleep(5)
            except Exception:
                pass
            # [v2.10.5 P2-12 checkpoint 真实 save：原只 load 打印"Resuming"
            # （save 从未被调 → 假断点）→ 用 frontier 真实统计落盘，下次 run 可续跑。
            # [v2.18 P3-1] 最终 stats 落盘：旧实现只在页面批循环体内写——纯直链媒体
            # 任务（种子直连 yt-dlp，零页面批）立即 break，stats.jsonl 恒 0 字节
            # （单页任务 0 字节 v2.15 遗留真根因）。收尾统一补写最终快照。
            try:
                _c = await self.frontier.get_counts()
                checkpoint.save({"done": _c.get("done", 0), "pending": _c.get("pending", 0) + _c.get("retry", 0)})
            except Exception:
                pass
            try:
                _c = await self.frontier.get_counts()
                _done, _failed = _c.get("done", 0), _c.get("failed", 0)
                _pending = _c.get("pending", 0) + _c.get("retry", 0)
                _total = _done + _failed + _pending + _c.get("dead", 0)
                _vs = await self.frontier.get_video_stats()
                if not append_stats_line(self.project.dir / "stats.jsonl",
                                         _done, _failed, _pending, _total, 0,
                                         videos=_vs.get("completed", 0),
                                         images=getattr(self, "_stats_images", 0),
                                         bytes_total=_vs.get("bytes", 0),
                                         final=True):
                    logger.warning("stats.jsonl 最终快照写入失败")
            except Exception as _se:
                logger.warning(f"stats.jsonl 最终快照写入失败: {_se}")
            await self._graceful_shutdown()
            # [v2.16.1] LLM 批量增强（默认关——GUI 设置页开关；失败/异常不影响任务结果）
            if getattr(self, "_llm_enhancer", None) is not None:
                try:
                    from .llm_enrich import enrich_project
                    _st = await enrich_project(
                        self.project.export_dir, self._llm_enhancer,
                        budget_month=_safe_int_cfg(self.cfg.get("llm_budget_month", 500), 500))
                    logger.info(f"LLM 增强完成: done={_st['done']} failed={_st['failed']} "
                                f"skipped={_st['skipped']} files={_st['files']}")
                except Exception as _le:
                    logger.warning(f"LLM 增强异常（忽略）: {_le}")

    async def _video_download_worker(self):
        while not self._should_stop:
            await asyncio.sleep(5)
            try:
                pending = await self.frontier.get_pending_videos(8)
                # [FIXED & MODIFIED] v2.10.5 P0-2 视频并发提速：原 for 循环逐个 await（串行
                # 单线程，20 核机器同时只下 1 个 4K）→ 改 asyncio.gather 并发处理全部 pending，
                # 由 downloader 内部 Semaphore(8) 自然限流。每任务单独 try，失败不拖垮整批。
                if pending:
                    await asyncio.gather(
                        *[self._download_one_video(video) for video in pending],
                        return_exceptions=True,
                    )
            except Exception as e:
                logger.error(f"Video worker error: {e}")

    async def _download_one_video(self, video):
        """单视频下载（独立任务体——供 gather 并发调用，失败仅标记 failed 不抛出）"""
        try:
            domain = video['domain']
            if await self.frontier.count_video_by_domain(domain) >= \
                    self.project.config.video_settings.max_downloads_per_domain:
                # [v2.19.9 修复] 原实现这里是一个**裸 `return`** —— 行仍留在
                # `status='pending'`。后果两层，第二层才是主祸：
                #  ① **排空阶段白烧满 150 秒**：`crawl()` 的 finally 里
                #     `while ...: _pv = get_pending_videos(1); if not _pv: break`
                #     —— 上限视频永远不清空 ⇒ 那个 break **永远不成立**；
                #  ② 这批视频**永远停在 pending**：进度口径里像"还有没下完的"，
                #     同一 project 目录续爬时还会被重新捞出来再空转一遍。
                # 这正是 `page_processor` 早就写下的那条教训（见
                # `tests/test_skip_vs_failed_and_cookie_truth.py` 第一节）：
                # **「移除动作是对的、标签是错的」——不把任务从 pending 移走就是死循环**。
                # 页面路径学会了，**视频路径从来没跟上**，故这里照同一套办：
                # 记 `skipped`（终态；**不是** failed，更**不是** completed），
                # 且第一次要把原因说出来（静默丢数据是本工程反复禁掉的做法）。
                #
                # 已知边界（如实记录）：worker 每轮取 8 个 ⇒ 超限积压很大时是
                # 8 个/5 秒地清，理论上仍追不上那 150 秒上限。现实里超限量远小于此，
                # 故**刻意不做**批量 SQL（那要动 frontier 接口与 Redis 平价守卫）。
                await self.frontier.update_video_status(video['video_url'], 'skipped')
                if domain not in self._video_cap_warned:
                    self._video_cap_warned.add(domain)
                    logger.warning(
                        f"视频已达每域上限（{domain} 上限 "
                        f"{self.project.config.video_settings.max_downloads_per_domain} 个）"
                        f"——该域**后续视频不再下载**，已记为 skipped（不是失败，"
                        f"产物里不会有它们）。要改就调 video_settings.max_downloads_per_domain")
                return
            vurl = video['video_url']
            # [v2.17 E-P2] 视频链路同样走身份捆绑出口（封锁整包退役反馈在完成/失败点）
            _v_sess = None
            proxy, _v_sess = await self._acquire_identity(domain)
            if not proxy:
                proxy = await self.exit_mgr.acquire_for_domain(domain or "default")
            headers = {"User-Agent": "KianaCrawler/1.0"}
            try:
                from urllib.parse import urlparse as _up2
                _dom = _up2(vurl).netloc.replace("www.", "") or "unknown"
            except Exception:
                _dom = "unknown"
            # [v6 修复] 再兜一层：`_dom` 也可能来自别处（例如 unknown），
            # 统一安全化，杜绝目录名非法导致的整条下载失败。
            from .url_utils import safe_filename as _sfn2
            _vdir = self.project.video_dir / _sfn2(_dom)
            _vdir.mkdir(parents=True, exist_ok=True)
            # [v6 修复·真机实测] **下载前拍快照** —— 下面的兜底只能认"新出现的文件"。
            # 原兜底是 `for _f in _vdir.rglob("*"): if _is_real_video_file(_f): break`
            # —— 它抓的是**目录里第一个**真视频。于是当**本次**产物判定失败时
            # （真机就是那个 46 字节错误页），它会拿**之前下过的别的视频**当本次成果：
            #   · 记 `completed`、file_size 记的是别人的大小；
            #   · 日志刷同一行 `视频下载完成: 021edaa56c4c.mp4 (146.0MB)` 十几次
            #     —— 用户日志里就是这个症状，而每个"完成"其实都没下成。
            # 这是**假成功**：比失败更坏，因为它让用户以为东西下好了。
            try:
                _pristine = set(_vdir.rglob("*"))
            except OSError:
                _pristine = set()
            # Primary: yt-dlp universal downloader
            # [FIXED & MODIFIED] v2.11 画质档接线（preferred_resolution 死配置 → fmt 链）
            # + 副产物开关（字幕/封面/info.json）
            _quality = _quality_for(self.project.config.video_settings.get("preferred_resolution"))
            result = await self.downloader.download_video(
                vurl, quality=_quality,
                want_subs=bool(self.cfg.get("download_subtitles", True)),
                want_thumb=bool(self.cfg.get("download_thumbnail", True)),
                want_infojson=bool(self.cfg.get("write_info_json", True)),
                # [v2.16.1] 代理透传：国内白名单域 None 直连；YT 等出口域名走代理池（媒体流 403 换出口才有效）
                proxy=proxy or "",
            )
            # [FIXED & MODIFIED] v2.6.11 completed 独立验证：不信任 download_video 返回值，
            # 直接检查文件（>1MB + 视频扩展名 + 非 HTML 头）——防误标 completed
            from pathlib import Path as _Path
            _real = None
            if result:
                try:
                    _rp = result if isinstance(result, _Path) else _Path(result)
                    if _is_real_video_file(_rp):
                        _real = _rp
                except Exception:
                    pass
            if _real is None:
                # 兜底：_vdir 里找**本次新出现**的真实视频（yt-dlp 产出但 result 判定失败时）。
                # [v6 修复] 必须排除下载前就存在的文件 —— 否则会把上一次的成果认成本次的
                # （真机表现：日志刷同一个旧文件名，而那些"完成"其实都没下成）。
                try:
                    for _f in _vdir.rglob("*"):
                        if _f in _pristine:
                            continue                      # 下载前就在 → 不是本次的产物
                        if _is_real_video_file(_f):
                            _real = _f
                            break
                except OSError:
                    pass
            if _real:
                await self.frontier.update_video_status(vurl, 'completed', 1.0,
                                                        file_size=_real.stat().st_size)
                self._identity_feedback(_dom, _v_sess, fail=False)  # [v2.17 E-P2] 成功清零
                logger.info(f"视频下载完成: {_real.name} ({_real.stat().st_size/1024/1024:.1f}MB)")
                # [FIXED & MODIFIED] v2.11 视频落盘后置采集：B站评论+弹幕（WBI 签名）
                _bv = _extract_bvid(vurl)
                if _bv:
                    try:
                        # [v2.19 P1] 评论采集任务持强引用（原裸 create_task 可被 GC 静默
                        # 吞掉，且不在 shutdown 取消名单里 → 可能在组件 close 后继续写）
                        _t_cmt = asyncio.create_task(self._collect_bili_comments(_bv))
                        self._emit_bg.add(_t_cmt)
                        _t_cmt.add_done_callback(self._emit_bg.discard)
                    except Exception:
                        pass
                return
            # [FIXED & MODIFIED] v2.6.4 下载失败/文件缺失 → 明确标 failed（可重试，不误标 completed）
            await self.frontier.update_video_status(vurl, 'failed', 0.0)
            logger.warning(f"yt-dlp 未产出文件: {vurl[:70]}")
            # Fallback: m3u8 or direct
            is_m3u8 = ('.m3u8' in vurl or '/hls/' in vurl)
            if is_m3u8:
                filename = sanitize_video_filename(vurl) + ".mp4"
                await self.m3u8_downloader.download(
                    vurl, headers,
                    str(_vdir / filename),
                )
            else:
                filename = sanitize_video_filename(vurl)
                if not any(filename.endswith(ext) for ext in ['.mp4','.mkv','.webm','.flv','.avi','.mp3']):
                    filename += '.mp4'
                await self.media.download_direct(
                    vurl, str(_vdir / filename), proxy=proxy,
                )
            # [FIXED & MODIFIED] v2.6.11 fallback 后独立验证：真实视频才算 completed；
            # 清理垃圾文件（HTML/0字节/分离流残留，豁免副产物与 .part/.ytdl——见 KEEP_SUFFIXES）
            _purge_non_video(_vdir)
            # HTML 垃圾（download_direct 下载的页面内容）删除并标 failed（原 L422 无条件 completed）
            _real2 = None
            try:
                for _f in _vdir.rglob("*"):
                    # [v6 修复] 同上面的兜底：**只认本次新出现的文件**。
                    # 否则会拿上一次下好的视频当本次成果（假成功）。
                    if _f in _pristine:
                        continue
                    if _is_real_video_file(_f):
                        _real2 = _f
                        break
            except OSError:
                pass
            if _real2:
                await self.frontier.update_video_status(vurl, 'completed', 1.0,
                                                        file_size=_real2.stat().st_size)
                self._identity_feedback(_dom, _v_sess, fail=False)  # [v2.17 E-P2]
                logger.info(f"视频下载完成(fallback): {_real2.name} ({_real2.stat().st_size/1024/1024:.1f}MB)")
                return
            # 清理垃圾文件（HTML/0字节/分离流残留，豁免副产物与 .part/.ytdl）
            _purge_non_video(_vdir)
            raise RuntimeError("Direct download failed")
        except Exception as e:
            logger.error(f"Video download failed: {e}")
            await self.frontier.update_video_status(video.get('video_url', ''), 'failed')

    async def _collect_bili_comments(self, bvid):
        """[FIXED & MODIFIED] v2.11 视频落盘后置采集：B站评论+弹幕（WBI 签名）→ exporter jsonl
        风控/未登录时模块内部诚实降级返回 error 字段，不影响视频主流程
        [v2.17 2.4] want_ass 接线：弹幕 ASS 副产物开关（biliass 可选依赖，缺失自动跳过）"""
        try:
            from .comment_danmaku import collect_bili_video_data
            data = await collect_bili_video_data(
                bvid, want_ass=bool(self.cfg.get("bili_danmaku_ass", True)))
            if data.get("ok"):
                self.exporter.add_jsonl("www.bilibili.com", data)
                logger.info(f"B站评论/弹幕已采集: {bvid} 评论={data.get('comment_count', 0)} "
                            f"弹幕={len(data.get('danmaku') or [])}")
            else:
                logger.info(f"B站评论/弹幕未采集({bvid}): {data.get('error')}")
        except Exception as e:
            logger.debug(f"B站评论/弹幕采集跳过({bvid}): {e}")

    def pause(self):
        """[FIXED & MODIFIED] v2.11 暂停：主循环停止取批（已入队任务处理完为止）"""
        self._paused = True

    def resume(self):
        """[FIXED & MODIFIED] v2.11 继续"""
        self._paused = False

    @property
    def paused(self) -> bool:
        return self._paused

    async def _graceful_shutdown(self):
        # [v2.17 稳定性门禁] 幂等守卫：crawler.run 收尾与 run_crawler 均会调用本方法
        # （双跑曾把 frontier.close 双调、后台任务取消二遍）。
        if getattr(self, "_shutdown_done", False):
            return
        self._shutdown_done = True
        self._should_stop = True
        # 恢复原电源计划（爬虫结束后用户日常设置不受影响）
        try:
            from .win32_native import restore_power_plan
            restore_power_plan(getattr(self, '_orig_power_plan', None))
        except Exception:
            pass
        # Flush exporter before shutdown
        if hasattr(self, 'exporter'):
            try:
                self.exporter.flush_all()
            except Exception:
                # [FIXED & MODIFIED] v2.14 except 治理：缓冲区最多 50 条/域的采集数据
                # 静默丢失不可接受（裸 except 连 KeyboardInterrupt 都吞）
                logger.exception("exporter flush 失败——部分缓冲数据可能未落盘")
        self.adaptive.stop()
        self.pressure_controller.stop()
        if getattr(self, 'autoscale_pool', None):
            try:
                self.autoscale_pool.stop()
            except Exception:
                pass
        # 发射结束信号
        self._emit_signal("crawler_stopped", worker_id=self.worker_id)
        # 修复：取消所有后台任务
        # [FIXED & MODIFIED] F15：task.cancel 后 await 加 3s 超时（task 不响应 cancel 时 shutdown 永久挂起）
        # [v2.19 P1] 纳入 fire-and-forget 任务集（_emit_bg：信号发射/autoscale 回调/评论采集）
        # ——此前不在名单里，可能在 exporter.flush_all / frontier.close 之后继续写。
        _bg = list(getattr(self, "_emit_bg", ()))
        for task in [self._monitor_task, self._adaptive_task, self._pressure_task,
                     self._recycle_task, self._autoscale_task, self._proxy_sync_task,
                     *_bg]:
            if task:
                task.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(task), 3)
                except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                    pass
        # [FIXED & MODIFIED] v2.10.6 D2 停止出口健康检查循环（原 shutdown 未调
        # stop_health_check → _check_loop 后台任务泄漏）
        try:
            if self.exit_mgr is not None and hasattr(self.exit_mgr, 'stop_health_check'):
                await asyncio.wait_for(self.exit_mgr.stop_health_check(), 3)
        except Exception:
            pass
        # [FIXED & MODIFIED] v2.11 爬完即删合并 cookies（%TEMP% 明文密钥不跨任务驻留）
        try:
            from .universal_downloader import cleanup_cookie_file
            cleanup_cookie_file()
        except Exception:
            pass
        # [FIXED & MODIFIED] v2.14 关闭超时兜底：media.close 要等在途下载结束（curl 120s
        # ×3 重试最坏挂 6 分钟）→ 整组 close 包 wait_for(30)，防关不掉
        try:
            await asyncio.wait_for(asyncio.gather(
                self.router.close(), self.media.close(),
                self.m3u8_downloader.close(), self.frontier.close(),
                return_exceptions=True,
            ), 30)
        except asyncio.TimeoutError:
            logger.warning("组件关闭超时（30s），继续收尾")
        # [FIXED & MODIFIED] v2.10.5 P1-8 显式停止防御恢复循环（ShieldSoldier 有 async stop 但
        # 无 close——shutdown 名单的 close 探测会跳过它 → 恢复循环任务泄漏）
        try:
            if getattr(self, 'shield', None) and hasattr(self.shield, 'stop'):
                await asyncio.wait_for(self.shield.stop(), 3)
        except Exception:
            pass
        # [FIXED & MODIFIED] F4：体检确认 8 个组件 shutdown 未关闭（Playwright 进程/aiohttp 会话
        # 泄漏 → 多次爬取后内存累积 → OOM/游戏掉帧）——全部补 close
        for _name, _comp in (("protocol", getattr(self, "protocol", None)),
                             ("solver", getattr(self, "solver", None)),
                             ("session_pool", getattr(self, "session_pool", None)),
                             ("exporter", getattr(self, "exporter", None)),
                             ("shield", getattr(self, "shield", None)),
                             ("habakiri", getattr(self, "habakiri", None)),
                             ("sentinel", getattr(self, "sentinel", None)),
                             ("oracle", getattr(self, "oracle", None))):
            if _comp is None:
                continue
            try:
                _c = getattr(_comp, "close", None)
                if _c:
                    _r = _c()
                    if asyncio.iscoroutine(_r):
                        await asyncio.wait_for(_r, 5)
            except Exception as e:
                logger.debug(f"shutdown close {_name}: {e}")
        # 关闭万能下载器（yt-dlp/aiohttp 会话，防止资源泄漏）
        try:
            await self.downloader.close()
        except Exception:
            pass
        # 关闭 LLM 增强会话（防止 aiohttp 资源泄漏）
        # [FIXED & MODIFIED] ai_parser close 已移除（ai_parser 已删）
        logger.info("Shutdown complete.")
