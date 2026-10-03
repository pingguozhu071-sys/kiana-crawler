"""扩展验证码求解器（Extended CAPTCHA Solver）

在 challenge_solver.py 的 7 种主流反爬挑战之上，补全以下验证类型：

  8.  图形文字验证码 (Image Text CAPTCHA) — 传统图片文字识别
  9.  滑块验证码 (Slider CAPTCHA) — 拖动滑块到缺口位置
  10. 点选验证码 (Click CAPTCHA) — 按顺序点击指定文字/图标
  11. 极验 GeeTest v3/v4 — 滑块拼图 + 点选文字
  12. AWS WAF Challenge — Cookie 生成 + 行为模拟
  13. Imperva/Incapsula — 传感器数据 + Cookie 处理
  14. Kasada Bot Detect — 挑战脚本执行 + 行为模拟
  15. 数学运算验证码 (Math CAPTCHA) — 简单算式求解
  16. 短信/邮箱 OTP 检测 — 识别并上报（无法自动绕过）

所有求解方法均集成人类行为模拟，最大化自动通过率。
"""

import asyncio
import os
import logging
import random
import time
import re
import base64
import tempfile
from typing import Optional

# [FIXED & MODIFIED] v2.5.0 电商滑块视觉识别（OpenCV 缺口识别 + 贝塞尔轨迹）
from . import slider_vision

logger = logging.getLogger(__name__)


class ExtendedChallengeSolver:
    """扩展验证码求解器：补全 challenge_solver.py 未覆盖的验证类型

    可与 ChallengeSolver 组合使用，也可独立使用。
    所有方法均为 async，适配 Playwright/PatchRight 异步模型。
    """

    def __init__(self, page, max_wait=30, two_captcha_key=None,
                 capsolver_key=None, anti_captcha_key=None):
        self.page = page
        self.max_wait = max_wait
        self.two_captcha_key = two_captcha_key
        self.capsolver_key = capsolver_key
        self.anti_captcha_key = anti_captcha_key

        # 延迟初始化打码 API
        self._solver = None
        try:
            if two_captcha_key:
                from twocaptcha import TwoCaptcha
                self._solver = TwoCaptcha(two_captcha_key)
        except ImportError:
            pass

    # ═══════════════════════════════════════════════════════════════
    # 扩展检测入口
    # ═══════════════════════════════════════════════════════════════
    async def detect_extended(self, content_override=None) -> Optional[str]:
        """检测页面上的扩展验证类型（challenge_solver.detect 未覆盖的）

        Args:
            content_override: [v2.11] 覆盖检测内容（solver_engine 聚合 iframe HTML 传入，
                挑战在子 frame 时仅查主 page.content() 会漏检）

        Returns:
            验证类型字符串，或 None
        """
        try:
            content = content_override if content_override is not None else await self.page.content()
            lower = content.lower()
            url = self.page.url.lower()

            # 8. 图形文字验证码
            if any(kw in lower for kw in ['captchaimg', 'captcha_img', 'verifycode',
                                           'captcha.php', 'captcha.aspx', 'kaptcha',
                                           'gdvcode', 'checkcode', 'vcode']):
                return 'image_captcha'

            # 9. 滑块验证码（非极验）
            if any(kw in lower for kw in ['slider', 'nc_iconfont', 'nc_1_n1z',
                                           'slider_block', 'slide_block',
                                           'tcaptcha', 't_captcha']):
                if 'geetest' not in lower:
                    return 'slider_captcha'

            # 10. 点选验证码
            if any(kw in lower for kw in ['click_captcha', 'clickcaptcha',
                                           'wordclick', 'textclick',
                                           '点选', '点击验证']):
                return 'click_captcha'

            # 11. 极验 GeeTest v3/v4
            #
            # [v6 修复·P-A **真机实测，这是整个 GeeTest 问题的真正根因**]
            # 原来只匹配 HTML 里的**裸字符串** `'geetest'`。
            # 真机实测（25 页 B站抓取）：
            #     `检测到挑战 [geetest]` **17 次**
            #     而我加的诊断把页面真实类名打出来 —— **`[]`，一个 geetest 元素都没有**
            #     入口点击 0 次（没有元素可点），`slider not found` 17 次
            # ⇒ **检测是误报**：B站页面里必然含 `geetest` 这个词
            #   （预加载脚本 URL / 风控 JS 里的字样），但**页面上根本没有极验控件**。
            #   代价：白起 17 次浏览器 + 刷 17 行"找不到滑块"，把排查引向
            #   "选择器是不是不对"这条**错误的路**（我先后怀疑过 iframe、选择器，
            #   两轮都改了代码 —— 虽然那两处**确实也是真 bug**，但都不是主因）。
            #
            # **修法：要求"结构证据"，不接受"文本里出现过"**。
            # 强证据（页面真的在跑极验）：
            #   · 存在 geetest 的 DOM 元素；
            #   · 存在 `script[src*=geetest]` / `gt4.js` / `gt.js` 的**脚本标签**；
            #   · 存在全局 `window.initGeetest` / `window.geetest`。
            # 文本里的 `geetest` 字样**只作为弱信号保留**，必须上面任一条成立才算数。
            _gt_text = any(kw in lower for kw in ['geetest', 'gt.js', 'gt_challenge',
                                                  'geetest_4', 'g4_', 'captcha.geetest'])
            if _gt_text:
                # ⚠️ **只认 DOM 元素**。我第一版还把
                # `script[src*=geetest]` 与 `window.initGeetest` 也算强证据 ——
                # 真机复跑证明**它们太松**：B站**预加载**了极验 JS，
                # 于是检测数只从 17 降到 2，而那 2 次页面上仍然 `[]`（零元素）。
                # **脚本被加载 ≠ 有挑战在跑**，只有控件真的渲染出来才算。
                _probe = """() => {
                    if (document.querySelector('[class*="geetest"]')) return true;
                    return Boolean(window.geetest_obj || window.geetest_4_obj);
                }"""
                _strong = False
                try:
                    _strong = bool(await self.page.evaluate(_probe))
                    if not _strong:
                        # 有可能挑战正在渲染（脚本刚加载完）→ **等一次**再判，
                        # 避免把"马上就出现的真挑战"漏掉。
                        await asyncio.sleep(2.0)
                        _strong = bool(await self.page.evaluate(_probe))
                except Exception as e:
                    # 取不到 DOM 证据时**宁可放过**（不报 geetest）：
                    # 误报的代价是白起浏览器 + 把排查引偏，比漏报贵得多。
                    logger.debug(f"geetest 结构证据探测失败，按未检测处理: {e}")
                if _strong:
                    return 'geetest'
                logger.info("页面上出现过 geetest 字样但**没有极验控件**（DOM 探测为空，"
                            "等 2s 复判仍为空）—— 判定为误报，不当作挑战")

            # 12. AWS WAF
            if any(kw in lower for kw in ['awswaf', 'aws-waf', 'challenge.js',
                                           'token.awswaf', 'waf-challenge']):
                return 'aws_waf'

            # 13. Imperva/Incapsula
            if any(kw in lower for kw in ['incap_ses', 'visid_incap',
                                           'reese84', 'incapsula',
                                           'imperva', '_im_']):
                return 'imperva'

            # 14. Kasada
            if any(kw in lower for kw in ['kasada', 'kp.js', 'kd_bot',
                                           'ksd_heartbeat', 'px-cdn.net/kp']):
                return 'kasada'

            # 15. 数学运算验证码
            math_patterns = [
                r'(\d+)\s*[\+\-\×\*\÷\/x]\s*(\d+)\s*=\s*\?',
                r'(\d+)\s*[\+\-\×\*\÷\/x]\s*(\d+)\s*等于',
                r'what is\s*(\d+)\s*[\+\-\*\/]\s*(\d+)',
                r'(\d+)\s*plus\s*(\d+)',
                r'(\d+)\s*minus\s*(\d+)',
            ]
            for pattern in math_patterns:
                if re.search(pattern, content, re.IGNORECASE):
                    return 'math_captcha'

            # 16. 短信/邮箱 OTP
            # [FIXED & MODIFIED] v2.10.5 OTP 误报修复：正文/楼层/论坛页出现"短信验证"
            # 等字样不再判 OTP（贴吧帖子页正文含此类词 → 误判 → 放弃任务）。
            # 严格限定：URL 是登录/验证页（login/verify/passport/captcha/account），
            # 或页面含 <form> 且含 name="code"/"sms"/"phone" 输入框、或含 otp 帧/iframe。
            _is_otp_page = (
                any(k in url for k in ('login', 'verify', 'passport', 'captcha', 'account', 'sso', 'signin'))
                or ('otp' in url)
                or ('sms_code' in lower and ('name="sms' in lower or 'name="code' in lower or 'name="phone' in lower))
                or ('com_verify' in lower and 'verification_code' in lower)
            )
            if _is_otp_page and any(kw in lower for kw in ['短信验证', 'sms_code', 'phone_verify',
                                                           '手机验证', '邮箱验证', 'email_code',
                                                           'verification code', 'otp', 'send_code']):
                return 'otp_verification'

            # iframe 扩展检测
            iframes = await self.page.query_selector_all('iframe')
            for iframe in iframes:
                src = (await iframe.get_attribute('src') or '').lower()
                if 'geetest' in src:
                    return 'geetest'
                if 'awswaf' in src:
                    return 'aws_waf'
                if 'kasada' in src:
                    return 'kasada'
                if 'incapsula' in src or 'imperva' in src:
                    return 'imperva'

            return None
        except Exception as e:
            logger.debug(f"Extended challenge detection error: {e}")
            return None

    async def solve_extended(self, challenge_type: str) -> bool:
        """求解扩展验证类型

        Args:
            challenge_type: detect_extended 返回的类型字符串

        Returns:
            True 如果求解成功
        """
        logger.info(f"Solving extended challenge: {challenge_type}")

        if challenge_type == 'image_captcha':
            return await self._solve_image_captcha()
        elif challenge_type == 'slider_captcha':
            return await self._solve_slider_captcha()
        elif challenge_type == 'click_captcha':
            return await self._solve_click_captcha()
        elif challenge_type == 'geetest':
            return await self._solve_geetest()
        elif challenge_type == 'aws_waf':
            return await self._solve_aws_waf()
        elif challenge_type == 'imperva':
            return await self._solve_imperva()
        elif challenge_type == 'kasada':
            return await self._solve_kasada()
        elif challenge_type == 'math_captcha':
            return await self._solve_math_captcha()
        elif challenge_type == 'otp_verification':
            return await self._handle_otp_verification()
        return False

    # ═══════════════════════════════════════════════════════════════
    # 0. 跨 frame 定位（所有求解器共用）
    # ═══════════════════════════════════════════════════════════════
    async def _loc(self, selector: str):
        """在**主文档与所有子 frame** 里找元素；都没有就返回**必然匹配不到**的 locator。

        [v6 修复·P-A 根因] 原实现到处直接用 `self.page.locator(sel)` ——
        Playwright 的 `page.locator()` **不跨 iframe**。而 **GeeTest、京东 nlogin
        等挑战恰恰渲染在子 frame 里**。于是出现一个自相矛盾的组合：

          · **检测阶段**能看到挑战 —— `solver_engine` 把**所有 frame 的 HTML**
            聚合成 `_frames_html` 传进 `detect(extra_html=...)`；
          · **求解阶段**永远找不到滑块 —— 只搜主文档。

        真机日志里每次都是 `GeeTest slider not found`，根因就在这里：
        **检测看得见、求解够不着**。

        返回 `Locator` 而不是 `(locator, frame)`：坐标方面 Playwright 的
        `bounding_box()` 本来就给**视口相对**坐标，跨 frame 拖拽不受影响；
        返回"必然匹配不到"的空 locator 则让调用方**不必加 None 判断**
        （`await el.count()` 自然还是 0），改动面最小。
        """
        try:
            loc = self.page.locator(selector)
            if await loc.count() > 0:
                return loc
        except Exception:
            pass
        # 主文档没有 → 逐个 frame 找（挑战弹窗通常在子 frame 里）
        try:
            frames = list(self.page.frames)
        except Exception:
            frames = []
        for fr in frames:
            try:
                if fr is self.page.main_frame:
                    continue
                loc = fr.locator(selector)
                if await loc.count() > 0:
                    return loc
            except Exception:
                continue
        return self.page.locator("__kiana_no_such_element__")

    async def _find_first(self, selectors):
        """按顺序在**主文档与所有 frame** 里找第一个命中的选择器。

        返回 `(locator, selector)`；都没找到返回 `(None, "")`。
        给需要 `element_handle` / `bounding_box` 的调用方用。
        """
        for sel in selectors:
            loc = await self._loc(sel)
            try:
                if await loc.count() > 0:
                    return loc, sel
            except Exception:
                continue
        return None, ""

    # ═══════════════════════════════════════════════════════════════
    # 8. 图形文字验证码求解
    # ═══════════════════════════════════════════════════════════════
    async def _solve_image_captcha(self) -> bool:
        """图形文字验证码求解

        策略：
        1. 截取验证码图片
        2. 发送到打码 API 识别（支持 2Captcha / CapSolver / AntiCaptcha）
        3. 填入识别结果
        4. 如果无 API，尝试 OCR 本地识别（依赖 ddddocr）
        """
        # 策略 1：打码 API
        if self._solver:
            try:
                # 查找验证码图片
                img_selectors = [
                    'img#captcha', 'img.captcha', 'img#captchaImg',
                    'img#verifyCodeImg', 'img#imgCaptcha', 'img[src*="captcha"]',
                    'img[src*="verifycode"]', 'img[src*="kaptcha"]',
                    'img[src*="checkcode"]', 'img[src*="vcode"]',
                    'canvas#captcha', 'canvas.captcha',
                ]

                img_element = None
                for sel in img_selectors:
                    el = await self._loc(sel)   # [v6] 跨 frame
                    if await el.count() > 0:
                        img_element = await el.first.element_handle()
                        break

                if not img_element:
                    logger.warning("Image captcha element not found")
                    return False

                # 截取图片 base64
                screenshot = await img_element.screenshot()
                img_b64 = base64.b64encode(screenshot).decode('utf-8')

                # 发送到打码 API
                result = await asyncio.to_thread(
                    self._solver.normal,
                    img_b64,
                )
                code = result.get('code', '').strip()

                if not code:
                    logger.warning("Image captcha API returned empty")
                    return False

                # 填入输入框
                input_selectors = [
                    'input#captcha', 'input[name="captcha"]',
                    'input[name="verifyCode"]', 'input[name="code"]',
                    'input[name="vcode"]', 'input[name="checkcode"]',
                    'input.captcha', 'input.verifycode',
                ]
                for sel in input_selectors:
                    inp = await self._loc(sel)   # [v6] 跨 frame
                    if await inp.count() > 0:
                        await inp.first.click()
                        await asyncio.sleep(random.uniform(0.1, 0.3))
                        await inp.first.fill(code, delay=random.randint(50, 150))
                        logger.info(f"Image captcha solved: {code}")
                        await asyncio.sleep(random.uniform(0.3, 0.8))
                        return True

                logger.warning("Captcha input field not found")
                return False

            except Exception as e:
                logger.error(f"Image captcha API error: {e}")

        # 策略 2：本地 OCR（ddddocr）
        try:
            from ddddocr import DdddOcr
            ocr = DdddOcr(show_ad=False)

            img_selectors = [
                'img#captcha', 'img.captcha', 'img[src*="captcha"]',
                'img[src*="verifycode"]', 'img[src*="kaptcha"]',
                'canvas#captcha', 'canvas.captcha',
            ]
            for sel in img_selectors:
                el = await self._loc(sel)   # [v6] 跨 frame
                if await el.count() > 0:
                    img_element = await el.first.element_handle()
                    screenshot = await img_element.screenshot()
                    code = ocr.classification(screenshot)
                    code = code.strip()

                    if code:
                        # 填入结果
                        input_selectors = [
                            'input#captcha', 'input[name="captcha"]',
                            'input[name="verifyCode"]', 'input[name="code"]',
                            'input[name="vcode"]', 'input.verifycode',
                        ]
                        for isel in input_selectors:
                            inp = await self._loc(isel)   # [v6] 跨 frame
                            if await inp.count() > 0:
                                await inp.first.click()
                                await asyncio.sleep(random.uniform(0.1, 0.3))
                                await inp.first.fill(code, delay=random.randint(50, 150))
                                logger.info(f"Image captcha solved via OCR: {code}")
                                return True
                    break

        except ImportError:
            logger.debug("ddddocr not installed, skipping local OCR")
        except Exception as e:
            logger.debug(f"OCR error: {e}")

        logger.warning("Image captcha: no solver available (need API key or ddddocr)")
        return False

    # ═══════════════════════════════════════════════════════════════
    # 9. 滑块验证码求解
    # ═══════════════════════════════════════════════════════════════
    async def _slider_verify(self, lower: str) -> bool:
        """[FIXED & MODIFIED] v2.10.5 P1-6 滑块验证器级判定（替代 success 子串的假验证）

        过关信号（任一）：
        · 淘宝 nc 系列：nc_1_n1z 滑块容器不存在（被移除=成功）且无 nc_*_error
        · 极验：gt_geetest/geetest_4 容器不存在且无 gt_ajax 错误
        · 兜底（显式成功元素）：nc-lang-cnt-success / gt_refresh_ok
        """
        try:
            if ('nc_1_n1z' not in lower and ('nc_iconfont' not in lower)):
                return True  # 淘宝滑块容器已消失 = 已过关
            if 'nc_1_n1z_error' in lower or 'nc-error' in lower:
                return False
            if ('geetest' in lower and 'geetest_4' not in lower and 'gt_geetest' not in lower):
                return True  # 极验容器消失
            if ('gt_refresh_ok' in lower or 'nc-lang-cnt-success' in lower
                    or 'slider_success' in lower or 'slide_success' in lower):
                return True
        except Exception:
            pass
        return False

    async def _solve_slider_captcha(self) -> bool:
        """滑块验证码求解（v2.5.0 强化：OpenCV 缺口识别 + 贝塞尔轨迹 + 3 次重试）

        策略：
        1. 识别滑块和缺口位置（OpenCV 模板匹配/边缘检测 → CSS 缩放换算）
        2. 贝塞尔人类轨迹拖动（加速-匀速-减速-回退微调-随机抖动）
        3. 失败自动重试（换轨迹/微调距离，最多 3 次）
        """
        for attempt in range(1, 4):
            try:
                # 查找滑块元素
                slider_selectors = [
                    '.slider_block', '.slide_block', '#slider',
                    '#nc_1_n1z', '.btn_slide', '.sliderbtn',
                    'div[role="slider"]', '.slider-button',
                    '.nc_iconfont.btn_slide', '.JDJRV-slide-btn',
                    '.yidun_slider', '.JCap-slide-btn',
                ]
                slider = None
                _matched = ""
                # [v6] 同 GeeTest：改走 `_find_first`（跨 frame + 报出命中的选择器）
                _loc1, _matched = await self._find_first(slider_selectors)
                if _loc1 is not None:
                    slider = await _loc1.first.element_handle()

                if not slider:
                    logger.warning("Slider element not found")
                    return False

                box = await slider.bounding_box()
                if not box:
                    return False

                start_x = box['x'] + box['width'] / 2
                start_y = box['y'] + box['height'] / 2

                # 缺口距离（视觉识别优先）
                slide_distance = await self._detect_slider_distance()
                if not slide_distance:
                    slide_distance = random.randint(200, 280)

                # 贝塞尔人类轨迹（带抖动与回退微调）
                tracks = slider_vision.bezier_track(slide_distance)

                # 按下鼠标
                await self.page.mouse.move(start_x, start_y, steps=random.randint(5, 10))
                await asyncio.sleep(random.uniform(0.1, 0.3))
                await self.page.mouse.down()
                await asyncio.sleep(random.uniform(0.05, 0.15))

                # 沿贝塞尔轨迹移动
                for x, y, delay in tracks:
                    await self.page.mouse.move(start_x + x, start_y + y)
                    await asyncio.sleep(delay)

                # 释放鼠标
                await asyncio.sleep(random.uniform(0.05, 0.15))
                await self.page.mouse.up()

                # 等待验证结果
                await asyncio.sleep(random.uniform(1.2, 2))

                # 检查是否通过
                # [FIXED & MODIFIED] v2.10.5 P1-6 滑块真验证：原 'success' in content 子串
                # 检查太粗糙（页面任何 success/通过 都算过）→ 改用验证器级信号：
                #   · 淘宝 nc 系列：滑块容器/iframe 移除且无 nc_1_n1z 错误
                #   · 极验：gt_geetest / geetest_4 容器移除
                content = await self.page.content()
                lower = content.lower()
                v = await self._slider_verify(lower)
                if v:
                    logger.info(f"Slider captcha passed (attempt {attempt})")
                    return True

                logger.debug(f"Slider attempt {attempt} failed")
            except Exception as e:
                logger.debug(f"Slider attempt {attempt} error: {e}")
            await asyncio.sleep(random.uniform(0.8, 1.5))
        return False

    def _generate_human_track(self, distance: int) -> list:
        """生成人类拖动轨迹（加速-匀速-减速-微调）

        Args:
            distance: 需要拖动的总距离（像素）

        Returns:
            轨迹列表，每项包含 {x, y, delay}
        """
        tracks = []
        current = 0
        # 分三个阶段：加速 (40%) -> 匀速 (30%) -> 减速 (30%)
        accel_end = int(distance * 0.4)
        uniform_end = int(distance * 0.7)

        # 加速阶段：步长逐渐增大
        step = 1
        while current < accel_end:
            step += random.randint(1, 3)
            current += step
            y_jitter = random.uniform(-1, 1)
            delay = random.uniform(0.008, 0.025)
            tracks.append({'x': min(current, accel_end), 'y': y_jitter, 'delay': delay})

        # 匀速阶段
        while current < uniform_end:
            current += random.randint(3, 6)
            y_jitter = random.uniform(-1, 1)
            delay = random.uniform(0.01, 0.03)
            tracks.append({'x': min(current, uniform_end), 'y': y_jitter, 'delay': delay})

        # 减速阶段：步长逐渐减小
        step = random.randint(4, 6)
        while current < distance:
            step = max(1, step - random.randint(0, 2))
            current += step
            y_jitter = random.uniform(-0.5, 0.5)
            delay = random.uniform(0.02, 0.05)
            tracks.append({'x': min(current, distance), 'y': y_jitter, 'delay': delay})

        # 微调：超出一点再回来（模拟人类修正）
        overshoot = random.randint(2, 8)
        tracks.append({'x': distance + overshoot, 'y': 0, 'delay': random.uniform(0.03, 0.08)})
        tracks.append({'x': distance, 'y': 0, 'delay': random.uniform(0.02, 0.05)})

        # 最终停顿
        tracks.append({'x': distance, 'y': 0, 'delay': random.uniform(0.1, 0.3)})

        return tracks

    async def _detect_slider_distance(self) -> Optional[int]:
        """检测滑块需要拖动的距离

        策略（v2.5.0 升级）：
        1. OpenCV 视觉识别（优先）：背景图(+缺口图)下载 → 模板匹配/边缘检测 → CSS 缩放换算
        2. 页面 JS 变量 / DOM 缺口元素（降级）
        """
        # ═══ 方法 A：OpenCV 视觉识别 ═══
        if slider_vision.available():
            try:
                imgs = await self.page.evaluate("""() => {
                    const sels = {
                        bg: ['.JDJRV-bigimg', '.slider_bg', '.captcha-bg',
                             '.bg_img', '.yidun_bg-img', '.geetest_slice_bg',
                             '.JCap-slider-bg', '.nc-container .nc_bg',
                             '.captcha_background', '.slide_bg'],
                        gap: ['.JDJRV-smallimg', '.slider_gap', '.captcha-gap',
                              '.yidun_jigsaw', '.JCap-slider-gap',
                              '.nc-container .nc_scale', '.captcha_slider_img']
                    };
                    const srcOf = (el) => {
                        // 1) img 标签
                        if (el.src || el.currentSrc) return el.src || el.currentSrc;
                        // 2) canvas（toDataURL 提取渲染结果）
                        if (el.tagName === 'CANVAS') {
                            try { return el.toDataURL('image/png'); } catch(e) {}
                        }
                        // 3) CSS background-image
                        const bg = getComputedStyle(el).backgroundImage;
                        const m = bg && bg.match(/url\\(["']?([^"')]+)["']?\\)/);
                        if (m) return m[1];
                        return null;
                    };
                    const pick = (list) => {
                        for (const s of list) {
                            const el = document.querySelector(s);
                            if (el) {
                                const src = srcOf(el);
                                if (src) return src;
                            }
                        }
                        return null;
                    };
                    return {bg: pick(sels.bg), gap: pick(sels.gap)};
                }""")
                bg_url = imgs.get("bg")
                # [v2.14 阶段3] 验证码图片 SSRF 闸：私网/环回/非 http(s) 直接丢弃
                try:
                    from .url_utils import is_private_url as _is_priv
                    if bg_url and not bg_url.startswith("data:") and _is_priv(bg_url):
                        bg_url = None
                    _gap_raw = imgs.get("gap")
                    if _gap_raw and _is_priv(_gap_raw):
                        imgs["gap"] = None
                except Exception:
                    pass
                if bg_url and bg_url.startswith(("http", "data:")):
                    # [v2.17 安全深扫] 验证码图下载改 safe_urlopen（协议+私网+重定向逐跳校验）；
                    # 落盘用 Path.write_bytes（mkdtemp 临时目录+常量名，路径无外部输入）
                    from .url_utils import safe_urlopen
                    from pathlib import Path as _P2
                    tmp = tempfile.mkdtemp(prefix="kiana_slider_")
                    bg_path = os.path.join(tmp, "bg.png")
                    gap_path = os.path.join(tmp, "gap.png")
                    # 下载背景图（带浏览器 UA；data: URI 直接解码）
                    if bg_url.startswith("data:"):
                        _P2(bg_path).write_bytes(base64.b64decode(bg_url.split(",", 1)[1]))
                    else:
                        _r = safe_urlopen(bg_url, allowed_hosts=(),
                                          headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                                          timeout=10)
                        if _r is not None:
                            _P2(bg_path).write_bytes(_r.read())
                    gap_url = imgs.get("gap")
                    dist_px = None
                    if gap_url and gap_url.startswith("http"):
                        _r2 = safe_urlopen(gap_url, allowed_hosts=(),
                                           headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
                        if _r2 is not None:
                            _P2(gap_path).write_bytes(_r2.read())
                        dist_px = slider_vision.find_gap_by_template(bg_path, gap_path)
                    if dist_px is None:
                        dist_px = slider_vision.find_gap_by_edge(bg_path)

                    if dist_px:
                        # CSS 缩放换算：背景图显示宽度（轨道/容器）vs 图片原始宽度
                        disp = await self.page.evaluate("""() => {
                            const el = document.querySelector(
                                '.JDJRV-bigimg, .slider_bg, .captcha-bg, .bg_img, .yidun_bg-img, .slide_bg');
                            if (el) { const r = el.getBoundingClientRect(); return r.width; }
                            return 0;
                        }""")
                        import cv2 as _cv2
                        img_w = _cv2.imread(bg_path).shape[1] if bg_path else 0
                        scale = (disp / img_w) if (disp and img_w) else 1.0
                        # 滑块按钮通常位于轨道左端（宽度 ~40px），距离 = 缺口中心 - 按钮半宽
                        btn_w = await self.page.evaluate("""() => {
                            const el = document.querySelector(
                                '.JDJRV-slide-btn, .slider_block, .slide_block, .btn_slide, .yidun_slider');
                            if (el) { const r = el.getBoundingClientRect(); return r.width; }
                            return 40;
                        }""") or 40
                        dist = int(dist_px * scale - btn_w / 2)
                        logger.info(f"滑块视觉识别: 缺口px={dist_px} 缩放={scale:.2f} → 拖动距离={dist}px")
                        return max(dist, 30)
            except Exception as e:
                logger.debug(f"滑块视觉识别失败: {e}")

        # ═══ 方法 B：页面 JS 变量 / DOM（原逻辑） ═══
        try:
            # 尝试从常见 JS 变量获取
            distance = await self.page.evaluate("""() => {
                // 尝试从常见 JS 变量获取
                if (window.slide_distance) return window.slide_distance;
                if (window._slideDistance) return window._slideDistance;

                // 查找缺口元素
                const gap = document.querySelector('.slider_gap, .slide_gap, .captcha-gap, #gap');
                if (gap) {
                    const rect = gap.getBoundingClientRect();
                    return Math.round(rect.left + rect.width / 2);
                }

                // 查找背景图中缺口位置
                const bg = document.querySelector('.slider_bg, .slide_bg, .captcha-bg');
                const slider = document.querySelector('.slider_block, .slide_block');
                if (bg && slider) {
                    const bgRect = bg.getBoundingClientRect();
                    const sliderRect = slider.getBoundingClientRect();
                    // 缺口通常在背景图的右半部分
                    return Math.round(bgRect.width * (0.5 + Math.random() * 0.4));
                }

                return null;
            }""")
            if distance and isinstance(distance, (int, float)):
                return int(distance)
        except Exception:
            pass
        return None

    # ═══════════════════════════════════════════════════════════════
    # 10. 点选验证码求解
    # ═══════════════════════════════════════════════════════════════
    async def _solve_click_captcha(self) -> bool:
        """点选验证码求解

        策略：
        1. 截取验证码图片
        2. 发送到打码 API 获取点击坐标
        3. 按返回顺序依次点击
        """
        try:
            # 查找验证码图片容器
            container_selectors = [
                '.click_captcha', '.wordclick',
                '#clickCaptcha', '.captcha-click',
                '.text_click_captcha', '.clickcaptcha',
            ]
            container = None
            for sel in container_selectors:
                el = await self._loc(sel)   # [v6] 跨 frame
                if await el.count() > 0:
                    container = await el.first.element_handle()
                    break

            if not container:
                logger.warning("Click captcha container not found")
                return False

            # 策略 1：打码 API
            if self._solver:
                try:
                    screenshot = await container.screenshot()
                    img_b64 = base64.b64encode(screenshot).decode('utf-8')

                    # 使用坐标识别模式
                    result = await asyncio.to_thread(
                        self._solver.coordinates,
                        img_b64,
                        lang='en',
                    )
                    coordinates = result.get('coordinates', [])

                    if coordinates:
                        box = await container.bounding_box()
                        if box:
                            for coord in coordinates:
                                x = box['x'] + coord.get('x', 0)
                                y = box['y'] + coord.get('y', 0)
                                # 人类式点击
                                await self._human_click_at(x, y)
                                await asyncio.sleep(random.uniform(0.3, 0.8))

                            # 点击确认按钮
                            confirm = await self._loc('.confirm, .submit, button[type="submit"]')   # [v6] 跨 frame
                            if await confirm.count() > 0:
                                await asyncio.sleep(random.uniform(0.3, 0.6))
                                await confirm.first.click(delay=random.randint(50, 150))

                            logger.info("Click captcha solved via API")
                            return True
                except Exception as e:
                    logger.debug(f"Click captcha API error: {e}")

            # 策略 2：从提示文字中获取点击目标
            hint = await self.page.evaluate("""() => {
                const tip = document.querySelector('.captcha-tip, .click-tip, .tip-text');
                if (tip) return tip.textContent.trim();
                return null;
            }""")

            if hint:
                logger.info(f"Click captcha hint: {hint}")
                # 尝试在图片区域中按提示点击（启发式）
                # 这里需要 OCR 或 ML 识别文字位置，暂返回 False
                logger.warning("Click captcha requires API for text recognition")
                return False

            logger.warning("Click captcha: no solver available")
            return False

        except Exception as e:
            logger.error(f"Click captcha error: {e}")
            return False

    # ═══════════════════════════════════════════════════════════════
    # 11. 极验 GeeTest v3/v4 求解
    # ═══════════════════════════════════════════════════════════════
    async def _solve_geetest(self) -> bool:
        """GeeTest 极验求解

        策略：
        1. 提取 gt/challenge 参数
        2. 优先使用打码 API（CapSolver 支持 GeeTest）
        3. 回退到本地滑块轨迹模拟
        """
        try:
            # 提取 GeeTest 参数
            params = await self.page.evaluate("""() => {
                let gt = null, challenge = null;

                // 方法 1：从 geetest 对象获取
                if (window.geetest_data) {
                    gt = window.geetest_data.gt;
                    challenge = window.geetest_data.challenge;
                }

                // 方法 2：从 data 属性获取
                if (!gt) {
                    const el = document.querySelector('[data-gt]');
                    if (el) {
                        gt = el.getAttribute('data-gt');
                        challenge = el.getAttribute('data-challenge');
                    }
                }

                // 方法 3：从 script 标签获取
                if (!gt) {
                    const scripts = document.querySelectorAll('script');
                    for (const s of scripts) {
                        const text = s.textContent || '';
                        const gtMatch = text.match(/gt\\s*[:=]\\s*['"]([a-f0-9]+)['"]/);
                        const chMatch = text.match(/challenge\\s*[:=]\\s*['"]([a-f0-9]+)['"]/);
                        if (gtMatch) gt = gtMatch[1];
                        if (chMatch) challenge = chMatch[1];
                        if (gt) break;
                    }
                }

                return {gt, challenge};
            }""")

            gt = params.get('gt')
            challenge = params.get('challenge')

            # 策略 1：CapSolver API
            if self.capsolver_key and gt and challenge:
                try:
                    # [FIXED & MODIFIED] curl_cffi 替换 aiohttp（本机 aiohttp 外网全超时）
                    # [FIXED & MODIFIED] v2.14 timeout=20：curl_cffi 无默认超时，打码 API 挂起
                    # 则协程永久悬挂（深查报告 B 级发现）
                    from curl_cffi.requests import AsyncSession

                    async with AsyncSession(timeout=20) as session:
                        # 创建任务
                        payload = {
                            "clientKey": self.capsolver_key,
                            "task": {
                                "type": "GeeTestTaskProxyless",
                                "gt": gt,
                                "challenge": challenge,
                                "websiteURL": self.page.url,
                            }
                        }
                        resp = await session.post("https://api.capsolver.com/createTask", json=payload)
                        result = resp.json()
                        task_id = result.get("taskId")
                        if not task_id:
                            logger.warning("CapSolver GeeTest task creation failed")
                            return False

                        # 轮询结果
                        for _ in range(30):
                            await asyncio.sleep(3)
                            resp = await session.post("https://api.capsolver.com/getTaskResult",
                                                      json={
                                                          "clientKey": self.capsolver_key,
                                                          "taskId": task_id,
                                                      })
                            result = resp.json()
                            if result.get("status") == "ready":
                                solution = result.get("solution", {})
                                seccode = solution.get("seccode")
                                validate = solution.get("validate")

                                if seccode and validate:
                                        # 注入结果（使用正确的极验回调键名）
                                        await self.page.evaluate(f"""() => {{
                                            try {{
                                                if (window.geetest_obj) {{
                                                    geetest_obj.success({{
                                                        geetest_challenge: '{challenge}',
                                                        geetest_validate: '{validate}',
                                                        geetest_seccode: '{seccode}'
                                                    }});
                                                }}
                                            }} catch(e) {{}}
                                        }}""")
                                        logger.info("GeeTest solved via CapSolver")
                                        return True
                except Exception as e:
                    logger.debug(f"CapSolver GeeTest error: {e}")

            # 策略 2：本地滑块模拟
            logger.info("GeeTest: attempting local slider simulation")

            # ── [v6 修复·P-A **实测**] 两步都是拿 GeeTest 官方 demo
            #    （`geetest.com/en/demo`）抓真实 DOM 量出来的，不是猜的 ──────────
            #
            # ① **真实类名带实例 ID 后缀**：实测是 `geetest_box_btn_38052eff`。
            #    所以 `.geetest_slider_button` 这类**普通类选择器永远匹配不上**
            #    —— 真正的类名是 `geetest_slider_button_38052eff`，
            #    不是 `geetest_slider_button`。
            #    实测：引擎原来那 10 个选择器里 **8 个在真实 DOM 里命中数为 0**
            #    （slider_button / btn_slide / slider / item-wrap / slider_wrap /
            #      section / radar_tip / popup_wrap **全都不存在**）。
            #    → 必须用**属性前缀选择器** `[class*="geetest_xxx"]`。
            #
            # ② **GeeTest 不是一上来就有滑块**：实测初始 DOM 里
            #    `[class*="geetest_box_btn"]` / `[class*="geetest_btn_svg"]` /
            #    `[class*="geetest_holder"]` 各命中 1 个（那是「点击验证」入口），
            #    而所有滑块类名**命中 0** —— 滑块要**点开入口之后**才出现。
            #    **引擎从来没点过那个入口**，直接去找滑块 ⇒ 即便选择器全对也找不到。
            try:
                # [v6 实测·2026-10-03] 顺序按"实测到的真实按钮"排：
                # `geetest_btn_click` 是官方 demo 上**量出来的**「Click to verify」真按钮
                # （300x50，文案 'Click to verify'）；
                # 其余几个是在同一页面上量到的同层/同容器元素，作为兜底。
                # —— 我第一版只写了 box_btn/btn_svg/holder，**恰恰漏了真正那个**。
                for _sel in ('[class*="geetest_btn_click"]',
                             '[class*="geetest_box_btn"]',
                             '[class*="geetest_btn_svg"]',
                             '[class*="geetest_holder"]'):
                    _el = await self._loc(_sel)
                    try:
                        if await _el.count() > 0:
                            await _el.first.click(timeout=3000)
                            logger.info(f"GeeTest: 已点开验证入口（{_sel}）")
                            await asyncio.sleep(1.5)   # 等挑战弹窗渲染
                            break
                    except Exception:
                        continue
            except Exception as e:
                logger.debug(f"GeeTest 入口点击失败（继续找滑块）: {e}")

            # 等待 GeeTest 弹窗加载（[FIXED & MODIFIED] 轮询等待替代固定 1-2s sleep，
            # 弹窗渲染慢时原逻辑直接判定滑块不存在）
            # [v6] GeeTest 一律用**属性前缀**（容忍 `_xxxxxxxx` 实例后缀）；
            # 后面的 `.nc_*` / `.JDJRV-*` / `.yidun_*` 是**别家**的选择器
            # （阿里 / 京东 / 网易），对各自站点仍然有效，**原样保留** ——
            # 删了会让那些站点退化。
            slider_selectors = [
                # ── GeeTest：前缀匹配，容忍实例后缀 ──
                '[class*="geetest_slider_btn"]', '[class*="geetest_btn_slide"]',
                '[class*="geetest_slider"]', '[class*="geetest_box_btn"]',
                '[class*="geetest_btn_svg"]',
                # ── 别家（阿里 / 京东 / 网易 / 通用）—— 原样保留 ──
                '.slider_block', '.slide_block', '#slider',
                '#nc_1_n1z', '.btn_slide', '.sliderbtn',
                'div[role="slider"]', '.slider-button',
                '.nc_iconfont.btn_slide', '.JDJRV-slide-btn',
                '.yidun_slider', '.JCap-slide-btn',
            ]
            slider = None
            _matched = ""
            deadline = time.time() + 6
            while time.time() < deadline:
                # [v6] 改走 `_find_first` —— 它跨 frame，**并且会报出是哪个选择器命中的**
                # （排查价值：真机日志能直接看到"到底是哪条匹配上的"，
                #   而原来只知道"找到了/没找到"）。
                _loc2, _matched = await self._find_first(slider_selectors)
                if _loc2 is not None:
                    slider = await _loc2.first.element_handle()
                    break
                await asyncio.sleep(0.3)

            if not slider:
                # [v6 诊断] 找不到时**把页面上真实的 geetest 类名打出来** ——
                # 下次真机跑到这里，日志就能直接告诉我们"真实类名是什么"，
                # 不用再靠猜（我已经用官方 demo 量过一批，但**站点可能用不同版本**）。
                try:
                    _names = await self.page.evaluate(
                        """() => {
                            const s = new Set();
                            document.querySelectorAll('[class*="geetest"],[class*="yidun"],'
                                + '[class*="JDJRV"],[id*="nc_"]').forEach(e => {
                                (e.className || '').toString().split(/\\s+/).forEach(c => {
                                    if (c) s.add(c);
                                });
                            });
                            return Array.from(s).slice(0, 40);
                        }""")
                    logger.warning(f"GeeTest slider not found；页面上真实的挑战类名: {_names}")
                except Exception as _e:
                    logger.warning(f"GeeTest slider not found（类名探针也失败: {_e}）")
                return False

            box = await slider.bounding_box()
            if not box:
                return False

            start_x = box['x'] + box['width'] / 2
            start_y = box['y'] + box['height'] / 2

            # 生成拖动轨迹（GeeTest 通常需要拖 200-300px）
            distance = random.randint(180, 260)
            tracks = self._generate_human_track(distance)

            # 执行拖动
            await self.page.mouse.move(start_x, start_y, steps=random.randint(5, 10))
            await asyncio.sleep(random.uniform(0.2, 0.5))
            await self.page.mouse.down()

            for track in tracks:
                await self.page.mouse.move(start_x + track['x'], start_y + track.get('y', 0))
                await asyncio.sleep(track['delay'])

            await asyncio.sleep(random.uniform(0.05, 0.15))
            await self.page.mouse.up()

            # 等待结果
            await asyncio.sleep(random.uniform(2, 4))

            # 检查是否通过
            #
            # [v6 修复·假成功] 原来判据里有个**裸的 `'success'`**：
            #     any(kw in lower for kw in ['geetest_success', 'success', '验证成功'])
            # `success` 在真实页面里到处都是（任何 JS 的 success 回调、埋点字段、按钮文案）。
            # **实测：12 个真实站点样本里就有 2 个含它（17%）**，真实页面上只会更高
            # ⇒ 滑块**明明没拖动**也会记成 `GeeTest slider passed` —— **假成功**，
            # 正是本工程最忌讳的一类。
            #
            # 注：同文件 :500 附近**已有一处** v2.10.5 修过的同类问题（普通滑块），
            # 这里漏了；两处现在都用"结构/具体信号"判。
            #
            # 与 v6 修 GeeTest **检测**误报同一个修法：**要结构证据，不要"文本里出现过"**。
            content = await self.page.content()
            lower = content.lower()
            _specific = any(kw in lower for kw in
                            ['geetest_success', 'geetest-success',
                             '验证成功', '验证通过', '滑动验证成功'])
            _dom_ok = False
            if not _specific:
                try:
                    _dom_ok = bool(await self.page.evaluate(
                        """() => Boolean(document.querySelector(
                            '[class*="geetest_success"],[class*="geetest-success"],'
                            + '[class*="geetest_lock_success"],[class*="geetest_slide_success"]'))"""))
                except Exception:
                    pass
            if _specific or _dom_ok:
                logger.info("GeeTest slider passed（依据: "
                            + ("具体成功文本" if _specific else "DOM 成功态") + "）")
                return True

            logger.warning("GeeTest slider attempt failed")
            return False

        except Exception as e:
            logger.error(f"GeeTest error: {e}")
            return False

    # ═══════════════════════════════════════════════════════════════
    # 12. AWS WAF Challenge 求解
    # ═══════════════════════════════════════════════════════════════
    async def _solve_aws_waf(self) -> bool:
        """AWS WAF Challenge 求解

        AWS WAF 使用 JavaScript challenge 生成 Cookie。
        策略：等待 JS 执行完成 + 行为模拟 + Cookie 验证。
        """
        try:
            # AWS WAF 需要 JavaScript 执行环境，先等待
            await self._simulate_human_behavior()

            # 等待 WAF token 生成
            for attempt in range(5):
                await asyncio.sleep(random.uniform(2, 4))

                # 检查是否已生成 WAF Cookie
                cookies = await self.page.context.cookies()
                waf_cookie = None
                for c in cookies:
                    if 'aws-waf' in c['name'].lower() or 'awswaf' in c['name'].lower():
                        waf_cookie = c
                        break

                if waf_cookie and waf_cookie.get('value'):
                    logger.info("AWS WAF token cookie generated")
                    # 尝试刷新页面
                    await self.page.reload(wait_until="domcontentloaded")
                    await asyncio.sleep(random.uniform(1, 2))

                    # 检查是否通过
                    content = await self.page.content()
                    if 'awswaf' not in content.lower() and 'challenge' not in content.lower():
                        logger.info("AWS WAF passed")
                        return True

                # 尝试点击可能的验证按钮
                btn = await self._loc('button, input[type="button"], a.btn')   # [v6] 跨 frame
                if await btn.count() > 0:
                    await btn.first.click(delay=random.randint(100, 300))
                    await asyncio.sleep(2)

            logger.warning("AWS WAF challenge not passed")
            return False

        except Exception as e:
            logger.error(f"AWS WAF error: {e}")
            return False

    # ═══════════════════════════════════════════════════════════════
    # 13. Imperva/Incapsula 求解
    # ═══════════════════════════════════════════════════════════════
    async def _solve_imperva(self) -> bool:
        """Imperva/Incapsula 求解

        Imperva 使用 Reese84 传感器数据。
        策略：行为模拟生成传感器 + 等待 Cookie 更新。
        """
        try:
            # Imperva 依赖行为传感器数据
            await self._simulate_human_behavior(extended=True)

            # 等待 Imperva JS 执行
            await asyncio.sleep(random.uniform(3, 5))

            # 检查 Cookie
            for attempt in range(4):
                cookies = await self.page.context.cookies()
                has_reese84 = any('reese84' in c['name'].lower() for c in cookies)
                has_incap_ses = any('incap_ses' in c['name'].lower() for c in cookies)

                if has_reese84 or has_incap_ses:
                    # Cookie 存在，尝试刷新
                    await self.page.reload(wait_until="domcontentloaded")
                    await asyncio.sleep(random.uniform(2, 3))

                    content = await self.page.content()
                    lower = content.lower()
                    if 'incapsula' not in lower and 'imperva' not in lower:
                        logger.info("Imperva passed")
                        return True

                await asyncio.sleep(random.uniform(2, 4))
                await self._simulate_human_behavior()

            logger.warning("Imperva challenge not passed")
            return False

        except Exception as e:
            logger.error(f"Imperva error: {e}")
            return False

    # ═══════════════════════════════════════════════════════════════
    # 14. Kasada Bot Detect 求解
    # ═══════════════════════════════════════════════════════════════
    async def _solve_kasada(self) -> bool:
        """Kasada 求解

        Kasada 使用复杂的 JS challenge + 指纹检测。
        策略：等待 challenge 完成 + 行为模拟 + Cookie 检查。
        """
        try:
            await self._simulate_human_behavior(extended=True)

            # Kasada challenge 通常需要较长时间
            for attempt in range(6):
                await asyncio.sleep(random.uniform(3, 5))

                # 检查是否通过（页面不再包含 challenge）
                content = await self.page.content()
                lower = content.lower()

                if 'kasada' not in lower and 'kp.js' not in lower:
                    # 检查 Cookie
                    cookies = await self.page.context.cookies()
                    has_kp = any('kp' in c['name'].lower() or 'kasada' in c['name'].lower() for c in cookies)
                    if has_kp:
                        logger.info("Kasada passed")
                        return True

                # 尝试等待自动跳转
                try:
                    await self.page.wait_for_url(
                        lambda url: 'challenge' not in url.lower(),
                        timeout=5000,
                    )
                    logger.info("Kasada passed via redirect")
                    return True
                except Exception:
                    pass

            logger.warning("Kasada challenge not passed")
            return False

        except Exception as e:
            logger.error(f"Kasada error: {e}")
            return False

    # ═══════════════════════════════════════════════════════════════
    # 15. 数学运算验证码求解
    # ═══════════════════════════════════════════════════════════════
    async def _solve_math_captcha(self) -> bool:
        """数学运算验证码求解

        解析页面中的算式并填入答案。
        支持 +, -, *, /, ×, ÷
        """
        try:
            # 从页面提取算式
            math_expr = await self.page.evaluate("""() => {
                // 查找包含算式的元素
                const candidates = document.querySelectorAll(
                    'label, span, div, p, .captcha-text, .question'
                );
                for (const el of candidates) {
                    const text = el.textContent.trim();
                    // 匹配 "3 + 5 = ?" 或 "3 × 5 = ?" 等
                    const match = text.match(/(\\d+)\\s*([\\+\\-\\×\\*\\÷\\/x])\\s*(\\d+)/);
                    if (match) {
                        return {a: match[1], op: match[2], b: match[3], raw: text};
                    }
                }
                return null;
            }""")

            if not math_expr:
                logger.warning("Math captcha expression not found")
                return False

            a = int(math_expr['a'])
            b = int(math_expr['b'])
            op = math_expr['op']

            # 计算结果
            if op in ('+', '＋'):
                result = a + b
            elif op in ('-', '－'):
                result = a - b
            elif op in ('*', '×', 'x', '✕'):
                result = a * b
            elif op in ('/', '÷'):
                result = a // b if b != 0 else 0
            else:
                logger.warning(f"Unknown operator: {op}")
                return False

            logger.info(f"Math captcha: {a} {op} {b} = {result}")

            # 查找输入框并填入
            input_selectors = [
                'input[name="captcha"]', 'input[name="answer"]',
                'input[name="result"]', 'input[name="code"]',
                'input#captcha', 'input#answer',
                'input.math-input', 'input.captcha',
            ]
            for sel in input_selectors:
                inp = await self._loc(sel)   # [v6] 跨 frame
                if await inp.count() > 0:
                    await inp.first.click()
                    await asyncio.sleep(random.uniform(0.1, 0.3))
                    await inp.first.fill(str(result), delay=random.randint(50, 150))
                    await asyncio.sleep(random.uniform(0.3, 0.6))
                    logger.info(f"Math captcha solved: {result}")
                    return True

            logger.warning("Math captcha input field not found")
            return False

        except Exception as e:
            logger.error(f"Math captcha error: {e}")
            return False

    # ═══════════════════════════════════════════════════════════════
    # 16. 短信/邮箱 OTP 验证处理
    # ═══════════════════════════════════════════════════════════════
    async def _handle_otp_verification(self) -> bool:
        """短信/邮箱 OTP 验证处理

        OTP 无法自动绕过（需要人工接收验证码）。
        此方法仅做识别和上报，不尝试绕过。
        """
        try:
            # 识别 OTP 类型
            content = await self.page.content()
            lower = content.lower()

            otp_type = 'unknown'
            if any(kw in lower for kw in ['短信', 'sms', 'phone', '手机']):
                otp_type = 'sms'
            elif any(kw in lower for kw in ['邮箱', 'email', 'mail']):
                otp_type = 'email'

            logger.warning(f"OTP verification detected (type={otp_type}) — "
                          "requires manual code input, cannot auto-solve")

            # 上报到全局状态（供上层决策）
            return False  # 明确返回 False 表示无法绕过

        except Exception as e:
            logger.error(f"OTP detection error: {e}")
            return False

    # ═══════════════════════════════════════════════════════════════
    # 辅助方法（与 ChallengeSolver 共享逻辑）
    # ═══════════════════════════════════════════════════════════════
    async def _simulate_human_behavior(self, extended=False):
        """模拟人类浏览行为"""
        try:
            duration = random.uniform(3, 6) if extended else random.uniform(1.5, 3)
            start = time.monotonic()

            while time.monotonic() - start < duration:
                action = random.random()
                if action < 0.35:
                    await self._random_mouse_move()
                elif action < 0.65:
                    await self._random_scroll()
                elif action < 0.85:
                    await asyncio.sleep(random.uniform(0.3, 1.0))
                else:
                    await self._hover_random_element()
                await asyncio.sleep(random.uniform(0.1, 0.4))
        except Exception as e:
            logger.debug(f"Behavior simulation error: {e}")

    async def _random_mouse_move(self):
        try:
            viewport = self.page.viewport_size or {"width": 1280, "height": 720}
            x = random.randint(50, viewport["width"] - 50)
            y = random.randint(50, viewport["height"] - 50)
            await self.page.mouse.move(x, y, steps=random.randint(5, 15))
        except Exception:
            pass

    async def _random_scroll(self):
        try:
            delta = random.randint(50, 300) * random.choice([1, -1])
            await self.page.mouse.wheel(0, delta)
        except Exception:
            pass

    async def _hover_random_element(self):
        try:
            elements = await self.page.query_selector_all('a, button, div, p, span')
            if elements and len(elements) > 5:
                idx = random.randint(0, min(len(elements) - 1, 20))
                await elements[idx].hover()
        except Exception:
            pass

    async def _human_click_at(self, x: float, y: float):
        """人类式点击"""
        try:
            offset_x = random.uniform(-15, 15)
            offset_y = random.uniform(-15, 15)
            await self.page.mouse.move(x + offset_x, y + offset_y, steps=random.randint(10, 20))
            await asyncio.sleep(random.uniform(0.05, 0.2))
            final_x = x + random.uniform(-3, 3)
            final_y = y + random.uniform(-3, 3)
            await self.page.mouse.move(final_x, final_y, steps=random.randint(3, 8))
            await asyncio.sleep(random.uniform(0.03, 0.1))
            await self.page.mouse.click(final_x, final_y, delay=random.randint(30, 100))
            await asyncio.sleep(random.uniform(0.1, 0.3))
        except Exception as e:
            logger.debug(f"Human click error: {e}")


def get_extended_challenge_types() -> list:
    """返回所有扩展验证类型的列表"""
    return [
        ("image_captcha", "图形文字验证码", "传统图片文字识别 (API + ddddocr OCR)"),
        ("slider_captcha", "滑块验证码", "人类轨迹拖动 (加速-匀速-减速-微调)"),
        ("click_captcha", "点选验证码", "按顺序点击指定文字/图标 (API 坐标识别)"),
        ("geetest", "极验 GeeTest v3/v4", "CapSolver API + 本地滑块模拟"),
        ("aws_waf", "AWS WAF Challenge", "Cookie 生成 + 行为模拟 + 重载验证"),
        ("imperva", "Imperva/Incapsula", "传感器数据 + Cookie 更新等待"),
        ("kasada", "Kasada Bot Detect", "JS challenge 等待 + 重定向检测"),
        ("math_captcha", "数学运算验证码", "算式解析 + 自动计算填入"),
        ("otp_verification", "短信/邮箱 OTP", "识别上报 (无法自动绕过)"),
    ]
