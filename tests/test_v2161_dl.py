"""Kiana Vnext Plus — v2.16.1 阶段2 下载链路回归

覆盖：PO Token server runtime（ready 探测/ensure 幂等拉起，绝不安装任何东西）；
yt-dlp 代理透传约定；4K/2K 音轨组合（_quality_for）；断点续传（probe_url 握手 +
.part+Range 流式 + 字节校验 + 原子改名）。
"""
import sys, os, asyncio, json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault('KIANA_CRYPTO_KEY', 'kv_test')

from kiana_vnext_plus import universal_downloader as ud
from kiana_vnext_plus.universal_downloader import (
    pot_server_ready, pot_server_ensure, probe_url, stream_to_file,
)
from kiana_vnext_plus.crawler import _quality_for


class TestQualityForAudioFix:
    """v2.16.1：4K/2K 原映射为纯 bestvideo[height<=N] → 无声；改组合选择器。"""

    def test_4k_has_audio(self):
        q = _quality_for("4k")
        assert "bestaudio" in q and "height<=2160" in q

    def test_2k_has_audio(self):
        q = _quality_for("2k")
        assert "bestaudio" in q and "height<=1440" in q

    def test_1080_is_single_file(self):
        assert _quality_for("1080") == "best[height<=1080]"

    def test_default_best(self):
        assert _quality_for("") == "best"
        assert _quality_for("highest") == "best"


class TestPotServerRuntime:
    def test_ready_true_when_tcp_ok(self, monkeypatch):
        class _Conn:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr("socket.create_connection", lambda *a, **k: _Conn())
        assert pot_server_ready() is True

    def test_ready_false_when_refused(self, monkeypatch):
        def _raise(*a, **k):
            raise OSError("refused")
        monkeypatch.setattr("socket.create_connection", _raise)
        assert pot_server_ready() is False

    def test_ensure_spawns_node_server(self, monkeypatch, tmp_path):
        # 构造候选目录（含 build/main.js）
        srv = tmp_path / "pot_server"
        (srv / "build").mkdir(parents=True)
        (srv / "build" / "main.js").write_text("console.log('x')", encoding="utf-8")
        monkeypatch.setattr(ud, "_pot_server_dirs", lambda: [srv])
        monkeypatch.setattr("shutil.which", lambda name: "C:/fake/node.exe")
        monkeypatch.setattr(ud, "_pot_proc", None)
        called = {}

        class _FakeProc:
            def poll(self):
                return None

            def terminate(self):
                pass

        class _FakePopen:
            def __init__(self, args, **kw):
                called["args"] = args
                called["kw"] = kw
                self.proc = _FakeProc()

            def poll(self):
                return self.proc.poll()

            def terminate(self):
                pass

        monkeypatch.setattr("subprocess.Popen", _FakePopen)
        seq = iter([False, False, True])  # 首查未起 → 拉起后循环探测

        def _ready_seq():
            return next(seq, True)

        monkeypatch.setattr(ud, "pot_server_ready", _ready_seq)
        monkeypatch.setattr(ud.time, "sleep", lambda s: None)
        assert pot_server_ensure() is True
        assert "node.exe" in str(called["args"][0])
        assert str(srv / "build" / "main.js") in str(called["args"][1])
        assert called["kw"]["cwd"] == str(srv)

    def test_ensure_returns_false_without_node(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ud, "_pot_server_dirs", lambda: [tmp_path])
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr(ud, "pot_server_ready", lambda: False)  # 隔离本机真实端口
        monkeypatch.setattr(ud, "_pot_proc", None)
        assert pot_server_ensure() is False  # 不安装/不下载任何东西，仅告警


class TestProbeUrl:
    def test_head_range_parses_total(self):
        class _Resp:
            status_code = 206
            headers = {"Content-Range": "bytes 0-4/12345"}

            async def aclose(self):
                pass

        class _Session:
            async def head(self, url, headers=None, timeout=60, stream=True, **k):
                return _Resp()

            async def get(self, *a, **k):
                raise AssertionError("不应走到 GET")

        async def flow():
            return await probe_url(_Session(), "http://x/f.bin", {}, timeout=10)

        total, resumable = asyncio.run(flow())
        assert total == 12345 and resumable is True

    def test_content_length_fallback(self):
        class _Resp:
            status_code = 200
            headers = {"Content-Length": "999", "Accept-Ranges": "bytes"}

            async def aclose(self):
                pass

        class _Session:
            async def head(self, url, headers=None, timeout=60, stream=True, **k):
                return _Resp()

        async def flow():
            return await probe_url(_Session(), "http://x/f.bin", {}, timeout=10)

        total, resumable = asyncio.run(flow())
        assert total == 999 and resumable is True


class TestStreamToFileResume:
    """断点续传核心：.part 存在 → Range 续传 → 字节校验 → 原子改名。"""

    def test_first_download(self, tmp_path):
        data = b"hello world" * 100

        class _Resp:
            status_code = 200
            headers = {}

            async def aiter_content(self):
                for i in range(0, len(data), 500):
                    yield data[i:i + 500]

            def close(self):
                pass

        class _Session:
            async def get(self, url, headers=None, proxy=None, timeout=180, stream=True, **k):
                assert "Range" not in (headers or {})  # 首下无 Range
                return _Resp()

        async def flow():
            return await stream_to_file(_Session(), "http://x/f.bin", tmp_path / "f.bin", {})
        out = asyncio.run(flow())
        assert out and out.stat().st_size == len(data)
        assert out.read_bytes() == data
        assert not (tmp_path / "f.bin.part").exists()  # 成功即无 .part

    def test_resume_from_half(self, tmp_path):
        data = b"0123456789" * 1000  # 10000 字节
        part = tmp_path / "f.bin.part"
        part.write_bytes(data[:5000])  # 已下 5000 字节

        class _Resp:
            status_code = 206
            headers = {}

            async def aiter_content(self):
                yield data[5000:7500]
                yield data[7500:]

            def close(self):
                pass

        class _Session:
            async def get(self, url, headers=None, proxy=None, timeout=180, stream=True, **k):
                assert headers.get("Range") == "bytes=5000-"  # 续传必须带 Range
                return _Resp()

        async def flow():
            return await stream_to_file(_Session(), "http://x/f.bin",
                                        tmp_path / "f.bin", {}, expect_total=len(data))
        out = asyncio.run(flow())
        assert out and out.stat().st_size == len(data)
        assert out.read_bytes() == data[:5000] + data[5000:] == data
        assert not part.exists()

    def test_size_mismatch_keeps_part(self, tmp_path):
        part = tmp_path / "f.bin.part"
        part.write_bytes(b"ab")

        class _Resp:
            status_code = 206
            headers = {}

            async def aiter_content(self):
                yield b"cd"  # 加上已有 = 4 字节 ≠ expect_total 5

            def close(self):
                pass

        class _Session:
            async def get(self, url, headers=None, proxy=None, timeout=180, stream=True, **k):
                return _Resp()

        async def flow():
            return await stream_to_file(_Session(), "http://x/f.bin",
                                        tmp_path / "f.bin", {}, expect_total=5, retries=1)
        assert asyncio.run(flow()) is None
        assert part.exists() and part.read_bytes() == b"abcd"  # 保留 .part 供续传

    def test_mirror_fallback_on_primary_failure(self, tmp_path):
        """[v2.17 3.2] 主源失败（网络错/非2xx）→ 自动切镜像下载成功"""
        data = b"mirror-data" * 200
        calls = []

        class _RespOK:
            status_code = 200
            headers = {}

            async def aiter_content(self):
                yield data[:len(data) // 2]
                yield data[len(data) // 2:]

            def close(self):
                pass

        class _Session:
            async def get(self, url, headers=None, proxy=None, timeout=180, stream=True, **k):
                calls.append(url)
                if "primary" in url:
                    raise RuntimeError("primary down")
                return _RespOK()

        async def flow():
            return await stream_to_file(_Session(), "http://x/primary.bin",
                                        tmp_path / "m.bin", {}, retries=1,
                                        mirrors=("http://x/mirror1.bin", "http://x/mirror2.bin"))
        out = asyncio.run(flow())
        assert out and out.read_bytes() == data
        assert calls == ["http://x/primary.bin", "http://x/mirror1.bin"]  # 主失败→第一个镜像成功即停

    def test_mirror_all_fail_returns_none(self, tmp_path):
        class _Session:
            async def get(self, url, headers=None, proxy=None, timeout=180, stream=True, **k):
                raise RuntimeError("all down")

        async def flow():
            return await stream_to_file(_Session(), "http://x/a.bin",
                                        tmp_path / "n.bin", {}, retries=1,
                                        mirrors=("http://x/b.bin",))
        assert asyncio.run(flow()) is None  # 全部失败 → None（.part 保留语义不变）


class TestDownloadFileResumeIntegration:
    """download_file 全链（假 session）：探测 → 续传 → 原子落盘；无压缩/无转码（字节相等）。"""

    def test_byte_identical(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiana_vnext_plus.universal_downloader.is_private_url",
                            lambda u, **kw: False, raising=False)
        data = bytes(range(256)) * 40  # 10240 字节
        state = {"first": True}

        class _Resp:
            def __init__(self, status_code, headers):
                self.status_code = status_code
                self.headers = headers

            async def aiter_content(self):
                yield data[:5000]
                yield data[5000:]

            async def aclose(self):
                pass

            def close(self):
                pass

        class _Session:
            async def head(self, url, headers=None, timeout=60, stream=True, **k):
                return _Resp(206, {"Content-Range": f"bytes 0-4/{len(data)}"})

            async def get(self, url, headers=None, proxy=None, timeout=180, stream=True, **k):
                return _Resp(200, {}) if state["first"] else _Resp(200, {})

        udl = ud.UniversalDownloader(tmp_path / "out", max_concurrent=4)
        udl.session = _Session()

        async def flow():
            out = await udl.download_file("http://files.example.test/awesome.bin")
            return out

        out = asyncio.run(flow())
        assert out and out.exists()
        assert out.read_bytes() == data, "下载字节必须与原文件完全相等（原画质红线 R1）"
        assert not Path(str(out) + ".part").exists()


class TestWallpaperOriginUntouched:
    """红线 R3/R8：wallpaper.process 纯内存衍生（裁切/模糊/暗化只在 QImage），
    绝不回写源图——原图字节与 mtime 必须保持不变。"""

    def test_process_does_not_touch_src(self, tmp_path):
        import cv2
        import numpy as np
        from kiana_vnext_plus.wallpaper import process
        p = tmp_path / "src.jpg"
        img = np.zeros((600, 400, 3), dtype=np.uint8)
        img[:] = (120, 130, 140)
        cv2.imwrite(str(p), img)
        before = (p.read_bytes(), p.stat().st_mtime_ns)
        out = process(str(p), 800, 600, focus=1, blur=0, dim=0.2, theme="dark")
        assert out is not None
        assert (p.read_bytes(), p.stat().st_mtime_ns) == before


class TestYtChainSelfConsistency:
    """[v2.17 安全/可用性收尾] YT 链路离线自洽：换 IP 后零改动可用的固化——
    PO server 可探测、插件装载、opts 构造含代理/断点/插件依赖键。"""

    def test_opts_include_proxy_and_resume(self, tmp_path, monkeypatch):
        import os, sys, types, asyncio, pathlib as _pl
        from kiana_vnext_plus.universal_downloader import UniversalDownloader
        captured = {}

        class _FakeYdl:
            def __init__(self, opts):
                captured["opts"] = opts.copy()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, url, download=True):
                return {"url": url}

            def prepare_filename(self, info):
                return ""

        # 直接注入模块级假 yt_dlp（download_video 内 import yt_dlp）
        fake = types.ModuleType("yt_dlp")
        fake.YoutubeDL = _FakeYdl
        monkeypatch.setitem(sys.modules, "yt_dlp", fake)
        udl = UniversalDownloader(tmp_path / "out", max_concurrent=1)
        asyncio.run(udl.download_video("https://www.youtube.com/watch?v=aqz-KE-bpKQ",
                                       quality="best", proxy="http://clean-proxy:8080",
                                       want_subs=False, want_thumb=False, want_infojson=False))
        opts = captured["opts"]
        assert opts.get("proxy") == "http://clean-proxy:8080"   # 代理透传自洽
        assert opts.get("continuedl") is True                    # 断点续传基线键
        assert opts.get("ignoreconfig") is True                  # 不受外部配置干扰

    def test_pot_plugin_available(self):
        import importlib.util
        # bgutil 插件默认连 127.0.0.1:4416——pip pin 包的插件通道即 YT 链路前提
        spec = importlib.util.find_spec("yt_dlp_plugins.extractor.getpot_bgutil_http")
        assert spec is not None, "bgutil 插件必须可被 yt-dlp 发现（链路上游）"

    def test_pot_server_probe_contract(self):
        from kiana_vnext_plus.universal_downloader import pot_server_ready
        assert isinstance(pot_server_ready(), bool)  # 探测契约（真值取决于本机 4416）


class TestOuttmplAbsolute:
    """[v2.17 2.11] out_tmpl 绝对化防御：yt-dlp 相对 outtmpl 会与 paths.home 二次拼接
    （out/videos/out/videos/...）——CLI -o 传相对路径时视频落盘但找不到。"""

    def test_relative_video_dir_outtmpl_has_no_dup_prefix(self, tmp_path, monkeypatch):
        import os, sys, types, asyncio
        from kiana_vnext_plus.universal_downloader import UniversalDownloader
        captured = {}
        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            udl = UniversalDownloader(Path("out"), max_concurrent=1)  # 相对输出目录
            fake = types.ModuleType("yt_dlp")

            class _FakeYdl:
                def __init__(self, opts):
                    captured["opts"] = opts.copy()

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def extract_info(self, url, download=True):
                    return {"url": url}

                def prepare_filename(self, info):
                    return ""

            fake.YoutubeDL = _FakeYdl
            sys.modules["yt_dlp"] = fake
            try:
                asyncio.run(udl.download_video(
                    "https://example.com/v.mp4", quality="best",
                    want_subs=False, want_thumb=False, want_infojson=False))
            finally:
                sys.modules.pop("yt_dlp", None)
        finally:
            os.chdir(old_cwd)
        tpl = captured["opts"]["outtmpl"].replace("\\", "/")
        assert "out/videos/out" not in tpl, f"outtmpl 不得双前缀: {tpl}"
        assert "out/videos" in tpl, f"outtmpl 应含 out/videos: {tpl}"
