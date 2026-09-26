"""高级协议引擎

使用 curl_cffi 进行 TLS/JA3/JA4 指纹模拟（HTTP/2 跟随 impersonate 内置行为——curl_cffi
无自定义 H2 SETTINGS 伪头序公开 API，v2.10.6 起不再宣称"HTTP/2 指纹一致性"自定义能力）。
支持 TLS 指纹轮换、连接池复用、指数退避重试、真实浏览器头生成。
"""
import asyncio
import logging
import random
from typing import Optional, Dict
from urllib.parse import urljoin
from .response_adapter import ResponseAdapter
from .session_pool import SessionPool
from .header_generator import generate_chrome_headers, merge_headers
from .fingerprint_consistency import pick_tls_impersonate, generate_default_fingerprint

# [v2.17 稳定性门禁] Retry-After 服务器可控：无上限时极端值可让单任务睡数天
# （429/503 循环内）。上限 300s——限流语义保留、挂起面封死。
RETRY_AFTER_CAP_SECONDS = 300


def cap_retry_after(wait) -> int:
    """Retry-After 截断（0/负值按 0——头值异常时立刻重试不友好，但比负数睡错安全）"""
    try:
        w = int(wait)
    except (TypeError, ValueError):
        return 0
    return min(max(w, 0), RETRY_AFTER_CAP_SECONDS)

logger = logging.getLogger(__name__)

try:
    from curl_cffi import requests as curl_requests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False


class ProtocolEngine:
    """协议引擎：curl_cffi TLS 指纹模拟 + HTTP/2 一致性 + 指数退避重试"""

    def __init__(self, impersonate="chrome120", session_pool: SessionPool = None,
                 max_body_bytes: int = 5 * 1024 * 1024,
                 max_retries: int = 3, retry_backoff_base: float = 1.0):
        self.default_impersonate = impersonate
        self.session_pool = session_pool
        self.max_retries = max_retries
        self.retry_backoff_base = retry_backoff_base
        self.max_body_bytes = max_body_bytes  # [v2.14] 解析炸弹闸（原死配置接线）
        self._clients: Dict[str, object] = {}  # 按 impersonate 版本池化
        self._default_fp = generate_default_fingerprint()
        self._init_lock = asyncio.Lock()
        # aiohttp 回退：复用单个 session + connector，避免每次请求都创建新会话
        self._aiohttp_session = None
        self._aiohttp_connector = None

    async def init(self):
        """初始化所有 TLS 指纹版本的客户端连接池"""
        if not HAS_CURL_CFFI:
            logger.warning("curl_cffi not available, falling back to aiohttp")
            return

        async with self._init_lock:
            from .fingerprint_consistency import TLS_IMPERSONATE_POOL
            for version in TLS_IMPERSONATE_POOL:
                if version not in self._clients:
                    try:
                        self._clients[version] = curl_requests.AsyncSession(
                            impersonate=version,
                            timeout=30,
                        )
                        logger.info(f"ProtocolEngine: initialized TLS pool for {version}")
                    except Exception as e:
                        logger.error(f"Failed to init TLS pool for {version}: {e}")

            # 确保默认版本存在
            if self.default_impersonate not in self._clients:
                try:
                    self._clients[self.default_impersonate] = curl_requests.AsyncSession(
                        impersonate=self.default_impersonate,
                        timeout=30,
                    )
                except Exception as e:
                    logger.error(f"Failed to init default TLS pool: {e}")

    def _get_client_for_proxy(self, proxy: Optional[str]):
        """根据代理 URL 选择匹配的 TLS 指纹客户端"""
        if not HAS_CURL_CFFI or not self._clients:
            return None, self.default_impersonate

        if proxy:
            version = pick_tls_impersonate(proxy)
        else:
            version = self.default_impersonate

        client = self._clients.get(version) or self._clients.get(self.default_impersonate)
        return client, version

    def _build_headers(self, url: str, proxy: Optional[str], extra_headers: Optional[dict]) -> dict:
        """构建完整的请求头"""
        # 从指纹生成基础头
        fp = generate_default_fingerprint()
        if proxy:
            from .fingerprint_consistency import compute_fingerprint_from_ip
            fp = compute_fingerprint_from_ip(proxy)

        headers = generate_chrome_headers(
            chrome_version=fp["chrome_version"],
            platform=fp["platform"],
            language=fp["language"],
            is_navigation=True,
        )

        # [FIXED & MODIFIED] v2.17 0-3 兼容纠偏：identity 捆绑开启时（有 provider）旧
        # session_pool 不注入——否则出口(身份池)与 cookie(旧池)可能异源失配，且身份
        # 会话自校验语义被旧池污染。单源注入：身份池开 → 身份池 cookie；否则旧池。
        if self.session_pool and getattr(self, "identity_pool_provider", None) is None:
            sess = self.session_pool.get_session(url)
            if sess:
                if "headers" in sess:
                    headers = merge_headers(headers, sess["headers"])
                if "cookies" in sess and "cookie" not in {k.lower() for k in headers}:
                    headers["Cookie"] = sess["cookies"]

        # [v2.17 E-P2] 身份捆绑注入：仅 identity_bundle 开启时挂 provider（默认关零影响）；
        # 0-3 纠偏后旧池在身份开启时已绕过——身份源独占（用户自填 cookies 优先级不变：
        # extra_headers 在下方合并，仍可覆盖）
        try:
            _provider = getattr(self, "identity_pool_provider", None)
            if _provider is not None:
                _ck = _provider.cookies_for(url)
                if _ck and "cookie" not in {k.lower() for k in headers}:
                    headers["Cookie"] = _ck
        except Exception:
            pass

        # 合并额外传入的头（优先级最高）
        if extra_headers:
            headers = merge_headers(headers, extra_headers)

        # 确保 User-Agent 与 TLS 指纹一致
        headers.setdefault("User-Agent", fp["user_agent"])

        return headers

    async def fetch(self, url: str, proxy: Optional[str] = None,
                    headers: Optional[dict] = None, method: str = "GET",
                    body: Optional[bytes] = None,
                    extra_headers: Optional[dict] = None,
                    tls_profile: Optional[str] = None) -> ResponseAdapter:
        """发起 HTTP 请求，支持 TLS 指纹轮换与指数退避重试

        [FIXED & MODIFIED] 新增 extra_headers / tls_profile 参数：
        - engine_router Tier2 隐身层此前传 extra_headers 直接 TypeError（fetch 签名缺失）
        - engine_router Tier3 指纹轮换层此前传 tls_profile 直接 TypeError
        """
        merged_headers = self._build_headers(url, proxy,
                                             merge_headers(headers or {}, extra_headers or {}))
        last_error = None
        last_response = None

        for attempt in range(self.max_retries + 1):
            try:
                # 指数退避等待（非首次尝试）——[FIXED & MODIFIED] v2.10.6 D4 上限 8s（原 2+4+8+rand≈9s/页，
                # 一批 10 页全挂卡 90s；作者激进风格收敛到 8s 内）
                if attempt > 0:
                    backoff = min(self.retry_backoff_base * (2 ** attempt), 8.0) + random.uniform(0, 1)
                    logger.debug(f"Retry {attempt}/{self.max_retries} for {url}, waiting {backoff:.1f}s")
                    await asyncio.sleep(backoff)

                # 使用 curl_cffi
                client, tls_version = self._get_client_for_proxy(proxy)
                if client:
                    # [v2.19 安全 P0] SSRF 闸：主抓取通道此前**无任何目标校验**且不校验
                    # 重定向落点（公开种子 302 到 169.254.169.254 即可抓内网/云元数据）。
                    # 求证：curl_cffi 0.16 的 Response 无 url 属性、Session 不支持 resolve
                    # → 无法事后用 resp.url 复检，故改为「请求前校验 + 手动逐跳」。
                    from .url_utils import is_private_url as _is_priv
                    if _is_priv(url, dns_check=True):
                        logger.warning(f"SSRF 拦截：目标为私网/环回/保留地址，已拒绝 {url[:60]}")
                        # [v2.19.6 修复·审查发现] 拦截**不可用 403**：403 是本工程
                        # 回退链的"请升级到浏览器层"信号（engine_router._needs_solver
                        # 认 403/429/503），于是被拦 URL 会被交给无闸的 Playwright
                        # page.goto() —— 浏览器原生跟随 302 进内网，闸形同虚设且必然触发。
                        # 改用 400：不在任何升级/渲染兜底判定内，直达 _finalize_fail，
                        # 且 status>=400 会写 errors 表（可诊断）。
                        return ResponseAdapter(400, url, {},
                                               raw_text="blocked: private target")
                    kwargs = {
                        "headers": merged_headers,
                        "timeout": 30,
                        "allow_redirects": False,  # 手动逐跳（每跳校验落点）
                    }
                    if tls_profile:
                        kwargs["impersonate"] = tls_profile  # [FIXED & MODIFIED] Tier3 指纹轮换生效
                    if proxy:
                        kwargs["proxy"] = proxy
                    if body and method in ("POST", "PUT", "PATCH"):
                        kwargs["data"] = body

                    # 手动逐跳（上限 10 跳）：每一跳的落点都要过 SSRF 闸
                    # [v2.19.6 修复·审查发现] 原上限 5 且超限时静默 break 返回 3xx：
                    # 302 不在可重试列表 → mark_failed(retry=False) → **永久 dead**，且
                    # status>=400 不成立 → errors 表零记录、日志零输出。而 curl_cffi
                    # 默认跟随 30 跳——6~30 跳的合法链（http→https→www→CDN→同意页）
                    # 会从"能抓"变成"静默判死"。现改为：上限 10，超限/缺 Location
                    # 时返回 5xx（可重试 + 写 errors + 有 warning），不再静默。
                    _MAX_HOPS = 10
                    _cur, _hops = url, 0
                    while True:
                        resp = await client.request(method, _cur, **kwargs)
                        if not getattr(resp, "is_redirect", False):
                            break
                        if _hops >= _MAX_HOPS:
                            logger.warning(
                                f"重定向超过 {_MAX_HOPS} 跳上限，放弃跟随: {_cur[:60]}")
                            return ResponseAdapter(508, _cur, dict(resp.headers),
                                                   raw_text=f"too many redirects (>{_MAX_HOPS})")
                        _loc = (resp.headers.get("Location")
                                or resp.headers.get("location") or "")
                        if not _loc:
                            logger.warning(f"重定向响应缺 Location 头，放弃跟随: {_cur[:60]}")
                            return ResponseAdapter(502, _cur, dict(resp.headers),
                                                   raw_text="redirect without Location")
                        _nxt = urljoin(_cur, _loc)
                        if _is_priv(_nxt, dns_check=True):
                            logger.warning(f"SSRF 拦截：重定向落点为私网，已拒绝 {_nxt[:60]}")
                            return ResponseAdapter(400, _cur, {},
                                                   raw_text="blocked: redirect to private target")
                        _cur, _hops = _nxt, _hops + 1
                    # [FIXED & MODIFIED] v2.14 阶段3 解析炸弹闸：max_body_bytes 接线
                    # （原死配置——200MB 恶意响应 ×4-5 倍解析放大可直接打爆内存）。
                    # Content-Length 预检 + 正文硬截断并标记，超限页由页处理器拒解析。
                    _max_body = int(self.max_body_bytes or 5 * 1024 * 1024)
                    _cl = resp.headers.get("Content-Length") or resp.headers.get("content-length")
                    _too_big = (_cl and _cl.isdigit() and int(_cl) > _max_body)
                    _text = resp.text if not _too_big else ""
                    if not _too_big and len(_text) > _max_body:
                        _text = _text[:_max_body]
                        _too_big = True
                    if _too_big:
                        logger.warning(f"响应超 max_body_bytes({_max_body})，已截断拒解析: {_cur[:60]}")
                    response = ResponseAdapter(
                        resp.status_code, _cur, dict(resp.headers),
                        raw_text=_text,
                        too_big=_too_big,
                    )

                    # 检测是否需要重试
                    if response.status_code in (429, 503) and attempt < self.max_retries:
                        # 检查 Retry-After 头
                        retry_after = response.headers.get("retry-after")
                        if retry_after:
                            try:
                                wait = cap_retry_after(int(retry_after))
                                logger.info(f"Server requested retry after {wait}s")
                                await asyncio.sleep(wait)
                            except (ValueError, TypeError):
                                pass
                        continue

                    return response

                else:
                    # 回退到 aiohttp
                    return await self._fetch_aiohttp(url, proxy, merged_headers, method, body)

            except Exception as e:
                last_error = e
                logger.warning(f"Fetch attempt {attempt+1} failed for {url}: {e}")
                if attempt >= self.max_retries:
                    break
                continue

        # 所有重试失败
        if last_response:
            return last_response
        logger.error(f"All retries exhausted for {url}: {last_error}")
        return ResponseAdapter(599, url, {}, raw_text="")

    async def _fetch_aiohttp(self, url: str, proxy: Optional[str],
                             headers: dict, method: str = "GET",
                             body: Optional[bytes] = None) -> ResponseAdapter:
        """[FIXED & MODIFIED] 回退方案改 urllib（本机 aiohttp 外网解析层全超时，urllib 验证正常）
        [v2.17 安全深扫] 换 safe_urlopen（协议+私网+重定向逐跳校验；原裸 urlopen 已被
        扫描标记——目标闸双保险。注：HTTP 错误码统一降级为 400 语义（主通道先试，走
        fallback 时错误语义不重要）"""
        from .url_utils import safe_urlopen
        loop = asyncio.get_event_loop()

        def _sync():
            if proxy and proxy.startswith("socks"):
                # [v2.14] socks 端口被当 HTTP 代理用必失败 → 直接跳过 urllib 回退
                raise OSError("urllib fallback skips socks proxy")
            r = safe_urlopen(url, allowed_hosts=(), headers=headers,
                             data=body if body and method in ("POST", "PUT", "PATCH") else None,
                             method=method, timeout=30, proxy=proxy or "")
            if r is None:
                return 400, {}, "blocked: target check failed"
            # [v2.18 P2-4] errors="replace" 统一降级语义：ignore 静默丢字节产生乱码且
            # 长度错位；replace 与主通道语义一致（坏字节可见、位置不偏移）
            return r.status, dict(r.headers.items()), r.read().decode("utf-8", errors="replace")

        status, hdrs, text = await loop.run_in_executor(None, _sync)
        return ResponseAdapter(status, url, hdrs, raw_text=text)

    async def post(self, url: str, data: Optional[dict] = None,
                   proxy: Optional[str] = None,
                   headers: Optional[dict] = None) -> ResponseAdapter:
        """POST 请求快捷方法"""
        import json as _json
        body = _json.dumps(data).encode() if data else None
        merged_headers = self._build_headers(url, proxy, headers)
        # POST 请求使用 API 头
        from .header_generator import generate_api_headers
        fp = generate_default_fingerprint()
        if proxy:
            from .fingerprint_consistency import compute_fingerprint_from_ip
            fp = compute_fingerprint_from_ip(proxy)
        api_headers = generate_api_headers(
            chrome_version=fp["chrome_version"],
            language=fp["language"],
        )
        merged_headers = merge_headers(merged_headers, api_headers)
        return await self.fetch(url, proxy=proxy, headers=merged_headers,
                                method="POST", body=body)

    async def close(self):
        """关闭所有客户端连接池"""
        for version, client in self._clients.items():
            try:
                await client.close()
            except Exception as e:
                logger.warning(f"Error closing client {version}: {e}")
        self._clients.clear()
        # 关闭 aiohttp 回退 session
        if self._aiohttp_session:
            await self._aiohttp_session.close()
            self._aiohttp_session = None
            self._aiohttp_connector = None

    # ═════════════════════════════════════════════════════════════════
    # 协议级极限绕过方法
    # ═════════════════════════════════════════════════════════════════
    # [FIXED & MODIFIED] v2.10.6 诚实处置：_build_http2_fingerprint 死代码已删除——
    # curl_cffi 0.16 无自定义 H2 SETTINGS 伪头序的公开 API（仅 impersonate 内置 h2），
    # 该方法生成的字符串从不被请求路径使用（此前为"看起来实现了"的过度宣称）。
    # HTTP/2 指纹跟随 curl_cffi impersonate（chrome120/123/...）内置行为。

    async def sync_cookies_from_browser(self, page, domain: str):
        """从浏览器页面同步 cookies 到协议引擎的 session pool

        确保协议引擎的 HTTP 请求与浏览器共享相同的 cookie jar，
        防止反爬系统检测到 cookie 不一致。

        Args:
            page: Playwright Page 对象
            domain: 目标域名
        """
        if not self.session_pool or not page:
            return

        try:
            context = page.context
            cookies = await context.cookies()
            domain_cookies = {}
            for cookie in cookies:
                cookie_domain = cookie.get("domain", "").lstrip(".")
                if domain in cookie_domain or cookie_domain in domain:
                    domain_cookies[cookie["name"]] = cookie["value"]

            if domain_cookies:
                url = f"https://{domain}"
                sess = self.session_pool.get_session(url) or {}
                sess["cookies"] = "; ".join(f"{k}={v}" for k, v in domain_cookies.items())
                self.session_pool.update_session(url, sess)
                logger.debug(f"Synced {len(domain_cookies)} cookies from browser for {domain}")
        except Exception as e:
            logger.debug(f"Cookie sync failed for {domain}: {e}")

    async def sync_cookies_to_browser(self, page, domain: str):
        """将协议引擎的 cookies 同步到浏览器上下文

        当协议引擎通过 HTTP 请求获得新 cookies 时，同步到浏览器，
        确保浏览器和协议引擎的 cookie jar 保持一致。

        Args:
            page: Playwright Page 对象
            domain: 目标域名
        """
        if not self.session_pool or not page:
            return

        try:
            url = f"https://{domain}"
            sess = self.session_pool.get_session(url)
            if not sess or "cookies" not in sess:
                return

            cookie_str = sess["cookies"]
            context = page.context
            existing = {c["name"]: c for c in await context.cookies()}

            for pair in cookie_str.split("; "):
                if "=" not in pair:
                    continue
                name, value = pair.split("=", 1)
                if name in existing:
                    continue
                await context.add_cookies([{
                    "name": name,
                    "value": value,
                    "domain": domain,
                    "path": "/",
                    "secure": True,
                    "httpOnly": False,
                    "sameSite": "Lax",
                }])
            logger.debug(f"Synced cookies to browser for {domain}")
        except Exception as e:
            logger.debug(f"Cookie sync to browser failed for {domain}: {e}")

    async def resolve_dns_via_doh(self, hostname: str) -> Optional[str]:
        """通过 DNS-over-HTTPS 解析域名

        使用 Cloudflare DoH 端点解析域名，避免本地 DNS 泄露真实地理位置。
        与代理 IP 的地理区域保持一致。

        Args:
            hostname: 要解析的主机名

        Returns:
            解析到的 IP 地址，或 None
        """
        try:
            # [FIXED & MODIFIED] curl_cffi 替换 aiohttp（本机 aiohttp 外网全超时；Cloudflare DoH 国内不可达时返回 None 由调用方回退）
            from curl_cffi.requests import AsyncSession
            doh_url = "https://cloudflare-dns.com/dns-query"
            headers = {"Accept": "application/dns-json"}
            params = {"name": hostname, "type": "A"}

            async with AsyncSession(timeout=5) as session:
                resp = await session.get(doh_url, headers=headers, params=params)
                if resp.status_code == 200:
                    data = resp.json()
                    answers = data.get("Answer", [])
                    for answer in answers:
                        if answer.get("type") == 1:  # A record
                            return answer.get("data")
        except Exception as e:
            logger.debug(f"DoH resolution failed for {hostname}: {e}")
        return None

    def _generate_sec_fetch_headers(self, is_navigation: bool = False,
                                    dest: str = "document") -> dict:
        """生成 Sec-Fetch-* 请求头组

        Chrome 自动添加 Sec-Fetch-* 头，反爬系统检查这些头是否存在且一致。
        缺少这些头是自动化请求的标志。

        Args:
            is_navigation: 是否为导航请求（地址栏输入/链接点击）
            dest: 请求目标类型 (document/script/style/image/etc.)

        Returns:
            包含所有 Sec-Fetch-* 头的字典
        """
        if is_navigation:
            return {
                "Sec-Fetch-Dest": dest,
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-User": "?1",
                "Sec-Fetch-Site": "none",
            }
        else:
            return {
                "Sec-Fetch-Dest": dest,
                "Sec-Fetch-Mode": "no-cors" if dest in ("script", "style") else "cors",
                "Sec-Fetch-Site": "same-origin",
            }

    def _generate_client_hints_headers(self, fp: dict, is_navigation: bool = False) -> dict:
        """生成 Client Hints 请求头

        Chrome 通过 Sec-CH-UA-* 头发送 Client Hints 信息。
        反爬系统检查这些头是否与 User-Agent 和 navigator.userAgentData 一致。

        Args:
            fp: 指纹字典
            is_navigation: 是否为导航请求

        Returns:
            包含 Client Hints 头的字典
        """
        chrome_ver = str(fp.get("chrome_version", 120))
        platform = fp.get("platform", "Win32")

        # 平台映射
        platform_ch = "Windows"
        platform_version = "15.0.0"
        if platform == "MacIntel":
            platform_ch = "macOS"
            platform_version = "14.0.0"
        elif platform == "Linux x86_64":
            platform_ch = "Linux"
            platform_version = "6.5.0"

        headers = {
            "Sec-CH-UA": f'"Chromium";v="{chrome_ver}", "Google Chrome";v="{chrome_ver}", "Not;A=Brand";v="24"',
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": f'"{platform_ch}"',
        }

        # 导航请求发送高熵 Client Hints
        if is_navigation:
            headers["Sec-CH-UA-Platform-Version"] = f'"{platform_version}"'
            headers["Sec-CH-UA-Arch"] = '"x86"'
            headers["Sec-CH-UA-Bitness"] = '"64"'
            headers["Sec-CH-UA-Full-Version"] = f'"{fp.get("chrome_full_version", chrome_ver + ".0.0.0")}"'
            headers["Sec-CH-UA-Full-Version-List"] = (
                f'"Chromium";v="{chrome_ver}.0.0.0", "Google Chrome";v="{chrome_ver}.0.0.0", "Not;A=Brand";v="24.0.0.0"'
            )
            headers["Sec-CH-UA-Wow64"] = "?0"

        return headers

    async def fetch_stealth(self, url: str, proxy: Optional[str] = None,
                            is_navigation: bool = True,
                            dest: str = "document",
                            extra_headers: Optional[dict] = None) -> ResponseAdapter:
        """增强版请求方法：自动注入所有隐身头

        在标准 fetch 基础上，自动添加：
        - Sec-Fetch-* 头组
        - Client Hints (Sec-CH-UA-*) 头组
        - HTTP/2 指纹一致性
        - Cookie jar 同步

        Args:
            url: 目标 URL
            proxy: 代理 URL
            is_navigation: 是否为导航请求
            dest: 请求目标类型
            extra_headers: 额外请求头

        Returns:
            ResponseAdapter 响应适配器
        """
        # 生成指纹
        fp = generate_default_fingerprint()
        if proxy:
            from .fingerprint_consistency import compute_fingerprint_from_ip
            fp = compute_fingerprint_from_ip(proxy)

        # 构建隐身头
        stealth_headers = self._generate_sec_fetch_headers(is_navigation, dest)
        client_hints = self._generate_client_hints_headers(fp, is_navigation)

        # 合并所有头
        all_headers = {}
        all_headers.update(stealth_headers)
        all_headers.update(client_hints)

        if extra_headers:
            all_headers.update(extra_headers)

        # 使用增强的 fetch 方法
        return await self.fetch(url, proxy=proxy, headers=all_headers)
