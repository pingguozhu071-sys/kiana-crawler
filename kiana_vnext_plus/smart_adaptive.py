"""智能自适应控制器（增强版）

结合反爬检测率、成功率、延迟、域名健康度四维度调整并发。
新增：域名级风险评分、预测性限流、恢复加速。
"""
import asyncio
import logging
import time
from collections import deque, defaultdict

logger = logging.getLogger(__name__)


class DomainHealth:
    """域名健康追踪器"""

    def __init__(self, domain: str):
        self.domain = domain
        self.status_codes = deque(maxlen=100)
        self.successes = deque(maxlen=100)
        self.latencies = deque(maxlen=100)
        self.consecutive_blocks = 0
        self.last_block_time = 0
        self.block_history = deque(maxlen=20)  # (timestamp, duration)
        self.recovery_state = "normal"  # normal / cooling / recovering

    def record(self, success: bool, latency: float = 0, status_code: int = None):
        self.successes.append(success)
        if latency > 0:
            self.latencies.append(latency)
        if status_code is not None:
            self.status_codes.append(status_code)

        if status_code in (403, 429, 503):
            self.consecutive_blocks += 1
            self.last_block_time = time.monotonic()
            self.recovery_state = "cooling"
        elif success:
            if self.recovery_state == "cooling":
                # 开始恢复
                self.recovery_state = "recovering"
            elif self.recovery_state == "recovering":
                if self.consecutive_blocks == 0:
                    self.recovery_state = "normal"
            self.consecutive_blocks = 0

    @property
    def block_rate(self) -> float:
        if not self.status_codes:
            return 0.0
        codes = list(self.status_codes)[-50:]
        return sum(1 for c in codes if c in (403, 429, 503)) / len(codes)

    @property
    def success_rate(self) -> float:
        if not self.successes:
            return 1.0
        return sum(self.successes) / len(self.successes)

    @property
    def avg_latency(self) -> float:
        if not self.latencies:
            return 1.0
        return sum(self.latencies) / len(self.latencies)

    @property
    def risk_score(self) -> int:
        """风险评分 0-10"""
        score = 0
        score += min(int(self.block_rate * 10), 5)
        score += min(self.consecutive_blocks, 3)
        if self.avg_latency > 5:
            score += 1
        if self.recovery_state == "cooling":
            score += 2
        return min(score, 10)


class SmartAdaptiveController:
    """智能自适应控制器：四维度并发调整 + 域名级风险评分 + 恢复加速"""

    def __init__(self, target_sr=0.95, interval=10, min_concurrency=3, max_concurrency=200):
        self.target_sr = target_sr
        self.interval = interval
        self.min_concurrency = min_concurrency
        self.max_concurrency = max_concurrency
        # 全局统计
        self.successes = deque(maxlen=200)
        self.latencies = deque(maxlen=200)
        self.status_codes = deque(maxlen=200)
        # 域名级健康追踪
        self._domain_health: dict[str, DomainHealth] = defaultdict(lambda: DomainHealth(""))
        self._running = False
        self._concurrency_ref = None
        # 预测性限流：连续慢响应计数
        self._slow_response_streak = 0

    def record(self, success, latency=0, status_code=None, domain=None):
        """记录请求结果"""
        self.successes.append(success)
        if latency > 0:
            self.latencies.append(latency)
        if status_code is not None:
            self.status_codes.append(status_code)

        # 预测性限流：检测慢响应趋势
        if latency > 3.0:
            self._slow_response_streak += 1
        else:
            self._slow_response_streak = 0

        # 域名级记录
        if domain:
            if domain not in self._domain_health:
                self._domain_health[domain] = DomainHealth(domain)
            self._domain_health[domain].record(success, latency, status_code)

    async def run(self, concurrency):
        """主控制循环"""
        self._running = True
        self._concurrency_ref = concurrency
        while self._running:
            await asyncio.sleep(self.interval)
            if len(self.successes) < 20:
                continue

            # 全局反爬检测率
            codes = list(self.status_codes)[-100:]
            anti_bot = sum(1 for c in codes if c in (403, 429, 503)) / len(codes) if codes else 0
            sr = sum(self.successes) / len(self.successes)
            current_max = concurrency.global_sem.max_permits

            # 策略 1：高反爬率 → 激进降并发
            if anti_bot > 0.3:
                new_max = max(self.min_concurrency, int(current_max * 0.5))
                logger.warning(f"Anti-bot rate {anti_bot:.1%}, reducing concurrency to {new_max}")
                await concurrency.adjust_global(new_max, source="smart_adaptive")

            # 策略 2：预测性限流 → 连续慢响应
            elif self._slow_response_streak > 10:
                new_max = max(self.min_concurrency, int(current_max * 0.7))
                logger.info(f"Predictive throttle: {self._slow_response_streak} slow responses, reducing to {new_max}")
                await concurrency.adjust_global(new_max, source="smart_adaptive")
                self._slow_response_streak = 0

            # 策略 3：成功率低 → 温和降并发
            elif sr < self.target_sr:
                new_max = max(self.min_concurrency, int(current_max * 0.8))
                logger.info(f"Success rate {sr:.1%} < target {self.target_sr:.1%}, reducing to {new_max}")
                await concurrency.adjust_global(new_max, source="smart_adaptive")

            # 策略 4：恢复加速 → 一切正常时快速恢复
            else:
                avg_lat = sum(self.latencies) / len(self.latencies) if self.latencies else 1
                if avg_lat < 2.0 and anti_bot < 0.05:
                    # 恢复加速：比正常增长更快
                    growth_rate = 1.15 if self._has_cooled_domains() else 1.05
                    new_max = min(self.max_concurrency, int(current_max * growth_rate))
                    if new_max != current_max:
                        logger.debug(f"Healthy state, increasing concurrency to {new_max} (growth={growth_rate})")
                        await concurrency.adjust_global(new_max, source="smart_adaptive")

            # 域名级调整
            await self._adjust_domains(concurrency)

    def _has_cooled_domains(self) -> bool:
        """检查是否有域名正在恢复"""
        return any(h.recovery_state != "normal" for h in self._domain_health.values())

    async def _adjust_domains(self, concurrency):
        """根据域名健康度调整各域名并发"""
        for domain, health in self._domain_health.items():
            risk = health.risk_score
            if risk >= 7:
                await concurrency.adjust_domain(domain, 1)
            elif risk >= 4:
                await concurrency.adjust_domain(domain, 2)
            elif risk == 0 and health.recovery_state == "normal":
                # 健康域名恢复正常并发
                await concurrency.adjust_domain(domain, max(5, concurrency.per_domain_default))

    def stop(self):
        self._running = False

    def get_domain_stats(self) -> dict:
        """获取域名健康统计"""
        return {
            domain: {
                "risk_score": h.risk_score,
                "block_rate": h.block_rate,
                "success_rate": h.success_rate,
                "recovery_state": h.recovery_state,
                "consecutive_blocks": h.consecutive_blocks,
            }
            for domain, h in self._domain_health.items()
        }
