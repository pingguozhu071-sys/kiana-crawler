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
        # [v6 补齐] 必须显式 impersonate —— 与 media_downloader / universal_downloader 同因。
        # 这三处 `init_session` 是**同一段代码的三份拷贝**，v2.19.8 只修了那两处，
        # **漏了这一份**：不传 impersonate 时 curl_cffi 落在"不模拟指纹"的默认档，
        # 既慢（实测同 CDN 同代理 4.7-8.2 MB/s vs 带指纹 12-25 MB/s），
        # 又与协议通道的 chrome136 指纹**不一致**——混用指纹本身就是可检测信号。
        # m3u8 分片下载恰恰是防盗链/反爬盯得最紧的一条链。
        from curl_cffi.requests import AsyncSession
        from .fingerprint_consistency import TLS_IMPERSONATE_POOL
        self.session = AsyncSession(timeout=120, impersonate=TLS_IMPERSONATE_POOL[0])

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
                if not key:
                    # [v2.19.9 修复] **绝不合并未解密的密文。**
                    # 原来 `if key:` **没有 else** ⇒ 取不到密钥也照走 `_merge_segments` 并
                    # `return True` ⇒ 产物是 AES 密文拼接块（**完全放不了**），而调用方的
                    # 三道判据（>1MB / 后缀 .mp4 / 头 256 字节非 HTML）**密文全过**
                    # ⇒ 记 completed 并带一个"看起来合理"的 file_size。
                    # 这正是本工程最忌的**假成功**：宁可如实失败，也不要一个放不了的 .mp4。
                    # 触发面是**现实路径**：密钥 403 / 超时 / 异常 —— `_fetch_bytes` 会把它们
                    # 全部吞成 `b""`。
                    logger.error(
                        f"m3u8 声明 AES-128 但密钥取不到（{str(key_url)[:60]}）——"
                        f"拒绝合并未解密的密文：本次如实记失败，不产出放不了的 .mp4")
                    return False
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
                # [v2.19.9 修复·**孪生处漏**] 原来写的是 `line.split(',')[1:]` ——
                # 标签前缀 `#EXT-X-STREAM-INF:` **没有被先切掉** ⇒ **第一个属性被整体丢掉**，
                # 而真实 master 播放列表里 `BANDWIDTH=` 几乎总是第一个属性 ⇒ `bw` 恒为 0
                # ⇒ `if bw > best_bw` 永不成立 ⇒ `best_url` 永远是 None ⇒
                # **整条 master 播放列表兜底从来下不动**（直接返回 ""）。
                # 同一个 bug 在 `#EXT-X-KEY` 处**已经修过**（见 :188-190 的修复注释），
                # 这里是它的孪生位置 —— 本工程最稳定的那类「一处修、另一处漏」。
                # 修法与上面**同源**：先按 ':' 切掉标签前缀，再按 ',' 拆属性。
                _attrs_txt = line.split(':', 1)[1] if ':' in line else ''
                for attr in _attrs_txt.split(','):
                    if attr.strip().startswith('BANDWIDTH='):
                        # 修复：使用 split('=', 1) 防止截断
                        parts = attr.strip().split('=', 1)
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

        # [v2.19.9 修复·**零转码红线**] 原来这两条路的**顺序是反的**：先跑 GPU **重编码**
        # （`-c:v h264_nvenc -preset p6 -b:v 5M`），只有它失败才退回 `-c copy`。两个后果都实：
        #   ① `concat` 合并**本来不需要编码** —— `-c copy` 是流拷贝（I/O 级、几秒完），
        #      而重编码要先解码再编码，**反而更慢**："GPU 加速"在这里是个误解；
        #   ② 它会**静默把画质压到 5 Mbps**，与工程自己写明的「零转码红线」直接冲突
        #      （见 universal_downloader 与 本工程的对外评审材料）。
        # 现改为**只调换顺序**（不增不减任何能力）：主路径一律 `-c copy` 无损拷贝，
        # 只有它失败时才动用重编码兜底 —— 并把"这是有损兜底"明确说出来，不静默。
        cmd_copy = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(filelist),
                    "-c", "copy", str(output_path)]
        proc = await asyncio.create_subprocess_exec(
            *cmd_copy, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, stderr = await proc.communicate()
        if proc.returncode == 0:
            return
        logger.warning(
            f"FFmpeg 流拷贝合并失败（分片参数不一致时才会走到）—— 改用**重编码兜底**，"
            f"本次产物**有损**、与零转码红线相违: {stderr.decode(errors='replace')[:200]}")

        # GPU 加速合并（**现在只是兜底**，不再是主路径）：自动检测可用编码器
        # 优先级：NVENC > QuickSync > AMF（CPU copy = 上面的主路径）
        # [FIXED & MODIFIED] v2.14 编码器探测结果缓存（原每次合并都跑 ffmpeg -encoders
        # 子进程，同步阻塞事件循环最坏 10s → 进程级只探测一次 + 挪线程池）
        if self.gpu_acceleration:
            encoder = await asyncio.to_thread(self._detect_gpu_encoder_cached)
            if encoder:
                cmd = ["ffmpeg", "-y", "-hwaccel", encoder["hwaccel"], "-f", "concat",
                       "-safe", "0", "-i", str(filelist), "-c:v", encoder["codec"],
                       "-preset", "p6", "-b:v", "5M", str(output_path)]
                proc_gpu = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                _, stderr_gpu = await proc_gpu.communicate()
                if proc_gpu.returncode == 0:
                    return
                logger.warning(
                    f"FFmpeg GPU 合并 ({encoder['name']}) failed: "
                    f"{stderr_gpu.decode(errors='replace')[:200]}")

        raise RuntimeError(f"M3U8 merge failed: {stderr.decode()[:500]}")

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
