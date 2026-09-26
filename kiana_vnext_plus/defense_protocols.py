"""防御协议模块（增强版）

四级分层防御 + 自动恢复 + 域名健康追踪 + 模式学习。
与 smart_adaptive.py 的 DomainHealth 系统深度集成。

防御层级：
  GREEN  (0-3)  → 正常运行，无需干预
  YELLOW (4-5)  → 轻量护盾，域名级降并发 + 延迟增加
  ORANGE (6-7)  → 中量护盾，全局降并发 + 代理轮换 + 挑战求解
  RED    (8-10) → 全面护盾，最低并发 + 代理熔断 + 进入冷却

恢复机制：
  - 冷却计时器到期后自动进入恢复阶段
  - 恢复阶段逐步提升并发，监控是否再次被封
  - 若恢复期间再次被封，自动升级防御层级
  - 模式学习：记录封禁时间间隔，预测下一次封禁窗口
"""

import asyncio
import logging
import time
from collections import deque, defaultdict
from enum import IntEnum
from typing import Optional, Dict

from .sanitizer import sanitize_proxy

logger = logging.getLogger(__name__)


class DefenseTier(IntEnum):
    """防御层级"""
    GREEN = 0   # 正常
    YELLOW = 1  # 轻量护盾
    ORANGE = 2  # 中量护盾
    RED = 3     # 全面护盾


# 各层级对应的并发限制
TIER_CONCURRENCY = {
    DefenseTier.GREEN:  None,    # 不调整
    DefenseTier.YELLOW: {"global": None, "domain": 3},   # 域名级降到 3
    DefenseTier.ORANGE: {"global": 30, "domain": 1},     # 全局降到 30，域名降到 1
    DefenseTier.RED:    {"global": 5,  "domain": 1},     # 全局降到 5，域名降到 1
}

# 各层级对应的冷却时间（秒）
TIER_COOLDOWN = {
    DefenseTier.YELLOW: 60,
    DefenseTier.ORANGE: 180,
    DefenseTier.RED: 600,
}

# 各层级对应的请求间延迟（秒）
TIER_DELAY = {
    DefenseTier.GREEN:  0,
    DefenseTier.YELLOW: 1.0,
    DefenseTier.ORANGE: 3.0,
    DefenseTier.RED:    8.0,
}


class DomainDefenseState:
    """域名防御状态追踪器

    追踪每个域名的防御层级、封禁历史、恢复进度和模式学习数据。
    """

    __slots__ = [
        'domain', 'tier', 'tier_since', 'cooldown_until',
        'block_count', 'block_history', 'recovery_attempts',
        'last_block_interval', 'predicted_block_window',
        'total_blocks', 'total_recoveries',
    ]

    def __init__(self, domain: str):
        self.domain = domain
        self.tier = DefenseTier.GREEN
        self.tier_since = time.monotonic()
        self.cooldown_until = 0.0
        self.block_count = 0
        self.block_history: deque = deque(maxlen=30)  # (timestamp, tier, duration)
        self.recovery_attempts = 0
        self.last_block_interval = 0.0
        self.predicted_block_window = 0.0
        self.total_blocks = 0
        self.total_recoveries = 0

    def escalate(self, risk: int):
        """根据风险评分升级防御层级（不降级）"""
        old_tier = self.tier

        if risk >= 8:
            new_tier = DefenseTier.RED
        elif risk >= 6:
            new_tier = DefenseTier.ORANGE
        elif risk >= 4:
            new_tier = DefenseTier.YELLOW
        else:
            return False  # 无需升级

        # 确保不降级：若当前层级已高于风险评定层级，保持当前层级
        if new_tier < old_tier:
            new_tier = old_tier
            # 延长冷却而非缩短
            now = time.monotonic()
            self.cooldown_until = max(self.cooldown_until, now + TIER_COOLDOWN[old_tier])
            self.block_count += 1
            self.total_blocks += 1
            self.block_history.append((now, old_tier, TIER_COOLDOWN[old_tier]))
            return False

        now = time.monotonic()

        # 记录封禁间隔（无论是否升级层级）
        if self.block_history:
            last_ts = self.block_history[-1][0]
            self.last_block_interval = now - last_ts

        # 即使层级不变也记录本次封禁（用于模式学习）
        self.block_count += 1
        self.total_blocks += 1
        self.block_history.append((now, new_tier, TIER_COOLDOWN[new_tier]))

        # 模式学习：如果连续 3+ 次封禁间隔相似，预测下一次封禁窗口
        if len(self.block_history) >= 3:
            intervals = []
            for i in range(1, len(self.block_history)):
                dt = self.block_history[i][0] - self.block_history[i-1][0]
                intervals.append(dt)
            if intervals:
                avg_interval = sum(intervals) / len(intervals)
                variance = sum((x - avg_interval) ** 2 for x in intervals) / len(intervals)
                std = variance ** 0.5
                # 如果标准差小于均值的 20%，认为有规律
                if avg_interval > 0 and std < avg_interval * 0.2:
                    self.predicted_block_window = avg_interval
                    logger.info(
                        f"【防御】域名 {self.domain} 检测到封禁模式："
                        f"间隔约 {avg_interval:.0f}s，预测下次封禁窗口"
                    )

        if new_tier > old_tier:
            self.tier = new_tier
            self.tier_since = now
            self.cooldown_until = now + TIER_COOLDOWN[new_tier]
            logger.warning(
                f"【防御】域名 {self.domain} 层级升级 "
                f"{old_tier.name} → {new_tier.name} (risk={risk}, "
                f"累计封禁 {self.total_blocks} 次)"
            )
            return True

        # 层级不变但记录了封禁（延长冷却）
        self.cooldown_until = max(self.cooldown_until, now + TIER_COOLDOWN[new_tier])
        return False

    def try_recover(self) -> bool:
        """尝试恢复到更低层级

        Returns:
            True 如果层级降低
        """
        if self.tier == DefenseTier.GREEN:
            return False

        now = time.monotonic()
        if now < self.cooldown_until:
            return False

        old_tier = self.tier
        # 逐级降低
        new_tier = DefenseTier(max(0, int(old_tier) - 1))
        self.tier = new_tier
        self.tier_since = now
        self.recovery_attempts += 1

        if new_tier == DefenseTier.GREEN:
            self.total_recoveries += 1
            self.cooldown_until = 0
            logger.info(
                f"【防御】域名 {self.domain} 完全恢复至 GREEN "
                f"(累计恢复 {self.total_recoveries} 次)"
            )
        else:
            # 恢复期间设置较短的冷却窗口用于观察
            self.cooldown_until = now + TIER_COOLDOWN[new_tier] // 2
            logger.info(
                f"【防御】域名 {self.domain} 恢复 "
                f"{old_tier.name} → {new_tier.name} "
                f"(观察期 {TIER_COOLDOWN[new_tier] // 2}s)"
            )

        return True

    @property
    def current_delay(self) -> float:
        """当前层级对应的请求间延迟"""
        return TIER_DELAY.get(self.tier, 0)

    @property
    def is_in_cooldown(self) -> bool:
        return time.monotonic() < self.cooldown_until

    def stats(self) -> dict:
        return {
            "domain": self.domain,
            "tier": self.tier.name,
            "block_count": self.block_count,
            "total_blocks": self.total_blocks,
            "total_recoveries": self.total_recoveries,
            "predicted_block_window": self.predicted_block_window,
            "in_cooldown": self.is_in_cooldown,
        }


# [FIXED & MODIFIED] v2.10.5 渲染友好站豁免（模块级）：贴吧/论坛静态 403 是防爬首屏（渲染能拿
# 正文），若按 403+anti_bot 累计 10 分降级 RED 会误伤正常采集。这些站的 403 不加分。
_RENDER_FRIENDLY = ("tieba.baidu.com", "zhihu.com", "xiaojuzi.com", "csdn.net",
                    "163.com", "sina.com.cn", "weibo.com", "qq.com", "jd.com", "taobao.com")


class ShieldSoldier:
    """盾兵：防御型并发压制

    根据域名防御层级动态调整并发，支持自动恢复。
    与 smart_adaptive.py 的 DomainHealth 系统协同工作。
    """

    def __init__(self):
        self.crawler = None
        self._domain_states: Dict[str, DomainDefenseState] = {}
        self._recovery_task: Optional[asyncio.Task] = None
        self._running = False
        self._persist_bg: set = set()  # [v2.18 P2-3] 落库 fire-and-forget 强引用集

    def mount(self, crawler):
        self.crawler = crawler
        # 确保 _shielded_domains 存在
        if not hasattr(crawler, '_shielded_domains'):
            crawler._shielded_domains = set()
        # 初始化已屏蔽域名的防御状态
        for domain in getattr(crawler, '_shielded_domains', set()):
            ds = DomainDefenseState(domain)
            ds.tier = DefenseTier.YELLOW
            self._domain_states[domain] = ds

    async def defend(self, domain, risk):
        """根据风险评分执行防御"""
        if domain not in self._domain_states:
            self._domain_states[domain] = DomainDefenseState(domain)

        ds = self._domain_states[domain]
        escalated = ds.escalate(risk)

        if escalated or ds.tier != DefenseTier.GREEN:
            await self._apply_tier(domain, ds.tier)
        # [FIXED & MODIFIED] v2.13 阶段2 冷却/等级落库（原纯内存重启全丢）
        await self._persist_domain_state(domain, ds)

    async def _apply_tier(self, domain, tier):
        """应用防御层级的并发限制
        [v2.18 P0 修复] 旧实现两颗雷：
        1. 恢复到 GREEN 时 TIER_CONCURRENCY[GREEN]=None 直接 return——RED 期间写入的
           adjust_global(5,"defense") min-wins 诉求永久残留，一次风控误判后全局并发
           钳死在 5、域并发钳死在 1，直到重启进程（forget_source 全仓零调用）。
        2. 多域同时降级时全局诉求"后写覆盖先写"：A 域 RED(5) 会被 B 域 ORANGE(30)
           覆盖，B 域恢复后 A 域的压制随之消失。
        现改为：全局诉求 = 所有非 GREEN 域名中最严厉的全局限制，每次层级变动全量重算；
        域级诉求走分域仲裁表，恢复时精确归还本域 defense 诉求。"""
        cc = getattr(self.crawler, "concurrency", None)
        if cc is None:
            return
        limits = TIER_CONCURRENCY.get(tier) or {}
        if limits.get("domain") is not None:
            await cc.adjust_domain(domain, limits["domain"], source="defense")
        else:
            # GREEN（或该层级不限域）：归还本域的 defense 分域诉求并重算该域上限
            await cc.forget_domain_claim_async(domain, "defense")
        # 全局诉求全量重算（含 GREEN 恢复：已无非 GREEN 域名时归还 defense 全局诉求）
        claim = None
        for ds in self._domain_states.values():
            if ds.tier == DefenseTier.GREEN:
                continue
            g = (TIER_CONCURRENCY.get(ds.tier) or {}).get("global")
            if g is not None and (claim is None or g < claim):
                claim = g
        if claim is None:
            await cc.forget_source_async("defense")
        else:
            await cc.adjust_global(claim, source="defense")
        if tier >= DefenseTier.ORANGE:
            self.crawler._shielded_domains.add(domain)
            # ORANGE/RED 层级触发代理轮换
            if hasattr(self.crawler, 'exit_mgr'):
                await self._rotate_proxy_for_domain(domain)

    async def _rotate_proxy_for_domain(self, domain):
        """为高风险域名轮换代理"""
        try:
            exit_mgr = self.crawler.exit_mgr
            # 清除粘性会话，强制下次获取新代理
            if domain in exit_mgr.sticky:
                old_proxy = exit_mgr.sticky.pop(domain)
                logger.info(f"【盾兵】域名 {domain} 代理轮换: {sanitize_proxy(old_proxy)} → 新代理")
        except Exception as e:
            logger.debug(f"Proxy rotation failed for {domain}: {e}")

    def _persist_domain_state_sync(self, domain, ds):
        """[v2.13] 域名冷却/等级落库（同步换算：monotonic 差值 + 当前 epoch）。
        GREEN 清行；非 GREEN 写 until_epoch。fire-and-forget 不阻塞防御主流程。
        [v2.18 P2-3] 持强引用（无引用任务可被 GC 静默吞掉 → 冷却落库随机丢失）。"""
        try:
            frontier = getattr(self.crawler, 'frontier', None)
            if frontier is None:
                return
            if ds.tier == DefenseTier.GREEN and ds.cooldown_until <= time.monotonic():
                _t = asyncio.create_task(frontier.clear_domain_cooldown(domain))
            else:
                remain = max(0.0, ds.cooldown_until - time.monotonic())
                _t = asyncio.create_task(frontier.set_domain_cooldown(
                    domain, time.time() + remain, int(ds.tier)))
            self._persist_bg.add(_t)
            _t.add_done_callback(self._persist_bg.discard)
        except Exception:
            pass

    async def _persist_domain_state(self, domain, ds):
        self._persist_domain_state_sync(domain, ds)

    async def start_recovery_loop(self):
        """启动自动恢复循环
        [FIXED & MODIFIED] v2.13 阶段2 启动恢复：从 cooldowns 表加载未过期冷却，
        重启后风控状态不再从零开始（之前一重启就忘光，被封域名立刻硬冲）。"""
        self._running = True
        try:
            frontier = getattr(self.crawler, 'frontier', None)
            if frontier is not None:
                active = await frontier.load_active_cooldowns()
                for domain, (until_epoch, tier) in active.items():
                    ds = self._domain_states.setdefault(domain, DomainDefenseState(domain))
                    restored_tier = DefenseTier(min(max(int(tier), 0), 3))
                    if restored_tier > ds.tier:
                        ds.tier = restored_tier
                    # epoch → monotonic（保留剩余冷却时长）
                    ds.cooldown_until = max(ds.cooldown_until,
                                            time.monotonic() + max(0.0, until_epoch - time.time()))
                    if ds.tier != DefenseTier.GREEN:
                        await self._apply_tier(domain, ds.tier)
                if active:
                    logger.info(f"【防御】已从数据库恢复 {len(active)} 个域名的冷却状态: "
                                f"{list(active.keys())[:5]}")
        except Exception as e:
            logger.debug(f"冷却状态恢复失败（非致命）: {e}")
        self._recovery_task = asyncio.create_task(self._recovery_loop())

    async def _recovery_loop(self):
        """定期检查各域名是否可以恢复"""
        while self._running:
            await asyncio.sleep(15)  # 每 15 秒检查一次
            for domain, ds in list(self._domain_states.items()):
                if ds.tier != DefenseTier.GREEN:
                    if ds.try_recover():
                        await self._apply_tier(domain, ds.tier)
                        self._persist_domain_state_sync(domain, ds)  # [v2.13] 恢复同步落库
                        # 如果恢复到 GREEN，从屏蔽列表移除
                        if ds.tier == DefenseTier.GREEN:
                            self.crawler._shielded_domains.discard(domain)

    async def stop(self):
        self._running = False
        if self._recovery_task:
            self._recovery_task.cancel()
            try:
                await self._recovery_task
            except asyncio.CancelledError:
                pass

    def get_domain_stats(self) -> dict:
        """获取所有域名的防御状态统计"""
        return {d: ds.stats() for d, ds in self._domain_states.items()}


class SentinelBrain:
    """哨兵大脑：风险评估与盾兵调度

    增强版风险评估：综合状态码、反爬检测、延迟、连续失败、
    挑战检测、封禁模式等多维度信号。
    """

    # 风险信号权重
    SIGNAL_WEIGHTS = {
        "status_403": 5,
        "status_429": 5,
        "status_503": 3,
        "status_500": 2,
        "anti_bot_detected": 4,
        "challenge_detected": 3,
        "high_latency": 1,
        "consecutive_fails": 1,  # 每次失败 +1，最多 5
        "rate_limited_header": 3,
        "captcha_detected": 4,
    }

    def __init__(self, soldier: ShieldSoldier, master_password: str):
        self.soldier = soldier
        self._password = master_password
        self._authorized = False
        self._event_history: deque = deque(maxlen=500)
        self._domain_patterns: Dict[str, deque] = defaultdict(lambda: deque(maxlen=50))

    async def authorize(self, pwd):
        self._authorized = (pwd == self._password)
        return self._authorized

    async def evaluate(self, event):
        """评估事件风险并触发防御"""
        if not self._authorized:
            return

        domain = event.get("domain", "unknown")
        risk = self._calc_risk(event)

        # 记录事件历史
        self._event_history.append({
            "time": time.monotonic(),
            "domain": domain,
            "risk": risk,
            "status": event.get("status_code", 0),
        })

        # 记录域名模式
        self._domain_patterns[domain].append({
            "time": time.monotonic(),
            "risk": risk,
            "status": event.get("status_code", 0),
        })

        if risk >= 5:
            await self.soldier.defend(domain, risk)

    def _calc_risk(self, event) -> int:
        """多维度风险评分 (0-10)"""
        score = 0
        status = event.get("status_code", 0)
        _dom = (event.get("domain") or "").lower()
        _rf = any(k in _dom for k in _RENDER_FRIENDLY)  # [FIXED & MODIFIED] v2.10.5 模块级豁免常量

        # 状态码风险
        if status == 403:
            if not _rf:
                score += self.SIGNAL_WEIGHTS["status_403"]
        elif status == 429:
            score += self.SIGNAL_WEIGHTS["status_429"]
        elif status == 503:
            score += self.SIGNAL_WEIGHTS["status_503"]
        elif status >= 500:
            score += self.SIGNAL_WEIGHTS["status_500"]

        # 反爬检测
        if event.get("anti_bot_detected"):
            if not _rf:
                score += self.SIGNAL_WEIGHTS["anti_bot_detected"]

        # 挑战检测
        if event.get("challenge_detected"):
            score += self.SIGNAL_WEIGHTS["challenge_detected"]

        # 验证码检测
        if event.get("captcha_detected"):
            score += self.SIGNAL_WEIGHTS["captcha_detected"]

        # 速率限制头
        if event.get("rate_limited"):
            score += self.SIGNAL_WEIGHTS["rate_limited_header"]

        # 延迟
        latency = event.get("latency", 0)
        if latency > 10:
            score += self.SIGNAL_WEIGHTS["high_latency"]
        elif latency > 5:
            score += 1

        # 连续失败（每次 +1，最多 +5）
        consecutive = event.get("consecutive_fails", 0)
        score += min(consecutive, 5)

        return min(score, 10)

    def get_risk_trend(self, domain: str) -> str:
        """获取域名风险趋势"""
        events = list(self._domain_patterns.get(domain, []))
        if len(events) < 3:
            return "insufficient_data"
        recent = events[-3:]
        avg_recent = sum(e["risk"] for e in recent) / len(recent)
        older = events[:-3] if len(events) > 3 else events
        avg_older = sum(e["risk"] for e in older) / len(older) if older else 0

        if avg_recent > avg_older + 2:
            return "rising"
        elif avg_recent < avg_older - 2:
            return "falling"
        return "stable"


class AmeNoHabakiri:
    """天羽羽斩：攻击型反制（增强版）

    检测到风险时智能提升并发并重置代理状态。
    增强功能：
    - 分级反制（不再一刀切拉满并发）
    - 智能代理轮换（仅重置被封代理）
    - 挑战求解器预热
    - 并发窗口控制（避免持续高并发触发更严封禁）
    """

    def __init__(self):
        self.crawler = None
        self._counter_attack_until: Dict[str, float] = {}  # 域名 -> 反制结束时间
        self._counter_attack_history: Dict[str, int] = defaultdict(int)

    def mount(self, crawler):
        self.crawler = crawler

    async def punish(self, domain, risk):
        """根据风险执行反制"""
        now = time.monotonic()

        # 检查是否在反制窗口内
        if domain in self._counter_attack_until:
            if now < self._counter_attack_until[domain]:
                return  # 反制窗口内不重复触发

        self._counter_attack_history[domain] += 1
        attack_count = self._counter_attack_history[domain]

        if risk >= 7:
            # 高风险：全面反制
            await asyncio.gather(
                self._thunder_strike(domain, attack_count),
                self._storm_rage(domain),
                self._inferno(domain),
            )
            self._counter_attack_until[domain] = now + 120  # 2 分钟反制窗口
        elif risk >= 4:
            # 中风险：局部反制
            await self._thunder_strike(domain, attack_count)
            self._counter_attack_until[domain] = now + 60  # 1 分钟反制窗口

    async def _thunder_strike(self, domain, attack_count):
        """雷霆一击：智能并发提升

        根据反制次数递减并发提升幅度，避免持续高并发。
        """
        # 第一次反制拉满，后续递减
        boost = max(50, 200 - (attack_count - 1) * 30)
        logger.info(f"【天羽羽斩】雷霆一击 -> {domain} (并发提升至 {boost}, 第 {attack_count} 次)")
        await self.crawler.concurrency.adjust_global(boost, source="defense")
        await self.crawler.concurrency.adjust_domain(domain, max(10, boost // 4), source="defense")

    async def _storm_rage(self, domain):
        """风暴之怒：智能代理重置

        仅重置被标记为 COOLDOWN/MELTDOWN 的代理，而非全部。
        """
        from .exit_manager import ExitState
        logger.info(f"【天羽羽斩】风暴之怒 -> {domain}")
        reset_count = 0
        for node in self.crawler.exit_mgr.nodes.values():
            if node.state in (ExitState.COOLDOWN, ExitState.MELTDOWN):
                node.state = ExitState.ACTIVE
                node.score = max(60.0, node.score + 20)
                node.cooldown_until = 0
                node.consecutive_fails = 0
                reset_count += 1
        logger.info(f"【天羽羽斩】重置 {reset_count} 个代理节点")

    async def _inferno(self, domain):
        """炼狱：求解器预热

        增加 solver 池大小并预热上下文，应对可能的挑战。
        """
        logger.info(f"【天羽羽斩】炼狱 -> {domain} (求解器预热)")
        solver = self.crawler.solver
        # 保存旧值，失败时完整回滚
        old_pool = solver.pool_size
        old_max_pages = solver.max_pages_per_context
        solver.pool_size = min(8, old_pool + 3)
        solver.max_pages_per_context = max(1, solver.max_pages_per_context - 2)
        try:
            # [v2.18 P3-12] 走带锁的 _safe_restart：原直调 restart() 会与其它路径的
            # _safe_restart 并发重入（双重启竞态）
            await solver._safe_restart()
        except Exception as e:
            logger.warning(f"Solver restart failed during inferno: {e}")
            # 完整回滚
            solver.pool_size = old_pool
            solver.max_pages_per_context = old_max_pages


class OracleBrain:
    """神谕大脑：风险评估与天羽羽斩调度（增强版）

    增强功能：
    - 预测性分析（基于历史模式预判风险）
    - 多信号融合评估
    - 反制效果追踪
    - 自适应阈值调整
    """

    def __init__(self, habakiri: AmeNoHabakiri, master_password: str):
        self.habakiri = habakiri
        self._password = master_password
        self._authorized = False
        self._fail_counter: Dict[str, int] = defaultdict(int)
        self._event_history: deque = deque(maxlen=500)
        self._domain_risk_baseline: Dict[str, float] = defaultdict(float)
        self._adaptive_threshold: Dict[str, int] = defaultdict(lambda: 4)

    async def authorize(self, pwd):
        self._authorized = (pwd == self._password)
        return self._authorized

    async def judge(self, event):
        """评估事件并触发反制"""
        if not self._authorized:
            return

        domain = event.get("domain", "unknown")
        risk = self._calc_risk(event)

        # 记录事件
        now = time.monotonic()
        self._event_history.append({
            "time": now,
            "domain": domain,
            "risk": risk,
            "status": event.get("status_code", 0),
        })

        # 更新域名风险基线（指数移动平均）
        alpha = 0.3
        self._domain_risk_baseline[domain] = (
            alpha * risk + (1 - alpha) * self._domain_risk_baseline[domain]
        )

        # 自适应阈值：如果域名频繁被封，提高触发阈值（避免过度反制）
        domain_fail_count = self._fail_counter[domain]
        threshold = min(7, self._adaptive_threshold[domain] + domain_fail_count // 3)

        if risk >= threshold:
            self._fail_counter[domain] += 1
            await self.habakiri.punish(domain, risk)
        elif risk <= 2 and domain_fail_count > 0:
            # 风险降低时逐步重置计数器
            self._fail_counter[domain] = max(0, domain_fail_count - 1)

    def _calc_risk(self, event) -> int:
        """多维度风险评分 (0-10)"""
        score = 0
        status = event.get("status_code", 0)
        _dom = (event.get("domain") or "").lower()
        # [FIXED & MODIFIED] v2.10.5 渲染友好站豁免（模块级常量，贴吧 403 是防爬首屏）
        _rf = any(k in _dom for k in _RENDER_FRIENDLY)

        if status == 403:
            if not _rf:
                score += 5
        elif status == 429:
            score += 5
        elif status == 503:
            score += 3
        elif status >= 500:
            score += 3

        if event.get("anti_bot_detected"):
            if not _rf:
                score += 4

        if event.get("challenge_detected"):
            score += 3

        if event.get("captcha_detected"):
            score += 4

        consecutive = event.get("consecutive_fails", 0)
        score += min(consecutive, 5)

        # 延迟异常
        latency = event.get("latency", 0)
        if latency > 15:
            score += 2
        elif latency > 8:
            score += 1

        return min(score, 10)

    def get_domain_risk_summary(self) -> dict:
        """获取域名风险评估摘要"""
        return {
            domain: {
                "baseline_risk": round(baseline, 1),
                "fail_count": self._fail_counter[domain],
                "threshold": min(7, self._adaptive_threshold[domain] + self._fail_counter[domain] // 3),
            }
            for domain, baseline in self._domain_risk_baseline.items()
        }
