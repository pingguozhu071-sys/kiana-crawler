"""全局配置（增强版）

新增配置组：
  - TLS 轮换设置（多版本指纹池）
  - 代理池设置（轮换间隔、带宽限制、预热）
  - 挑战求解器设置（支持的类型、超时、重试）
  - 自适应控制参数（恢复加速、预测限流）
  - 防御协议参数（层级阈值、冷却时间）
  - 性能调优参数（连接池、HTTP/2、DNS 缓存）
  - 单机极限优化参数（针对 15.4GB RAM / Ultra 7 255H 16 核）
"""
import logging as _logging

# ═══ [v2.16 阶段1] 可移植性：统一数据目录入口 ═══
def data_root() -> str:
    """Kiana 数据/配置根目录：
    - KIANA_PORTABLE=1 → exe 同级 KianaData/（绿色随行，便携版用）
    - 默认 → %LOCALAPPDATA%\\KianaVnextPlus（覆盖安装兼容）
    配置/密钥/DB 路径统一走这里，换机即用。"""
    import os as _os
    import pathlib as _pl
    import sys as _sys
    if _os.environ.get("KIANA_PORTABLE", "") == "1":
        # [v2.19 修复] 便携数据根改为 **exe 同级**。原实现用 sys._MEIPASS——PyInstaller 6
        # 的 onedir 模式下它指向 `_internal/`，而安装器升级时会 `RMDir /r "$INSTDIR\_internal"`
        # （kiana_setup.nsi）→ 便携数据会在升级时被连带删除。exe 同级才是"随行数据"语义。
        if getattr(_sys, "frozen", False):
            base = _pl.Path(_sys.executable).parent
        else:
            base = _pl.Path(__file__).parent.parent
        if not base.is_dir():
            base = base.parent
        return str(base / "KianaData")
    return str(_pl.Path(_os.environ.get("LOCALAPPDATA", str(_pl.Path.home()))) / "KianaVnextPlus")

# ─────────────────────────────────────────────────────────────
# [FIXED & MODIFIED] v2.10.5 P2-10 日志脱敏 Filter（安装一次，全项目 logger 生效）：
#   sanitize_url 此前零调用 → 所有含 token/session/key 的完整 URL 直接进日志。
#   在根 logger 装 Filter，对每条消息做 sanitize_url + sanitize_text 后再输出。
#   （crawler/solver/engine_router/page_processor 打印完整 URL 的日志点全部被覆盖）
# ─────────────────────────────────────────────────────────────
class _SanitizeLogFilter(_logging.Filter):
    def filter(self, record):
        try:
            from .sanitizer import sanitize_url, sanitize_text
            msg = record.getMessage()
            if msg and isinstance(msg, str):
                sanitized = sanitize_text(sanitize_url(msg))
                if sanitized != msg:
                    record.msg = sanitized
                    record.args = ()
        except Exception:
            pass
        return True

def attach_log_sanitizer(handler):
    """[v2.19 安全 P0] 把脱敏 Filter 挂到指定 **handler** 上。

    根因：原实现把 Filter 挂在 root **logger** 上（`_r.addFilter(...)`），但 Python
    logging 的传播只调用祖先的 **handler**、**不调用**祖先 logger 的 filter
    （`Logger.callHandlers` 遍历 handlers；`Logger.filter` 只在记录经该 logger 自身
    处理时调用）→ 全仓 `logging.getLogger(__name__)` 的子 logger 记录**完全未被
    脱敏**，含 `?token=` 的代理 URL / API Key 形态会明文落盘 `*.log`。

    任何新建 handler（CLI/GUI 文件日志）都必须调用本函数。"""
    try:
        if handler is not None and not any(
                isinstance(f, _SanitizeLogFilter) for f in handler.filters):
            handler.addFilter(_SanitizeLogFilter())
    except Exception:
        pass


def _install_handler_autosanitize():
    """[v2.19.3 质检加固] 包装 `logging.Handler.__init__`，使**任何新建 handler**
    （含第三方库自建、以及未来任何忘记显式调用的地方）自动获得脱敏 Filter。

    动机：`attach_log_sanitizer` 需要调用方记得挂——这是**脆弱约定**（质检轮指出）。
    漏挂一次的代价是密钥/凭据明文落盘且无人察觉。改为在 Handler 构造时自动挂载后，
    "忘记挂"这个失效模式被结构性消除。

    安全性：仅包装构造函数（不改 handle/emit 语义）；脱敏逻辑幂等（已脱敏文本再跑一遍
    结果不变）；异常一律吞掉（日志系统自身绝不能因此崩溃）。对第三方库的日志同样生效，
    这正是期望行为（凭据泄漏不分来源）。"""
    try:
        if getattr(_logging.Handler, "_kiana_autosanitized", False):
            return
        _orig_init = _logging.Handler.__init__

        def _patched_init(self, level=_logging.NOTSET):
            _orig_init(self, level)
            try:
                attach_log_sanitizer(self)
            except Exception:
                pass
        _logging.Handler.__init__ = _patched_init
        _logging.Handler._kiana_autosanitized = True
    except Exception:
        pass


def _install_log_sanitizer():
    """给 root logger 的每个 handler 挂脱敏 Filter，并**启用 handler 自动脱敏**
    （之后新建的任何 handler 都会自动带上，见 _install_handler_autosanitize）。
    同时保留 root logger 上的 Filter（兼容旧行为——对直接经 root 记录的日志生效）。"""
    _install_handler_autosanitize()
    try:
        _r = _logging.getLogger()
        for _h in list(_r.handlers):
            attach_log_sanitizer(_h)
        # [v2.19.6 补漏·审查发现] lastResort 在 logging 首次 import 时即创建
        # （早于本 patch），因此在"尚未配置任何 handler"之前的 WARNING+ 日志
        # （走 stderr，GUI 下即界面）此前不受脱敏——顺手挂上。
        _lr = getattr(_logging, "lastResort", None)
        if _lr is not None:
            attach_log_sanitizer(_lr)
        if not any(isinstance(f, _SanitizeLogFilter) for f in _r.filters):
            _r.addFilter(_SanitizeLogFilter())
    except Exception:
        pass

_install_log_sanitizer()

from omegaconf import OmegaConf, DictConfig

# ═══════════════════════════════════════════════════════════════════
# [CONFIG NOTE] 配置卫生历史：v2.10.5~v2.11 删 21 个死键；[v2.17 稳定性门禁 B1] 再删
#   26 个零消费键（dns_servers/doh_endpoint/use_stealth_enhanced/tls_rotation_enabled/
#   tls_impersonate_pool/challenge_solver_types/health_check_*/predictive_*/
#   concurrency_*/connection_pool_size/connection_keepalive_seconds/dns_cache_ttl/
#   retry_max_backoff/export_formats/pressure_check_interval/defense_tier_*_threshold/
#   recovery_* —— 全仓(包/GUI/tools/tests)子串检索证实零消费者，"未接线键"从此清零）。
#   已接线的关键键：max_retries(→ProtocolEngine)、captcha_api_keys(→SolverEngine)、
#   lazy_solver_init(→crawler)、privacy_sanitize(→page_processor)、
#   m3u8_concurrency(→M3U8Downloader)、gpu_acceleration(→M3U8Downloader)。
# ═══════════════════════════════════════════════════════════════════
DEFAULT_GLOBAL = OmegaConf.create({
    # ═══════════════════════════════════════════
    # 基础设置
    # ═══════════════════════════════════════════
    "global_concurrency": 200,        # Ultra 7 255H 20核可支撑
    "temp_dir": "./temp",
    "log_level": "INFO",
    "max_retries": 5,
    "forbid_direct": False,  # 默认允许直连，配置代理后可改为 True
    "max_body_bytes": 5 * 1024 * 1024,

    # ═══════════════════════════════════════════
    # 浏览器引擎设置
    # ═══════════════════════════════════════════
    "browser_pool_size": 5,
    "browser_max_pages_per_context": 30,
    "browser_memory_limit_mb": 2048,  # 2GB — 15.4GB总内存绰绰有余
    "headless": True,
    "use_humanization": True,        # 内置人类行为模拟（interaction_engine）
    "stealth_injection_enabled": True,
    "trajectory_engine_enabled": True,
    "ultimate_evasion_enabled": True,   # 终极绕过引擎（55维全覆写）
    "evasion_dimensions": 55,           # 总绕过维度数

    # ═══════════════════════════════════════════
    # TLS 指纹轮换设置
    # ═══════════════════════════════════════════
    "protocol_engine_impersonate": "chrome136",
    # [v2.17 E-P1-5] 池升级到 curl_cffi 0.16.0 内置最新 chrome 段（136/131/124/120/116——全部
    # 实测 get_fingerprint 可用；safari/firefox 目标待 UA 同源绑定适配后再入；chrome150 需在线 update 拉取）
    "tls_consistency_with_proxy": True,  # TLS 指纹与代理 IP 关联
    # [v2.17 E-P1-5] 在线指纹更新（curl-cffi FingerprintManager.update_fingerprints，免升级拉取
    # 最新浏览器指纹；默认关——开启后每次任务启动时尝试更新一次，失败仅日志不阻塞）
    "fingerprint_update_enabled": False,  # [v2.17 E-P1-5] 在线指纹更新（见上）；默认关不联网
    "cdp_attach": False,           # [v2.17 3.5] CDP 接管既有浏览器（实验默认关——用户已开调试端口 Chrome 时直连复用登录态/指纹）
    "cdp_port": 9222,              # 上述调试端口（Chrome --remote-debugging-port=9222 启动即可）
    "identity_bundle": False,      # [v2.17 E-P2] 身份捆绑轮换（默认关=零影响；开启时出口+cookie捆绑为虚拟用户、封锁整包退役换新）

    # ═══════════════════════════════════════════
    # 连接池设置
    # ═══════════════════════════════════════════
    # [FIXED & MODIFIED] v2.10.6 删除死键：http2_enabled/http2_stream_window_size
    # （curl_cffi 无 H2 定制 API，0 消费者——详见 protocol_engine 注释）
    "retry_backoff_base": 1.0,        # 指数退避基础延迟

    # ═══════════════════════════════════════════
    # 代理池设置
    # ═══════════════════════════════════════════
    "rotation_interval": 600,         # 代理轮换间隔（秒）
    "max_requests_per_proxy": 500,    # 每个代理最大请求数
    "max_bandwidth_mb": 500,          # 每个代理最大带宽 (MB)
    "proxy_warmup_requests": 10,      # 预热期请求数
    "cooldown_threshold": 3,
    "meltdown_threshold": 5,
    "cooldown_seconds": 300,

    # ═══════════════════════════════════════════
    # 挑战求解器设置
    # ═══════════════════════════════════════════
    "challenge_max_wait": 30,
    "captcha_api_keys": {                 # 多打码平台密钥
        "twocaptcha": "",
        "capsolver": "",
        "anticaptcha": "",
    },

    # ═══════════════════════════════════════════
    # 自适应控制设置
    # ═══════════════════════════════════════════
    "adaptive_adjust_interval": 10,
    "adaptive_target_success_rate": 0.95,
    "adaptive_min_concurrency": 3,
    "adaptive_max_concurrency": 100,    # 调低适配 15.4GB

    # ═══════════════════════════════════════════
    # 防御协议设置
    # ═══════════════════════════════════════════
    "defense_mode": "shield",         # shield / habakiri / hybrid
    "master_password": "your_strong_password",

    # ═══════════════════════════════════════════
    # 并发控制设置
    # ═══════════════════════════════════════════
    "min_concurrency": 20,
    "max_concurrency": 100,            # 调低适配 15.4GB
    "per_domain_default": 5,
    "per_exit_default": 10,

    # ═══════════════════════════════════════════
    # 压力控制设置
    # ═══════════════════════════════════════════
    "pressure_enabled": True,
    "mem_threshold_green": 90.0,
    "mem_threshold_yellow": 93.0,
    "mem_threshold_orange": 94.0,
    "mem_threshold_critical": 97.0,
    "cpu_threshold_green": 70.0,
    "cpu_threshold_yellow": 85.0,
    "cpu_threshold_critical": 95.0,

    # ═══════════════════════════════════════════
    # 媒体下载设置
    # ═══════════════════════════════════════════
    "video_download_enabled": True,
    "m3u8_concurrency": 5,            # 5 路并发（15.4GB 调低）
    "gpu_acceleration": True,
    # [v2.16 M6] 数据质量闭环全链：低分页拒收阈值 + 增量缓存 TTL
    "quality_min_score": 0.12,     # DataValidator 低于此分且 text<400 拒收隔离
    "http_cache_ttl": 3600,        # HttpCache 304 协商 TTL 秒（可配）
    # [FIXED & MODIFIED] v2.11 副产物层（副产物随视频同目录落盘）
    "download_subtitles": True,       # B站 AI 字幕 / YouTube 字幕（zh-Hans/zh/en）
    "download_thumbnail": True,       # 封面
    "write_info_json": True,          # 元数据 info.json
    "export_markdown": True,          # 每页 Markdown 快照
    # [FIXED & MODIFIED] v2.11 产物保鲜（历史任务目录自动清理）
    "task_retention_days": 7,         # 任务目录保留天数（0=永不过期）
    "task_retention_max_gb": 50,      # 任务目录总占用上限 GB（0=不限）

    # ═══════════════════════════════════════════
    # 导出设置
    # ═══════════════════════════════════════════

    # ═══════════════════════════════════════════
    # 前沿队列设置
    # ═══════════════════════════════════════════
    "frontier_backend": "sqlite",
    "redis_url": "redis://localhost:6379/0",

    # ═══════════════════════════════════════════
    # 求解引擎设置
    # ═══════════════════════════════════════════

    # ═══════════════════════════════════════════
    # 地理匹配设置
    # ═══════════════════════════════════════════

    # ═══════════════════════════════════════════
    # 隐私设置
    # ═══════════════════════════════════════════
    # [FIXED & MODIFIED] v2.11 删除 5 个零读取的"防护开关"（webrtc/canvas/webgl/audio/font）：
    # 防护脚本在求解器隐身链中无条件全开（evasion_v2 + 55 维链），假开关只会误导配置方。
    "privacy_sanitize": True,        # 内容脱敏开关（手机号/邮箱/IP → 占位符），可关闭以保留原始数据
    "private_dns_resolve_check": True,  # [v2.16.1] SSRF 域名解析校验（域名解析到私网 → 拒抓；解析失败放行防误伤）
    "llm_budget_month": 500,         # [v2.16.1] LLM 月度预算（条数；默认关 llm_enabled=False 时零开销）
    "bili_danmaku_ass": True,        # [v2.17 2.4] B站弹幕 ASS 副产物（biliass 可选依赖——缺失自动跳过，不报错）
    "robots_respect": False,         # [v2.17 E-P1-1] robots.txt 合规（默认关=既有行为；开启后链接入队前按域缓存判定）
    "sitemap_discover": False,       # [v2.17 E-P1-2] sitemap 种子播种（默认关；开启后种子域自动发现站点地图并批量入队）
    "crawl_strategy": "bfs",         # [v2.17 E-P1-4] 爬行策略（bfs=默认入队序/dfs=后入先出近似深度/bff=优先值降序）
    "dynamic_priority": False,   # [v2.17 B4b] 证据驱动动态优先级（默认关；规则命中提前/空壳后排）
    "proxy_fetcher_enabled": False,  # [v2.17 3-C] 代理源拉取器（默认关；开启后按 interval 自动注池换批）
    "proxy_source": "",            # 代理源配置串：api:https://provider/api?token=xxx,ip:port,...
    "proxy_fetch_interval": 600,   # 拉取间隔（秒）
    "llm_link_scoring": False,    # [v2.17 2-B] LLM 链接打分（默认关；种子级批处理，未配置降级）
    "llm_link_budget": 64,        # 上述打分单轮预算（条数）
    # [v2.19 P1] 单页处理超时看门狗：由 0（关闭）改为 300s 默认。
    # 原默认 0 意味着"一页挂起即冻住整批 gather"（无任何兜底）。取 300s 而非更小值：
    # 单页正常耗时 = 拟人延迟(≤45s) + 抓取 + 解析 + 导出，通常 < 60s，300s 留 5 倍余量，
    # 避免误杀慢站/大页面；同时足以兜住真正的挂起。
    # [v2.19.8 修复] "设为 0 可恢复完全关闭"此前**是假的**：CLI 的 `--page-timeout 0` 与
    # 配置文件里的 page_timeout 都到不了引擎（两条入口的键表都缺这个键），只能改这里。
    # 现已双向接线（run_crawler.page_timeout_override / launcher_v8 翻译表）：
    #   CLI  `--page-timeout 0`；GUI/配置文件 `"page_timeout": 0`  → 真正关闭看门狗。
    # 本键仍是唯一默认源：两处入口不指定时都用这里的值。
    "page_timeout": 300,
    "smart_adaptive_tuning": False,  # [v2.17 0-6] 保留旧 smart_adaptive 调参循环（默认关——三方互搏纠偏；仅取证用）

    # ═══════════════════════════════════════════
    # 单机极限优化（针对 15.4GB RAM / Ultra 7 255H 16 核）
    # ═══════════════════════════════════════════
    "single_machine_optimized": True,
    "gc_threshold_generation0": 700,  # GC 第 0 代阈值
    "gc_threshold_generation1": 10,
    "gc_threshold_generation2": 5,
    "memory_recycle_interval": 300,   # 内存回收检查间隔（秒）
})


class GlobalConfig:
    """全局配置，支持动态属性访问与合并更新

    使用 OmegaConf 合并默认配置与用户配置，支持点路径访问：
        cfg.global_concurrency
        cfg.tls_rotation_enabled
        cfg.health_check_interval
    """

    def __init__(self, cfg: DictConfig = None):
        self.cfg = OmegaConf.merge(DEFAULT_GLOBAL, cfg if cfg else {})
        # [FIXED & MODIFIED] v2.10.5c master_password 安全化：默认"your_strong_password"
        # 是公开字面量（SentinelBrain/OracleBrain 授权用）→ 首次运行时随机生成。
        # [FIXED & MODIFIED] v2.11 DPAPI 加密落盘（master.key.bin）：原明文 master.key
        # 任何本机进程可读；加密后密钥由当前 Windows 用户持有，拷到别的机器无法解密。
        # 旧明文 master.key 迁移后立即删除。
        if cfg is None or not cfg.get("master_password") or cfg.get("master_password") == "your_strong_password":
            try:
                import os
                import pathlib
                import secrets
                _dir = pathlib.Path(data_root())   # [v2.16] 统一数据根（便携版支持）
                _bin = _dir / "master.key.bin"
                _plain = _dir / "master.key"
                if _bin.exists():
                    from .privacy_store import load_protected
                    self.cfg.master_password = load_protected(_bin).strip()
                elif _plain.exists():
                    _pw = _plain.read_text(encoding="utf-8").strip()
                    try:
                        from .privacy_store import save_protected
                        save_protected(_bin, _pw)
                        _plain.unlink(missing_ok=True)  # 明文迁移后即删
                    except Exception:
                        pass  # DPAPI 不可用（非 Windows）→ 保留明文
                    self.cfg.master_password = _pw
                else:
                    _pw = secrets.token_urlsafe(24)
                    try:
                        from .privacy_store import save_protected
                        save_protected(_bin, _pw)
                    except Exception:
                        _dir.mkdir(parents=True, exist_ok=True)
                        (_dir / "master.key").write_text(_pw, encoding="utf-8")
                    self.cfg.master_password = _pw
            except Exception:
                pass

    def __getattr__(self, name):
        # __getattr__ 仅在常规查找失败时调用
        if name.startswith("_"):
            # 下划线属性（库内部协议如 _is_none/_get_node）交给默认机制，不拦截
            raise AttributeError(name)
        cfg = super().__getattribute__("cfg")
        val = cfg.get(name)
        if val is None:
            raise AttributeError(f"'{type(self).__name__}' has no attribute '{name}'")
        return val

    def get(self, key, default=None):
        """安全获取配置项"""
        return self.cfg.get(key, default)

    def update(self, updates: dict):
        """合并更新配置"""
        self.cfg = OmegaConf.merge(self.cfg, OmegaConf.create(updates))

    # [v2.19 清理] 删除两个死访问器 get_tls_pool() / get_challenge_types()：
    # 它们读取的键（tls_rotation_enabled / tls_impersonate_pool / challenge_solver_types）
    # 已在 v2.17 配置卫生轮删除 → 方法**永远走 fallback**，且全仓零调用（仅定义无消费者），
    # 保留会误导维护者以为配置生效。TLS 指纹与挑战类型实际由 fingerprint_consistency /
    # solver_engine 自行决定。

    def get_captcha_api_keys(self) -> dict:
        """获取打码 API 密钥配置（返回普通字典）"""
        from omegaconf import OmegaConf as _OC
        keys = self.cfg.get("captcha_api_keys", {})
        if keys is None:
            return {}
        return _OC.to_container(keys, resolve=True) if hasattr(keys, '_metadata') else dict(keys)

    def get_total_challenge_types(self) -> int:
        """获取启用的挑战类型总数"""
        return len(self.cfg.get("challenge_solver_types", []))

    def is_defense_mode(self, mode: str) -> bool:
        """检查当前防御模式"""
        return self.cfg.get("defense_mode", "shield") == mode

    def get_memory_thresholds(self) -> dict:
        """获取内存阈值（四档：green/yellow/orange/critical）"""
        return {
            "green": self.cfg.get("mem_threshold_green", 90.0),
            "yellow": self.cfg.get("mem_threshold_yellow", 93.0),
            "orange": self.cfg.get("mem_threshold_orange", 94.0),
            "critical": self.cfg.get("mem_threshold_critical", 97.0),
        }

    def to_dict(self) -> dict:
        """转换为普通字典（用于序列化）"""
        return OmegaConf.to_container(self.cfg, resolve=True)
