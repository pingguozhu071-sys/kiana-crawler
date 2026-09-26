import asyncio


class ResizableSemaphore:
    """可动态调整大小的异步信号量"""

    def __init__(self, initial: int):
        self._max = initial
        self._count = 0
        self._cond = asyncio.Condition()

    async def acquire(self):
        async with self._cond:
            while self._count >= self._max:
                await self._cond.wait()
            self._count += 1

    async def release(self):
        async with self._cond:
            # 修复：防止计数变负导致并发失控
            if self._count > 0:
                self._count -= 1
            self._cond.notify(1)

    async def set_max(self, new_max: int):
        async with self._cond:
            self._max = max(1, new_max)
            self._cond.notify_all()

    @property
    def max_permits(self):
        return self._max

    @property
    def used_permits(self):
        return self._count


class ConcurrencyController:
    """全局+分域+分出口三级并发控制"""

    def __init__(self, global_max=200, per_domain_default=5, per_exit_default=10):
        self.global_sem = ResizableSemaphore(global_max)
        self.per_domain_default = per_domain_default
        self.per_exit_default = per_exit_default
        self._domain_sems = {}
        self._exit_sems = {}
        self._lock = asyncio.Lock()
        self._global_requests = {"default": global_max}  # [v2.11] min-wins 仲裁来源表
        # [v2.18] 分域 min-wins 仲裁表：domain -> {source: claim}，与全局同规则
        self._domain_requests: dict = {}

    async def _get_domain_sem(self, domain):
        if domain not in self._domain_sems:
            async with self._lock:
                if domain not in self._domain_sems:
                    self._domain_sems[domain] = ResizableSemaphore(self.per_domain_default)
        return self._domain_sems[domain]

    async def _get_exit_sem(self, exit_url):
        if exit_url not in self._exit_sems:
            async with self._lock:
                if exit_url not in self._exit_sems:
                    self._exit_sems[exit_url] = ResizableSemaphore(self.per_exit_default)
        return self._exit_sems[exit_url]

    async def acquire_all(self, domain, exit_url=None):
        # 修复：部分失败时回滚已获取的信号量，避免泄漏
        # 注意：Python 3.10+ 中 CancelledError 继承 BaseException 而非 Exception
        await self.global_sem.acquire()
        try:
            await (await self._get_domain_sem(domain)).acquire()
        except (Exception, asyncio.CancelledError):
            await self.global_sem.release()
            raise
        if exit_url:
            try:
                await (await self._get_exit_sem(exit_url)).acquire()
            except (Exception, asyncio.CancelledError):
                await (await self._get_domain_sem(domain)).release()
                await self.global_sem.release()
                raise

    async def release_all(self, domain, exit_url=None):
        # 修复：释放顺序与获取相反，且每个释放都独立 try-except 避免一个失败影响其他
        if exit_url:
            try:
                await (await self._get_exit_sem(exit_url)).release()
            except Exception:
                pass
        try:
            await (await self._get_domain_sem(domain)).release()
        except Exception:
            pass
        try:
            await self.global_sem.release()
        except Exception:
            pass

    async def adjust_global(self, new_max, source: str = "default"):
        """[FIXED & MODIFIED] v2.11 min-wins 仲裁：4 个控制器（adaptive_v2/smart_adaptive/
        autoscale_pool/defense）此前并发抢写同一信号量——后写覆盖先写，defense RED 的
        "全局降到 5"会被 adaptive 恢复逻辑悄悄抬高。现按来源记录诉求，生效值 = 所有来源
        请求的最小值：defense 熔断（最低值）天然最高优先，任一来源想抬升必须所有来源同意。"""
        try:
            new_max = int(new_max)
        except (TypeError, ValueError):
            return
        self._global_requests[source] = max(1, new_max)
        await self.global_sem.set_max(min(self._global_requests.values()))

    def forget_source(self, source: str):
        """[v2.11] 控制器停止（shutdown）时移除其诉求，避免残留低值卡死全局并发
        [v2.18] 仅清仲裁表（同步版无法 await set_max）；需要同步重算生效上限请用
        forget_source_async——defense GREEN 恢复等运行时路径一律走异步版"""
        self._global_requests.pop(source, None)
        for domain in list(self._domain_requests.keys()):
            self.forget_domain_claim(domain, source)

    async def forget_source_async(self, source: str):
        """[v2.18] 移除来源的全部诉求（全局+分域）并重算生效上限"""
        self._global_requests.pop(source, None)
        if self._global_requests:
            await self.global_sem.set_max(max(1, min(self._global_requests.values())))
        for domain in list(self._domain_requests.keys()):
            await self.forget_domain_claim_async(domain, source)

    def forget_domain_claim(self, domain, source: str):
        """[v2.18] 移除某域上某来源的分域诉求（仅清表；重算用异步版）"""
        claims = self._domain_requests.get(domain)
        if not claims or source not in claims:
            return
        claims.pop(source, None)

    async def forget_domain_claim_async(self, domain, source: str):
        """[v2.18] 移除某域上某来源的分域诉求并重算该域生效上限"""
        claims = self._domain_requests.get(domain)
        if not claims or source not in claims:
            return
        claims.pop(source, None)
        if claims:
            await (await self._get_domain_sem(domain)).set_max(
                max(1, min(claims.values())))

    @property
    def global_requests(self) -> dict:
        """当前各控制器的并发诉求（诊断用）"""
        return dict(self._global_requests)

    async def adjust_domain(self, domain, new_max, source: str = "default"):
        """[v2.18] 分域 min-wins 仲裁：与 adjust_global 同规则。旧版 defense/smart_adaptive
        直接抢写同一域 sem（后写覆盖先写），defense 恢复 GREEN 无法知道该还原成什么。"""
        try:
            new_max = int(new_max)
        except (TypeError, ValueError):
            return
        claims = self._domain_requests.setdefault(
            domain, {"default": self.per_domain_default})
        claims[source] = max(1, new_max)
        await (await self._get_domain_sem(domain)).set_max(
            max(1, min(claims.values())))
