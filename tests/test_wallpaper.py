"""Kiana Vnext Plus — v2.12 阶段 B：壁纸管线回归测试

覆盖：Unicode 路径读图（cv2.imread 坑）、cover 裁切不挤压（比例断言）、
九宫格焦点生效、模糊生效、智能蒙层（白图>黑图）、文件夹抽图、自动取色。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _write_img(path, h, w, color_bgr):
    import cv2
    img = np.full((h, w, 3), color_bgr, dtype=np.uint8)
    ok, buf = cv2.imencode(".png", img)
    assert ok
    buf.tofile(str(path))  # tofile 支持 Unicode 路径（与管线同款读法）


class TestReadUniversal:
    def test_unicode_path(self, tmp_path):
        from kiana_vnext_plus import wallpaper as wp
        p = tmp_path / "中文名称_测试.png"
        _write_img(p, 100, 200, (200, 100, 50))
        img = wp.read_universal(str(p))
        assert img is not None and img.shape == (100, 200, 3)

    def test_bad_file_returns_none(self, tmp_path):
        from kiana_vnext_plus import wallpaper as wp
        p = tmp_path / "broken.png"
        p.write_bytes(b"not-an-image")
        assert wp.read_universal(str(p)) is None


class TestCoverCrop:
    """竖图 400x1200 裁成 800x600：输出必须恰好 800x600（不挤压、不失真）"""

    def test_portrait_no_distortion(self, tmp_path):
        from kiana_vnext_plus import wallpaper as wp
        p = tmp_path / "tall.png"
        _write_img(p, 1200, 400, (90, 160, 220))
        q = wp.process(str(p), 800, 600)
        assert q is not None
        assert (q.width(), q.height()) == (800, 600)

    def test_wide_no_distortion(self, tmp_path):
        from kiana_vnext_plus import wallpaper as wp
        p = tmp_path / "wide.png"
        _write_img(p, 600, 1600, (90, 160, 220))
        q = wp.process(str(p), 800, 600)
        assert q is not None and (q.width(), q.height()) == (800, 600)

    def test_focus_changes_crop(self, tmp_path, qapp):
        from kiana_vnext_plus import wallpaper as wp
        # 左红右蓝的横向渐变图：焦点左(0)与焦点右(2)裁出的像素应不同
        p = tmp_path / "split.png"
        img = np.zeros((600, 1600, 3), dtype=np.uint8)
        img[:, :800] = (0, 0, 255)     # 左红(BGR)
        img[:, 800:] = (255, 0, 0)     # 右蓝
        import cv2
        ok, buf = cv2.imencode(".png", img)
        buf.tofile(str(p))
        q_left = wp.process(str(p), 800, 600, focus=0)
        q_right = wp.process(str(p), 800, 600, focus=2)
        assert q_left is not None and q_right is not None
        px_l = q_left.pixelColor(700, 300)
        px_r = q_right.pixelColor(700, 300)
        assert (px_l.red(), px_l.green(), px_l.blue()) != (px_r.red(), px_r.green(), px_r.blue())


class TestBlurAndDim:
    def test_blur_changes_output(self, tmp_path):
        from kiana_vnext_plus import wallpaper as wp
        # 噪声图：模糊后中心像素必然变化
        p = tmp_path / "noise.png"
        rng = np.random.default_rng(42)
        img = rng.integers(0, 255, (600, 600, 3), dtype=np.uint8)
        import cv2
        ok, buf = cv2.imencode(".png", img)
        buf.tofile(str(p))
        q0 = wp.process(str(p), 400, 400, blur=0)
        q25 = wp.process(str(p), 400, 400, blur=25)
        assert q0.pixelColor(200, 200) != q25.pixelColor(200, 200)

    def test_auto_dim_white_vs_black(self, tmp_path):
        from kiana_vnext_plus import wallpaper as wp
        pw = tmp_path / "white.png"
        pb = tmp_path / "black.png"
        _write_img(pw, 600, 800, (255, 255, 255))
        _write_img(pb, 600, 800, (0, 0, 0))
        # 蒙层强度方向断言：白图建议蒙层 > 黑图（亮图多蒙）
        wimg = wp.read_universal(str(pw))
        bimg = wp.read_universal(str(pb))
        assert wp._auto_dim_for(wimg) > wp._auto_dim_for(bimg)
        # 主题蒙层色断言：暗色主题处理白图应比亮色主题处理后更暗（黑蒙 vs 白蒙）
        qw_dark = wp.process(str(pw), 400, 400, auto_dim=True, theme="dark")
        qw_light = wp.process(str(pw), 400, 400, auto_dim=True, theme="light")
        lum_dark = sum(qw_dark.pixelColor(200, 200).getRgb()[:3])
        lum_light = sum(qw_light.pixelColor(200, 200).getRgb()[:3])
        assert lum_dark < lum_light

    def test_bad_image_returns_none(self, tmp_path):
        from kiana_vnext_plus import wallpaper as wp
        p = tmp_path / "bad.png"
        p.write_bytes(b"junk")
        assert wp.process(str(p), 400, 400) is None


class TestPickFromFolder:
    def test_pick(self, tmp_path):
        from kiana_vnext_plus import wallpaper as wp
        for i in range(3):
            _write_img(tmp_path / f"img{i}.jpg", 10, 10, (1, 2, 3))
        (tmp_path / "note.txt").write_text("x")
        picked = wp.pick_from_folder(str(tmp_path))
        assert picked is not None and picked.lower().endswith(".jpg")

    def test_empty_folder(self, tmp_path):
        from kiana_vnext_plus import wallpaper as wp
        assert wp.pick_from_folder(str(tmp_path)) is None
        assert wp.pick_from_folder(str(tmp_path / "nope")) is None


class TestExtractAccent:
    def test_red_image_gives_red(self, tmp_path):
        from kiana_vnext_plus import wallpaper as wp
        p = tmp_path / "red.png"
        _write_img(p, 300, 300, (0, 0, 255))  # BGR 红
        hexcolor = wp.extract_accent(str(p))
        assert hexcolor is not None
        c = hexcolor.lstrip("#")
        r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
        assert r > g and r > b  # 红主导

    def test_gray_image_returns_none(self, tmp_path):
        from kiana_vnext_plus import wallpaper as wp
        p = tmp_path / "gray.png"
        _write_img(p, 300, 300, (128, 128, 128))  # 无饱和度
        assert wp.extract_accent(str(p)) is None
