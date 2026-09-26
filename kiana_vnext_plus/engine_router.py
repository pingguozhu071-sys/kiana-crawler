"""引擎路由：多级回退链

回退链: protocol → stealth → TLS switch → solver → direct
每级失败后自动升级到下一级，确保最终可到达目标。
"""
import logging
from .protocol_engine import ProtocolEngine
from .solver_engine import SolverEngine
from .session_pool import SessionPool
from .exit_manager import ExitManager
from .response_adapter import ResponseAdapter

logger = logging.getLogger(__name__)


class EngineRouter:
    """引擎路由：多级回退链，protocol → stealth → TLS → solver → direct"""

    # 回退链标识
    FALLBACK_PROTOCOL = "protocol"
    FALLBACK_STEALTH = "stealth"
    FALLBACK_TLS = "tls_switch"
    FALLBACK_SOLVER = "solver"
    FALLBACK_DIRECT = "direct"

    def __init__(self, protocol: ProtocolEngine, solver: SolverEngine, session_pool: SessionPool,
                 exit_mgr: ExitManager, challenge_wait=30):
        self.protocol = protocol
        self.solver = solver
        self.session_pool = session_pool
        self.exit_mgr = exit_mgr
        self.challenge_wait = challenge_wait
        self._tls_fingerprint_idx = 0

    async def fetch(self, url, domain, job) -> ResponseAdapter:
        """多级回退获取：protocol → stealth → TLS → solver → direct"""
        last_error = None

        # Tier 1: 协议引擎（标准请求）
        proxy = await self.exit_mgr.acquire_for_domain(domain)
        try:
            # [FIXED & MODIFIED] v2.15 真增量：job 携带条件请求头（If-None-Match 等）
            # → Tier1 透传给协议引擎（304 协商在 page_processor 闭环）
            resp = await self._try_protocol(url, domain, proxy,
                                            job.get('_cond_headers') if isinstance(job, dict) else None)
            if resp is not None:
                return resp
        except Exception as e:
            logger.warning(f"协议引擎失败 [{domain}]: {e}")
            last_error = e
        finally:
            await self.exit_mgr.release(proxy)

        # Tier 2: 隐身协议（附加 stealth headers/cookies）
        try:
            resp = await self._try_stealth(url, domain)
            if resp is not None:
                return resp
        except Exception as e:
            logger.warning(f"隐身协议失败 [{domain}]: {e}")
            last_error = e

        # Tier 3: TLS 指纹切换
        try:
            resp = await self._try_tls_switch(url, domain)
            if resp is not None:
                return resp
        except Exception as e:
            logger.warning(f"TLS切换失败 [{domain}]: {e}")
            last_error = e

        # Tier 4: 浏览器求解引擎
        try:
            resp = await self._try_solver(url, domain)
            if resp is not None:
                return resp
        except Exception as e:
            logger.warning(f"求解引擎失败 [{domain}]: {e}")
            last_error = e

        # Tier 5: 直连（无代理，最后手段）
        try:
            resp = await self._try_direct(url, domain)
            if resp is not None:
                return resp
        except Exception as e:
            logger.error(f"直连失败 [{domain}]: {e}")
            last_error = e

        raise last_error or RuntimeError(f"所有回退层级均失败: {url}")

    async def _try_protocol(self, url, domain, proxy, extra_headers=None):
        """Tier 1: 标准协议请求"""
        resp = await self.protocol.fetch(url, proxy=proxy, extra_headers=extra_headers)
        if not self._needs_solver(resp):
            logger.debug(f"[{self.FALLBACK_PROTOCOL}] 成功: {url}")
            return resp
        return None  # 需要升级

    async def _try_stealth(self, url, domain):
        """Tier 2: 隐身协议（附加反检测头）"""
        if not hasattr(self.session_pool, 'get_stealth_headers'):
            return None  # session_pool 不支持，跳过
        stealth_headers = self.session_pool.get_stealth_headers(url)
        proxy = await self.exit_mgr.acquire_for_domain(domain)
        try:
            resp = await self.protocol.fetch(url, proxy=proxy, extra_headers=stealth_headers)
            if not self._needs_solver(resp):
                logger.debug(f"[{self.FALLBACK_STEALTH}] 成功: {url}")
                return resp
        finally:
            await self.exit_mgr.release(proxy)
        return None

    async def _try_tls_switch(self, url, domain):
        """Tier 3: TLS 指纹切换"""
        if not hasattr(self.session_pool, 'get_tls_profile'):
            return None  # session_pool 不支持，跳过
        self._tls_fingerprint_idx = (self._tls_fingerprint_idx + 1) % 3
        tls_profile = self.session_pool.get_tls_profile(self._tls_fingerprint_idx)
        proxy = await self.exit_mgr.acquire_for_domain(domain)
        try:
            # [v2.17 0-7] 同源绑定：_build_headers 的 UA 主版本由 pick_tls_impersonate(proxy)
            # 决定——这里必须用同一选择函数，Tier3 才不破坏 UA↔TLS 同源签名
            # （原直接透传 idx 轮换 profile，与 UA 版本可能错位）。
            from .fingerprint_consistency import pick_tls_impersonate
            tls_profile = pick_tls_impersonate(proxy or "")
            resp = await self.protocol.fetch(url, proxy=proxy, tls_profile=tls_profile)
            if not self._needs_solver(resp):
                logger.debug(f"[{self.FALLBACK_TLS}] 成功: {url}")
                return resp
        finally:
            await self.exit_mgr.release(proxy)
        return None

    async def _try_solver(self, url, domain):
        """Tier 4: 浏览器求解引擎

        [v2.19.6 安全·审查发现] 浏览器层必须**自建 SSRF 闸**（纵深防御）：
        此前该层零校验，而它使用的 Playwright `page.goto()` 会**原生跟随重定向**
        与 JS 跳转——主通道的闸即使拦住，也会被"升级到浏览器"这条回退链绕过
        （实测：主通道返回拦截态 → 升级 → 浏览器跟随 302 进 169.254.169.254 → 200）。
        """
        from .url_utils import is_private_url as _is_priv
        if _is_priv(url, dns_check=True):
            logger.warning(f"[{self.FALLBACK_SOLVER}] SSRF 拦截：私网/保留地址，拒绝浏览器加载: {url[:60]}")
            return None
        proxy = await self.exit_mgr.acquire_for_domain(domain)
        try:
            html, status, headers = await self.solver.solve(
                url, proxy=proxy, challenge_wait=self.challenge_wait)
            # 求解引擎返回，检查是否仍需继续
            if status and status not in (403, 429, 503, 0):
                logger.debug(f"[{self.FALLBACK_SOLVER}] 成功: {url} status={status}")
                return ResponseAdapter(status, url, headers, raw_text=html)
        except Exception:
            pass
        finally:
            await self.exit_mgr.release(proxy)
        return None

    async def _try_direct(self, url, domain):
        """Tier 5: 直连（无代理，最后手段）

        [v2.19.6 安全] 同样补闸——直连路径此前依赖 protocol.fetch 内部校验，
        但该层在 `_needs_solver` 判定后可能被跳过，显式校验避免遗漏。
        """
        from .url_utils import is_private_url as _is_priv
        if _is_priv(url, dns_check=True):
            logger.warning(f"[{self.FALLBACK_DIRECT}] SSRF 拦截：私网/保留地址，拒绝直连: {url[:60]}")
            return None
        resp = await self.protocol.fetch(url, proxy=None)
        logger.debug(f"[{self.FALLBACK_DIRECT}] 尝试直连: {url} status={resp.status_code}")
        return resp

    def _needs_solver(self, resp: ResponseAdapter) -> bool:
        """检测响应是否需要升级到下一级回退"""
        if resp.status_code in (403, 429, 503):
            return True
        text = resp._raw_text or ""
        if any(kw in text.lower() for kw in ["cf-challenge", "turnstile", "datadome", "captcha"]):
            return True
        return False

    async def close(self):
        await self.protocol.close()
        await self.solver.close()
