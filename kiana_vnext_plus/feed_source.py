# -*- coding: utf-8 -*-
"""RSS/Atom 订阅源通道 feed_source.py（v2.17 1-2，零新依赖）

种子 URL 判定为 RSS/Atom（.xml/.rss/.atom/路径含 /rss /feed）→ 预解析条目 →
逐条 push 为 depth=0 种子（parent_hash=feed 源 url——血缘审计复用；frontier 去重天然）。
复用 sitemap 的正则模式（<loc>/<link>），不引 feedparser（全 pin 依赖纪律）。

语义：
  looks_like_feed(url)     形态判定（后缀/路径启发式；误判走正常页面通道兜底）
  parse_feed(text)         条目 URL 列表（RSS <item><link>、Atom <entry><link href>、
                           全集 <link> 兜底；仅 http(s) 收）
  collect_feed(url)        抓取+解析（curl_cffi AsyncSession；失败返回 [] 诚实降级）
"""
import logging
import re
from urllib.parse import urlparse, urljoin

logger = logging.getLogger(__name__)

_FEED_HINTS = (".xml", ".rss", ".atom", "/rss", "/feed", "/atom")


def looks_like_feed(url: str) -> bool:
    try:
        p = urlparse(str(url or ""))
        path = (p.path or "").lower()
        return any(path.endswith(h) or h in path for h in _FEED_HINTS)
    except Exception:
        return False


def parse_feed(text) -> list:
    """RSS/Atom 条目 URL（正则；相对链接补全需基址——collect_feed 内部 urljoin）。"""
    t = str(text or "")
    urls = []
    # RSS: <item> ... <link>xxx</link>
    for m in re.finditer(r"<item>\s*(.*?)</item>", t, re.S):
        for lm in re.finditer(r"<link[^>]*>\s*https?://[^\s<]+", m.group(1)):
            urls.append(re.sub(r"<link[^>]*>", "", lm.group(0)).strip())
    # Atom: <entry> ... <link href="https://..."/>
    for m in re.finditer(r"<entry>\s*(.*?)</entry>", t, re.S):
        for lm in re.finditer(r'<link[^>]*href="(https?://[^"]+)"', m.group(1)):
            urls.append(lm.group(1))
    # RSS 无 <item> 时的全集 <link>/<loc> 兜底（兼容 sitemap 风格源）
    if not urls:
        urls += [m.group(1) for m in re.finditer(r"<loc>\s*(https?://[^<\s]+)\s*</loc>", t)]
        urls += [m.group(1) for m in re.finditer(r'<link[^>]*href="(https?://[^"]+)"', t)] \
            if not urls else urls
        if not urls:
            urls += [re.sub(r"<link[^>]*>", "", m.group(0)).strip()
                     for m in re.finditer(r"<link[^>]*>\s*https?://[^\s<]+", t)]
    seen, out = set(), []
    for u in urls:
        if u.startswith(("http://", "https://")) and u not in seen:
            seen.add(u)
            out.append(u)
    return out[:300]


async def collect_feed(url: str) -> list:
    """抓取并解析订阅源（curl_cffi AsyncSession；任何失败 → []，调用方诚实降级）。

    [v2.19.7 安全·扫描发现] 改用 `safe_get`：此前是裸 `session.get`（libcurl 会跟随
    最多 30 跳），上游只有 crawler 的 `dns_check=False` 字面闸 → `http://169.254.169.254.nip.io/rss`
    这类"域名解析到内网"的订阅源可直取云元数据。现入口校验 + 手动逐跳复检落点。
    """
    try:
        from curl_cffi.requests import AsyncSession
        from .url_utils import safe_get
        async with AsyncSession(timeout=15) as session:
            r = await safe_get(session, url, timeout=15)
            if r is None:
                logger.debug(f"feed 被 SSRF 闸拦截/请求失败: {url[:60]}")
                return []
            if r.status_code != 200:
                logger.debug(f"feed 抓取失败({r.status_code}): {url[:60]}")
                return []
            entries = parse_feed(r.text)
            if not entries:
                logger.debug(f"feed 无可解析条目: {url[:60]}")
                return []
            # [v2.19.7 修复] curl_cffi 的 Response **无 url 属性**（原 `r.url` 会抛
            # AttributeError → 被外层 except 吞掉 → feed 通道静默降级为普通页面）。
            base = str(getattr(r, "url", None) or url)
            out = [urljoin(base, e) for e in entries]
            logger.info(f"feed 解析成功: {len(out)} 条（{url[:60]}）")
            return out
    except Exception as e:
        logger.warning(f"feed 抓取失败（按普通页面降级）: {e}")
        return []
