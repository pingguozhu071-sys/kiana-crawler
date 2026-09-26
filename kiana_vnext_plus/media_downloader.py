import asyncio
import aiofiles
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


# [FIXED & MODIFIED] F2：防盗链 Referer 推导（B站图片 i0.hdslb.com 等 CDN 校验主站 Referer，
# 微博/公众号图床同理——无 Referer 直接 403/空响应）
def _referer_for(url: str) -> str:
    try:
        from urllib.parse import urlparse as _up
        host = (_up(url).netloc or "").lower()
    except Exception:
        return ""
    if "hdslb.com" in host:
        return "https://www.bilibili.com/"
    if "alicdn.com" in host or "taobao.com" in host:
        return "https://www.taobao.com/"
    if "sinaimg" in host:
        return "https://weibo.com/"
    if "douyinvod.com" in host or "bytecdn.cn" in host or "douyin" in host:
        return "https://www.douyin.com/"
    return f"https://{host}/" if host else ""


class MediaDownloader:
    """媒体文件下载器，队列化异步下载"""

    def __init__(self, output_dir: Path, max_concurrent=3):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.session = None
        self.queue = asyncio.Queue()
        self._worker_task = None

    async def init_session(self):
        # [FIXED & MODIFIED] curl_cffi 替换 aiohttp（本机 aiohttp 外网全超时）
        # [v2.19.8 性能修复] 必须显式 impersonate：不传时 curl_cffi 落在"不模拟指纹"的默认档，
        # 实测同一 CDN、同一代理下稳定压在 4.7-8.2 MB/s（多次取样），带指纹档 12-25 MB/s。
        # 指纹从工程自己的 TLS 池取（与协议通道同源）——混用不同指纹本身是可检测信号。
        from curl_cffi.requests import AsyncSession
        from .fingerprint_consistency import TLS_IMPERSONATE_POOL
        self.session = AsyncSession(timeout=120, impersonate=TLS_IMPERSONATE_POOL[0])
        self._worker_task = asyncio.create_task(self.worker())

    def _dl_headers(self, url: str) -> dict:
        """[FIXED & MODIFIED] F2：构造防盗链请求头（UA + 主站 Referer）"""
        return {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
            "Referer": _referer_for(url),
        }

    async def worker(self):
        while True:
            url, dest, proxy = await self.queue.get()
            if url is None:
                self.queue.task_done()
                break
            try:
                await self._download(url, dest, proxy)
            finally:
                self.queue.task_done()

    async def _download(self, url, dest, proxy=None):
        async with self.semaphore:
            if not self.session:
                return False
            # [v2.19.7 安全·扫描发现] 原为裸 `session.get`（follows redirects，入口无判定）：
            # 本函数由 worker 从队列取 URL 执行，被下载的字节直接落盘 → 重定向到内网即可
            # 把内网响应写成文件。当前无 enqueue 调用点，但 worker 任务随对象创建即启动，
            # 一次 enqueue 就上线——统一走 safe_get（入口校验 + 逐跳复检，proxy 经 kw 透传）。
            from .url_utils import safe_get
            # [FIXED & MODIFIED] v2.11 单发零重试 → 3 次重试
            for _try in range(3):
                try:
                    resp = await safe_get(self.session, url, proxy=proxy,
                                          headers=self._dl_headers(url), timeout=60)
                    if resp is not None and resp.status_code == 200 and resp.content:
                        data = resp.content
                        async with aiofiles.open(dest, 'wb') as f:
                            await f.write(data)
                        return True
                except Exception:
                    pass
                await asyncio.sleep(0.8)
            return False

    async def enqueue(self, url, filename, proxy=None):
        await self.queue.put((url, self.output_dir / filename, proxy))

    async def download_direct(self, url, output_path, proxy=None):
        """直接下载文件（用于mp4/mp3等直链），返回True/False
        [v2.16.1] 断点续传：probe + .part+Range 流式（复用 universal_downloader 公共件）——
        大文件中断保留 .part，下次任务自动续传（纯字节搬运，零转码）。"""
        if not self.session:
            return False
        # [FIXED & MODIFIED] URL 协议校验：无协议/非 http(s) 的脏链接（如 parser 提取丢协议的
        # player.bilibili.com/player.html?bvid=...）直接拒绝——实测产生 0 字节空文件 + unknown 目录
        if not url or not url.startswith(("http://", "https://")):
            logger.warning(f"download_direct 拒绝非法 URL: {str(url)[:80]}")
            return False
        # [v2.19.6 安全·审查发现] SSRF 闸：本函数是**多处 fallback 的终点**
        # （如 download_video 被拦后 crawler 仍会用同一 URL 走这里）——上一轮只在
        # download_video 加了闸，会被 fallback 绕过。此处补齐，覆盖所有调用方。
        from .url_utils import is_private_url
        if is_private_url(url, dns_check=True):
            logger.warning(f"download_direct SSRF 拦截：私网/保留地址 {str(url)[:80]}")
            return False
        from .universal_downloader import stream_to_file, probe_url
        try:
            dest = Path(output_path)
            _probe = await probe_url(self.session, url, self._dl_headers(url))
            _expect = _probe[0] if _probe else None
            _ok = await stream_to_file(self.session, url, dest, self._dl_headers(url),
                                       proxy=proxy or "", expect_total=_expect, retries=3)
            if _ok:
                return True
            return False
        except Exception as _e:
            logger.debug(f"download_direct failed: {_e}")
            return False

    async def close(self):
        if self._worker_task:
            await self.queue.put((None, None, None))
            await self._worker_task
        if self.session:
            await self.session.close()
