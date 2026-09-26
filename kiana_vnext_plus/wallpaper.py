"""壁纸管线（v2.12 阶段 B）——二次元底图：cover 裁切 + 毛玻璃 + 智能蒙层 + 自动取色

设计红线（v2.12 计划坑表）：
  - cv2.imread 在 Windows 不支持 Unicode 路径 → 一律 np.fromfile + cv2.imdecode（坑1）
  - 只出 QImage（RGB888 + copy），QPixmap 转换只在主线程（坑5）
  - 8K 图先下采样长边 2560（坑6）
  - 全程 try/except：任何坏图返回 None，GUI 回退纯色
"""
import os
import random
import numpy as np

try:
    import cv2
    _CV = True
except Exception:
    _CV = False

from PySide6.QtGui import QImage, QColor

_IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
_DOWNSCALE_MAX = 2560  # 长边上限（内存保护）


def available() -> bool:
    return _CV


def pick_from_folder(folder: str, exclude_name: str = None) -> str | None:
    """文件夹随机抽一张图（排除上次那张），无图返回 None"""
    try:
        p = str(folder)
        if not p or not os.path.isdir(p):
            return None
        names = [n for n in os.listdir(p)
                 if n.lower().endswith(_IMG_EXTS)
                 and not n.startswith(".")]
        if exclude_name:
            names = [n for n in names if n != exclude_name] or \
                    [n for n in os.listdir(p) if n.lower().endswith(_IMG_EXTS)]
        if not names:
            return None
        return os.path.join(p, random.choice(names))
    except Exception:
        return None


def read_universal(path: str):
    """Unicode 路径安全的读图（BGR ndarray）；失败返回 None"""
    if not _CV:
        return None
    try:
        buf = np.fromfile(str(path), dtype=np.uint8)  # 坑1：不走 cv2.imread
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        return img
    except Exception:
        return None


def _downscale(img, max_side: int = _DOWNSCALE_MAX):
    h, w = img.shape[:2]
    long_side = max(h, w)
    if long_side <= max_side:
        return img
    ratio = max_side / long_side
    return cv2.resize(img, (int(w * ratio), int(h * ratio)), interpolation=cv2.INTER_AREA)


def _cover_crop(img, win_w: int, win_h: int, focus: int = 4):
    """等比缩放填满窗口后裁切（绝不拉伸）。focus 0-8 九宫格：
    0 1 2 / 3 4 5 / 6 7 8 —— 决定裁切窗口保留哪一侧（人物定位）"""
    src_h, src_w = img.shape[:2]
    scale = max(win_w / src_w, win_h / src_h)
    scaled_w, scaled_h = int(src_w * scale), int(src_h * scale)
    resized = cv2.resize(img, (scaled_w, scaled_h), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
    # 焦点 → 裁切起点（列 0/1/2 → x 比例 0/0.5/1）
    fx, fy = (focus % 3) / 2 if focus % 3 in (0, 2) else 0.5, (focus // 3) / 2 if focus // 3 in (0, 2) else 0.5
    off_x = int((scaled_w - win_w) * fx)
    off_y = int((scaled_h - win_h) * fy)
    return resized[off_y:off_y + win_h, off_x:off_x + win_w]


def _auto_dim_for(img) -> int:
    """按图片亮度给蒙层建议（百分比 8-45）：亮图多蒙（暗色主题下刺眼）、暗图少蒙"""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    lum = float(gray.mean())  # 0-255
    return int(max(8, min(45, (lum - 128) * 0.35 + 20)))


def process(src_path: str, win_w: int, win_h: int, focus: int = 4,
            blur: int = 0, dim: int = 0, theme: str = "dark",
            auto_dim: bool = True) -> QImage | None:
    """完整管线：读图→下采样→cover 裁切→高斯模糊→蒙层合成→QImage。
    任何失败返回 None（GUI 回退纯色）。"""
    try:
        if not _CV or not src_path or not os.path.isfile(str(src_path)):
            return None
        img = read_universal(src_path)
        if img is None or img.size == 0:
            return None
        img = _downscale(img)
        img = _cover_crop(img, max(win_w, 2), max(win_h, 2), focus=focus)
        if blur and blur > 0:
            k = int(blur) * 2 + 1
            img = cv2.GaussianBlur(img, (k, k), 0)
        # 智能蒙层：自动档与手动档取大者（手动只在"加蒙"方向生效）
        strength = int(dim)
        if auto_dim:
            strength = max(strength, _auto_dim_for(img))
        if strength > 0:
            alpha = min(strength, 60) / 100.0
            overlay_color = (8, 10, 14) if theme != "light" else (245, 247, 250)
            overlay = np.full_like(img, overlay_color, dtype=np.uint8)
            img = cv2.addWeighted(img, 1 - alpha, overlay, alpha, 0)
        # BGR → RGB → QImage（copy 保住数据生命周期）
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        qimg = QImage(rgb.tobytes(), w, h, ch * w, QImage.Format.Format_RGB888)
        return qimg.copy()
    except Exception:
        return None


def extract_accent(src_path: str) -> str | None:
    """从底图提取主色（#RRGGBB）：HSV 过滤白黑灰 → 直方图众数 → 可读性钳制。
    取不出干净色返回 None（GUI 保持现强调色）。"""
    try:
        if not _CV:
            return None
        img = read_universal(src_path)
        if img is None:
            return None
        img = _downscale(img, max_side=128)
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        # 滤灰/白（S 低）与黑（V 低）；V 无上限——纯饱和色 V=255 是合法主色
        mask = (hsv[:, :, 1] > 60) & (hsv[:, :, 2] > 40)
        if not mask.any():
            return None
        pixels = img[mask]
        # 量化到 32/格直方图取众数
        quant = (pixels // 32).astype(np.int32)
        keys = quant[:, 0] * 4096 + quant[:, 1] * 64 + quant[:, 2]
        values, counts = np.unique(keys, return_counts=True)
        top = values[np.argmax(counts)]
        # keys = B*4096 + G*64 + R（BGR 顺序：B 高位、R 低位）
        r = int((top % 64) * 32 + 16)
        g = int(((top // 64) % 64) * 32 + 16)
        b = int((top // 4096) * 32 + 16)
        color = QColor(r, g, b)
        h, s, v, _ = color.getHsvF() if color.getHsvF()[3] >= 0 else (0, 0, 0, 0)
        if color.isValid():
            # 可读性钳制：过暗提亮、过亮压暗（强调色文字/按钮可见）
            h, s, v, _ = color.getHsvF()
            v = min(max(v, 0.45), 0.85)
            s = min(s, 0.9)
            color.setHsvF(h, s, v)
            return color.name()
        return None
    except Exception:
        return None
