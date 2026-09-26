"""电商滑块视觉识别模块（Slider Vision）
v2.5.0 新增——OpenCV 缺口识别引擎，支撑京东 nlogin / 淘宝无痕 / 拼多多拼图滑块：

  1. find_gap_by_template : 背景图+缺口图 → 模板匹配（京东 JCap / 拼多多式，最准）
  2. find_gap_by_edge     : 单背景图 → 边缘+形态学缺口定位（淘宝无痕 / geetest 式）
  3. bezier_track         : 贝塞尔人类轨迹（加速-匀速-减速-回退微调-抖动）

依赖：opencv-python-headless（本机已装 5.0.0）、numpy
"""
import logging
import random
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    import cv2
    import numpy as np
    _CV_OK = True
except Exception as e:  # pragma: no cover
    _CV_OK = False
    logger.warning(f"OpenCV 不可用（滑块视觉识别降级）: {e}")


def available() -> bool:
    """OpenCV 是否可用"""
    return _CV_OK


# [FIXED & MODIFIED] v2.11 阈值参数化：原硬编码阈值无法实测调优（验证码闭环 bench 网格搜索入口）
SLIDER_PARAMS = {
    "template_threshold": 0.6,   # 灰度模板匹配最低相关性（低于则尝试边缘匹配）
    "edge_threshold": 0.45,      # 边缘模板匹配最低相关性（低于判定缺口图不匹配）
    "track_steps_min": 18,       # 贝塞尔轨迹最小步数
    "track_steps_div": 6,        # 轨迹步数 = max(min, distance/div)
}


def set_slider_params(**kwargs):
    """运行时更新滑块求解参数（captcha_bench --tune 网格搜索入口）"""
    SLIDER_PARAMS.update(kwargs)


def get_slider_params() -> dict:
    return dict(SLIDER_PARAMS)


# ═══════════════════════════════════════════════════════════════
# 1. 模板匹配（背景图 + 缺口图）
# ═══════════════════════════════════════════════════════════════
def find_gap_by_template(bg_path: str, gap_path: str,
                         scale: float = 1.0) -> Optional[int]:
    """背景图 + 缺口图模板匹配，返回缺口中心 x 坐标（相对背景图原始像素）。

    京东 nlogin / 拼多多滑块背景是完整大图，缺口图是单独小图 → 模板匹配精度最高。
    scale: 页面 CSS 缩放系数（若图片按 CSS 缩放显示，需换算回页面像素）
    """
    if not _CV_OK:
        return None
    try:
        bg = cv2.imread(bg_path, cv2.IMREAD_GRAYSCALE)
        gap = cv2.imread(gap_path, cv2.IMREAD_GRAYSCALE)
        if bg is None or gap is None:
            logger.debug("模板匹配: 图片读取失败")
            return None

        # [FIXED & MODIFIED] 灰度模板匹配优先（缺口图=背景裁剪块，同图案灰度相关性最高）；
        # Canny 边缘匹配仅作备选（缺口图带纯色/半透明块时边缘法失效）
        bg_gray = bg
        hb, wb = bg_gray.shape
        search = bg_gray[:, int(wb * 0.35):]
        res = cv2.matchTemplate(search, gap, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        if max_val < SLIDER_PARAMS.get("template_threshold", 0.6):
            # 备选：边缘匹配（模板加粗）
            bg_edge = cv2.Canny(bg, 100, 200)
            gap_edge = cv2.Canny(gap, 100, 200)
            gap_edge = cv2.dilate(gap_edge, np.ones((3, 3), np.uint8), iterations=1)
            res2 = cv2.matchTemplate(bg_edge[:, int(wb * 0.35):], gap_edge, cv2.TM_CCOEFF_NORMED)
            _, max_val2, _, max_loc2 = cv2.minMaxLoc(res2)
            if max_val2 < SLIDER_PARAMS.get("edge_threshold", 0.45):
                logger.debug(f"模板匹配: 相关性过低 {max_val2:.2f}")
                return None
            max_val, max_loc = max_val2, max_loc2

        center = int(max_loc[0] + int(wb * 0.35) + gap.shape[1] / 2)
        # 模板匹配结果含缺口图自身宽度偏移，按 CSS 缩放换算
        return int(center * scale)
    except Exception as e:
        logger.debug(f"模板匹配异常: {e}")
        return None


# ═══════════════════════════════════════════════════════════════
# 2. 单图边缘检测（无缺口图场景）
# ═══════════════════════════════════════════════════════════════
def find_gap_by_edge(bg_path: str, scale: float = 1.0) -> Optional[int]:
    """单背景图 → 边缘检测 + 形态学闭合 + 列投影，定位缺口左边界。

    淘宝无痕验证 / geetest 滑块：缺口是背景图上的一块异色区域，
    边缘检测后缺口区域会形成密集边缘块 → 列投影找最大块的起始 x。
    """
    if not _CV_OK:
        return None
    try:
        img = cv2.imread(bg_path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return None
        h, w = img.shape

        # [FIXED & MODIFIED] 方法 1：亮度连通域（缺口=与背景色差明显的区域，最通用）
        # 自适应阈值：以背景中位亮度为基准，缺口显著偏离背景即被分离
        bg_med = int(np.median(img))
        thresh_val = max(bg_med + 45, 190) if bg_med < 200 else bg_med - 45
        if bg_med < 200:
            mask = (img > thresh_val).astype(np.uint8)
        else:
            mask = (img < thresh_val).astype(np.uint8)
        # 形态学闭合连接碎片
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE,
                                cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9)))
        n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        if n > 1:
            # 取右半区最大连通域（排除背景/滑块本体：滑块在左侧起始位）
            best = None
            for i in range(1, n):
                x, y, bw, bh, area = stats[i]
                cx = x + bw // 2
                if area < 200:  # 面积过滤
                    continue
                if cx < w * 0.3:  # 排除左侧（滑块本体/起点）
                    continue
                if best is None or area > best[0]:
                    best = (area, cx)
            if best:
                logger.debug(f"亮度连通域: 缺口中心 x={best[1]}")
                return int(best[1] * scale)

        # [FIXED & MODIFIED] 方法 2：边缘检测 + 列投影（亮度法失败的降级）
        blur = cv2.GaussianBlur(img, (5, 5), 0)
        edges = cv2.Canny(blur, 50, 150)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (13, 13))
        closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel)

        # 列投影：每列边缘像素数
        col_sum = closed.sum(axis=0) // 255
        # 找所有连续"活跃列"段（>2 像素边缘）
        segments = []
        in_seg = False
        start = 0
        for x in range(w):
            if col_sum[x] > 2 and not in_seg:
                in_seg = True
                start = x
            elif col_sum[x] <= 2 and in_seg:
                in_seg = False
                if x - start > 20:  # 宽度过滤（缺口通常 >20px）
                    segments.append((start, x - start))
        if in_seg and w - start > 20:
            segments.append((start, w - start))

        if not segments:
            logger.debug("边缘检测: 未找到缺口段")
            return None

        # 缺口通常在右半区且面积最大——取右半区最宽段
        right = [(s, l) for s, l in segments if s > w * 0.35]
        pool = right if right else segments
        best_start, best_len = max(pool, key=lambda t: t[1])
        # 缺口中心 ≈ 段起点 + 段宽/2
        return int((best_start + best_len / 2) * scale)
    except Exception as e:
        logger.debug(f"边缘检测异常: {e}")
        return None


# ═══════════════════════════════════════════════════════════════
# 3. 贝塞尔人类轨迹
# ═══════════════════════════════════════════════════════════════
def bezier_track(distance: int, y_jitter: float = 3.0,
                 steps: Optional[int] = None) -> List[Tuple[float, float, float]]:
    """三次贝塞尔拖动轨迹（更拟人）→ [(x, y, delay), ...]

    特征：起始加速、中段匀速、末端减速、随机 Y 抖动、终点回退微调。
    """
    if distance <= 0:
        return [(0.0, 0.0, 0.1)]
    steps = steps or max(SLIDER_PARAMS.get("track_steps_min", 18),
                         int(distance / SLIDER_PARAMS.get("track_steps_div", 6)))

    # 控制点：起始(0,0)、两个随机控制点（第一段快、第二段慢）
    p0 = (0.0, 0.0)
    p1 = (distance * random.uniform(0.35, 0.55), random.uniform(-y_jitter, y_jitter))
    p2 = (distance * random.uniform(0.75, 0.9), random.uniform(-y_jitter, y_jitter))
    p3 = (float(distance), random.uniform(-y_jitter * 0.5, y_jitter * 0.5))

    track = []
    prev_x = 0.0
    for i in range(1, steps + 1):
        t = i / steps
        # 三次贝塞尔插值
        mt = 1 - t
        x = (mt**3 * p0[0] + 3 * mt**2 * t * p1[0] +
             3 * mt * t**2 * p2[0] + t**3 * p3[0])
        y = (mt**3 * p0[1] + 3 * mt**2 * t * p1[1] +
             3 * mt * t**2 * p2[1] + t**3 * p3[1])
        # 延迟：起步慢→加速→收尾慢
        if t < 0.15:
            delay = random.uniform(0.02, 0.05)
        elif t < 0.8:
            delay = random.uniform(0.005, 0.015)
        else:
            delay = random.uniform(0.02, 0.06)
        if x > prev_x:
            track.append((x, y, delay))
            prev_x = x

    # 终点回退微调（拟人修正）
    if random.random() < 0.8:
        back = distance * random.uniform(0.02, 0.06)
        track.append((distance - back, random.uniform(-1, 1), random.uniform(0.02, 0.06)))
        track.append((distance, random.uniform(-1, 1), random.uniform(0.03, 0.08)))
    # 终点停顿
    track.append((distance, 0.0, random.uniform(0.15, 0.35)))
    return track
