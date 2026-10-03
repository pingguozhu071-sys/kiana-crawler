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


    def test_master_playlist_picks_variant_when_bandwidth_is_first_attr(self):
        """**行为**：master 里 `BANDWIDTH=` 是**第一个属性**时必须能选出变体

        真机形态就是 `#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=...`。
        原实现用 `line.split(',')[1:]`，**没先切掉标签前缀** ⇒ 第一个属性（也就是
        BANDWIDTH）被整体丢掉 ⇒ bw 恒 0 ⇒ 选不出任何变体 ⇒ 直接返回 ""
        （整条 master 播放列表兜底从来下不动）。
        """
        import asyncio
        from unittest.mock import patch

        master = ("#EXTM3U\n"
                  "#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\nlow/index.m3u8\n"
                  "#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1920x1080\nhigh/index.m3u8\n")
        seen = []

        async def _fake_fetch_text(url, headers, *a, **kw):
            seen.append(url)
            return "#EXTM3U\n#EXTINF:5.0,\nseg.ts\n"

        dl = _dl()
        with patch.object(dl, "_fetch_text", _fake_fetch_text):
            got = asyncio.new_event_loop().run_until_complete(
                dl._select_best_stream(master, {}, "https://cdn.example.com/a/master.m3u8"))

        self.assertTrue(seen, "一个变体都没被请求 ⇒ master 兜底仍然是死的")
        self.assertIn("high/index.m3u8", seen[0], f"选的不是最高带宽那个：{seen}")
        self.assertTrue(got, "应返回变体播放列表正文")

    def test_key_unavailable_refuses_to_merge_ciphertext(self):
        """**行为**：声明 AES-128 却取不到密钥时**必须拒绝合并**（不许产出密文 .mp4）

        原来 `if key:` 没有 else ⇒ 取不到密钥照样 `_merge_segments` + `return True`
        ⇒ 产物是 AES 密文拼接块，而调用方判据（>1MB / 后缀 / 头非 HTML）**密文全过**
        ⇒ 记 completed。这是本工程最忌的假成功。
        """
        import asyncio
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        media = ("#EXTM3U\n"
                 '#EXT-X-KEY:METHOD=AES-128,URI="https://cdn.example.com/key.bin"\n'
                 "#EXTINF:5.0,\nseg0.ts\n")
        calls = {"key": 0, "merge": 0}

        async def _fake_fetch_text(url, headers, *a, **kw):
            return media

        async def _fake_fetch_bytes(url, headers, *a, **kw):
            calls["key"] += 1
            return b""          # 403/超时/异常都会被 `_fetch_bytes` 吞成这个

        async def _fake_seg(semaphore, url, dest, headers):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"\x00" * 64)
            return True

        async def _fake_merge(temp_dir, output_path):
            calls["merge"] += 1
            Path(output_path).write_bytes(b"merged")

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            out = Path(td) / "out.mp4"
            dl = _dl()
            # `_dl()` 是**裸实例**（`__new__`，故意不初始化 session）⇒ 这里补上
            # `_download_once` 唯一需要的那一个字段（段并发度）
            dl.concurrency = 2
            with patch.object(dl, "_fetch_text", _fake_fetch_text), \
                    patch.object(dl, "_fetch_bytes", _fake_fetch_bytes), \
                    patch.object(dl, "_download_segment", _fake_seg), \
                    patch.object(dl, "_merge_segments", _fake_merge):
                ok = asyncio.new_event_loop().run_until_complete(
                    dl._download_once("https://cdn.example.com/a/play.m3u8", {}, out))

            # 非空断言：证明**真的走到了取密钥那一步**（否则本用例会"因为别的原因"绿）
            self.assertTrue(calls["key"], "没走到取密钥那一步（用例失效：可能没装 pycryptodome）")
            self.assertFalse(ok, "取不到密钥却返回了成功")
            self.assertEqual(calls["merge"], 0, "取不到密钥却仍然合并了密文")
            self.assertFalse(out.exists(), "产出了放不了的密文 .mp4")


    def test_merge_is_zero_transcode_unless_copy_fails(self):
        """**行为**：合并**主路径必须是 `-c copy`**（零转码红线）；重编码只在拷贝失败后才跑

        原来是反的：先跑 GPU **重编码**（`-c:v …nvenc -b:v 5M`），只有它失败才退回
        `-c copy` ⇒ 正常路径**静默把画质压到 5 Mbps**。而 `concat` 合并本来不需要编码
        —— `-c copy` 是流拷贝（I/O 级），重编码反而更慢。
        """
        import asyncio
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        def _scenario(copy_rc):
            cmds = []

            class _P:
                def __init__(self, rc):
                    self.returncode = rc

                async def communicate(self):
                    return b"", b"boom"

            async def _fake_exec(*argv, **kw):
                cmds.append([str(a) for a in argv])
                return _P(copy_rc if len(cmds) == 1 else 0)

            async def _flow(tmp, out):
                dl = _dl()
                dl.gpu_acceleration = True
                with patch("kiana_vnext_plus.m3u8_downloader.asyncio.create_subprocess_exec",
                           _fake_exec), \
                        patch.object(dl, "_detect_gpu_encoder_cached",
                                     lambda: {"name": "nv", "hwaccel": "cuda",
                                              "codec": "h264_nvenc"}):
                    await dl._merge_segments(tmp, out)

            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
                tmp = Path(td) / "segs"
                tmp.mkdir()
                (tmp / "00000.ts").write_bytes(b"\x00" * 8)
                asyncio.new_event_loop().run_until_complete(_flow(tmp, Path(td) / "o.mp4"))
            return cmds

        ok_cmds = _scenario(0)
        self.assertEqual(len(ok_cmds), 1, f"主路径成功就不该再跑第二条 ffmpeg：{ok_cmds}")
        self.assertIn("copy", ok_cmds[0], f"主路径不是流拷贝（零转码红线）：{ok_cmds[0]}")

        fb_cmds = _scenario(1)
        self.assertGreaterEqual(len(fb_cmds), 2, f"拷贝失败后应有重编码兜底：{fb_cmds}")
        self.assertIn("copy", fb_cmds[0], f"第一条仍必须是流拷贝：{fb_cmds[0]}")
        self.assertIn("h264_nvenc", fb_cmds[1], f"兜底没走重编码：{fb_cmds[1]}")


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


if __name__ == "__main__":
    unittest.main()
