"""
trajectory_engine.py - 人类行为轨迹模拟引擎

为 Python asyncio 爬虫提供高拟真度的人类交互模拟，包括：
  - 基于最小急动度模型与费茨定律的鼠标移动
  - 多步骤点击（偏移定位 + 点击前停顿 + 中心偏移点击 + 点击后停顿）
  - 动量衰减式滚动（含间歇暂停与过冲回弹）
  - 带拼写错误与退格修正的打字模拟
  - 随机空闲行为（微移动 / 滚动 / 暂停 / 抖动）
  - 视口边界感知的坐标裁剪

兼容 playwright / patchright 的 Page 对象。
"""

import random
import asyncio
import math
import time
from typing import List, Tuple, Optional

# ============================================================
# 常量定义
# ============================================================

# 视口回退默认值（当无法从 Page 获取视口尺寸时使用）
_DEFAULT_VIEWPORT_WIDTH = 1920
_DEFAULT_VIEWPORT_HEIGHT = 1080

# 打字相关常量
_KEY_DELAY_MIN = 0.10          # 按键间最小延迟（秒）
_KEY_DELAY_MAX = 0.30          # 按键间最大延迟（秒）
_TYPE_ERROR_RATE = 0.03        # 单字符拼写错误概率
_THINKING_PAUSE_RATE = 0.04    # 打字中途思考停顿概率

# 费茨定律经验常数: MT = a + b * log2(2D / W)
_FITTS_INTERCEPT = 0.10        # 基础时间 a（秒）
_FITTS_SLOPE = 0.15            # 斜率 b
_FITTS_DEFAULT_TARGET_W = 15.0  # 默认目标宽度（像素）

# QWERTY 键盘邻键映射，用于模拟打字时按错相邻按键
_KEYBOARD_NEIGHBORS = {
    'a': 'qwsz',   'b': 'vghn',   'c': 'xdfv',   'd': 'serfcx',
    'e': 'wsdr',   'f': 'drtgvc', 'g': 'ftyhbv', 'h': 'gyujnb',
    'i': 'ujko',   'j': 'huikmn', 'k': 'jiolm',  'l': 'kop',
    'm': 'njk',    'n': 'bhjm',   'o': 'iklp',   'p': 'ol',
    'q': 'wa',     'r': 'edft',   's': 'awedxz', 't': 'rfgy',
    'u': 'yhji',   'v': 'cfgb',   'w': 'qase',   'x': 'zsdc',
    'y': 'tghu',   'z': 'asx',
}


# ============================================================
# 内部辅助函数
# ============================================================

def _minimum_jerk_position(tau: float) -> float:
    """计算最小急动度模型在归一化时间 tau∈[0,1] 处的位置占比。

    位置公式: s(tau) = 10*tau^3 - 15*tau^4 + 6*tau^5
    该函数在 tau=0 时返回 0（起点），tau=1 时返回 1（终点），
    速度在起止处为零、中段最大，天然产生加速-匀速-减速效果。
    """
    return 10.0 * tau ** 3 - 15.0 * tau ** 4 + 6.0 * tau ** 5


def _minimum_jerk_velocity(tau: float) -> float:
    """计算最小急动度模型在归一化时间 tau∈[0,1] 处的归一化速度。

    速度公式: v(tau) = 30*tau^2 - 60*tau^3 + 30*tau^4
    最大值出现在 tau=0.5，约为 1.875；起止处为零。
    返回值已归一化至 [0, 1] 区间。
    """
    raw = 30.0 * tau ** 2 - 60.0 * tau ** 3 + 30.0 * tau ** 4
    return raw / 1.875  # 归一化至 [0, 1]


def _fitts_steps(distance: float, target_width: float = _FITTS_DEFAULT_TARGET_W) -> int:
    """根据费茨定律计算鼠标移动轨迹步数。

    难度指数 ID = log2(2D / W)，距离越远、目标越小则步数越多，
    对应更长的移动时间，符合人类运动控制规律。
    """
    if distance <= 0:
        return 8
    w = max(target_width, 1.0)
    difficulty = math.log2(2.0 * distance / w)
    steps = int(12 + difficulty * 2.5)
    return max(10, min(steps, 50))


def lognormal_delay(base: float, sigma: float = 0.55, min_v: float = 0.02) -> float:
    """[v2.13] 对数正态分布延迟（人类反应/操作间隔的真实分布——右偏长尾：
    多数偏快、偶尔明显拖长；uniform 的对称分布在统计检测下一眼假）。
    base 为中位数近似，sigma 控制离散度。"""
    try:
        v = random.lognormvariate(math.log(max(base, 1e-3)), sigma)
        return max(min_v, v)
    except Exception:
        return base


_TREMOR_PHASE = random.uniform(0, 6.283)


_TREMOR_PHASE = random.uniform(0, 6.283)
_TREMOR_FREQ = random.uniform(9.0, 11.0)  # 当前震颤频率（连续随机游走，保持波形连续）


def _physiological_tremor(x: float, y: float, amplitude: float = 1.0):
    """[v2.13] 8-12Hz 生理性震颤叠加（人类肌肉无法绝对静止——手部微颤
    集中在 8-12Hz 波段，即"生理性震颤"；uniform 抖动不具此频谱特征）。
    频率连续随机游走（真实生理频率也是缓慢漂移）、相位由时间连续驱动。
    返回 (x', y')。"""
    global _TREMOR_FREQ
    _TREMOR_FREQ = min(12.0, max(8.0, _TREMOR_FREQ + random.uniform(-0.05, 0.05)))
    t = time.monotonic()
    # [坑] Windows CRT 的 sin 对大参数（>1e4 rad）参数归约精度差 → 输出近随机。
    # 对 100s 取模把相位参数压在 <2π×12×100≈7540 rad 内（可接受），100s 一循环。
    tt = t % 100.0
    dx = amplitude * math.sin(2 * math.pi * _TREMOR_FREQ * tt + _TREMOR_PHASE)
    dy = amplitude * 0.6 * math.sin(2 * math.pi * _TREMOR_FREQ * 1.13 * tt + _TREMOR_PHASE * 1.4)
    return x + dx, y + dy


def _get_neighbor_key(char: str) -> Optional[str]:
    """从 QWERTY 键盘布局中获取指定字符的相邻按键，用于模拟打字错误。"""
    neighbors = _KEYBOARD_NEIGHBORS.get(char.lower(), '')
    if not neighbors:
        return None
    return random.choice(neighbors)


def _clamp(value: float, lo: float, hi: float) -> float:
    """将数值限制在 [lo, hi] 区间内。"""
    return max(lo, min(value, hi))


def _clamp_to_viewport(x: float, y: float, vw: int, vh: int, margin: int = 2) -> Tuple[float, float]:
    """将坐标裁剪至视口边界内，留出 margin 像素的安全边距。"""
    x = _clamp(x, margin, vw - margin)
    y = _clamp(y, margin, vh - margin)
    return x, y


async def _get_viewport(page) -> Tuple[int, int]:
    """从 Page 对象获取视口尺寸 (width, height)。

    优先使用 page.viewport_size 属性；若不可用则通过 JS 读取
    window.innerWidth/innerHeight；最终回退到模块默认值。
    """
    # 尝试从 Playwright 属性获取
    try:
        size = page.viewport_size
        if size and size.get('width') and size.get('height'):
            return size['width'], size['height']
    except Exception:
        pass
    # 尝试通过 JS 获取
    try:
        result = await page.evaluate(
            "() => ({w: window.innerWidth, h: window.innerHeight})")
        if result and result.get('w') and result.get('h'):
            return result['w'], result['h']
    except Exception:
        pass
    return _DEFAULT_VIEWPORT_WIDTH, _DEFAULT_VIEWPORT_HEIGHT


def _text_complexity(text: str) -> float:
    """评估文本复杂度，返回 [0, 1] 区间的评分。

    包含大写字母、数字、特殊符号越多则复杂度越高，
    复杂文本的打字速度会相应变慢。
    """
    if not text:
        return 0.0
    special = sum(1 for c in text if not c.isalnum() and c != ' ')
    upper = sum(1 for c in text if c.isupper())
    digits = sum(1 for c in text if c.isdigit())
    score = (special * 1.5 + upper * 0.8 + digits * 0.5) / len(text)
    return min(score, 1.0)


async def _get_current_mouse_pos(page) -> Tuple[float, float]:
    """从页面全局变量中读取当前鼠标位置，失败时返回随机位置。"""
    try:
        prev = await page.evaluate(
            "() => window._kiana_mouse_pos || {x: 100, y: 100}")
        return float(prev['x']), float(prev['y'])
    except Exception:
        return float(random.randint(50, 400)), float(random.randint(50, 400))


async def _safe_wheel(page, delta_y: int) -> None:
    """安全执行鼠标滚轮事件，若 wheel API 不可用则回退到 JS scrollBy。"""
    try:
        await page.mouse.wheel(0, delta_y)
    except Exception:
        try:
            await page.evaluate(f"window.scrollBy(0, {delta_y})")
        except Exception:
            pass


# ============================================================
# 公共 API
# ============================================================

def generate_human_mouse_path(
    start_x: float, start_y: float,
    end_x: float, end_y: float,
    steps: int = 20
) -> List[Tuple[float, float]]:
    """生成基于最小急动度模型的鼠标移动路径。

    使用 minimum-jerk 轨迹模型替代简单贝塞尔曲线，实现：
      - 起止处速度为零（平滑启停）
      - 中段速度最大（加速-减速曲线）
      - 垂直方向的弧线偏移（人类移动并非直线）
      - 概率性过冲后修正回弹
      - 全程微噪声抖动

    参数:
        start_x, start_y: 起点坐标
        end_x, end_y:     终点坐标
        steps:            采样步数（越大越平滑）

    返回:
        [(x, y), ...] 轨迹点列表
    """
    points: List[Tuple[float, float]] = []
    dx = end_x - start_x
    dy = end_y - start_y
    distance = math.hypot(dx, dy)

    # 距离极小时仅添加微小抖动
    if distance < 1.0:
        for _ in range(steps + 1):
            points.append((
                end_x + random.gauss(0, 0.5),
                end_y + random.gauss(0, 0.5),
            ))
        return points

    # 垂直方向单位向量，用于生成弧线偏移
    perp_x = -dy / distance
    perp_y = dx / distance
    curve_amplitude = min(distance * 0.12, 40.0) * random.choice([-1.0, 1.0])

    # 概率性过冲：将实际终点延伸一小段距离
    use_overshoot = random.random() < 0.55
    if use_overshoot:
        overshoot_dist = min(distance * random.uniform(0.02, 0.08), 15.0)
        over_end_x = end_x + (dx / distance) * overshoot_dist
        over_end_y = end_y + (dy / distance) * overshoot_dist
    else:
        overshoot_dist = 0.0
        over_end_x, over_end_y = end_x, end_y

    # 第一阶段：从起点到（过冲）终点的最小急动度轨迹
    phase1_steps = max(steps - 4, 10) if use_overshoot else steps
    for i in range(phase1_steps + 1):
        tau = i / phase1_steps
        s = _minimum_jerk_position(tau)
        x = start_x + (over_end_x - start_x) * s
        y = start_y + (over_end_y - start_y) * s
        # 弧线偏移：正弦曲线，起止为零、中段最大
        curve_offset = curve_amplitude * math.sin(math.pi * tau)
        x += perp_x * curve_offset
        y += perp_y * curve_offset
        # 微噪声
        x += random.gauss(0, 0.6)
        y += random.gauss(0, 0.6)
        points.append((x, y))

    # 第二阶段：从过冲点修正回真实终点
    if use_overshoot and overshoot_dist > 0:
        correction_steps = 4
        for i in range(1, correction_steps + 1):
            tau = i / correction_steps
            s = _minimum_jerk_position(tau)
            x = over_end_x + (end_x - over_end_x) * s
            y = over_end_y + (end_y - over_end_y) * s
            x += random.gauss(0, 0.4)
            y += random.gauss(0, 0.4)
            points.append((x, y))

    return points


async def perform_human_mouse_move(page, to_x: float, to_y: float) -> None:
    """模拟人类鼠标移动（基于最小急动度模型与费茨定律）。

    实现要点：
      - 视口边界感知：目标坐标裁剪至页面可见区域内
      - 费茨定律：根据移动距离动态计算采样步数
      - 最小急动度：位置分布天然产生加速(中段)-减速(近终点)效果
      - 速度自适应延迟：近终点处延迟加长，进一步模拟减速瞄准
      - 概率性微停顿：模拟人类移动中的犹豫
      - CPU 卸载：轨迹计算通过 asyncio.to_thread 放入线程池

    参数:
        page:  playwright/patchright Page 对象
        to_x, to_y: 目标坐标
    """
    vw, vh = await _get_viewport(page)
    to_x, to_y = _clamp_to_viewport(to_x, to_y, vw, vh)

    start_x, start_y = await _get_current_mouse_pos(page)
    distance = math.hypot(to_x - start_x, to_y - start_y)
    steps = _fitts_steps(distance)

    # CPU 卸载：轨迹生成放到线程池，避免阻塞事件循环
    points = await asyncio.to_thread(
        generate_human_mouse_path, start_x, start_y, to_x, to_y, steps)

    n = len(points)
    for idx, (x, y) in enumerate(points):
        mx, my = x, y
        # [v2.13] 轨迹末端（速度衰减段）叠加 8-12Hz 生理震颤
        if idx >= n - 4:
            mx, my = _physiological_tremor(x, y, amplitude=1.0)
        await page.mouse.move(mx, my)
        # 基于最小急动度速度曲线的自适应延迟：
        # 速度低（近起止点）→ 延迟长；速度高（中段）→ 延迟短
        tau = idx / max(n - 1, 1)
        norm_vel = _minimum_jerk_velocity(tau)
        delay = 0.006 + (1.0 - norm_vel) * 0.012
        delay += random.uniform(-0.001, 0.003)
        delay = max(0.004, delay)
        # 概率性微停顿（模拟犹豫或瞄准调整）——[v2.13] 对数正态分布
        if random.random() < 0.07:
            delay += lognormal_delay(0.07, sigma=0.5, min_v=0.03)
        await asyncio.sleep(delay)

    # 确保最终落点精确（带一次微颤修正——人类不会两次落点完全一致）
    fx, fy = _physiological_tremor(to_x, to_y, amplitude=0.6)
    await page.mouse.move(fx, fy)
    await page.evaluate(
        f"() => window._kiana_mouse_pos = {{x: {to_x}, y: {to_y}}}")


async def perform_human_click(
    page,
    to_x: float,
    to_y: float,
    button: str = 'left'
) -> None:
    """模拟人类多步骤点击行为。

    点击流程：
      1. 先将鼠标移动至目标附近（带随机偏移，非精确到达）
      2. 点击前短暂随机停顿（瞄准确认）
      3. 微调至点击位置（偏离元素中心，非正中点击）
      4. 按下-保持-释放（模拟真实按压时长）
      5. 点击后短暂停顿（观察反馈）

    参数:
        page:  playwright/patchright Page 对象
        to_x, to_y: 目标坐标
        button: 鼠标按键 ('left' / 'right' / 'middle')
    """
    vw, vh = await _get_viewport(page)
    to_x, to_y = _clamp_to_viewport(to_x, to_y, vw, vh)

    # 步骤 1：移动至目标附近（带偏移，非精确到达）
    near_offset_x = random.uniform(-8, 8)
    near_offset_y = random.uniform(-8, 8)
    near_x, near_y = _clamp_to_viewport(
        to_x + near_offset_x, to_y + near_offset_y, vw, vh)
    await perform_human_mouse_move(page, near_x, near_y)

    # 步骤 2：点击前短暂停顿（瞄准确认）
    await asyncio.sleep(lognormal_delay(0.11, sigma=0.6, min_v=0.05))  # [v2.13] 对数正态：多数快瞄、偶尔拖长

    # 步骤 3：微调至最终点击位置（偏离中心）
    click_offset_x = random.uniform(-3, 3)
    click_offset_y = random.uniform(-3, 3)
    click_x, click_y = _clamp_to_viewport(
        to_x + click_offset_x, to_y + click_offset_y, vw, vh)
    await page.mouse.move(click_x, click_y)
    await asyncio.sleep(random.uniform(0.01, 0.05))

    # 步骤 4：按下-保持-释放（真实按压时长）
    await page.mouse.down(button=button)
    await asyncio.sleep(random.uniform(0.03, 0.09))
    await page.mouse.up(button=button)

    # 步骤 5：点击后停顿（观察反馈）
    await asyncio.sleep(random.uniform(0.10, 0.35))

    # 更新鼠标位置追踪
    try:
        await page.evaluate(
            f"() => window._kiana_mouse_pos = {{x: {click_x}, y: {click_y}}}")
    except Exception:
        pass


async def perform_human_scroll(
    page,
    direction: str = 'down',
    total_distance: Optional[int] = None
) -> None:
    """模拟人类滚动行为，含动量衰减、间歇暂停与过冲回弹。

    实现要点：
      - 变速滚动：每段滚动速度不同
      - 动量衰减：单段内速度逐渐降低
      - 间歇暂停：段落间随机停顿（阅读内容）
      - 过冲回弹：概率性超过目标后回弹修正

    参数:
        page: playwright/patchright Page 对象
        direction: 滚动方向 ('down' / 'up')
        total_distance: 总滚动距离（像素），None 时随机生成
    """
    if direction not in ('up', 'down'):
        direction = 'down'

    if total_distance is None:
        total_distance = random.randint(200, 800)

    sign = 1 if direction == 'down' else -1
    scrolled = 0

    while scrolled < total_distance:
        remaining = total_distance - scrolled
        # 每段滚动距离（变速）
        chunk = min(random.randint(50, 200), remaining)

        # 单段内动量衰减滚动
        sub_steps = random.randint(3, 8)
        initial_momentum = random.uniform(0.8, 1.5)
        for i in range(sub_steps):
            # 衰减因子：随子步推进逐渐降低
            decel = 1.0 - (i / sub_steps) * 0.6
            step_pixels = chunk / sub_steps * initial_momentum * decel
            delta_y = int(sign * max(step_pixels, 1))
            await _safe_wheel(page, delta_y)
            await asyncio.sleep(random.uniform(0.02, 0.06))

        scrolled += chunk

        # 概率性过冲与回弹
        if random.random() < 0.15 and scrolled >= total_distance:
            over = random.randint(10, 40)
            await _safe_wheel(page, sign * over)
            await asyncio.sleep(random.uniform(0.1, 0.3))
            await _safe_wheel(page, -sign * over)
            await asyncio.sleep(random.uniform(0.05, 0.15))

        # 段落间间歇暂停
        if scrolled < total_distance and random.random() < 0.5:
            await asyncio.sleep(random.uniform(0.15, 0.6))
        else:
            await asyncio.sleep(random.uniform(0.03, 0.12))


async def perform_human_typing(
    page,
    text: str,
    selector: Optional[str] = None
) -> None:
    """模拟人类打字行为，含随机延迟、偶发拼写错误及退格修正。

    实现要点：
      - 按键间延迟 100-300ms，带随机抖动
      - 偶发拼写错误：按错相邻键后退格修正
      - 文本复杂度感知：大写/数字/特殊符号越多打字越慢
      - 打字中途概率性思考停顿
      - 空格与特殊字符有不同延迟分布

    参数:
        page: playwright/patchright Page 对象
        text: 待输入文本
        selector: 可选的输入框选择器，提供则先点击聚焦
    """
    if not text:
        return

    # 可选：先点击输入框聚焦
    if selector:
        try:
            await page.click(selector)
            await asyncio.sleep(random.uniform(0.1, 0.3))
        except Exception:
            pass

    complexity = _text_complexity(text)
    # 复杂度越高，整体延迟倍率越大
    delay_scale = 1.0 + complexity * 0.6

    for i, char in enumerate(text):
        # 打字中途概率性思考停顿
        if i > 0 and random.random() < _THINKING_PAUSE_RATE:
            await asyncio.sleep(random.uniform(0.3, 1.2))

        # 概率性拼写错误：按错相邻键后退格修正
        if char.isalpha() and random.random() < _TYPE_ERROR_RATE:
            wrong = _get_neighbor_key(char)
            if wrong:
                await page.keyboard.type(wrong)
                await asyncio.sleep(random.uniform(0.08, 0.20))
                await page.keyboard.press('Backspace')
                await asyncio.sleep(random.uniform(0.05, 0.15))

        # 输入当前字符（处理换行与制表符）
        if char == '\n':
            await page.keyboard.press('Enter')
        elif char == '\t':
            await page.keyboard.press('Tab')
        else:
            await page.keyboard.type(char)

        # 根据字符类型确定基础延迟
        if char.isupper() or (not char.isalnum() and char != ' ' and char != '\n'):
            base = random.uniform(0.15, 0.35)
        elif char == ' ':
            base = random.uniform(0.12, 0.28)
        else:
            base = lognormal_delay((_KEY_DELAY_MIN + _KEY_DELAY_MAX) / 2, sigma=0.55,
                                   min_v=_KEY_DELAY_MIN)
            base = min(base, _KEY_DELAY_MAX)

        await asyncio.sleep(base * delay_scale)


async def perform_idle_behavior(page, duration: float = 0.3) -> None:
    """随机执行微小的人类行为，使爬虫在空闲时更像真人。

    随机选择以下行为之一：
      - move:   鼠标轻微移动至附近随机位置
      - scroll: 小幅度滚动（上或下）
      - pause:  纯停顿（模拟阅读或思考）
      - jitter: 原地鼠标微抖动

    参数:
        page:     playwright/patchright Page 对象
        duration: 行为总时长预算（秒）——pause 分支停顿 = duration + 随机 0.2~0.8s
    """
    vw, vh = await _get_viewport(page)

    action = random.choices(
        ['move', 'scroll', 'pause', 'jitter'],
        weights=[0.30, 0.25, 0.30, 0.15],
        k=1
    )[0]

    if action == 'move':
        # 鼠标轻微移动至附近随机位置
        cur_x, cur_y = await _get_current_mouse_pos(page)
        new_x = _clamp(cur_x + random.randint(-80, 80), 5, vw - 5)
        new_y = _clamp(cur_y + random.randint(-80, 80), 5, vh - 5)
        await perform_human_mouse_move(page, new_x, new_y)

    elif action == 'scroll':
        # 小幅度随机滚动
        direction = random.choice(['up', 'down'])
        await perform_human_scroll(
            page, direction=direction,
            total_distance=random.randint(100, 400))

    elif action == 'pause':
        # 纯停顿（模拟阅读或思考）——时长随调用方 duration 预算缩放
        await asyncio.sleep(max(float(duration), 0.2) + lognormal_delay(0.5, sigma=0.7, min_v=0.2))

    elif action == 'jitter':
        # 原地鼠标微抖动
        cur_x, cur_y = await _get_current_mouse_pos(page)
        for _ in range(random.randint(2, 5)):
            jx = cur_x + random.gauss(0, 3)
            jy = cur_y + random.gauss(0, 3)
            await page.mouse.move(jx, jy)
            await asyncio.sleep(random.uniform(0.05, 0.15))
