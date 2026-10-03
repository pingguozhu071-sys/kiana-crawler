"""出口代理管理器（增强版）

支持粘性会话、地理匹配、健康评分、自动健康检查、智能轮换触发、
带宽追踪、自适应冷却和代理池预热。

核心增强：
  - 周期性健康探测（自动检测死代理）
  - 地理一致性（代理地理与指纹时区匹配）
  - 智能轮换触发（基于请求计数、时间、健康评分、带宽）
  - 自适应冷却（不同失败类型使用不同冷却时长）
  - 带宽追踪（防止单个代理带宽耗尽）
  - 代理池预热（新代理渐进式加载）
"""

import asyncio
import random
import time
import logging
from collections import deque
from typing import Optional, Dict, List, Tuple
from enum import Enum

logger = logging.getLogger(__name__)


def _safe_proxy(proxy_url: str) -> str:
    """[FIXED & MODIFIED] v2.10.5 P2-10 代理 URL 日志脱敏：剥离 user:pass@ 凭据，仅显示 host"""
    try:
        from urllib.parse import urlsplit
        u = urlsplit(proxy_url)
        if u.username or u.password:
            return f"{u.scheme}://***@{(u.hostname or '')}{':' + str(u.port) if u.port else ''}"
        return proxy_url
    except Exception:
        return proxy_url

# [FIXED & MODIFIED] v2.9.2 国内站直连白名单：这些域名不走代理（海外代理 IP 访问国内站
# 触发风控——B站 GeeTest 实测；且国内站直连更快）。子域名自动匹配（www.bilibili.com 等）
# [FIXED & MODIFIED] v2.10.5 P0-3 扩充到 40+：csdn/douban/xiaohongshu/toutiao/meituan/
# dianping/ctrip/gitee/smzdm/cnblogs/mihoyo 等国内门户/平台——海外 IP 访问这些同样秒风控
_DOMESTIC_DOMAINS = (
    "bilibili.com", "bilivideo.com", "hdslb.com", "b23.tv",        # B站系
    "taobao.com", "tmall.com", "jd.com", "pinduoduo.com",          # 电商
    "weibo.com", "zhihu.com", "douyin.com", "baidu.com", "qq.com",
    "163.com", "sina.com.cn", "sohu.com", "youku.com", "iqiyi.com",
    "tencent.com", "aliyun.com", "cn", "com.cn", "net.cn",          # 国内 TLD/大站
    # [FIXED & MODIFIED] v2.10.5 P0-3 补充（海外 IP 访问必风控的国内门户/社区/工具）
    "csdn.net", "douban.com", "xiaohongshu.com", "xhslink.com", "toutiao.com",
    "meituan.com", "dianping.com", "ctrip.com", "gitee.com", "smzdm.com",
    "cnblogs.com", "mihoyo.com", "bilibili.tv", "kuaishou.com", "gifshow.com",
    "zhipin.com", "zhihu.com", "b612.net", "163.com", "ximalaya.com",
    "douban.com", "bilibili.tv", "hupu.com", "dota2.com.cn", "nvidia.com",
    "steamcommunity.com", "battle.net", "netease.com", "boke.com", "vip.com",
    "suning.com", "xiaomi.com", "huawei.com", "lenovo.com", "bytedance.com",
    # [v2.17 1-1] 新规则站点直连白名单（36kr/sspai/微信文章曾不在表→走海外代理必风控）
    "ifeng.com", "36kr.com", "sspai.com", "weixin.qq.com", "mp.weixin.qq.com",
)


class ExitState(Enum):
    """代理状态"""
    WARMING = "warming"      # 预热中（新加入，请求量逐渐增加）
    ACTIVE = "active"        # 活跃（正常使用）
    COOLDOWN = "cooldown"    # 冷却中（暂时不可用）
    MELTDOWN = "meltdown"    # 熔断（严重故障，长时间冷却）
    DISABLED = "disabled"    # 禁用（永久不可用）


class ExitNode:
    """代理出口节点

    追踪代理的健康状态、请求统计、带宽使用和地理信息。
    """

    __slots__ = [
        'proxy_url', 'group', 'geo_country', 'geo_region', 'state', 'score',
        'successes', 'failures', 'latencies',
        'consecutive_fails', 'cooldown_until', 'inflight',
        'total_requests', 'total_bytes', 'last_used', 'last_health_check',
        'warmup_count', 'max_warmup', 'rotation_due_at',
    ]

    def __init__(self, proxy_url, group="default", geo_country=None, geo_region=None):
        self.proxy_url = proxy_url
        self.group = group
        self.geo_country = geo_country
        self.geo_region = geo_region
        self.state = ExitState.WARMING
        self.score = 50.0
        self.successes = deque(maxlen=100)
        self.failures = deque(maxlen=100)
        self.latencies = deque(maxlen=100)
        self.consecutive_fails = 0
        self.cooldown_until = 0
        self.inflight = 0
        # 增强字段
        self.total_requests = 0
        self.total_bytes = 0
        self.last_used = 0
        self.last_health_check = 0
        self.warmup_count = 0
        self.max_warmup = 10  # 预热期 10 个请求
        self.rotation_due_at = 0  # 下次轮换时间

    def update_score(self):
        """更新健康评分 (0-100)"""
        total = len(self.successes) + len(self.failures)
        if total == 0:
            return
        sr = len(self.successes) / total
        avg_lat = sum(self.latencies) / len(self.latencies) if self.latencies else 1.0
        # 成功率 50 分 + 速度 30 分 + 稳定性 20 分
        speed_score = min(30.0, 10.0 / (avg_lat + 0.1))
        stability_score = max(0.0, 20.0 - self.consecutive_fails * 5)
        self.score = max(0.0, min(100.0, sr * 50 + speed_score + stability_score))

    @property
    def is_warming(self) -> bool:
        """是否在预热期"""
        return self.state == ExitState.WARMING and self.warmup_count < self.max_warmup

    @property
    def bandwidth_mb(self) -> float:
        """已用带宽 (MB)"""
        return self.total_bytes / (1024 * 1024)

    @property
    def needs_rotation(self) -> bool:
        """是否需要轮换"""
        now = time.time()
        return (
            self.rotation_due_at > 0 and now >= self.rotation_due_at
            or self.total_requests > 0 and self.total_requests % 500 == 0
        )

    def mark_used(self, bytes_transferred: int = 0):
        """标记代理被使用"""
        self.total_requests += 1
        self.total_bytes += bytes_transferred
        self.last_used = time.time()
        if self.is_warming:
            self.warmup_count += 1
            if self.warmup_count >= self.max_warmup:
                self.state = ExitState.ACTIVE
                logger.debug(f"Exit {_safe_proxy(self.proxy_url)} warmed up → ACTIVE")

    def schedule_rotation(self, delay_seconds: int = 0):
        """安排轮换"""
        self.rotation_due_at = time.time() + delay_seconds


class NoAvailableExit(Exception):
    """无可用代理"""
    pass


class HealthChecker:
    """代理健康检查器

    周期性探测所有代理，自动标记死代理和恢复代理。
    """

    def __init__(self, exit_manager, check_interval: int = 60,
                 probe_url: str = "https://httpbin.org/ip",
                 probe_timeout: int = 10):
        self.exit_manager = exit_manager
        self.check_interval = check_interval
        self.probe_url = probe_url
        self.probe_timeout = probe_timeout
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self):
        """启动健康检查循环"""
        self._running = True
        self._task = asyncio.create_task(self._check_loop())

    async def stop(self):
        """停止健康检查"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _check_loop(self):
        """主检查循环"""
        while self._running:
            await asyncio.sleep(self.check_interval)
            await self.check_all()

    async def check_all(self):
        """检查所有代理"""
        nodes = list(self.exit_manager.nodes.values())
        if not nodes:
            return

        tasks = [self._probe_node(node) for node in nodes]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        alive_count = sum(1 for r in results if r is True)
        dead_count = len(nodes) - alive_count
        logger.info(f"Health check: {alive_count} alive, {dead_count} dead out of {len(nodes)}")

    async def _probe_node(self, node: ExitNode) -> bool:
        """探测单个代理"""
        # 跳过正在使用的代理
        if node.inflight > 0:
            node.last_health_check = time.time()
            return True

        try:
            # 使用 protocol engine 发起探测请求
            if hasattr(self.exit_manager, '_protocol_engine') and self.exit_manager._protocol_engine:
                resp = await self.exit_manager._protocol_engine.fetch(
                    self.probe_url, proxy=node.proxy_url
                )
                alive = resp.status_code == 200
            else:
                # 无 protocol engine，使用简单的 TCP 连接检查
                alive = await self._tcp_check(node.proxy_url)

            node.last_health_check = time.time()

            if alive:
                if node.state in (ExitState.COOLDOWN, ExitState.MELTDOWN):
                    node.state = ExitState.WARMING
                    node.warmup_count = 0
                    node.consecutive_fails = 0
                    logger.info(f"Health check: {_safe_proxy(node.proxy_url)} recovered → WARMING")
            else:
                node.consecutive_fails += 1
                if node.consecutive_fails >= 3:
                    node.state = ExitState.DISABLED
                    logger.warning(f"Health check: {_safe_proxy(node.proxy_url)} DISABLED (3 consecutive probe failures)")

            return alive

        except Exception as e:
            logger.debug(f"Health probe failed for {_safe_proxy(node.proxy_url)}: {e}")
            node.consecutive_fails += 1
            return False

    async def _tcp_check(self, proxy_url: str) -> bool:
        """简单的 TCP 连接检查"""
        try:
            # 解析代理 URL
            import urllib.parse
            parsed = urllib.parse.urlparse(proxy_url)
            host = parsed.hostname
            port = parsed.port or 8080

            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=self.probe_timeout
            )
            writer.close()
            await writer.wait_closed()
            return True
        except Exception:
            return False


class GeoConsistencyManager:
    """地理一致性管理器

    确保代理的地理位置与浏览器指纹的时区/语言一致。
    """

    # 地理区域到时区/语言的映射
    GEO_MAPPING = {
        "US": ("America/New_York", "en-US"),
        "GB": ("Europe/London", "en-GB"),
        "DE": ("Europe/Berlin", "de-DE"),
        "FR": ("Europe/Paris", "fr-FR"),
        "JP": ("Asia/Tokyo", "ja-JP"),
        "KR": ("Asia/Seoul", "ko-KR"),
        "CN": ("Asia/Shanghai", "zh-CN"),
        "HK": ("Asia/Hong_Kong", "zh-HK"),
        "TW": ("Asia/Taipei", "zh-TW"),
        "SG": ("Asia/Singapore", "en-SG"),
        "AU": ("Australia/Sydney", "en-AU"),
        "CA": ("America/Toronto", "en-CA"),
        "NL": ("Europe/Amsterdam", "nl-NL"),
        "RU": ("Europe/Moscow", "ru-RU"),
        "BR": ("America/Sao_Paulo", "pt-BR"),
        "IN": ("Asia/Kolkata", "hi-IN"),
    }

    @classmethod
    def get_geo_info(cls, country: str) -> Tuple[str, str]:
        """获取国家的时区和语言"""
        return cls.GEO_MAPPING.get(country, ("America/New_York", "en-US"))

    @classmethod
    def match_fingerprint(cls, node: ExitNode, fingerprint: dict) -> bool:
        """检查代理地理是否与指纹一致"""
        if not node.geo_country:
            return True  # 无地理信息，不限制

        fp_timezone = fingerprint.get("timezone", "")
        fp_language = fingerprint.get("language", "")

        expected_tz, expected_lang = cls.get_geo_info(node.geo_country)

        # 时区前缀匹配
        tz_match = (
            not fp_timezone
            or fp_timezone == expected_tz
            or fp_timezone.split("/")[0] == expected_tz.split("/")[0]
        )

        # 语言前缀匹配
        lang_match = (
            not fp_language
            or fp_language == expected_lang
            or fp_language.split("-")[0] == expected_lang.split("-")[0]
        )

        return tz_match and lang_match


class ExitManager:
    """出口代理管理器（增强版）

    支持粘性会话、地理匹配、健康评分、自动健康检查、智能轮换。
    """

    def __init__(self, project_config):
        self.config = project_config
        self.nodes: Dict[str, ExitNode] = {}
        self._active_nodes: List[ExitNode] = []
        self.sticky: Dict[str, str] = {}
        self._lock = asyncio.Lock()
        self.forbid_direct = True
        self.cooldown_threshold = 3
        self.meltdown_threshold = 5
        self.cooldown_seconds = 300
        self._health_checker: Optional[HealthChecker] = None
        self._protocol_engine = None
        self._rotation_interval = 600  # 默认 10 分钟轮换一次
        self._max_requests_per_proxy = 500  # 每个代理最大请求数
        self._max_bandwidth_mb = 500  # 每个代理最大带宽 (MB)
        self._sticky_enabled = True  # 粘性会话默认开启

        # 从配置中读取参数
        if hasattr(project_config, 'get'):
            self.cooldown_threshold = project_config.get("cooldown_threshold", 3)
            self.meltdown_threshold = project_config.get("meltdown_threshold", 5)
            self.cooldown_seconds = project_config.get("cooldown_seconds", 300)
            self._rotation_interval = project_config.get("rotation_interval", 600)
            self._max_requests_per_proxy = project_config.get("max_requests_per_proxy", 500)
            self._max_bandwidth_mb = project_config.get("max_bandwidth_mb", 500)

        # 检查是否有 privacy.proxy_sticky 配置
        if hasattr(project_config, 'privacy'):
            privacy = project_config.privacy
            if hasattr(privacy, 'proxy_sticky'):
                self._sticky_enabled = bool(privacy.proxy_sticky)

    def set_protocol_engine(self, engine):
        """设置协议引擎用于健康检查"""
        self._protocol_engine = engine

    def add_node(self, proxy_url, group="default", geo_country=None, geo_region=None):
        """添加代理节点"""
        node = ExitNode(proxy_url, group, geo_country, geo_region)
        self.nodes[proxy_url] = node
        self._active_nodes.append(node)
        # 安排初始轮换时间
        node.schedule_rotation(self._rotation_interval)
        logger.info(f"Added exit: {_safe_proxy(proxy_url)} (geo={geo_country})")

    async def acquire_for_domain(self, domain, expected_country=None, strict=False,
                                  fingerprint=None) -> Optional[str]:
        """为域名获取代理

        Args:
            domain: 目标域名
            expected_country: 期望的地理国家代码
            strict: 是否严格地理匹配
            fingerprint: 浏览器指纹（用于地理一致性检查）

        Returns:
            代理 URL，或 None（如果允许直连）
        """
        async with self._lock:
            return await self._acquire_internal(
                domain, expected_country, strict, fingerprint, check_sticky=True
            )

    async def _acquire_internal(self, domain, expected_country=None, strict=False,
                                 fingerprint=None, check_sticky=True) -> Optional[str]:
        """内部获取代理逻辑（调用者必须已持有锁）"""
        now = time.time()

        # [FIXED & MODIFIED] v2.9.2 国内站直连规则：B站/淘宝/京东等国内域名的出口若走
        # 海外代理（如 SakuraCat 美国节点）→ 触发风控（B站 GeeTest 实测）——国内站
        # 一律直连（速度快 + 不触发海外 IP 风控），国外站才走代理
        if domain:
            _d = domain.lower()
            if any(_d == k or _d.endswith("." + k) for k in _DOMESTIC_DOMAINS):
                return None  # 国内站直连（不隐藏 IP 也无妨——访问国内站无需隐身）

        # 1. 检查粘性会话
        if check_sticky and self._sticky_enabled and domain in self.sticky:
            sticky_url = self.sticky[domain]
            node = self.nodes.get(sticky_url)
            if node and node.state in (ExitState.WARMING, ExitState.ACTIVE):
                # 检查是否需要轮换
                if node.needs_rotation or node.bandwidth_mb >= self._max_bandwidth_mb:
                    logger.info(f"Sticky rotation triggered for {domain}")
                    self.sticky.pop(domain, None)
                else:
                    node.inflight += 1
                    node.mark_used()
                    return sticky_url

        # 2. 收集候选代理
        candidates = []
        _expiring = []  # [v2.16.1] 轮换窗口内节点（备用兜底，不空手返回）
        for node in self._active_nodes:
            # 恢复冷却期到期的代理
            if node.state == ExitState.COOLDOWN and node.cooldown_until <= now:
                node.state = ExitState.WARMING
                node.warmup_count = 0
                logger.info(f"Exit {_safe_proxy(node.proxy_url)} cooldown expired → WARMING")

            if node.state in (ExitState.WARMING, ExitState.ACTIVE):
                # 地理匹配
                if expected_country and node.geo_country != expected_country:
                    continue

                # 地理一致性检查（与指纹匹配）
                if fingerprint and not GeoConsistencyManager.match_fingerprint(node, fingerprint):
                    continue

                # 检查带宽限制
                if node.bandwidth_mb >= self._max_bandwidth_mb:
                    node.schedule_rotation(0)  # 立即轮换
                    continue

                # [v2.16.1] 30s 预检：轮换窗口内的节点优先排除（用未过期节点——
                # 通用做法：给过期判定留一段"提前量缓冲"，避免踩点换节点）
                if node.rotation_due_at and node.rotation_due_at <= now + 30:
                    _expiring.append(node)
                    continue

                candidates.append(node)

        # [v2.16.1] 非空手兜底：所有节点都进入轮换窗口时照常使用（返回 None 直连更差）
        if not candidates and _expiring:
            candidates = _expiring

        # 3. 严格模式检查
        if expected_country and strict and not candidates:
            raise NoAvailableExit("no strict geo-matched proxy")

        if not candidates:
            if self.forbid_direct:
                raise NoAvailableExit("no exit available")
            return None

        # 4. 加权随机选择
        # 预热期代理使用较低权重，避免新代理过载
        weights = []
        for node in candidates:
            base_weight = max(node.score, 0.1)
            if node.is_warming:
                base_weight *= 0.3  # 预热期降低权重
            weights.append(base_weight)

        chosen = random.choices(candidates, weights=weights, k=1)[0]
        chosen.inflight += 1
        chosen.mark_used()
        self.sticky[domain] = chosen.proxy_url
        return chosen.proxy_url

    async def release(self, proxy_url):
        """释放代理"""
        if not proxy_url:
            return
        async with self._lock:
            node = self.nodes.get(proxy_url)
            if node:
                node.inflight = max(0, node.inflight - 1)

    async def report_result(self, proxy_url, success, latency=0, error_type=None,
                             bytes_transferred=0):
        """报告请求结果

        Args:
            proxy_url: 代理 URL
            success: 是否成功
            latency: 延迟（秒）
            error_type: 错误类型（用于自适应冷却）
            bytes_transferred: 传输字节数
        """
        if not proxy_url:
            return
        async with self._lock:
            node = self.nodes.get(proxy_url)
            if not node:
                return

            if bytes_transferred > 0:
                node.total_bytes += bytes_transferred

            if success:
                node.successes.append(1)
                node.consecutive_fails = 0
                # 修复：mark_used() 已处理 warmup_count 递增和状态转换
                # 此处仅处理从非活跃状态恢复的情况
                if node.state == ExitState.COOLDOWN:
                    node.state = ExitState.WARMING
                    node.warmup_count = 0
                elif node.state == ExitState.MELTDOWN:
                    node.state = ExitState.WARMING
                    node.warmup_count = 0
                    logger.info(f"Exit {_safe_proxy(proxy_url)} recovered from MELTDOWN → WARMING")
            else:
                node.failures.append(1)
                node.consecutive_fails += 1

                # 自适应冷却：不同错误类型使用不同冷却时长
                cooldown_duration = self._get_adaptive_cooldown(error_type)

                if node.consecutive_fails >= self.meltdown_threshold:
                    node.state = ExitState.MELTDOWN
                    node.cooldown_until = time.time() + cooldown_duration * 3
                    logger.warning(
                        f"Exit {_safe_proxy(proxy_url)} MELTDOWN after {node.consecutive_fails} "
                        f"consecutive fails (error={error_type})"
                    )
                elif node.consecutive_fails >= self.cooldown_threshold:
                    node.state = ExitState.COOLDOWN
                    node.cooldown_until = time.time() + cooldown_duration
                    logger.info(
                        f"Exit {_safe_proxy(proxy_url)} COOLDOWN for {cooldown_duration}s "
                        f"(error={error_type})"
                    )

            if latency > 0:
                node.latencies.append(latency)
            node.update_score()

    def _get_adaptive_cooldown(self, error_type: Optional[str]) -> int:
        """根据错误类型获取自适应冷却时长

        不同错误类型代表不同的严重程度：
        - connection_error: 网络问题，短冷却
        - timeout: 超时，中等冷却
        - 403/429: 被封禁，长冷却
        - ssl_error: SSL 问题，长冷却
        - 其他: 默认冷却
        """
        cooldown_map = {
            "connection_error": 60,
            "timeout": 120,
            "403": 300,
            "429": 300,
            "ssl_error": 600,
            "captcha": 600,
            "challenge": 300,
        }
        if error_type and error_type in cooldown_map:
            return cooldown_map[error_type]
        return self.cooldown_seconds

    async def get_inflight(self, proxy_url):
        """获取代理的 inflight 请求数"""
        async with self._lock:
            node = self.nodes.get(proxy_url)
            return node.inflight if node else 0

    async def start_health_check(self, interval: int = 60):
        """启动健康检查"""
        if self._health_checker:
            await self.stop_health_check()
        self._health_checker = HealthChecker(self, check_interval=interval)
        await self._health_checker.start()
        logger.info(f"Health checker started (interval={interval}s)")

    async def stop_health_check(self):
        """停止健康检查"""
        if self._health_checker:
            await self._health_checker.stop()
            self._health_checker = None

    def get_stats(self) -> dict:
        """获取代理池统计
        [v2.18 P3-11] GUI 跨线程调用时直接迭代 self.nodes 会撞 "dictionary changed
        size during iteration"（RuntimeError 被上层吞掉 → 面板随机显示旧数据）——
        先取快照再迭代。"""
        _nodes = list(self.nodes.values())
        total = len(_nodes)
        if total == 0:
            return {"total": 0}

        states = {}
        for node in _nodes:
            state_name = node.state.value
            states[state_name] = states.get(state_name, 0) + 1

        avg_score = sum(n.score for n in _nodes) / total
        total_requests = sum(n.total_requests for n in _nodes)
        total_bw = sum(n.bandwidth_mb for n in _nodes)

        return {
            "total": total,
            "states": states,
            "avg_score": round(avg_score, 1),
            "total_requests": total_requests,
            "total_bandwidth_mb": round(total_bw, 1),
            "active_count": states.get("active", 0) + states.get("warming", 0),
        }

    async def force_rotate_domain(self, domain: str) -> Optional[str]:
        """强制为域名轮换代理"""
        async with self._lock:
            old_proxy = self.sticky.pop(domain, None)
            if old_proxy:
                logger.info(f"Force rotated {domain}: {_safe_proxy(old_proxy)} → new")
            # 使用内部方法获取新代理（已持有锁，不重复获取）
            return await self._acquire_internal(domain, check_sticky=False)

    async def disable_node(self, proxy_url: str, reason: str = "manual"):
        """禁用代理节点"""
        async with self._lock:
            node = self.nodes.get(proxy_url)
            if node:
                node.state = ExitState.DISABLED
                logger.warning(f"Exit {_safe_proxy(proxy_url)} DISABLED ({reason})")
                # 清除所有使用此代理的粘性会话
                domains_to_clear = [
                    d for d, p in self.sticky.items() if p == proxy_url
                ]
                for d in domains_to_clear:
                    self.sticky.pop(d, None)
