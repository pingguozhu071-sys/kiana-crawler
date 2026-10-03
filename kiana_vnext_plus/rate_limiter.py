"""限流引擎（强化版）— Token Bucket + 全局/域名双档 + 指数退避 + 速率边界探测

设计：
  - TokenBucket: 经典令牌桶（突发容忍）
  - RateLimiter: 全局桶 + 每域名桶两级限流
    - 429/403 触发指数退避（backoff *= 2），成功后缓慢恢复（backoff *= 0.99）
    - 每 100 次成功发送 3 个探测请求试探速率上限
"""
import asyncio
import time
import random
from collections import defaultdict



class TokenBucket:
    """令牌桶限流器"""

    def __init__(self, rate, burst):
        self.rate = rate
        self.burst = burst
        self.tokens = burst
        self.last_update = time.monotonic()

    async def consume(self, tokens=1):
        if self.rate <= 0:
            return  # No rate limiting
        while True:
            now = time.monotonic()
            elapsed = now - self.last_update
            self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
            self.last_update = now
            if self.tokens >= tokens:
                self.tokens -= tokens
                return
            await asyncio.sleep((tokens - self.tokens) / self.rate)


class RateLimiter:
    """全局 + 域名双档令牌桶限流，带指数退避与速率边界探测"""

    def __init__(self, global_rate: float = 100.0, global_burst: float = 200.0,
                 domain_rate: float = 10.0, domain_burst: float = 20.0,
                 probe_interval: int = 100, probe_count: int = 3):
        self._global = TokenBucket(global_rate, global_burst)
        self._domain_buckets: dict = defaultdict(lambda: TokenBucket(domain_rate, domain_burst))
        self._domain_backoff: dict = defaultdict(float)   # 每域名退避乘数
        self._global_backoff: float = 0.0                 # 全局退避乘数
        self._success_count: int = 0                      # 成功请求计数（用于触发探测）
        self._probe_interval = probe_interval
        self._probe_count = probe_count
        self._lock = asyncio.Lock()
        self._domain_rate = domain_rate
        self._domain_burst = domain_burst

    @staticmethod
    def _domain_of(url: str) -> str:
        """[v6 修复] 改为委托 `url_utils.extract_domain`（**域名提取的唯一实现**）。

        此前本函数与 `session_pool._domain_of`、`url_utils.extract_domain` 是**三份**，
        且真实输入上结论不一致：裸域名 `example.com` 在本处得到 `example.com`，
        而 `extract_domain`（frontier 用它存 `domain` 列）得到**空串**——
        于是限流桶与 frontier 的域名桶**对不上**。现统一。
        """
        from .url_utils import extract_domain
        return extract_domain(url)

    async def acquire(self, url: str, tokens: int = 1):
        """获取发送许可：全局桶 + 域名桶 + 退避等待

        [FIXED & MODIFIED] v2.13 归还语义（学 Jormungandr rate_limiter.py:70-73 的正确性细节）：
        原实现先扣全局再扣域名，域名桶不足时全局令牌已被消耗却不退回——高并发下
        全局令牌被"占着等域名"的请求白白吃掉。现改为：域名桶需要等待时，把全局令牌
        归还后再 sleep。
        [v2.18 P2-1] 归还后必须重扣：旧版归还全局令牌 → await 域名桶 → 直接返回，
        归还的令牌再也不扣回——429 风暴下域名受限请求全部"免单"，全局限流被膨胀
        击穿。改为 归还→等域名恢复→重扣全局 的循环；预检与归还在同一同步块内（无
        await → 单循环原子），不再有跨 await 读改写竞态。"""
        domain = self._domain_of(url)
        async with self._lock:
            backoff = max(self._global_backoff, self._domain_backoff[domain])
        if backoff > 0:
            await asyncio.sleep(backoff * random.uniform(0.8, 1.2))
        bucket = self._domain_buckets[domain]
        if bucket.rate <= 0:
            await self._global.consume(tokens)
            return
        while True:
            await self._global.consume(tokens)
            # 同步预检域名桶（本同步块与 consume 快路径之间无挂起点 → 原子）
            now = time.monotonic()
            elapsed = now - bucket.last_update
            bucket.tokens = min(bucket.burst, bucket.tokens + elapsed * bucket.rate)
            bucket.last_update = now
            if bucket.tokens >= tokens:
                await bucket.consume(tokens)  # 立即可得（快路径同步扣减）
                return
            # 不足：归还全局令牌（同步原子），等域名令牌恢复后重扣全局
            self._global.tokens = min(self._global.burst, self._global.tokens + tokens)
            await asyncio.sleep(min((tokens - bucket.tokens) / max(bucket.rate, 0.01), 5.0))

    async def record(self, url: str, status_code: int):
        """记录响应结果，驱动退避与探测"""
        domain = self._domain_of(url)
        async with self._lock:
            if status_code in (429, 403):
                # 触发退避：指数倍增，上限 30s
                self._domain_backoff[domain] = min(30.0, (self._domain_backoff[domain] or 0.5) * 2)
                self._global_backoff = min(30.0, (self._global_backoff or 0.5) * 2)
                return
            if 200 <= status_code < 400:
                # 成功：缓慢恢复
                self._domain_backoff[domain] *= 0.99
                if self._global_backoff > 0:
                    self._global_backoff *= 0.99
                self._success_count += 1
            # 探测：每 N 次成功发送探测请求试探速率上限
            if self._success_count > 0 and self._success_count % self._probe_interval == 0:
                await self._probe_locked(domain)

    async def _probe_locked(self, domain: str):
        """速率边界探测：试探性提升域名速率供给

        [FIXED & MODIFIED] v2.13 修复死代码：原提升写 self._domain_rate（构造期局部
        捕获，对 defaultdict 新桶永不生效）→ 现直接写当前 bucket.rate 持久生效；
        探测失败（仍有退避）恢复原速率。"""
        bucket = self._domain_buckets[domain]
        old = bucket.rate
        if self._domain_backoff[domain] <= 0.0:
            # 连续成功且无退避 → 接受 1.5x 提速（持久生效到具体桶）
            bucket.rate = min(old * 1.5, old + 20.0)
        else:
            bucket.rate = old
            self._domain_backoff[domain] = max(0.0, self._domain_backoff[domain] - 0.1)

    def set_domain_rate(self, domain: str, rate: float):
        """手动调整域名速率"""
        self._domain_buckets[domain].rate = rate

    def status(self) -> dict:
        """限流器状态快照"""
        return {
            "global_backoff": round(self._global_backoff, 3),
            "success_count": self._success_count,
            "domains": len(self._domain_buckets),
            "domain_backoffs": {k: round(v, 3) for k, v in list(self._domain_backoff.items())[:10]},
        }
