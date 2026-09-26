"""v2.19 安全回归：SSRF 闸（主通道 / 下载通道 / m3u8 / 入队）

对应 v2.19 优化批次 1.1-1.3。
全部离线（不发起任何真实请求）。
"""
import asyncio
import inspect
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiana_vnext_plus.url_utils import is_private_url


class _FakeResp:
    """最小 curl_cffi 响应替身（含 is_redirect / headers / text）"""

    def __init__(self, status=200, headers=None, text="ok"):
        self.status_code = status
        self.headers = dict(headers or {})
        self.text = text
        self.content = text.encode()
        self.is_redirect = status in (301, 302, 303, 307, 308)


class _FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def request(self, method, url, **kw):
        self.calls.append(url)
        return self._responses.pop(0) if self._responses else _FakeResp(404)


def _engine(responses, **attrs):
    """构造一个绕过 __init__ 的 ProtocolEngine（只挂 fetch 需要的属性）"""
    from kiana_vnext_plus.protocol_engine import ProtocolEngine
    eng = ProtocolEngine.__new__(ProtocolEngine)
    eng.max_retries = 0
    eng.retry_backoff_base = 0.01
    eng.max_body_bytes = 1024 * 1024
    fake = _FakeClient(responses)
    eng._get_client_for_proxy = lambda proxy: (fake, None)
    eng._build_headers = lambda *a, **k: {}
    for k, v in attrs.items():
        setattr(eng, k, v)
    return eng, fake


class TestPrivateUrlDetection(unittest.TestCase):
    """is_private_url 的对抗样本（本工程 SSRF 闸的基石）"""

    def test_private_variants_blocked(self):
        for u in ("http://127.0.0.1/", "http://localhost/", "http://169.254.169.254/",
                  "http://2130706433/", "http://0x7f000001/", "http://[::ffff:127.0.0.1]/",
                  "http://10.0.0.1/", "http://192.168.1.1/", "http://[fd00::1]/",
                  "file:///etc/passwd"):
            self.assertTrue(is_private_url(u), f"应拦截: {u}")

    def test_public_allowed(self):
        for u in ("https://www.bilibili.com/", "https://example.com/a?b=1",
                  "https://1.1.1.1/", "https://api.github.com/x"):
            self.assertFalse(is_private_url(u), f"不应拦截: {u}")

    def test_cgnat_and_special_ranges_blocked(self):
        """[v2.19.3 质检补强] 标准 ipaddress 未覆盖的特殊用途段必须显式拦截。

        CGNAT（100.64.0.0/10）曾在外部评估中被指为漏点；v2.19 建 SSRF 闸时沿用旧函数
        未补 → 质检轮发现并修复。此测试防回退。"""
        for u in ("http://100.64.0.1/", "http://100.127.255.254/",   # CGNAT 段
                  "http://192.88.99.1/",                              # 6to4 中继
                  "http://192.0.0.1/", "http://198.18.0.1/", "http://192.0.2.1/",
                  "http://255.255.255.255/", "http://224.0.0.1/", "http://240.0.0.1/",
                  "http://198.51.100.1/", "http://203.0.113.1/",
                  "http://[::1]/", "http://[64:ff9b::1]/", "http://0.0.0.0/",
                  "http://metadata.google.internal/"):
            self.assertTrue(is_private_url(u, dns_check=False), f"应拦截: {u}")

    def test_cgnat_boundaries_not_overblocked(self):
        """边界之外不得误伤（避免把正常公网判成内网）"""
        for u in ("http://100.63.255.255/", "http://100.128.0.1/",   # CGNAT 段外
                  "http://192.88.98.1/", "http://192.88.100.1/"):    # 6to4 段外
            self.assertFalse(is_private_url(u, dns_check=False), f"不应拦截: {u}")


class TestProtocolEngineSSRF(unittest.TestCase):
    """主通道（curl_cffi）SSRF 闸 —— 此前完全无校验"""

    def _run(self, coro):
        return asyncio.new_event_loop().run_until_complete(coro)

    def test_private_target_rejected_without_request(self):
        async def go():
            eng, fake = _engine([_FakeResp(200)])
            r = await eng.fetch("http://169.254.169.254/latest/meta-data/")
            # [v2.19.6] 拦截态改 400（**不可用 403**：403 是回退链"升级到浏览器"的信号，
            # 会让被拦 URL 被交给无闸的 Playwright page.goto —— 审查发现并修复）
            self.assertEqual(r.status_code, 400, "私网目标必须直接拒绝（400，非 403）")
            self.assertEqual(len(fake.calls), 0, "私网目标不得发出请求")
        self._run(go())

    def test_redirect_to_private_blocked(self):
        async def go():
            eng, fake = _engine([_FakeResp(302, {"Location": "http://127.0.0.1/admin"})])
            r = await eng.fetch("https://example.com/")
            self.assertEqual(r.status_code, 400, "重定向到私网必须拦截（400，非 403）")
            self.assertEqual(len(fake.calls), 1, "不得跟随到私网目标")
        self._run(go())

    def test_relative_redirect_followed(self):
        """正常同站相对跳转应被跟随（防误伤）"""
        async def go():
            eng, fake = _engine([
                _FakeResp(302, {"Location": "/page2"}),
                _FakeResp(200, {}, "final"),
            ])
            r = await eng.fetch("https://example.com/page1")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(len(fake.calls), 2)
            self.assertTrue(fake.calls[1].startswith("https://example.com/page2"))
        self._run(go())

    def test_redirect_loop_capped(self):
        """[v2.19.6 修复] 重定向循环超限必须**可诊断且可重试**。

        审查发现原实现（上限 5、超限静默 break 返回 3xx）会造成：
        302 → 不在可重试列表 → mark_failed(retry=False) → **永久 dead**，
        且 status>=400 不成立 → errors 表零记录、日志零输出。
        而 curl_cffi 默认跟随 30 跳——6~30 跳的合法链会从"能抓"变成"静默判死"。
        现改为：上限 10，超限返回 508（可重试 + 写 errors + warning）。
        """
        async def go():
            eng, fake = _engine([_FakeResp(302, {"Location": "/loop"})] * 20)
            r = await eng.fetch("https://example.com/")
            self.assertEqual(r.status_code, 508, "超限应返回 508（可诊断/可重试），而非 3xx")
            self.assertLessEqual(len(fake.calls), 12, "跳数应有上限（初跳 + 10 跳）")
            self.assertGreaterEqual(len(fake.calls), 10, "上限不应过度收紧到 5 跳")
        self._run(go())

    def test_redirect_without_location_reported(self):
        """[v2.19.6] 3xx 但缺 Location：返回 502 而非静默把 3xx 交给下游"""
        async def go():
            eng, fake = _engine([_FakeResp(302, {})])
            r = await eng.fetch("https://example.com/")
            self.assertEqual(r.status_code, 502, "缺 Location 应显式报错")
        self._run(go())

    def test_normal_request_unchanged(self):
        async def go():
            eng, fake = _engine([_FakeResp(200, {}, "hello")])
            r = await eng.fetch("https://example.com/")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(len(fake.calls), 1)
        self._run(go())


class TestDownloadGuards(unittest.TestCase):
    """下载通道闸：video/audio 此前缺失（同文件 image/file 却有）"""

    def test_video_audio_have_ssrf_guard(self):
        from kiana_vnext_plus.universal_downloader import UniversalDownloader
        for m in (UniversalDownloader.download_video, UniversalDownloader.download_audio):
            src = inspect.getsource(m)
            self.assertIn("is_private_url", src,
                          f"{m.__name__} 必须含 SSRF 闸")

    def test_audio_blocks_private(self):
        async def go():
            from kiana_vnext_plus.universal_downloader import UniversalDownloader
            d = UniversalDownloader.__new__(UniversalDownloader)
            r = await d.download_audio("http://127.0.0.1/a.mp3")
            self.assertIsNone(r, "私网音频应被拒绝")
        asyncio.new_event_loop().run_until_complete(go())


class TestM3U8Guards(unittest.TestCase):
    """m3u8 通道：分片/密钥 URL 来自远端播放列表，可指向任意主机"""

    def _dl(self):
        from kiana_vnext_plus.m3u8_downloader import M3U8Downloader
        return M3U8Downloader.__new__(M3U8Downloader)

    def test_blocked_helper(self):
        d = self._dl()
        self.assertTrue(d._blocked("http://127.0.0.1/seg.ts"))
        self.assertTrue(d._blocked("http://169.254.169.254/x"))
        self.assertTrue(d._blocked("ftp://example.com/x"))
        self.assertTrue(d._blocked(""))
        self.assertFalse(d._blocked("https://cdn.example.com/seg1.ts"))

    def test_private_segments_filtered_from_playlist(self):
        d = self._dl()
        content = ("#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI=\"http://127.0.0.1/key\"\n"
                   "#EXTINF:5,\nhttp://127.0.0.1/a.ts\n"
                   "#EXTINF:5,\nseg2.ts\n")
        segs, key = d._parse_media_playlist(content, "https://cdn.example.com/x/play.m3u8")
        self.assertEqual(len(segs), 1, "私网分片必须被过滤")
        self.assertIn("seg2.ts", segs[0])
        self.assertIsNone(key, "私网密钥 URL 必须被拒绝")

    def test_same_host_segments_kept(self):
        d = self._dl()
        content = "#EXTM3U\n#EXTINF:5,\nseg1.ts\n#EXTINF:5,\nseg2.ts\n"
        segs, _ = d._parse_media_playlist(content, "https://cdn.example.com/x/play.m3u8")
        self.assertEqual(len(segs), 2, "正常同源分片不得被过滤")

    def test_download_entry_blocked(self):
        async def go():
            from kiana_vnext_plus.m3u8_downloader import M3U8Downloader
            d = M3U8Downloader.__new__(M3U8Downloader)
            d.session = None
            r = await d.download("http://169.254.169.254/x.m3u8", {}, "out.mp4")
            self.assertFalse(r, "私网 m3u8 入口必须拒绝")
        asyncio.new_event_loop().run_until_complete(go())


if __name__ == "__main__":
    unittest.main()
