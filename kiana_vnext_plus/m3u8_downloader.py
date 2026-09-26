import asyncio
import aiofiles
import logging
import re
import uuid
import shutil
from pathlib import Path
from urllib.parse import urljoin

try:
    from Crypto.Cipher import AES
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

logger = logging.getLogger(__name__)


def sanitize_video_filename(name: str) -> str:
    """清理视频文件名中的非法字符"""
    return re.sub(r'[<>:"/\\|?*]', '_', name)[:200]


class M3U8Downloader:
    """M3U8 视频下载器，支持加密流、GPU 加速合并、软件回退"""

    def __init__(self, concurrency=5, proxy_func=None, gpu_acceleration=True):
        self.concurrency = concurrency
        self.proxy_func = proxy_func
        self.gpu_acceleration = gpu_acceleration
        self.session = None

    async def init_session(self):
        # [FIXED & MODIFIED] curl_cffi 替换 aiohttp（本机 aiohttp 外网全超时）
        from curl_cffi.requests import AsyncSession
        self.session = AsyncSession(timeout=120)

    @staticmethod
    def _blocked(url) -> bool:
        """[v2.19 安全 P0] m3u8 通道 SSRF 闸。分片/密钥 URL 由 `urljoin(base, line)`
        从**远端播放列表**生成——恶意 m3u8 可指向任意主机。

        [v2.19.6 修复·审查发现] 原来用 `dns_check=False`（只判字面），实测
        `http://localtest.me/seg.ts`（公开 DNS 解析到 127.0.0.1）可**穿过**闸。
        原性能理由不成立：`url_utils._HOST_CACHE` 已按 host 缓存，真实播放列表的
        不同 host 通常只有 1–3 个（分片多为同源或少数 CDN 子域）——成本是
        "每 distinct host 一次解析"，而非"每分片一次"。改为 `dns_check=True`。"""
        try:
            from .url_utils import is_private_url
            if not url or not str(url).startswith(("http://", "https://")):
                return True
            return bool(is_private_url(str(url), dns_check=True))
        except Exception as e:
            # 安全谓词自身失败必须留痕（原静默 return False → 闸失效无人知晓）
            try:
                logger.warning(f"SSRF 闸不可用（url_utils 导入/判定失败）: {e}")
            except Exception:
                pass
            return False

    async def download(self, m3u8_url, headers, output_path):
        # [v2.19 安全 P0] 入口闸：播放列表 URL 强校验（含 DNS）
        try:
            from .url_utils import is_private_url as _ipu
            if not m3u8_url or _ipu(str(m3u8_url), dns_check=True):
                logger.warning(f"M3U8 拦截（SSRF/协议闸）: {str(m3u8_url)[:60]}")
                return False
        except Exception:
            if self._blocked(m3u8_url):
                return False
        if not self.session:
            await self.init_session()
        # [FIXED & MODIFIED] v2.11 整次重试：原单次失败即弃（网络抖动导致整体失败）
        last_err = None
        for _attempt in range(2):
            try:
                if await self._download_once(m3u8_url, headers, output_path):
                    return True
                last_err = RuntimeError("segments incomplete")
            except Exception as e:
                last_err = e
            logger.warning(f"M3U8 attempt {_attempt + 1}/2 failed: {last_err}")
        logger.error(f"M3U8 download failed after retries: {last_err}")
        return False

    async def _download_once(self, m3u8_url, headers, output_path):
        playlist = await self._fetch_text(m3u8_url, headers)
        if not playlist:
            return False
        # [FIXED & MODIFIED] v2.14 播放列表大小上限（异常大列表本身即攻击向量）
        if len(playlist) > 4 * 1024 * 1024:
            logger.warning(f"m3u8 播放列表超 4MB，拒绝: {m3u8_url[:60]}")
            return False
        if '#EXT-X-STREAM-INF' in playlist:
            playlist = await self._select_best_stream(playlist, headers, m3u8_url)
        segments, key_info = self._parse_media_playlist(playlist, m3u8_url)
        if not segments:
            return False
        # [FIXED & MODIFIED] v2.14 阶段3 磁盘炸弹闸：恶意 m3u8 列百万段 → 限 5000 段
        if len(segments) > 5000:
            logger.warning(f"m3u8 段数超限({len(segments)} > 5000)，拒绝下载: {m3u8_url[:60]}")
            return False
        if key_info and not HAS_CRYPTO:
            logger.error(f"Video {m3u8_url} is encrypted but pycryptodome is not installed. Aborting.")
            return False

        # [FIXED & MODIFIED] v2.11 每次任务独立 temp 目录（uuid 后缀）：原固定目录名会让
        # 上次失败残留的旧段混入新合并，产出新旧段混合的损坏视频
        temp_dir = Path(output_path).parent / f"tmp_{sanitize_video_filename(Path(output_path).stem)}_{uuid.uuid4().hex[:8]}"
        temp_dir.mkdir(exist_ok=True)
        try:
            semaphore = asyncio.Semaphore(self.concurrency)
            tasks = [
                asyncio.create_task(self._download_segment(semaphore, seg, temp_dir / f"{i:05d}.ts", headers))
                for i, seg in enumerate(segments)
            ]
            # [FIXED & MODIFIED] v2.14 return_exceptions：单段抛异常不再让 gather 立即上抛
            # （其余段变孤儿继续跑且往被 finally rmtree 的目录里写文件——损坏视频根因之一）
            results = await asyncio.gather(*tasks, return_exceptions=True)
            _failed = sum(1 for r in results if r is not True)
            if _failed:
                raise RuntimeError(f"{_failed}/{len(results)} segments failed")

            if key_info and HAS_CRYPTO:
                key_url, iv = key_info
                key = await self._fetch_bytes(key_url, headers)
                if key:
                    for i, seg_file in enumerate(sorted(temp_dir.glob("*.ts"))):
                        await self._decrypt_file(seg_file, key, iv, i)

            await self._merge_segments(temp_dir, output_path)
            return True
        finally:
            # [FIXED & MODIFIED] v2.11 失败路径也清理（原仅成功路径 rmtree → 半成品 .ts 永久残留）
            shutil.rmtree(temp_dir, ignore_errors=True)

    async def _fetch_text(self, url, headers):
        if self._blocked(url):   # [v2.19 安全] 取流闸（master/子播放列表都经此处）
            logger.warning(f"M3U8 取流拦截（SSRF）: {str(url)[:60]}")
            return ""
        proxy = await self._get_proxy()
        try:
            # [FIXED & MODIFIED] curl_cffi 语法（resp.text 非 await）
            # [v2.19.7 安全·扫描发现] 走 safe_get（逐跳复检落点）：
            # m3u8 的取流此前只在入口判字面/DNS，302 到内网会被 libcurl 自行跟随
            from .url_utils import safe_get as _safe_get
            resp = await _safe_get(self.session, url, headers, proxy=proxy, timeout=60)
            if resp is None:
                return ""
            return resp.text if resp.status_code == 200 else ""
        except Exception:
            return ""

    async def _fetch_bytes(self, url, headers):
        if self._blocked(url):   # [v2.19 安全] 密钥等二进制取流闸
            logger.warning(f"M3U8 取流拦截（SSRF）: {str(url)[:60]}")
            return b""
        proxy = await self._get_proxy()
        try:
            # [FIXED & MODIFIED] curl_cffi 语法（resp.content 非 await resp.read()）
            from .url_utils import safe_get as _safe_get
            resp = await _safe_get(self.session, url, headers, proxy=proxy, timeout=60)
            if resp is None:
                return b""
            return resp.content if resp.status_code == 200 else b""
        except Exception:
            return b""

    async def _get_proxy(self):
        if self.proxy_func:
            return await self.proxy_func()
        return None

    def _parse_media_playlist(self, content, base_url):
        segments, key_info = [], None
        lines = content.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line.startswith('#EXT-X-KEY'):
                # 修复：先截取 #EXT-X-KEY: 之后的内容，再按逗号分割
                # 原来 line.split(',')[1:] 会跳过 METHOD 属性（它在前缀中）
                key_content = line.split(':', 1)[1] if ':' in line else ''
                attrs = {}
                for attr in key_content.split(','):
                    parts = attr.split('=', 1)
                    if len(parts) == 2:
                        k, v = parts
                        attrs[k.strip()] = v.strip().strip('"')
                if attrs.get('METHOD') == 'AES-128':
                    key_uri = attrs.get('URI')
                    iv = attrs.get('IV')
                    if key_uri:
                        _ku = urljoin(base_url, key_uri)
                        # [v2.19 安全] 密钥 URL 同样过闸（密钥可被指向内网）
                        if not self._blocked(_ku):
                            key_info = (_ku, iv)
            elif not line.startswith('#'):
                _seg = urljoin(base_url, line)
                # [v2.19 安全] 分片 URL 过闸：跨域到私网的直接丢弃（同源/CDN 子域放行）
                if not self._blocked(_seg):
                    segments.append(_seg)
            i += 1
        return segments, key_info

    async def _select_best_stream(self, master, headers, base_url):
        best_bw, best_url = 0, None
        lines = master.splitlines()
        for i, line in enumerate(lines):
            if line.startswith('#EXT-X-STREAM-INF'):
                bw = 0
                for attr in line.split(',')[1:]:
                    if attr.startswith('BANDWIDTH='):
                        # 修复：使用 split('=', 1) 防止截断
                        parts = attr.split('=', 1)
                        if len(parts) == 2:
                            bw = int(parts[1])
                # 修复：检查 i+1 是否越界
                if bw > best_bw and i + 1 < len(lines):
                    best_bw = bw
                    best_url = urljoin(base_url, lines[i + 1].strip())
        if best_url:
            return await self._fetch_text(best_url, headers)
        return ""

    async def _download_segment(self, semaphore, url, dest, headers):
        async with semaphore:
            # [v2.19 安全] 分片下载兜底闸（解析处已过滤，此处防其他调用路径）
            if self._blocked(url):
                logger.warning(f"M3U8 分片拦截（SSRF）: {str(url)[:60]}")
                return False
            # [FIXED & MODIFIED] v2.11 已存在非空段跳过（同次任务重跑时避免重复下载）
            try:
                if dest.exists() and dest.stat().st_size > 0:
                    return True
            except Exception:
                pass
            proxy = await self._get_proxy()
            for _ in range(3):
                try:
                    # [FIXED & MODIFIED] curl_cffi 语法
                    # [v2.19.7 安全] 分片下载同样走 safe_get（逐跳复检落点）
                    from .url_utils import safe_get as _safe_get
                    resp = await _safe_get(self.session, url, headers, proxy=proxy, timeout=60)
                    if resp is None:
                        return False          # 被 SSRF 闸拦 → 不做无谓重试
                    if resp.status_code == 200:
                        data = resp.content
                        async with aiofiles.open(dest, 'wb') as f:
                            await f.write(data)
                        return True
                except Exception:
                    await asyncio.sleep(1)
            return False

    async def _decrypt_file(self, path, key, iv_str, index):
        if not HAS_CRYPTO:
            return
        # 修复：IV 解析防御性检查
        if iv_str:
            iv_hex = iv_str[2:] if iv_str.startswith('0x') else iv_str
            try:
                iv = bytes.fromhex(iv_hex)
            except ValueError:
                iv = index.to_bytes(16, 'big')
        else:
            iv = index.to_bytes(16, 'big')
        cipher = AES.new(key, AES.MODE_CBC, iv)
        async with aiofiles.open(path, 'rb') as f:
            encrypted = await f.read()
        decrypted = cipher.decrypt(encrypted)
        # 修复：PKCS7 去填充前验证 padding
        if decrypted:
            pad_len = decrypted[-1]
            if 1 <= pad_len <= 16 and all(b == pad_len for b in decrypted[-pad_len:]):
                decrypted = decrypted[:-pad_len]
        async with aiofiles.open(path, 'wb') as f:
            await f.write(decrypted)

    async def _merge_segments(self, temp_dir, output_path):
        filelist = temp_dir / "filelist.txt"
        # [v2.18 P1-10] 必须显式 utf-8：Windows 默认 GBK 写中文分片路径 →
        # ffmpeg 读 filelist 乱码找不到分片，合并崩
        with open(filelist, 'w', encoding='utf-8') as f:
            for ts in sorted(temp_dir.glob("*.ts")):
                f.write(f"file '{ts.absolute()}'\n")

        # GPU 加速合并：自动检测可用编码器并选择最佳方案
        # 优先级：NVENC > QuickSync > AMF > CPU copy
        # [FIXED & MODIFIED] v2.14 编码器探测结果缓存（原每次合并都跑 ffmpeg -encoders
        # 子进程，同步阻塞事件循环最坏 10s → 进程级只探测一次 + 挪线程池）
        if self.gpu_acceleration:
            encoder = await asyncio.to_thread(self._detect_gpu_encoder_cached)
            if encoder:
                cmd = ["ffmpeg", "-y", "-hwaccel", encoder["hwaccel"], "-f", "concat",
                       "-safe", "0", "-i", str(filelist), "-c:v", encoder["codec"],
                       "-preset", "p6", "-b:v", "5M", str(output_path)]
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                _, stderr = await proc.communicate()
                if proc.returncode == 0:
                    return
                logger.warning(
                    f"FFmpeg GPU merge ({encoder['name']}) failed, "
                    f"falling back to software copy: {stderr.decode()[:200]}"
                )

        # 软件 CPU 合并（核心功能保留：适配消费级设备）
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(filelist),
               "-c", "copy", str(output_path)]
        proc2 = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, stderr2 = await proc2.communicate()
        if proc2.returncode != 0:
            raise RuntimeError(f"M3U8 merge failed: {stderr2.decode()[:500]}")

    @staticmethod
    def _detect_gpu_encoder():
        return M3U8Downloader._detect_gpu_encoder_cached()

    _gpu_encoder_cache = None

    @classmethod
    def _detect_gpu_encoder_cached(cls):
        """[v2.14] 进程级缓存：硬件编码器进程内不会变，探测一次即可"""
        if cls._gpu_encoder_cache is not None:
            return cls._gpu_encoder_cache if cls._gpu_encoder_cache != "none" else None
        enc = cls._detect_gpu_encoder_impl()
        cls._gpu_encoder_cache = enc if enc else "none"
        return enc

    @staticmethod
    def _detect_gpu_encoder_impl():
        """检测系统可用的 GPU 硬件编码器，返回 {'name','hwaccel','codec'} 或 None"""
        import subprocess as _sp
        try:
            result = _sp.run(
                ["ffmpeg", "-hide_banner", "-encoders"],
                capture_output=True, text=True, timeout=10
            )
            encoders = result.stdout
            # 按优先级检测
            if "h264_nvenc" in encoders:
                return {"name": "NVENC", "hwaccel": "cuda", "codec": "h264_nvenc"}
            if "h264_qsv" in encoders:
                return {"name": "Intel QuickSync", "hwaccel": "qsv", "codec": "h264_qsv"}
            if "h264_amf" in encoders:
                return {"name": "AMD AMF", "hwaccel": "d3d11va", "codec": "h264_amf"}
        except Exception as e:
            logger.debug(f"GPU encoder detection failed: {e}")
        return None

    async def close(self):
        if self.session:
            await self.session.close()
