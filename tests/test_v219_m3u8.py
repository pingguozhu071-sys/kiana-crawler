"""v2.19 测试补强：m3u8 AES-128 解密链路（批次 6.2）

背景：评估指出 `m3u8_downloader`（含 AES-128 解密）**零测试覆盖**。
本文件用**本地构造**的加密分片验证解密链路，不发起任何网络请求。
"""
import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    HAS_CRYPTO = True
except Exception:  # pragma: no cover
    HAS_CRYPTO = False

from kiana_vnext_plus.m3u8_downloader import M3U8Downloader


def _encrypt(plain: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-128-CBC + PKCS7 填充（模拟 HLS 分片加密）"""
    pad = 16 - (len(plain) % 16)
    padded = plain + bytes([pad]) * pad
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    enc = cipher.encryptor()
    return enc.update(padded) + enc.finalize()


def _dl():
    d = M3U8Downloader.__new__(M3U8Downloader)   # 不初始化 session
    return d


@unittest.skipUnless(HAS_CRYPTO, "需要 cryptography")
class TestAes128Decrypt(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="kiana_m3u8_"))
        self.key = bytes(range(16))             # 固定测试密钥
        self.iv = bytes([0] * 12 + [0, 0, 0, 7])  # 显式 IV

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_decrypt_with_explicit_iv(self):
        plain = b"KIANA-SEGMENT-PAYLOAD-0123456789"
        f = self.tmp / "seg0.ts"
        f.write_bytes(_encrypt(plain, self.key, self.iv))
        _run(_dl()._decrypt_file(f, self.key, self.iv.hex(), 0))
        self.assertEqual(f.read_bytes(), plain, "显式 IV 应正确解密")

    def test_decrypt_with_0x_prefixed_iv(self):
        """IV 带 0x 前缀（部分播放列表写法）"""
        plain = b"payload-with-0x-iv"
        f = self.tmp / "seg1.ts"
        f.write_bytes(_encrypt(plain, self.key, self.iv))
        _run(_dl()._decrypt_file(f, self.key, "0x" + self.iv.hex(), 0))
        self.assertEqual(f.read_bytes(), plain)

    def test_decrypt_uses_sequence_number_when_no_iv(self):
        """无 IV → 用分片序号作为 IV（HLS 规范行为）"""
        seq = 5
        iv_from_seq = seq.to_bytes(16, "big")
        plain = b"payload-seq-iv"
        f = self.tmp / "seg5.ts"
        f.write_bytes(_encrypt(plain, self.key, iv_from_seq))
        _run(_dl()._decrypt_file(f, self.key, None, seq))
        self.assertEqual(f.read_bytes(), plain, "无 IV 时应以分片序号为 IV")

    def test_invalid_iv_falls_back_to_index(self):
        """非法 IV 字符串 → 回退用序号（不得抛异常）"""
        seq = 2
        iv_from_seq = seq.to_bytes(16, "big")
        plain = b"payload-bad-iv"
        f = self.tmp / "seg2.ts"
        f.write_bytes(_encrypt(plain, self.key, iv_from_seq))
        _run(_dl()._decrypt_file(f, self.key, "not-a-hex-iv", seq))
        self.assertEqual(f.read_bytes(), plain, "非法 IV 应回退序号 IV")

    def test_pkcs7_unpadded(self):
        """去填充后不得残留 padding 字节"""
        plain = b"x" * 32          # 正好整块 → 加密时补满 16 字节 padding
        f = self.tmp / "seg3.ts"
        f.write_bytes(_encrypt(plain, self.key, self.iv))
        _run(_dl()._decrypt_file(f, self.key, self.iv.hex(), 0))
        self.assertEqual(f.read_bytes(), plain)
        self.assertNotIn(b"\x10" * 16, f.read_bytes())

    def test_empty_file_safe(self):
        """空分片不得抛异常"""
        f = self.tmp / "empty.ts"
        f.write_bytes(b"")
        out = _run(_dl()._decrypt_file(f, self.key, self.iv.hex(), 0))
        self.assertIsNone(out)


class TestPlaylistParsingRegression(unittest.TestCase):
    """分片列表解析（含 v2.19 新增的 SSRF 过滤）——防回退"""

    def test_extinf_and_segments_count(self):
        content = ("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:5\n"
                   "#EXTINF:5.0,\nseg0.ts\n#EXTINF:5.0,\nseg1.ts\n#EXT-X-ENDLIST\n")
        segs, key = _dl()._parse_media_playlist(content, "https://cdn.example.com/a/play.m3u8")
        self.assertEqual(len(segs), 2, "应解析出 2 个分片")
        self.assertIsNone(key, "无加密时 key 应为 None")

    def test_aes_key_line_parsed(self):
        content = ('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x00000000000000000000000000000007\n'
                   '#EXTINF:5.0,\nseg0.ts\n')
        segs, key = _dl()._parse_media_playlist(content, "https://cdn.example.com/a/play.m3u8")
        self.assertEqual(len(segs), 1)
        self.assertIsNotNone(key, "AES-128 密钥行应被解析")
        self.assertTrue(key[0].startswith("https://cdn.example.com/a/key.bin"),
                        "密钥 URL 应相对解析为绝对")

    def test_private_segments_filtered(self):
        """v2.19 SSRF 闸：私网分片必须被过滤（此前无校验）"""
        content = ("#EXTM3U\n#EXTINF:5.0,\nhttp://169.254.169.254/seg.ts\n"
                   "#EXTINF:5.0,\nseg_ok.ts\n")
        segs, _ = _dl()._parse_media_playlist(content, "https://cdn.example.com/a/play.m3u8")
        self.assertEqual(len(segs), 1, "私网分片应被过滤")
        self.assertIn("seg_ok.ts", segs[0])

    def test_relative_key_with_private_target_rejected(self):
        """私网密钥 URL 应被拒绝（key 置空而非指向内网）"""
        content = ('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="http://127.0.0.1/key"\n'
                   '#EXTINF:5.0,\nseg0.ts\n')
        _segs, key = _dl()._parse_media_playlist(content, "https://cdn.example.com/a/play.m3u8")
        self.assertIsNone(key, "私网密钥必须被拒绝")


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


if __name__ == "__main__":
    unittest.main()
