"""引擎路由：多级回退链

回退链: protocol → stealth → TLS switch → solver → direct
每级失败后自动升级到下一级，确保最终可到达目标。
"""
import logging
import re
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
        # [v6 可观测性] 求解引擎抛异常的次数。此前这类异常被静默吞掉，
        # "求解器崩了"与"求解器没给出可用响应"无法区分；留一个可读的计数。
        self.solver_exceptions = 0

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
                # [v6 修复·R6 根因 A **主路径**] 原来这里写的是 `url`（**请求 URL**），
                # 而浏览器**真实落点**被求解器塞在 `headers["_final_url"]` 里
                # （见 `solver_engine.py` 的 `render_simple` / `_do_solve`）—— **没人读**。
                # 后果：`page_processor._final_url_of` 先读 `response.url` 就命中短链主机，
                # 于是整页的相对链接都被按 `b23.tv` 解析 ⇒
                # `links.internal` 里几十条 `https://b23.tv/video/BV...`。
                #
                # 而 B站**每个视频页骨架**都内联
                # `<script src=".../risk-captcha-sdk/CaptchaLoader.js">`，
                # `_needs_solver` 的裸子串 `"captcha"` 必然命中 ⇒ **B站每页都走这条 Tier-4**，
                # 所以这个 bug 在 B站上是**必然**发生、不是偶发。
                #
                # `ResponseAdapter.url` 的语义本来就是"重定向链终点"（全工程只有
                # `page_processor._final_url_of` 一个消费者），浏览器落点正是这个语义。
                _final = str((headers or {}).get("_final_url") or url)
                return ResponseAdapter(status, _final, headers, raw_text=html)
        except Exception as e:
            # [v6 修复] 原为 `except Exception: pass`——求解引擎抛异常（浏览器崩 / CDP 失联 /
            # 超时）会被**无声吞掉**，调用方只看到 None，于是
            # "**求解器崩了**"与"**求解器没给出可用响应**"变得无法区分。
            # 而整条回退链存在的意义就是回答"这个站为什么失败"（M2 的结构探针与 trace
            # 取证都是为它服务的）——这里是回退链上**最该留下线索**的一处。
            self.solver_exceptions += 1
            logger.warning(f"[{self.FALLBACK_SOLVER}] 求解异常，按无响应继续回退 "
                           f"(已计 {self.solver_exceptions} 次): "
                           f"{type(e).__name__}: {str(e)[:120]} | {url[:60]}")
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
        """检测响应是否需要升级到下一级回退。

        [v6 修复·R6 根因 A 的触发面 —— **真机实测出来的误报**]
        原来判据里有个**裸子串 `"captcha"`**：
        ```python
        if any(kw in text.lower() for kw in ["cf-challenge","turnstile","datadome","captcha"])
        ```
        而 **B站每个视频页骨架**都内联
        `<script defer src="https://s1.hdslb.com/bfs/seed/jinkela/risk-captcha-sdk/CaptchaLoader.js">`
        ⇒ 裸子串必然命中 ⇒ **Tier1/2/3 已经拿到 200 完好正文，仍被踢到 Tier-4 浏览器层**。
        真机插桩实测：
        ```
        [NEEDS-PROBE] status=200 url=https://www.bilibili.com/video/BV18sKG6KERy/ -> True 命中=['captcha']
        ```
        **代价有两层**：
          · 性能 —— **每个 B站页面白起一次 Chromium**（一次跑一页就初始化 5 个浏览器上下文）；
          · 正确性 —— Tier-4 正是 R6 基址丢失的入口，所以这个误报**必然**引出 R6。

        **修法**（与本工程既有先例一致 —— `captcha_solver_extended` 里
        "页面上出现过 geetest 字样但没有极验控件 ⇒ 判定为误报"）：
        要求**真挑战信号**，不接受"整页里出现过某个词"：
          · **强信号**照旧（都是厂商专有标识，基本不会出现在无关内容里）；
          · 裸 `captcha` **降级**为"仅当它出现在**非脚本外链**的位置才算"——
            先把 `<script src>` / `<link href>` 的取值抠掉再看，
            这样 `CaptchaLoader.js` 这类**预加载**不再触发误报，
            而真正的验证码表单/图片仍然命中。

        ⚠️ `(403, 429, 503)` 与"不得含 400"是既有测试
        （`test_v2196_review_fixes.py`）钉死的回退信号，**未动**。
        """
        if resp.status_code in (403, 429, 503):
            return True
        text = resp._raw_text or ""
        if not text:
            return False
        low = text.lower()
        # ① 强信号：厂商专有标识，不会因为"页面里提了一句"就误报
        if any(kw in low for kw in ("cf-challenge", "cf_challenge", "cf-mitigated",
                                    "turnstile", "datadome", "dd-key",
                                    "g-recaptcha", "h-captcha", "hcaptcha",
                                    "geetest_holder", "geetest_btn_click",
                                    "nc_1_n1z", "px-captcha", "perimeterx")):
            return True
        # ② 弱信号 `captcha`：必须出现在**脚本/样式外链之外**。
        #    先把 src/href 的取值抹掉再判 —— 这样预加载的
        #    `.../risk-captcha-sdk/CaptchaLoader.js` 不再触发误报。
        stripped = re.sub(r'(?:src|href)\s*=\s*["\'][^"\']*["\']', '""', low)
        if "captcha" in stripped:
            return True
        return False

    async def close(self):
        await self.protocol.close()
        await self.solver.close()
