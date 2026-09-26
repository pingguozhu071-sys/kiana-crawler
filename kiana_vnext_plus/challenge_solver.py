"""高级反爬挑战求解器

检测并求解 Turnstile、DataDome、reCAPTCHA v2/v3、hCaptcha、Akamai、PerimeterX。
集成人类行为模拟以提升自动通过率。
"""
import asyncio
import logging
import random
import time
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

try:
    from twocaptcha import TwoCaptcha
    HAS_TWOCAPTCHA = True
except ImportError:
    HAS_TWOCAPTCHA = False


class ChallengeSolver:
    """反爬挑战求解器：检测并求解主流验证码与反爬挑战

    支持的挑战类型：
    - Cloudflare Turnstile（自动通过 + 点击模拟）
    - DataDome（行为模拟 + Cookie 提取）
    - reCAPTCHA v2（打码 API）
    - reCAPTCHA v3（行为模拟 + 评分优化）
    - hCaptcha（打码 API）
    - Akamai Bot Manager（检测 + _abck Cookie 处理）
    - PerimeterX（检测 + 行为模拟）
    """

    def __init__(self, page, max_wait=30, two_captcha_key=None,
                 capsolver_key=None, anti_captcha_key=None):
        self.page = page
        self.max_wait = max_wait
        # 多打码平台支持
        self.solver = None
        if two_captcha_key and HAS_TWOCAPTCHA:
            self.solver = TwoCaptcha(two_captcha_key)
        self.capsolver_key = capsolver_key
        self.anti_captcha_key = anti_captcha_key

    async def detect(self) -> Optional[str]:
        """检测页面上的反爬挑战类型

        Returns:
            挑战类型字符串，或 None（无挑战）
        """
        try:
            content = await self.page.content()
            lower = content.lower()

            # Cloudflare Turnstile / CF Challenge
            if any(kw in lower for kw in ['turnstile', 'cf-challenge', 'cf_challenge',
                                           'challenges.cloudflare.com']):
                return 'turnstile'

            # DataDome
            if any(kw in lower for kw in ['datadome', 'dd-key', 'datadomecdn']):
                return 'datadome'

            # Akamai Bot Manager
            if any(kw in lower for kw in ['_abck', 'akamai', 'bm_sz', 'akamaized']):
                return 'akamai'

            # PerimeterX / HUMAN
            if any(kw in lower for kw in ['_px', 'px-captcha', 'perimeterx',
                                           'pxhd', 'human security']):
                return 'perimeterx'

            # reCAPTCHA
            if any(kw in lower for kw in ['g-recaptcha', 'recaptcha',
                                           'google.com/recaptcha']):
                # 区分 v2 和 v3
                if 'g-recaptcha-response' in lower and 'data-size' in lower:
                    return 'recaptcha_v2'
                if 'grecaptcha.execute' in lower or 'data-action' in lower:
                    return 'recaptcha_v3'
                return 'recaptcha_v2'

            # hCaptcha
            if 'hcaptcha' in lower or 'h-captcha' in lower or 'hcaptcha.com' in lower:
                return 'hcaptcha'

            # 通用 iframe 挑战检测
            iframes = await self.page.query_selector_all('iframe')
            for iframe in iframes:
                src = await iframe.get_attribute('src') or ''
                src_lower = src.lower()
                if 'challenges.cloudflare.com' in src_lower:
                    return 'turnstile'
                if 'recaptcha' in src_lower:
                    return 'recaptcha_v2'
                if 'hcaptcha' in src_lower:
                    return 'hcaptcha'
                if 'px-captcha' in src_lower:
                    return 'perimeterx'

            return None
        except Exception as e:
            logger.debug(f"Challenge detection error: {e}")
            return None

    async def solve(self) -> bool:
        """求解检测到的挑战

        Returns:
            True 如果求解成功，False 否则
        """
        t = await self.detect()
        if t is None:
            return True  # 无挑战，视为成功

        logger.info(f"Solving challenge: {t}")

        if t == 'turnstile':
            return await self._solve_turnstile()
        elif t == 'datadome':
            return await self._solve_datadome()
        elif t == 'recaptcha_v2':
            return await self._solve_recaptcha_v2()
        elif t == 'recaptcha_v3':
            return await self._solve_recaptcha_v3()
        elif t == 'hcaptcha':
            return await self._solve_hcaptcha()
        elif t == 'akamai':
            return await self._solve_akamai()
        elif t == 'perimeterx':
            return await self._solve_perimeterx()
        return False

    # ═══════════════════════════════════════════════════════════════
    # Cloudflare Turnstile 求解
    # ═══════════════════════════════════════════════════════════════
    async def _solve_turnstile(self) -> bool:
        """Turnstile 求解：行为模拟 + 多策略点击 + 等待自动通过"""
        # 策略 1：先模拟人类行为（鼠标移动、滚动）以提升自动通过率
        await self._simulate_human_behavior()

        for attempt in range(3):
            try:
                # 查找 Turnstile iframe
                iframe = await self._find_turnstile_iframe()

                if iframe:
                    # 获取 iframe 位置
                    box = await iframe.bounding_box()
                    if box:
                        # 人类式移动到复选框位置
                        target_x = box['x'] + box['width'] / 2
                        target_y = box['y'] + box['height'] / 2
                        await self._human_click_at(target_x, target_y)
                        logger.info(f"Turnstile click at ({target_x:.0f}, {target_y:.0f})")
                else:
                    # 尝试直接点击 .check 元素
                    cb = self.page.locator('div.check, input[type="checkbox"]')
                    if await cb.count() > 0:
                        await cb.first.click(delay=random.randint(50, 200))

                # 等待 Turnstile 通过
                if await self._wait_turnstile_pass():
                    logger.info("Turnstile passed")
                    return True

            except Exception as e:
                logger.debug(f"Turnstile attempt {attempt+1} failed: {e}")

            # 重试前刷新页面
            if attempt < 2:
                await asyncio.sleep(random.uniform(2, 4))
                try:
                    await self.page.reload(wait_until="domcontentloaded")
                    await asyncio.sleep(1)
                except Exception:
                    pass

        return False

    async def _find_turnstile_iframe(self):
        """查找 Turnstile iframe 元素"""
        selectors = [
            'iframe[src*="challenges.cloudflare.com"]',
            'iframe[src*="turnstile"]',
            'iframe[title*="Cloudflare"]',
            'iframe[title*="turnstile"]',
        ]
        for sel in selectors:
            try:
                el = self.page.locator(sel)
                if await el.count() > 0:
                    return await el.first.element_handle()
            except Exception:
                continue
        return None

    async def _wait_turnstile_pass(self) -> bool:
        """等待 Turnstile 通过的多种检测方式"""
        checks = [
            # 检查 cf-turnstile-response 是否有值
            """() => {
                const el = document.querySelector('[name="cf-turnstile-response"]');
                return el && el.value && el.value.length > 10;
            }""",
            # 检查 data-callback 是否已触发
            """() => {
                const el = document.querySelector('[data-callback]');
                return el !== null;
            }""",
            # 检查 Turnstile 是否显示成功状态
            """() => {
                const el = document.querySelector('.cf-turnstile');
                if (!el) return false;
                const response = el.querySelector('input[name="cf-turnstile-response"]');
                return response && response.value && response.value.length > 10;
            }""",
        ]
        for check_js in checks:
            try:
                await self.page.wait_for_function(check_js, timeout=self.max_wait * 1000)
                return True
            except Exception:
                continue
        return False

    # ═══════════════════════════════════════════════════════════════
    # DataDome 求解
    # ═══════════════════════════════════════════════════════════════
    async def _solve_datadome(self) -> bool:
        """DataDome 求解：行为模拟 + Cookie 提取 + 重试"""
        # DataDome 依赖行为分析，先进行充分的人类行为模拟
        await self._simulate_human_behavior(extended=True)

        # 等待 DataDome JS 执行完成
        await asyncio.sleep(random.uniform(3, 6))

        # 尝试点击可能的验证按钮
        try:
            btn = self.page.locator('button, input[type="button"], a.btn')
            if await btn.count() > 0:
                await btn.first.click(delay=random.randint(100, 300))
                await asyncio.sleep(random.uniform(2, 4))
        except Exception:
            pass

        # 检查是否已通过（页面变化或 Cookie 更新）
        try:
            content = await self.page.content()
            lower = content.lower()
            if 'datadome' not in lower and 'captcha' not in lower:
                logger.info("DataDome challenge passed")
                return True
        except Exception:
            pass

        # 尝试重新加载页面（有时 DataDome 在行为足够后自动放行）
        try:
            await self.page.reload(wait_until="networkidle", timeout=20000)
            await asyncio.sleep(2)
            content = await self.page.content()
            if 'datadome' not in content.lower():
                logger.info("DataDome passed after reload")
                return True
        except Exception:
            pass

        return False

    # ═══════════════════════════════════════════════════════════════
    # reCAPTCHA v2 求解
    # ═══════════════════════════════════════════════════════════════
    async def _solve_recaptcha_v2(self) -> bool:
        """reCAPTCHA v2 求解：优先使用打码 API"""
        if not self.solver:
            logger.warning("No captcha solver API configured for reCAPTCHA v2")
            return False

        site_key = await self._get_site_key('g-recaptcha', 'data-sitekey')
        if not site_key:
            return False

        try:
            # 调用打码 API（在线程池中执行以避免阻塞）
            result = await asyncio.to_thread(
                self.solver.recaptcha,
                sitekey=site_key,
                url=self.page.url,
            )
            token = result.get('code', '')
            if not token:
                return False

            # 注入 token
            await self.page.evaluate(f"""() => {{
                const el = document.getElementById('g-recaptcha-response');
                if (el) {{
                    el.style.display = 'block';
                    el.value = '{token}';
                }}
                // 尝试触发回调
                const cb = document.querySelector('[data-callback]');
                if (cb) {{
                    const cbName = cb.getAttribute('data-callback');
                    if (cbName && window[cbName]) {{
                        window[cbName]('{token}');
                    }}
                }}
            }}""")

            await asyncio.sleep(random.uniform(1, 3))
            logger.info("reCAPTCHA v2 solved via API")
            return True
        except Exception as e:
            logger.error(f"reCAPTCHA v2 solve error: {e}")
            return False

    # ═══════════════════════════════════════════════════════════════
    # reCAPTCHA v3 评分优化
    # ═══════════════════════════════════════════════════════════════
    async def _solve_recaptcha_v3(self) -> bool:
        """reCAPTCHA v3 求解：通过行为模拟提升评分

        reCAPTCHA v3 不弹验证码，而是基于行为评分。
        通过模拟真实用户行为来提升评分。
        """
        # 充分的人类行为模拟（滚动、移动鼠标、点击等）
        await self._simulate_human_behavior(extended=True)

        # 等待页面完全加载
        try:
            await self.page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

        # 尝试自然生成 token
        site_key = await self._get_site_key('g-recaptcha', 'data-sitekey')
        if not site_key:
            # 尝试从 script 标签获取
            site_key = await self.page.evaluate("""() => {
                const scripts = document.querySelectorAll('script[src*="recaptcha"]');
                for (const s of scripts) {
                    const m = s.src.match(/render=([^&]+)/);
                    if (m) return m[1];
                }
                return null;
            }""")

        if not site_key:
            logger.warning("Could not find reCAPTCHA v3 site key")
            return False

        # 获取 action
        action = await self.page.evaluate("""() => {
            const el = document.querySelector('[data-action]');
            return el ? el.getAttribute('data-action') : 'homepage';
        }""")

        # 执行 grecaptcha.execute 获取 token
        try:
            token = await self.page.evaluate(f"""() => {{
                return new Promise((resolve) => {{
                    if (window.grecaptcha && grecaptcha.execute) {{
                        grecaptcha.ready(() => {{
                            grecaptcha.execute('{site_key}', {{action: '{action}'}})
                                .then(token => resolve(token))
                                .catch(() => resolve(null));
                        }});
                    }} else {{
                        resolve(null);
                    }}
                    // 超时回退
                    setTimeout(() => resolve(null), 8000);
                }});
            }}""")

            if token and len(token) > 20:
                logger.info(f"reCAPTCHA v3 token obtained (action={action})")
                # 将 token 注入到表单中
                await self.page.evaluate(f"""() => {{
                    const el = document.querySelector('textarea[name="g-recaptcha-response"]') ||
                               document.getElementById('g-recaptcha-response');
                    if (el) el.value = '{token}';
                }}""")
                return True
        except Exception as e:
            logger.debug(f"reCAPTCHA v3 token error: {e}")

        return False

    # ═══════════════════════════════════════════════════════════════
    # hCaptcha 求解
    # ═══════════════════════════════════════════════════════════════
    async def _solve_hcaptcha(self) -> bool:
        """hCaptcha 求解：优先使用打码 API"""
        if not self.solver:
            logger.warning("No captcha solver API configured for hCaptcha")
            return False

        site_key = await self._get_site_key('h-captcha', 'data-sitekey')
        if not site_key:
            site_key = await self._get_site_key('g-recaptcha', 'data-sitekey')
        if not site_key:
            return False

        try:
            result = await asyncio.to_thread(
                self.solver.hcaptcha,
                sitekey=site_key,
                url=self.page.url,
            )
            token = result.get('code', '')
            if not token:
                return False

            await self.page.evaluate(f"""() => {{
                const el = document.querySelector('[name="h-captcha-response"]') ||
                           document.querySelector('textarea[name="g-recaptcha-response"]');
                if (el) el.value = '{token}';
                const cb = document.querySelector('[data-callback]');
                if (cb) {{
                    const cbName = cb.getAttribute('data-callback');
                    if (cbName && window[cbName]) window[cbName]('{token}');
                }}
            }}""")
            await asyncio.sleep(random.uniform(1, 3))
            logger.info("hCaptcha solved via API")
            return True
        except Exception as e:
            logger.error(f"hCaptcha solve error: {e}")
            return False

    # ═══════════════════════════════════════════════════════════════
    # Akamai Bot Manager 处理
    # ═══════════════════════════════════════════════════════════════
    async def _solve_akamai(self) -> bool:
        """Akamai 处理：行为模拟触发 _abck Cookie 更新"""
        # Akamai 依赖传感器数据，行为模拟有助于生成有效传感器
        await self._simulate_human_behavior(extended=True)

        # 等待 _abck Cookie 更新
        try:
            await asyncio.sleep(random.uniform(3, 5))

            # 检查 _abck Cookie 是否已更新为有效值
            cookies = await self.page.context.cookies()
            abck = next((c for c in cookies if c['name'] == '_abck'), None)
            if abck:
                # _abck 值中包含 "~-1~" 表示被拦截，其他值可能已通过
                if '~~-1~~' not in abck.get('value', '') and '~-1~' not in abck.get('value', ''):
                    logger.info("Akamai _abck cookie appears valid")
                    return True

            # 尝试重新发送请求
            await self.page.reload(wait_until="networkidle", timeout=20000)
            await asyncio.sleep(2)

            # 再次检查
            cookies = await self.page.context.cookies()
            abck = next((c for c in cookies if c['name'] == '_abck'), None)
            if abck and '~~-1~~' not in abck.get('value', ''):
                logger.info("Akamai passed after reload")
                return True
        except Exception as e:
            logger.debug(f"Akamai check error: {e}")

        return False

    # ═══════════════════════════════════════════════════════════════
    # PerimeterX 处理
    # ═══════════════════════════════════════════════════════════════
    async def _solve_perimeterx(self) -> bool:
        """PerimeterX 处理：行为模拟 + 等待通过"""
        await self._simulate_human_behavior(extended=True)

        # PerimeterX 有时显示 "Press & Hold" 按钮
        try:
            btn = self.page.locator('#px-captcha, div[role="button"]')
            if await btn.count() > 0:
                box = await btn.first.bounding_box()
                if box:
                    # 模拟按住按钮
                    x = box['x'] + box['width'] / 2
                    y = box['y'] + box['height'] / 2
                    await self.page.mouse.move(x, y)
                    await self.page.mouse.down()
                    await asyncio.sleep(random.uniform(3, 5))
                    await self.page.mouse.up()
                    await asyncio.sleep(2)
        except Exception:
            pass

        # 等待页面跳转或挑战消失
        try:
            await self.page.wait_for_function(
                "() => !document.querySelector('#px-captcha')",
                timeout=self.max_wait * 1000,
            )
            logger.info("PerimeterX challenge passed")
            return True
        except Exception:
            pass

        return False

    # ═══════════════════════════════════════════════════════════════
    # 人类行为模拟（核心辅助方法）
    # ═══════════════════════════════════════════════════════════════
    async def _simulate_human_behavior(self, extended=False):
        """模拟人类浏览行为以提升反爬自动通过率

        Args:
            extended: 是否进行更长时间的行为模拟（用于高难度挑战）
        """
        try:
            duration = random.uniform(3, 6) if extended else random.uniform(1.5, 3)
            start = time.monotonic()

            while time.monotonic() - start < duration:
                action = random.random()
                if action < 0.35:
                    # 鼠标随机移动
                    await self._random_mouse_move()
                elif action < 0.65:
                    # 滚动页面
                    await self._random_scroll()
                elif action < 0.85:
                    # 短暂停顿（阅读）
                    await asyncio.sleep(random.uniform(0.3, 1.0))
                else:
                    # 鼠标悬停在某个元素上
                    await self._hover_random_element()

                await asyncio.sleep(random.uniform(0.1, 0.4))
        except Exception as e:
            logger.debug(f"Behavior simulation error: {e}")

    async def _random_mouse_move(self):
        """随机鼠标移动"""
        try:
            viewport = self.page.viewport_size or {"width": 1280, "height": 720}
            x = random.randint(50, viewport["width"] - 50)
            y = random.randint(50, viewport["height"] - 50)
            # 分步移动
            steps = random.randint(5, 15)
            await self.page.mouse.move(x, y, steps=steps)
        except Exception:
            pass

    async def _random_scroll(self):
        """随机滚动"""
        try:
            delta = random.randint(50, 300) * random.choice([1, -1])
            await self.page.mouse.wheel(0, delta)
        except Exception:
            try:
                await self.page.evaluate(f"window.scrollBy(0, {random.randint(50, 300)})")
            except Exception:
                pass

    async def _hover_random_element(self):
        """悬停在随机元素上"""
        try:
            elements = await self.page.query_selector_all('a, button, div, p, span')
            if elements and len(elements) > 5:
                idx = random.randint(0, min(len(elements) - 1, 20))
                await elements[idx].hover()
        except Exception:
            pass

    async def _human_click_at(self, x: float, y: float):
        """人类式点击指定坐标"""
        try:
            # 先移动到附近（偏移）
            offset_x = random.uniform(-15, 15)
            offset_y = random.uniform(-15, 15)
            await self.page.mouse.move(x + offset_x, y + offset_y, steps=random.randint(10, 20))
            await asyncio.sleep(random.uniform(0.05, 0.2))
            # 精确移动到目标（有微小偏移）
            final_x = x + random.uniform(-3, 3)
            final_y = y + random.uniform(-3, 3)
            await self.page.mouse.move(final_x, final_y, steps=random.randint(3, 8))
            await asyncio.sleep(random.uniform(0.03, 0.1))
            # 点击
            await self.page.mouse.click(final_x, final_y, delay=random.randint(30, 100))
            await asyncio.sleep(random.uniform(0.1, 0.3))
        except Exception as e:
            logger.debug(f"Human click error: {e}")

    async def _get_site_key(self, element_selector: str, attr: str) -> Optional[str]:
        """获取验证码 site key"""
        try:
            return await self.page.evaluate(f"""() => {{
                const el = document.querySelector('[{attr}]') ||
                           document.querySelector('.{element_selector}');
                return el ? el.getAttribute('{attr}') : null;
            }}""")
        except Exception:
            return None


# ═══════════════════════════════════════════════════════════════
# 统一挑战求解器：整合标准 7 类 + 扩展 9 类 = 16 类全覆盖
# ═══════════════════════════════════════════════════════════════

class UnifiedChallengeSolver:
    """统一反爬挑战求解器

    整合 ChallengeSolver（7 类标准挑战）与 ExtendedChallengeSolver（9 类扩展验证），
    提供 16 类验证码/反爬挑战的统一检测与求解入口。

    检测优先级：标准挑战 -> 扩展验证
    求解流程：检测 -> 行为模拟 -> 求解 -> 验证结果

    支持的 16 类验证：
    标准（7类）：turnstile, datadome, recaptcha_v2, recaptcha_v3,
                 hcaptcha, akamai, perimeterx
    扩展（9类）：image_captcha, slider_captcha, click_captcha,
                 geetest, aws_waf, imperva, kasada, math_captcha,
                 otp_verification
    """

    def __init__(self, page, max_wait=30, two_captcha_key=None,
                 capsolver_key=None, anti_captcha_key=None):
        self.page = page
        self.max_wait = max_wait
        self.two_captcha_key = two_captcha_key
        self.capsolver_key = capsolver_key
        self.anti_captcha_key = anti_captcha_key

        # 标准求解器
        self.standard_solver = ChallengeSolver(
            page, max_wait, two_captcha_key,
            capsolver_key, anti_captcha_key
        )

        # 扩展求解器（延迟导入避免循环依赖）
        try:
            from .captcha_solver_extended import ExtendedChallengeSolver
            self.extended_solver = ExtendedChallengeSolver(
                page, max_wait, two_captcha_key,
                capsolver_key, anti_captcha_key
            )
        except (ImportError, ModuleNotFoundError):
            try:
                from captcha_solver_extended import ExtendedChallengeSolver
                self.extended_solver = ExtendedChallengeSolver(
                    page, max_wait, two_captcha_key,
                    capsolver_key, anti_captcha_key
                )
            except ImportError:
                self.extended_solver = None
                logger.warning("ExtendedChallengeSolver not available, "
                               "only 7 standard challenge types supported")

    async def detect(self, extra_html: str = "") -> Optional[str]:
        """统一检测：先检测标准挑战，再检测扩展验证

        Args:
            extra_html: [v2.11] 附加 HTML（iframe 聚合内容——挑战在子 frame 时关键词
                检测才能命中；solver_engine._do_solve 收集 page.frames 内容传入）

        Returns:
            挑战类型字符串，或 None（无挑战）
        """
        # 优先检测标准挑战（Turnstile/DataDome/reCAPTCHA 等）
        challenge = await self.standard_solver.detect()
        if challenge:
            return challenge

        # 再检测扩展验证（图形码/滑块/点选/极验等）
        if self.extended_solver:
            ext_challenge = await self.extended_solver.detect_extended(
                content_override=extra_html or None)
            if ext_challenge:
                return ext_challenge

        return None

    async def solve(self) -> bool:
        """统一求解：检测挑战类型并调用对应求解器

        Returns:
            True 如果求解成功
        """
        challenge_type = await self.detect()
        if challenge_type is None:
            return True  # 无挑战

        logger.info(f"Unified solver: detected challenge type={challenge_type}")

        # 标准挑战类型
        standard_types = {
            'turnstile', 'datadome', 'recaptcha_v2',
            'recaptcha_v3', 'hcaptcha', 'akamai', 'perimeterx'
        }

        # 扩展挑战类型
        extended_types = {
            'image_captcha', 'slider_captcha', 'click_captcha',
            'geetest', 'aws_waf', 'imperva', 'kasada',
            'math_captcha', 'otp_verification'
        }

        if challenge_type in standard_types:
            return await self.standard_solver.solve()
        elif challenge_type in extended_types and self.extended_solver:
            return await self.extended_solver.solve_extended(challenge_type)
        else:
            logger.warning(f"Unknown challenge type: {challenge_type}")
            return False

    async def detect_and_solve(self) -> Tuple[Optional[str], bool]:
        """检测并求解挑战，返回挑战类型和求解结果

        Returns:
            (challenge_type, success) 元组
        """
        challenge_type = await self.detect()
        if challenge_type is None:
            return None, True

        success = await self.solve()
        return challenge_type, success

    @staticmethod
    def get_all_supported_types() -> list:
        """返回所有支持的挑战类型列表"""
        return [
            # 标准 7 类
            ('turnstile', 'Cloudflare Turnstile', '行为模拟 + 点击 + 自动通过'),
            ('datadome', 'DataDome', '行为模拟 + Cookie 提取 + 重载'),
            ('recaptcha_v2', 'reCAPTCHA v2', '打码 API (2Captcha/CapSolver)'),
            ('recaptcha_v3', 'reCAPTCHA v3', '行为模拟 + 评分优化'),
            ('hcaptcha', 'hCaptcha', '打码 API + Token 注入'),
            ('akamai', 'Akamai Bot Manager', '_abck Cookie + 行为模拟'),
            ('perimeterx', 'PerimeterX/HUMAN', '按住模拟 + 等待通过'),
            # 扩展 9 类
            ('image_captcha', '图形文字验证码', 'API + ddddocr OCR'),
            ('slider_captcha', '滑块验证码', '人类轨迹拖动 (加速-减速-微调)'),
            ('click_captcha', '点选验证码', 'API 坐标识别 + 顺序点击'),
            ('geetest', '极验 GeeTest v3/v4', 'CapSolver API + 本地滑块'),
            ('aws_waf', 'AWS WAF Challenge', 'Cookie 生成 + 行为模拟'),
            ('imperva', 'Imperva/Incapsula', '传感器数据 + Cookie 更新'),
            ('kasada', 'Kasada Bot Detect', 'JS challenge + 重定向检测'),
            ('math_captcha', '数学运算验证码', '算式解析 + 自动计算'),
            ('otp_verification', '短信/邮箱 OTP', '识别上报 (需人工)'),
        ]
