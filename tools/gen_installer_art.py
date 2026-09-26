# -*- coding: utf-8 -*-
"""生成安装/卸载向导美术资源（v2.18.1 全新"深空玻璃"设计）——对齐 GUI 令牌：

设计语言（与 launcher 暗色玻璃一致）:
  · 深空渐变底 #0B0F14→#131B26 + 顶部/底部 accent 光晕（GaussianBlur 柔化）
  · 细点阵纹理（科技感，低对比）
  · 渐变圆角 Logo 芯片 "K" + 微光描边
  · 特性列表（accent 圆点）+ 底部版本徽章（版本号自动读 VERSION.json）
  · 卸载向导专属侧图 unwelcome.bmp（同语言，语义换成"移除/数据保留"提示）

resources: assets/installer/{header,welcome,unwelcome}.bmp
  MUI2 规范: header 150x57 · welcome/unwelcome 164x314
运行: python tools/gen_installer_art.py
"""
from pathlib import Path
import json
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "installer"

BG_TOP = (11, 15, 20)
BG_BOT = (19, 27, 38)
ACCENT = (79, 163, 232)
ACCENT_SOFT = (140, 196, 245)
TXT_MAIN = (232, 239, 246)
TXT_SUB = (148, 160, 176)
GLASS = (22, 27, 34, 216)
RADIUS = 8


def _ver() -> str:
    try:
        return json.loads((ROOT / "VERSION.json").read_text(encoding="utf-8"))["latest"]
    except Exception:
        return "2.18.1"


def _font(size, bold=False):
    for f in (("msyhbd.ttc" if bold else "msyhl.ttc"), "msyh.ttc"):
        try:
            return ImageFont.truetype(f, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _base(w, h) -> Image.Image:
    """深空渐变 + 双光晕 + 点阵纹理"""
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        t = y / max(h - 1, 1)
        row = tuple(int(BG_TOP[i] + (BG_BOT[i] - BG_TOP[i]) * t) for i in range(3))
        for x in range(w):
            px[x, y] = row
    # 顶部 accent 光晕（左上，柔化半径大）
    glow = Image.new("RGB", (w, h), (0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((-w * 0.45, -h * 0.12, w * 0.75, h * 0.28), fill=(26, 46, 66))
    gd.ellipse((w * 0.15, h * 0.82, w * 1.5, h * 1.35), fill=(18, 34, 50))
    glow = glow.filter(ImageFilter.GaussianBlur(28))
    img = Image.blend(img, Image.composite(glow, img, Image.new("L", (w, h), 255)), 0.0)
    # screen 叠加光晕（提亮不脏色）
    base = img.load(); gl = glow.load()
    for y in range(h):
        for x in range(w):
            b, g = base[x, y], gl[x, y]
            base[x, y] = tuple(min(255, b[i] + g[i] // 2) for i in range(3))
    # 点阵纹理
    d = ImageDraw.Draw(img, "RGBA")
    step = 14
    for gy in range(step, h, step):
        for gx in range(step, w, step):
            d.point((gx, gy), fill=(255, 255, 255, 10))
    return img


def _logo_chip(img: Image.Image, cx: int, cy: int, size: int):
    """渐变圆角 Logo 芯片 + 白 K + 微光"""
    d = ImageDraw.Draw(img, "RGBA")
    # 外发光
    halo = Image.new("RGBA", img.size, (0, 0, 0, 0))
    hd = ImageDraw.Draw(halo)
    hd.rounded_rectangle((cx - size // 2 - 6, cy - size // 2 - 6,
                          cx + size // 2 + 6, cy + size // 2 + 6),
                         radius=14, fill=(79, 163, 232, 70))
    halo = halo.filter(ImageFilter.GaussianBlur(10))
    img.paste(Image.alpha_composite(img.convert("RGBA"), halo).convert("RGB"), (0, 0))
    d = ImageDraw.Draw(img, "RGBA")
    # 芯片：竖向渐变填充
    chip = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    cpx = chip.load()
    top, bot = (96, 178, 246, 255), (52, 130, 205, 255)
    for y in range(size):
        t = y / max(size - 1, 1)
        row = tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(4))
        for x in range(size):
            cpx[x, y] = row
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=12, fill=255)
    img.paste(chip, (cx - size // 2, cy - size // 2), mask)
    d.rounded_rectangle((cx - size // 2, cy - size // 2, cx + size // 2, cy + size // 2),
                        radius=12, outline=(255, 255, 255, 60), width=1)
    f = _font(int(size * 0.58), bold=True)
    tb = d.textbbox((0, 0), "K", font=f)
    d.text((cx - (tb[2] - tb[0]) / 2 - tb[0], cy - (tb[3] - tb[1]) / 2 - tb[1]),
           "K", font=f, fill=(255, 255, 255, 245))


def _features(d, x, y, w, feats, step=22):
    f = _font(12)
    for i, t in enumerate(feats):
        fy = y + i * step
        d.ellipse((x, fy + 3, x + 8, fy + 11), fill=ACCENT)
        d.text((x + 16, fy), t, font=f, fill=(226, 233, 240))


def make_welcome(path, uninstall=False):
    w, h = 164, 314
    img = _base(w, h)
    d = ImageDraw.Draw(img, "RGBA")
    if uninstall:
        _logo_chip(img, w // 2, 64, 44)
        d = ImageDraw.Draw(img, "RGBA")
        f_big = _font(16, bold=True)
        d.text((w / 2, 108), "卸载 Kiana", font=f_big, anchor="ma", fill=TXT_MAIN)
        d.text((w / 2, 130), "Vnext Plus", font=_font(11), anchor="ma", fill=TXT_SUB)
        d.line((28, 156, w - 28, 156), fill=(255, 255, 255, 26), width=1)
        tips = ["移除程序文件与快捷方式", "您的下载数据不会被删除", "随时欢迎再次安装使用"]
        _features(d, 24, 172, w - 40, tips)
        # 底部提示徽章
        d.rounded_rectangle((12, h - 66, w - 12, h - 18), radius=RADIUS,
                            fill=GLASS, outline=(255, 255, 255, 22), width=1)
        d.rectangle((12, h - 66, 16, h - 18), fill=ACCENT)
        d.text((24, h - 58), "卸载向导", font=_font(12, bold=True), fill=(159, 216, 255))
        d.text((24, h - 38), "确认后开始移除程序文件", font=_font(10), fill=TXT_SUB)
    else:
        _logo_chip(img, w // 2, 58, 48)
        d = ImageDraw.Draw(img, "RGBA")
        d.text((w / 2, 100), "Kiana", font=_font(19, bold=True), anchor="ma", fill=TXT_MAIN)
        d.text((w / 2, 124), "Vnext Plus", font=_font(12), anchor="ma", fill=ACCENT_SOFT)
        d.text((w / 2, 142), "全能数据采集引擎", font=_font(11), anchor="ma", fill=TXT_SUB)
        d.line((28, 166, w - 28, 166), fill=(255, 255, 255, 26), width=1)
        _features(d, 24, 174, w - 40,
                  ["多平台视频/番剧下载", "全站数据/图片采集", "隐身代理 · 反反爬",
                   "开箱即用 · 零依赖"], step=18)
        d.rounded_rectangle((12, h - 68, w - 12, h - 18), radius=RADIUS,
                            fill=GLASS, outline=(79, 163, 232, 120), width=1)
        d.text((24, h - 60), f"采集引擎 v{_ver()}", font=_font(12, bold=True),
               fill=(159, 216, 255))
        d.text((24, h - 40), "6 平台解析 · 8 语种向导", font=_font(10), fill=TXT_SUB)
    img.convert("RGB").save(path, "BMP")
    print("saved", path)


def make_header(path):
    w, h = 150, 57
    img = _base(w, h).resize((w, h))
    d = ImageDraw.Draw(img, "RGBA")
    _logo_chip(img, 20, h // 2, 28)
    d = ImageDraw.Draw(img, "RGBA")
    d.text((42, h // 2 - 14), "Kiana Vnext Plus", font=_font(11, bold=True),
           fill=TXT_MAIN)
    d.text((42, h // 2 + 4), f"v{_ver()}", font=_font(9), fill=TXT_SUB)
    # 底部渐变强调线（accent 渐隐）
    for x in range(w):
        t = 1 - x / w
        d.point((x, h - 3), fill=(int(ACCENT[0] * t + 20 * (1 - t)),
                                  int(ACCENT[1] * t + 24 * (1 - t)),
                                  int(ACCENT[2] * t + 30 * (1 - t))))
        d.point((x, h - 2), fill=(int(ACCENT[0] * t * 0.6 + 20 * (1 - t)),
                                  int(ACCENT[1] * t * 0.6 + 24 * (1 - t)),
                                  int(ACCENT[2] * t * 0.6 + 30 * (1 - t))))
    img.convert("RGB").save(path, "BMP")
    print("saved", path)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    make_header(OUT / "header.bmp")
    make_welcome(OUT / "welcome.bmp", uninstall=False)
    make_welcome(OUT / "unwelcome.bmp", uninstall=True)
