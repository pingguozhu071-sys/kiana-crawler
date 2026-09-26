"""页面处理器：抓取、解析、去重、链接发现、防御评估"""

import re
import time
import asyncio
import random
import logging

from .sanitizer import sanitize_text, sanitize_headers, sanitize_record
from .parser import compute_content_hash, extract_metadata_async, simhash_64, simhash_hamming
from .json_util import json_dumps
from .url_utils import url_hash as _uh

logger = logging.getLogger(__name__)

# [FIXED & MODIFIED] 静态资源后缀 + 媒体 CDN 域名（链接入队过滤）
_STATIC_EXTS = {
    ".js", ".mjs", ".css", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif",
    ".svg", ".ico", ".bmp", ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".mp4", ".m4s", ".mp3", ".flv", ".webm", ".ogg", ".wav", ".aac",
    ".json", ".map", ".pdf", ".zip", ".gz", ".7z", ".rar", ".xml", ".txt",
    ".wasm", ".dat",
}
_MEDIA_CDN_DOMAINS = {
    "bilivideo.com", "hdslb.com", "akamaized.net", "cloudfront.net",
    "cdninstagram.com", "fbcdn.net", "ytimg.com", "googlevideo.com",
}


def sanitize_video_filename(url: str) -> str:
    """从 URL 生成安全的视频文件名（修复：原来直接取 URL 后 20 字符可能包含非法字符）"""
    # 提取 URL 中有意义的部分作为文件名
    safe = re.sub(r'[^\w\-.]', '_', url)[-60:]
    return safe if safe else "video_unknown"


def _placeholder_trigger(data, url, html) -> bool:
    """[FIXED & MODIFIED] v2.17 稳定性门禁：占位/空页是否触发浏览器渲染兜底。

    历史 bug：原条件末项 `comment_content not in html and p_floor not in html` 对**任何
    不含贴吧标记的页面恒真**——任何正文<200 字的页面都会无条件开浏览器渲染（~13s/页、
    无预算上限）。修复后语义：空内容 / 占位标题 / 无 html / 贴吧页 / 知乎页无正文容器
    才触发；普通短页（通用抽取拿到正文但短）不触发。纯函数供单测锁定。"""
    if not ((data or {}).get("title") or (data or {}).get("text")):
        return True
    ph = re.sub(r'<[^>]+>', ' ', str((data or {}).get("text") or '')).strip()
    if len(ph) >= 200:
        return False
    if str((data or {}).get("title") or "") in ("", "百度贴吧", "登录", "百度一下"):
        return True
    if not html:
        return True
    if "tieba.baidu.com" in str(url):
        return True
    if "zhihu.com" in str(url) and "QuestionRichText" not in (html or ""):
        return True
    return False


def _render_budget_cfg(cfg) -> int:
    """[v2.17 稳定性门禁] 渲染兜底预算：browser_render_max（默认 20，0=禁用兜底）。
    独立纯函数供单测锁定（曾因内联+`0 or 20` 使 0=禁用语义失效）。"""
    try:
        if cfg is None:
            return 20
        return max(int(cfg.get("browser_render_max", 20)), 0)
    except Exception:
        return 20


class PageProcessor:
    """页面处理器：抓取、解析、去重、链接发现、防御评估
    [v2.17 B2] 组件化进行中：解析段/质量段/入队段逐步抽为私有方法（纯搬移，行为不变）。"""

    async def _validate_quality(self, data: dict, url: str, uh: str, domain: str,
                                status: int, t_start: float, proxy, leased_at=None) -> str:
        """[v2.17 B2-S2] 质量闭环（纯搬移）：返回 'rejected'（已完整执行拒收流程：
        LOW_QUALITY 写错误表+mark_done+进度+_finalize_done(quality, rejected)+
        report_result——调用方收到后必须 return）或 'ok'/'warn'；无 validator 视为 ok。"""
        if not hasattr(self.crawler, 'validator'):
            return "ok"
        score = self.crawler.validator.score(data)
        _min_score = float(self.cfg.get("quality_min_score", 0.12))
        if score < _min_score and len(data.get('text', '') or '') < 400:
            await self.frontier.write_error(uh, "LOW_QUALITY",
                                            f"score={score:.2f} text_len={len(data.get('text', ''))} {url}")
            self._log(f"低分页拒收隔离 {url[:50]} (score={score:.1f})", "warn")
            # [v2.19 P1] 走 CAS：租约已易主则不改状态（防旧持有者覆盖新持有者的结果）
            await self.frontier.mark_done(uh, leased_at=leased_at)
            self._update_progress('done')
            await self._finalize_done(uh, url, domain, status,
                                      time.monotonic() - t_start, proxy,
                                      {"url": url, "title": data.get("title", ""),
                                       "quality": round(score, 2), "rejected": True})
            # [v2.18 P1-7] 双写已删：_finalize_done 内部已 report_result(proxy, True)
            return "rejected"
        if score < 0.3:
            self._log(f"Low quality page {url[:50]} (score={score:.1f})")
        return "ok"

    async def _enqueue_and_render(self, data: dict, html_sanitized, url: str, domain: str) -> tuple:
        """[v2.17 B2-S3] 入队段（视频/图片/B站/电商/通用渲染兜底——纯代码搬移，保序）。
        通用渲染可能更新 data/html_sanitized——以元组回传。"""
        if getattr(self.crawler, '_dl_video', True) and 'bilibili.com/video/' not in url and 'b23.tv' not in url:
            for vurl in data.get('videos', []):
                if vurl and vurl.startswith('http'):
                    try:
                        # [FIXED & MODIFIED] player.html 嵌入播放器 URL → 标准视频页 URL
                        # （yt-dlp 不认 player.bilibili.com/player.html?bvid=，实测必失败）
                        _m = re.search(r'player\.bilibili\.com/player\.html\?[^"\'<>\s]*bvid=(BV\w+)', vurl)
                        if _m:
                            vurl = f"https://www.bilibili.com/video/{_m.group(1)}"
                        await self.frontier.add_video_download(vurl, domain)
                    except Exception:
                        pass

        # Download images from page（受下载开关控制）
        img_urls = data.get('images', [])
        if img_urls and getattr(self.crawler, '_dl_image', True) and hasattr(self.crawler, 'downloader'):
            # [v2.18 P2-3] fire-and-forget 必须持强引用（无引用可被 GC 静默吞掉）
            _t = asyncio.create_task(self._download_images_with_ocr(img_urls, url, domain))
            self._bg.add(_t)
            _t.add_done_callback(self._bg.discard)

        # B站 video resolver: 页面 URL 直接入队（yt-dlp 全权处理，含 wbi 签名/风控/登录）
        if ('bilibili.com/video/' in url or 'b23.tv' in url) and getattr(self.crawler, '_dl_video', True):
            if 'bilibili' in url and '__INITIAL_STATE__' not in html_sanitized and len(html_sanitized or '') < 100_000:
                self._log(f"B站风控壳页（无视频数据，IP 限流中，可稍后重试）: {url[:60]}", "warn")
            try:
                if 'built files will be auto injected' in url:
                    self._log(f"B站 JS 假链接已过滤: {url[:60]}", "warn")
                else:
                    await self.frontier.add_video_download(url, domain)
                    self._log(f"B站视频入队: {url[:60]}")
            except Exception as e:
                logger.debug(f"B站 video enqueue: {e}")
            try:
                from .video_resolver import extract_bilibili_info, resolve_bilibili_video
                info = extract_bilibili_info(html_sanitized, url)
                if info['bvid'] and info['cid']:
                    streams = await resolve_bilibili_video(info['bvid'], info['cid'])
                    for s in streams:
                        _su = s.get('url') or ''
                        if (_su.startswith('http') and 'player.html' not in _su
                                and s.get('type') == 'video'):
                            await self.frontier.add_video_download(_su, domain)
                            self._log(f"B站视频流: {s.get('desc','')} {s.get('quality','')}")
            except Exception as e:
                logger.debug(f"B站 resolver: {e}")

        # 电商商品解析（页面内嵌数据 → products 字段；搜索页 JS 动态 → 渲染通道）
        try:
            from .ecommerce_parser import parse_ecommerce, detect_platform
            products = parse_ecommerce(html_sanitized, url)
            if products:
                data['products'] = products
                self._log(f"电商解析: 提取 {len(products)} 个商品（{url[:40]}）")
            else:
                plat = detect_platform(url)
                is_search = any(k in url.lower() for k in
                                ("search", "s.m.taobao", "search_result", "keyword", "q="))
                need_render = (is_search or "tb.cn" in url or
                               not (data.get("title") or data.get("text")))
                solver = getattr(self.crawler, 'solver', None)
                if plat and need_render and solver is not None and \
                        getattr(solver, '_browser_available', True):
                    self._log(f"电商静态解析为空 → 隐身浏览器渲染通道（{plat}）")
                    html2, st2, _ = await solver.solve(
                        url, challenge_wait=25, extra_wait=4.0,
                        wait_selector=".gl-item, .J_goodsList li, [data-sku], .items .item" if plat == "jd" else None,
                    )
                    if st2 < 400 and html2:
                        products = parse_ecommerce(html2, url)
                        if products:
                            data['products'] = products
                            data['rendered'] = True
                            self._log(f"电商解析(渲染通道): 提取 {len(products)} 个商品")
        except Exception as e:
            logger.debug(f"ecommerce parse: {e}")

        # [v2.17 0-5b] 占位渲染已前置于质量闸（process_job 先调 _maybe_render_placeholder）
        return data, html_sanitized

    async def _maybe_render_placeholder(self, data: dict, html_sanitized, url: str) -> tuple:
        """[v2.17 0-5b] 占位页渲染兜底（预算共享 browser_render_max）：从
        _enqueue_and_render 前移——原位置在质量闸之后，真·空壳页（无 title 无 text）
        在闸前即被拒收，永远等不到渲染兜底；正文偏短但有线索的页也被闸拦。
        渲染成功 → (新 data, 新 html)；失败/无浏览器/无预算 → 原样返回。"""
        _rk = getattr(self, '_render_success_count', 0)
        _rb = _render_budget_cfg(getattr(self.crawler, 'cfg', None))
        if not (_placeholder_trigger(data, url, html_sanitized)
                and not data.get("rendered") and _rk < _rb):
            return data, html_sanitized
        solver = getattr(self.crawler, 'solver', None)
        if solver is None or not getattr(solver, '_browser_available', True):
            return data, html_sanitized
        # [v2.19 P1 竞态修复] 预算**先占位**：原实现"读 _rk → await solve → 写 _rk+1"
        # 跨 await，并发下可超额突破 browser_render_max（而这预算正是为防浏览器风暴）。
        # 同步块内先递增占位，失败再回滚（无 await 的读-改-写 → 单循环原子）。
        self._render_success_count = _rk + 1
        self._log(f"静态解析为空/占位页 → 通用浏览器渲染兜底（JS 渲染站点: {url[:45]}）")
        _rendered_ok = False
        try:
            if ('tieba.baidu.com' in url or 'zhihu.com' in url) and hasattr(solver, 'render_simple'):
                html2, st2, _ = await solver.render_simple(url, extra_wait=3.0)
            else:
                html2, st2, _ = await solver.solve(url, challenge_wait=25, extra_wait=3.0)
            if st2 < 400 and html2:
                data2 = self.parser.extract_metadata(html2, url)
                if data2.get("title") or data2.get("text"):
                    data = data2
                    data['rendered'] = True
                    html_sanitized = await asyncio.to_thread(sanitize_text, html2)
                    _rendered_ok = True
                    self._log(f"渲染兜底成功: title={str(data.get('title',''))[:30]}")
        except Exception as e2:
            logger.debug(f"render fallback: {e2}")
        if not _rendered_ok:
            # 渲染失败/无产出 → 回滚占位（保持"失败的渲染尝试不计数"原语义）
            self._render_success_count = max(0, getattr(self, '_render_success_count', 1) - 1)
        return data, html_sanitized

    async def _discover_links(self, data: dict, url: str, html_sanitized, job: dict, uh: str):
        """[v2.17 B2-S3] 链接发现段（静态资源过滤/SSRF 闸/robots 合规/血缘 push——纯搬移）。"""
        if job.get('depth', 0) < self.project.config.limits.max_depth:
            links = self._extract_links(data, url, html_sanitized)
            llm_priorities = {}
            # 关键词白名单：不匹配的链接不入队（内容过滤设置，GUI 传入）
            kf = (getattr(self.crawler, '_crawl_filters', None) or {})
            kf_kw = kf.get('keywords', []) if isinstance(kf, dict) else []
            for link in links[:20]:
                if kf_kw and not any(k.lower() in link.lower() for k in kf_kw):
                    continue  # 白名单过滤：跳过不匹配的链接
                if self._is_static_link(link):
                    continue  # 静态资源/视频流链接不入队
                from .url_utils import is_private_url
                if is_private_url(link):
                    continue  # SSRF 闸：入队前拒私网/云元数据
                if getattr(self.crawler, '_robots_respect', False):
                    try:
                        from .robots_policy import is_allowed
                        if not is_allowed(link):
                            logger.debug(f"robots 拒绝入队: {link[:80]}")
                            continue
                    except Exception:
                        pass
                if not await self.frontier.is_visited(_uh(link)):
                    priority = llm_priorities.get(link, self._calc_priority(link))
                    await self.frontier.push(link, depth=job['depth'] + 1,
                                             priority=priority, parent_hash=uh)

    async def _persist_export(self, data: dict, url: str, domain: str, uh: str) -> dict:
        """[v2.17 B2-S4] 持久化/管道/导出段（纯搬移）：images 统计计数、写 extracted 表、
        进度、落盘副本、ItemPipeline 声明式处理、exporter jsonl/csv + markdown 快照。
        返回 pipeline 处理后的 data（链条下游用）。

        [v2.19.7 安全·扫描发现] 落盘/导出的**每一份副本**都过 `sanitize_record`
        （url / images[].src / author / description / entities 此前只脱敏了 text）——
        边界见 `sanitizer.sanitize_record` 的说明。返回给下游的 data 保持原样：里面的
        图片/视频 URL 还要用来真下载，抹掉 ?token= 的值就下不动了。
        """
        _san_enabled = bool(getattr(self.crawler, '_sanitize_enabled', True))

        def _safe_rec(d: dict) -> dict:
            return sanitize_record(d) if _san_enabled else d

        _img_n = len(data.get('images') or [])
        if _img_n and hasattr(self.crawler, '_stats_images'):
            self.crawler._stats_images += _img_n
        await self.frontier.write_extracted(uh, json_dumps(_safe_rec(data)))
        self._update_progress('done')
        # [v2.19 P1] 文件 IO（open+json.dump+mkdir）移出事件循环——每成功页一次
        await asyncio.to_thread(self._save_extracted_file, _safe_rec(data), url)
        pipeline = getattr(self.crawler, 'pipeline', None)
        if pipeline:
            try:
                processed = pipeline.process(data, url)
                if processed:
                    data = processed
            except Exception:
                pass
        if hasattr(self.crawler, 'exporter'):
            try:
                self.crawler.exporter.add(_safe_rec(data), domain)
                if getattr(self.crawler, '_export_markdown', True) \
                        and data.get('text') and len(data.get('text', '')) > 50:
                    self.crawler.exporter.add_markdown(domain, _safe_rec(data))
            except Exception:
                pass
        return data

    async def _parse_page(self, html: str, url: str, job: dict, uh: str) -> dict:
        """[v2.17 B2-S1] 解析段（纯代码搬移，行为不变）：metadata 提取 → 站点规则层
        （fields/images/list_items 并入；规则翻页 next_url 直接入队，语义原样）→
        页面类型分类 → 文本脱敏 → 内容过滤。返回处理后的 data dict。"""
        data = await extract_metadata_async(html, url)
        # [v2.17 2-C] JSON-LD 实体扁平化（entities[]——检索/AI 消费面）
        try:
            from .parser import flatten_json_ld
            _ents = flatten_json_ld(data.get("json_ld") or [])
            if _ents:
                data["entities"] = _ents
        except Exception:
            pass
        try:
            from .site_rules import apply_rule
            _rule_data = apply_rule(html, url)
            if _rule_data:
                data['rule'] = _rule_data['name']
                data['fields'] = {k: v for k, v in (_rule_data.get('fields') or {}).items() if v}
                for _img in _rule_data.get('images', []):
                    if _img not in data.setdefault('images', []):
                        data['images'].append(_img)
                if _rule_data.get('list_items'):
                    data['list_items'] = _rule_data['list_items'][:200]
                _next = _rule_data.get('next_url')
                if _next and not await self.frontier.is_visited(_uh(_next)):
                    await self.frontier.push(_next, depth=job['depth'] + 1, priority=2,
                                             parent_hash=uh)
                    self._log(f"规则翻页入队: {_next[:60]}")
                self._log(f"站点规则命中: {_rule_data['name']} fields={len(data['fields'])}")
        except Exception as _re:
            logger.debug(f"规则层跳过: {_re}")
        classifier = getattr(self.crawler, 'page_classifier', None)
        if classifier:
            try:
                data['page_type'] = classifier.classify(url, html[:20000])
            except Exception:
                pass
        if getattr(self.crawler, '_sanitize_enabled', True):
            data['text'] = await asyncio.to_thread(sanitize_text, data.get('text', ''))
        crawl_filters = self._get_crawl_filters()
        if crawl_filters:
            data = self._apply_content_filters(data, crawl_filters)
        return data

    def __init__(self, crawler):
        self.crawler = crawler
        self._bg: set = set()  # [v2.18 P2-3] fire-and-forget 任务强引用集
        self.project = crawler.project
        self.cfg = crawler.cfg
        self.frontier = crawler.frontier
        self.exit_mgr = crawler.exit_mgr
        self.router = crawler.router
        self.adaptive = crawler.adaptive
        # [FIXED & MODIFIED] v2.10.5c 断链修复：self.parser 从未定义 → 渲染兜底
        # self.parser.extract_metadata() 必 AttributeError 被 except 吞（贴吧拿不到正文根因）
        from . import parser as _parser
        self.parser = _parser
        self._sim_index = {}   # [FIXED & MODIFIED] v2.11 内容级去重索引：domain -> [(url_hash, simhash)]
        # [v2.17 B3] URL 打分/过滤扩展点注入路由标签提供器（默认实现=原内联逻辑）
        from . import link_scoring as _ls
        _ls.set_label_provider(self._url_label)

    def _near_dup(self, domain: str, uh: str, sh: int):
        """[FIXED & MODIFIED] v2.11 SimHash 近似重复判定（Hamming<=3 返回重复源 url_hash；
        域内索引上限 500 条，超出丢弃最旧 100 条防内存膨胀）"""
        lst = self._sim_index.get(domain)
        if not lst:
            self._sim_index[domain] = [(uh, sh)]
            return None
        for _h, _s in lst:
            if simhash_hamming(_s, sh) <= 3:
                return _h
        lst.append((uh, sh))
        if len(lst) > 500:
            del lst[:100]
        return None

    async def _download_images_with_ocr(self, img_urls, url, domain):
        """[FIXED & MODIFIED] v2.11 图片下载 + OCR 索引（ddddocr classification，
        结果作为补充 jsonl 记录写回——不阻塞页面主流程）"""
        try:
            paths = await self.crawler.downloader.download_images(img_urls, url)
            if not paths:
                return
            ocr_map = await self.crawler.downloader.ocr_images(paths)
            if ocr_map:
                exporter = getattr(self.crawler, 'exporter', None)
                if exporter:
                    exporter.add_jsonl(domain, {"type": "image_ocr", "url": url,
                                                "image_ocr": ocr_map})
        except Exception:
            pass

    def _log(self, msg: str, level: str = "info"):
        """输出日志（通过标准 logging）——[FIXED & MODIFIED] 支持 warn 级别（GUI 黄色）"""
        if level == "warn":
            logger.warning(msg)
        elif level == "error":
            logger.error(msg)
        else:
            logger.info(msg)

    async def process_job(self, job: dict):
        url = job['normalized_url']
        domain = job['domain']
        uh = job['url_hash']
        t_start = time.monotonic()

        # 获取代理
        expected_country = None
        strict_geo = self.project.config.get("strict_geo_match", False)
        proxy = None
        try:
            if self.crawler._fp_gen:
                temp_fp = self.crawler._fp_gen.generate()
                # [FIXED & MODIFIED] v2.11 出厂级回归修复：FingerprintGenerator.generate()
                # 返回扁平 dict（无 .navigator 属性）→ 原代码 AttributeError 被 except 吞成
                # NoAvailableExit → 无代理场景所有广撒网任务直接死（压力基线实测暴露）。
                if isinstance(temp_fp, dict):
                    expected_country = temp_fp.get("geo_code") \
                        or (str(temp_fp.get("language", "")).split("-")[-1] or None)
                else:
                    _nav = getattr(temp_fp, "navigator", None)
                    expected_country = getattr(_nav, 'country', None) or \
                        self._language_to_country(getattr(_nav, 'language', ''))

            if await self.frontier.count_done_by_domain(domain) >= \
                    self.project.config.limits.max_pages_per_domain:
                await self.frontier.mark_failed(uh, retry=False)
                # [v2.18 P1-8] 记账补齐：上限跳过的任务原来凭空消失（零进度计数）
                self._update_progress('failed')
                return

            # [FIXED & MODIFIED] max_pages 总页数限制（原实现从未生效——done 已超上限即停止处理新任务）
            if await self.frontier.count_done_total() >= self.project.config.limits.max_pages:
                await self.frontier.mark_failed(uh, retry=False)
                self._update_progress('failed')
                return

            # [v2.17 E-P2] 身份捆绑开启时出口/反馈走会话池（封锁整包退役）；关闭时零影响
            _sess = None
            proxy = None
            if getattr(self.crawler, "_identity_pool", None) is not None:
                proxy, _sess = await self.crawler._acquire_identity(domain)
            if not proxy:
                proxy = await self.exit_mgr.acquire_for_domain(domain, expected_country, strict=strict_geo)
        except Exception as e:
            # 修复：代理临时不可用时应允许重试，而非永久杀死任务
            await self.frontier.mark_failed(uh, retry=True)
            await self.frontier.write_error(uh, "NoAvailableExit", str(e))
            # [v2.18 P1-8] 记账补齐：此分支原来无进度计数、无身份反馈
            self._update_progress('failed')
            self.crawler._identity_feedback(domain, _sess, fail=True, err="NoAvailableExit")
            return

        # 修复：将 acquire_all 放入 try 块，确保 CancelledError 时能释放信号量
        try:
            await self.crawler.concurrency.acquire_all(domain, proxy)
        except asyncio.CancelledError:
            # [v2.18 P1-5] 取消路径：原直接抛出——出口 inflight 只增不减 + 任务无失败档案
            await self.frontier.mark_failed(uh, retry=True)
            await self.exit_mgr.release(proxy)
            raise
        except Exception:
            await self.frontier.mark_failed(uh, retry=True)
            await self.frontier.write_error(uh, "ConcurrencyAcquireFailed", "signal acquire failed")
            # [v2.18 P1-5] inflight 泄漏点：此分支 return 不经过主 try 的 finally，
            # 出口 inflight 只增不减 → 长跑代理池假死。补 release + 记账。
            await self.exit_mgr.release(proxy)
            self._update_progress('failed')
            self.crawler._identity_feedback(domain, _sess, fail=True, err="ConcurrencyAcquireFailed")
            return
        # [v2.18 P1-3] 租约心跳：单页最坏处理（human_delay+渲染+媒体下载）可超
        # 300s 租约 → 被回收重发后同 URL 双协程并发处理（媒体/导出双份）。
        # 处理期间每 60s 续租（CAS 在 leased_at 上，不动原始值），finally 取消。
        _hb_task = None
        if job.get('leased_at') is not None:
            async def _lease_heartbeat():
                while True:
                    await asyncio.sleep(60)
                    if not await self.frontier.heartbeat_lease(uh, job['leased_at']):
                        return  # 租约已易主：停止续租（结果将被 CAS 丢弃）
            _hb_task = asyncio.create_task(_lease_heartbeat())
        # [v2.19 P1] CAS 打卡结果（在持久化前置位）；供异常分支判断"是否已置 done"
        # [v2.19 P1] CAS 打卡结果（三态：True 赢 / False 易主 / None 落库失败）；
        # 供异常分支判断"是否已置 done"（True 时不标 failed，避免把 done 打回重爬）
        _cas = None
        try:
            await self._human_delay(domain=domain)
            # [v2.17 1-5] robots Crawl-delay 尊重（robots_respect 开启时按域最小请求间隔；
            # 假体/降级爬虫可能无该方法——getattr 守卫）
            _pd = getattr(self.crawler, "_polite_delay", None)
            if _pd:
                await _pd(domain)
            # 限流引擎：全局+域名双档令牌桶（429 指数退避自动生效）
            rate_limiter = getattr(self.crawler, 'rate_limiter', None)
            if rate_limiter:
                await rate_limiter.acquire(url)
            # HTTP 磁盘缓存：命中直接返回（TTL 1h），减少重复请求
            http_cache = getattr(self.crawler, 'http_cache', None)
            cached = None
            _stale = False
            if http_cache:
                try:
                    cached = http_cache.get(url)
                    _stale = bool(cached and cached.get('stale'))
                except Exception:
                    cached = None
            # [FIXED & MODIFIED] v2.15 阶段2 真增量爬取：缓存过期（stale）但持有
            # ETag/Last-Modified → 发条件请求，服务器 304 则复用缓存跳过重复解析
            # （原实现过期即全量重抓+重解析，重复页流量浪费 ~90%）
            if _stale and isinstance(cached, dict):
                _meta = cached.get('meta', {})
                if _meta.get('etag'):
                    job['_cond_headers'] = {"If-None-Match": _meta['etag']}
                elif _meta.get('last_modified'):
                    job['_cond_headers'] = {"If-Modified-Since": _meta['last_modified']}
            if cached and not _stale:
                html = cached.get('content', '')
                status = 200
                # 缓存命中：使用缓存元数据中的 headers（无则空）
                response = None
                logger.debug(f"[cache] hit: {url[:60]}")
            else:
                response = await self.router.fetch(url, domain, job)
                status = response.status_code
                # [FIXED & MODIFIED] v2.15 阶段2 304 协商闭环：未变化 → 复用缓存内容，
                # 跳过重复解析（内容没变，上次已提取过——增量语义）
                if status == 304 and cached and isinstance(cached, dict) and cached.get('content'):
                    self._log(f"304 Not Modified（增量命中）: {url[:60]}")
                    html = cached['content']
                    status = 200
                    # 刷新 fetched_at（原条目已 stale，不刷新则每 TTL 都重新协商）
                    if http_cache:
                        _m = cached.get('meta', {})
                        http_cache.store(url, html, {"etag": _m.get('etag', ''),
                                                     "last-modified": _m.get('last_modified', '')})
                    await self.frontier.mark_done(uh, leased_at=job.get('leased_at'))
                    self._update_progress('done')
                    await self._finalize_done(uh, url, domain, status, time.monotonic() - t_start, proxy,
                                              {"url": url, "title": "", "not_modified": True})
                    # [v2.18 P1-7] 双写已删：_finalize_done 内部已 report_result(proxy, True)
                    return
                # [FIXED & MODIFIED] v2.15 阶段2 304 协商闭环：未变化 → 复用缓存内容，
                # 跳过重复解析（内容没变，上次已提取过——增量语义）
                if status == 304 and http_cache:
                    _old = http_cache.get(url)
                    if _old and _old.get('content'):
                        self._log(f"304 Not Modified（增量命中）: {url[:60]}")
                        html = _old['content']
                        status = 200
                        await self.frontier.mark_done(uh, leased_at=job.get('leased_at'))
                        self._update_progress('done')
                        await self._finalize_done(uh, url, domain, status, time.monotonic() - t_start, proxy,
                                                  {"url": url, "title": "", "not_modified": True})
                        # [v2.18 P1-7] 双写已删：_finalize_done 内部已 report_result(proxy, True)
                        return
                # [FIXED & MODIFIED] v2.14 解析炸弹闸：超 max_body_bytes 的响应拒解析
                # （截断文本仅记档——200MB 恶意页 ×4-5 倍解析放大可打爆内存）
                if getattr(response, "too_big", False):
                    await self.frontier.write_error(uh, "BODY_TOO_BIG",
                                                    f"response exceeds max_body_bytes: {url}")
                    await self.frontier.mark_failed(uh, retry=False)
                    self._log(f"响应超限拒解析: {url[:60]}", "warn")
                    await self._finalize_fail(uh, url, domain, 413, time.monotonic() - t_start, proxy)
                    return
                # 限流反馈：记录响应状态驱动退避/探测
                if rate_limiter:
                    try:
                        await rate_limiter.record(url, status)
                    except Exception:
                        pass
                html = await response.text()
            # 脱敏开关：关闭时保留手机号/邮箱/IP 等原始数据
            # [v2.19 P1] 全页正则走线程池（50 路并发 × 大页面时压在事件循环上会累积停顿；
            # 同函数 :318 早已 to_thread，此处原为直调——自身不一致）
            if getattr(self.crawler, '_sanitize_enabled', True):
                html_sanitized = await asyncio.to_thread(sanitize_text, html)
            else:
                html_sanitized = html
            # [v2.19 安全 P0] 成功响应写缓存——改存**脱敏后**内容。原实现在脱敏之前
            # store 原始 html → PII（手机号/邮箱/IP）明文长期落盘 <data_root>/http_cache/*.html。
            # 渲染兜底不依赖原始 html（用 html_sanitized 判定 + 渲染时重新取页），前置安全。
            if http_cache and status == 200 and html_sanitized:
                try:
                    http_cache.store(url, html_sanitized,
                                     response.headers if response is not None else {})
                except Exception:
                    pass
            latency = time.monotonic() - t_start
            content_hash = compute_content_hash(html_sanitized) if html_sanitized else ""
            cached_headers = cached.get('meta', {}) if cached else {}
            safe_headers = json_dumps(sanitize_headers(
                response.headers if response is not None else cached_headers))

            # [FIXED & MODIFIED] v2.11 内容级去重：SimHash 落库 + 近似重复标记
            # （frontier 的 simhash/duplicate_of 列原空置——广撒网"能采能管"接线）
            # [v2.19 P1] simhash（纯 Python 4096 token × 64 位 ≈ 26 万次迭代/页）走线程池
            _simhash = (await asyncio.to_thread(simhash_64, html_sanitized)
                        if html_sanitized else 0)
            await self.frontier.write_page(uh, status, len(html_sanitized), latency,
                                           safe_headers, content_hash, simhash=_simhash)
            if _simhash and len(html_sanitized or '') > 500:
                _dup_of = self._near_dup(domain, uh, _simhash)
                if _dup_of:
                    await self.frontier.mark_duplicate(uh, _dup_of)

            # [FIXED & MODIFIED] v2.10.5c 403/429 渲染兜底：贴吧等 JS 重站静态抓取必 403
            # （防爬首屏），但带 cookies 浏览器渲染能拿正文 → 403 也允许走渲染。
            # [FIXED & MODIFIED] v2.15 阶段3 壳页内容兜底：贴吧 200 壳页（防爬给导航骨架
            # 非 403——实测 20260829 任务：jsonl 只有"扫码登录/下载贴吧App"菜单文字）——
            # 壳页特征（正文<800 字 且 title/正文含登录墙特征词）同样触发渲染兜底。
            _shell_page = (
                html_sanitized is not None
                and ('tieba.baidu.com' in url or 'zhihu.com' in url)
                and ('扫码登录' in html_sanitized or '下载贴吧App' in html_sanitized
                     or '百度贴吧App' in html_sanitized)
            )
            # [FIXED & MODIFIED] v2.17 稳定性门禁：原 _rendered_403 一次性语义——首次渲染
            # 成功后永久置真 → 之后所有 403/壳页跳过渲染兜底（长跑后期数据质量隐性恶化）。
            # 改预算制：成功渲染计数 < browser_render_max（默认 20）才继续兜底（防浏览器
            # 风暴语义保留：预算耗尽即停）；失败的渲染尝试不计数。
            _render_ok = getattr(self, '_render_success_count', 0)
            _render_budget = _render_budget_cfg(getattr(self.crawler, 'cfg', None))
            if ((status in (403, 429)) or _shell_page) and \
                    html_sanitized is not None and _render_ok < _render_budget:
                solver = getattr(self.crawler, 'solver', None)
                if solver is not None and getattr(solver, '_browser_available', True):
                    _why = "壳页" if _shell_page else f"HTTP {status}"
                    # [v2.19 P1 竞态修复] 预算先占位、失败回滚（同 _maybe_render_placeholder）
                    self._render_success_count = _render_ok + 1
                    _ok2 = False
                    self._log(f"{_why} → 浏览器渲染兜底（JS 重站/防爬: {url[:45]}）")
                    try:
                        if ('tieba.baidu.com' in url or 'zhihu.com' in url) and hasattr(solver, 'render_simple'):
                            html2, st2, _ = await solver.render_simple(url, extra_wait=3.0)
                        else:
                            html2, st2, _ = await solver.solve(url, challenge_wait=25, extra_wait=3.0)
                        if st2 < 400 and html2 and len(html2) > 20000:
                            html_sanitized = await asyncio.to_thread(sanitize_text, html2)
                            status = st2
                            _ok2 = True
                            self._log(f"{_why} 渲染兜底成功: len={len(html2)} "
                                      f"({self._render_success_count}/{_render_budget})")
                    except Exception as e2:
                        logger.debug(f"403 render: {e2}")
                    if not _ok2:
                        self._render_success_count = max(
                            0, getattr(self, '_render_success_count', 1) - 1)

            # [FIXED & MODIFIED] v2.10.5 P0-3 防御评估时序：原在渲染兜底前 evaluate/judge——
            # 403 在渲染成功前已被记为"反爬风控"→ 单次 403 = risk 9 → RED（全局 5/s + 600s 冷却），
            # 而 csdn/163/sina 等国内门户"静态 403 但渲染能拿"会被误伤（爬一次 csdn 全局废 10 分钟）。
            # 现挪到渲染兜底之后，用渲染成功后的 status 评估；渲染成功（status 200）不再记风控。
            _eval_status = status  # 渲染后 status（403→200 则不记风控）
            if self.crawler._defense_mode == 'shield' and self.crawler.sentinel:
                await self.crawler.sentinel.evaluate({
                    "domain": domain, "status_code": _eval_status,
                    "anti_bot_detected": _eval_status in (403, 429), "latency": latency,
                })
            elif self.crawler._defense_mode == 'habakiri' and self.crawler.oracle:
                consec_fails = 0
                if proxy and proxy in self.exit_mgr.nodes:
                    consec_fails = self.exit_mgr.nodes[proxy].consecutive_fails
                await self.crawler.oracle.judge({
                    "domain": domain, "status_code": _eval_status,
                    "anti_bot_detected": _eval_status in (403, 429),
                    "consecutive_fails": consec_fails,
                })

            if status == 200 and html_sanitized:
                # [v2.17 B2-S1] 解析段组件化（纯代码搬移——规则翻页入队等副作用语义原样保持）
                data = await self._parse_page(html_sanitized, url, job, uh)
                # [v2.17 B4b] 证据驱动动态优先级（默认关——规则命中提前/空壳后排）
                if getattr(self.crawler, '_dynamic_priority', False):
                    try:
                        from .link_scoring import priority_delta_from_data as _pd
                        _dlt = _pd(data)
                        if _dlt:
                            await self.frontier.adjust_priority(uh, _dlt)
                    except Exception:
                        pass
                # [v2.17 0-5b] 占位渲染前移到质量闸之前（预算内；成功则后续质量/导出
                # 基于渲染后数据——真·空壳等待渲染，不再被闸一票否决）
                data, html_sanitized = await self._maybe_render_placeholder(
                    data, html_sanitized, url)
                # [v2.18 P1-6] 质量闸前移：低分页在写 extracted/jsonl/csv 之前拒收隔离
                # （旧序先 _persist_export 再 _validate_quality——"隔离"承诺落空：拒收页
                # 已落盘，且 _persist_export 与拒收路径各计一次 done → 双计数）
                if await self._validate_quality(data, url, uh, domain, status, t_start, proxy,
                                                leased_at=job.get('leased_at')) == "rejected":
                    return

                # [v2.19 P1] CAS 打卡**前移到持久化之前**：原序 persist → enqueue → CAS，
                # 使"CAS 失守时产物已落盘"——注释声称的"丢弃本结果防重复劳动"只对 done
                # 标记成立，对已写入的 jsonl/extracted/视频队列不成立（会重复导出）。
                # 现改为：先赢 CAS 再落盘；失守则一行不写，直接丢弃。
                if job.get('leased_at') is not None:
                    _cas = await self.frontier.mark_done_checked(uh, job['leased_at'])
                    if _cas is False:
                        # 确实易主：新持有者在跑，丢弃本结果（一行不写）
                        self._log(f"租约已易主，丢弃过期结果（未落盘）: {url[:60]}", "warn")
                        # [v2.18 P1-8] 不计 done：新持有者会记账
                        await self.exit_mgr.report_result(proxy, True, latency)
                        return
                    if _cas is None:
                        # [v2.19.6] CAS 落库失败（DB/磁盘异常）——结果**未落盘**，
                        # 与"租约易主"是两回事：抛出让外层标失败重试（已落盘部分除外）
                        raise RuntimeError("CAS 落库失败（DB 异常），本页结果未落盘")

                # [v2.17 B2-S4] 持久化/管道/导出组件化（含统计计数）
                data = await self._persist_export(data, url, domain, uh)

                # [v2.17 B2-S3] 入队段组件化（视频/图片/B站/电商/通用渲染——保序搬移）
                data, html_sanitized = await self._enqueue_and_render(
                    data, html_sanitized, url, domain)
                await self._discover_links(data, url, html_sanitized, job, uh)

                # 无租约任务：保持旧语义（此刻才置 done）
                if job.get('leased_at') is None:
                    await self.frontier.mark_done(uh)
                await self._finalize_done(uh, url, domain, status, latency, proxy, data)
                # [v2.17 E-P2] 身份捆绑反馈：成功清零坏计数（封锁判定保守——不算零碎失败）
                self.crawler._identity_feedback(domain, _sess, fail=False)
            else:
                await self._finalize_fail(uh, url, domain, status, latency, proxy)
                # [v2.17 E-P2] 风控/限流状态码（403/429/410/503）→ 封锁计数
                self.crawler._identity_feedback(domain, _sess, fail=True, status=status)

        except Exception as e:
            logger.error(f"Job failed: {url} ({e})")
            self._update_progress('failed')
            # [FIXED & MODIFIED] 验证码/求解类失败快速失败不重试（能力不足，重试 3 轮也是白等——B站 geetest 实测 93s 空耗）
            err_type = self._classify_error(e)
            retryable = err_type not in ('captcha', 'challenge')
            # [v2.17 4.3] 平台语义异常（KianaAPIError）结构化落 platform/code：
            # 审计可 GROUP BY "哪个平台什么码"；其余异常走 error_type 字符串
            _plat = getattr(e, "platform", None)
            _code = getattr(e, "code", None)
            if _cas is True:
                # [v2.19 P1] CAS 已赢（状态已是 done）→ 不标 failed，避免把 done 打回 retry
                # 造成整页重爬与重复导出。
                # [v2.19.6 修复·审查发现] 但此时"产物/出链发现"可能**永久缺失**且不重试——
                # 必须用**专属错误码**落库，否则"done 数量 vs 落盘数量"的差额只能靠人工翻日志
                # （原实现只记 generic 的 type(e).__name__，审计里看不出这是"打卡后残缺页"）。
                logger.warning(f"打卡后异常（状态保持 done，产物可能不全）: {url[:60]} ({type(e).__name__})")
                await self.frontier.write_error(
                    uh, "POST_CAS_INCOMPLETE",
                    f"完成打卡后异常，产物/出链可能缺失: {type(e).__name__}: {e}"[:500],
                    platform=_plat, code=_code)
            else:
                await self.frontier.mark_failed(uh, retry=retryable)
                await self.frontier.write_error(uh, type(e).__name__, str(e),
                                                platform=_plat, code=_code)
            # [v2.17 E-P2] 异常侧封锁反馈（IPBlockError/captcha/challenge 等→整包退役计数）
            self.crawler._identity_feedback(domain, _sess, fail=True, err=err_type)
            self.adaptive.record(False)
            # 修复：传入异常类名而非完整字符串，与冷却映射表匹配
            await self.exit_mgr.report_result(proxy, False, error_type=err_type)
        finally:
            # [v2.18 P1-3] 心跳任务必须先取消并等待退出（不留孤儿）
            if _hb_task is not None:
                _hb_task.cancel()
                await asyncio.gather(_hb_task, return_exceptions=True)
            await self.crawler.concurrency.release_all(domain, proxy)
            # 修复：释放 exit_mgr 中代理的 inflight 计数（原来遗漏导致泄漏）
            await self.exit_mgr.release(proxy)

    async def _finalize_done(self, uh, url, domain, status, latency, proxy, data):
        """成功打卡/导出/调优（v2.10.6 B1 从 process_job 抽出的行为等价段）"""
        self.adaptive.record(True, latency, status_code=status)
        # [FIXED & MODIFIED] v2.10.5c 接线 adaptive_v2.record：多因子逐域调优
        # （原 record 从未被调 → _tune 恒空转，只有 psutil 压力分支生效）
        try:
            pc = getattr(self.crawler, 'pressure_controller', None)
            if pc is not None and hasattr(pc, 'record'):
                pc.record(domain, True, latency, ban_detected=(status in (403, 429)))
        except Exception:
            pass
        # [FIXED & MODIFIED] v2.10.5 P1-7 AutoscaledPool 接线（record 此前零调用 → 幻影池）
        try:
            if getattr(self.crawler, 'autoscale_pool', None):
                self.crawler.autoscale_pool.record(True, latency)
        except Exception:
            pass
        await self.exit_mgr.report_result(proxy, True, latency)

    async def _finalize_fail(self, uh, url, domain, status, latency, proxy):
        """失败打卡/错误表（v2.10.6 B1 抽出；含 D1 HTTP 状态补录）"""
        # 按状态码决定重试：404/410 等永久失败不重试（避免 30s×3 轮白等）；5xx/429/403 可重试
        # [v2.17 E-P1-3] 429/503 = 暂态限流 → 冷却等待但不消耗 retry_count（限流≠失败）
        retryable = status >= 500 or status in (429, 403)
        throttled = status in (429, 503)
        await self.frontier.mark_failed(uh, retry=retryable, throttled=throttled)
        # [FIXED & MODIFIED] v2.10.6 D1 errors 表补 HTTP 状态：5xx/429/403/404 等
        # HTTP 失败此前只写 pages 表不记 errors（审计不完整）→ 补齐错误档案
        if status >= 400 and status != 200:
            try:
                await self.frontier.write_error(uh, f"HTTP_{status}", f"HTTP {status} for {url}")
            except Exception:
                pass
        self._update_progress('failed')
        self.adaptive.record(False, latency, status_code=status)
        try:
            pc = getattr(self.crawler, 'pressure_controller', None)
            if pc is not None and hasattr(pc, 'record'):
                pc.record(domain, False, latency, ban_detected=(status in (403, 429)))
        except Exception:
            pass
        try:
            if getattr(self.crawler, 'autoscale_pool', None):
                self.crawler.autoscale_pool.record(False, latency)
        except Exception:
            pass
        await self.exit_mgr.report_result(proxy, False, latency, error_type=str(status))

    async def _human_delay(self, domain=None):
        delay = 3 + random.expovariate(1 / 3)
        if domain and domain in self.crawler._shielded_domains:
            delay += random.uniform(10, 30)
        await asyncio.sleep(min(delay, 45))

    @staticmethod
    def _language_to_country(lang):
        return lang.split('-')[-1].upper() if lang and '-' in lang else None

    def _extract_links(self, data, base_url, raw_html=""):
        links = []
        # 增强：使用 FullSourceExtractor（a/img/script/iframe/form/meta/注释/JS事件全源提取）
        extractor = getattr(self.crawler, 'link_extractor', None)
        cleaner = getattr(self.crawler, 'cleaner', None)
        if extractor and raw_html:
            try:
                raw_links = extractor.extract(raw_html, base_url)
                # 过滤垃圾链接 + 只保留 http(s) + [FIXED & MODIFIED] 排除 XML namespace/协议占位
                # （B站实测 http://www.w3.org/2000/svg 等 xmlns 值被当链接入队，重试 4 次浪费 30s+）
                links = [l for l in raw_links if l.startswith(('http://', 'https://'))
                         and not any(ns in l for ns in ('w3.org/', 'schemas.microsoft', 'xmlns.com', 'purl.org'))
                         and not self._is_junk_link(l)
                         and (not cleaner or not cleaner.filter_junk_url(l))]
            except Exception:
                links = []
        if not links:
            for link in data.get("links", {}).get("internal", []):
                if any(kw in link for kw in ["login", "logout", "cart", "register"]):
                    continue
                links.append(link)
        return links

    def _is_junk_link(self, url: str) -> bool:
        """[v2.17 B3] 垃圾链接判定——委托 link_scoring 过滤链（默认 builtin_junk =
        原内联逻辑逐字迁移；返回 True=垃圾）。"""
        from . import link_scoring as _ls
        return not _ls._filter_junk(url)

    def _is_static_link(self, url: str) -> bool:
        """[v2.17 B3] 静态资源判定——委托 link_scoring（默认 builtin_static = 原逻辑）。"""
        from . import link_scoring as _ls
        return not _ls._filter_static(url)

    def _url_label(self, url):
        """[v2.17 B3] 路由标签提供器（link_scoring 扩展点注入用）——无 router 返回 None。"""
        url_router = getattr(self.crawler, 'url_router', None)
        if url_router is None:
            return None
        try:
            return url_router.extract_label(url)
        except Exception:
            return None

    def _calc_priority(self, url):
        # [v2.17 B3] 委托扩展点（默认 builtin_basic=原内联逻辑逐字；插件可 front 覆盖）
        from . import link_scoring as _ls
        return _ls.score_url(url)

    def _get_crawl_filters(self):
        """从爬虫配置中获取内容过滤设置（由 GUI 传入）"""
        return getattr(self.crawler, '_crawl_filters', None)

    def _apply_content_filters(self, data, filters):
        """
        根据用户勾选的内容过滤设置，移除不需要的数据类型。
        filters 为 {type: bool} 字典，False 表示该类型被屏蔽。
        """
        if not filters:
            return data
        # 关键词白名单过滤：仅保留包含任一关键词的链接（launcher 内容过滤）
        keywords = filters.get('keywords', []) if isinstance(filters, dict) else []
        if keywords:
            keep_links = []
            for link in data.get("links", {}).get("internal", []):
                if any(k.lower() in link.lower() for k in keywords):
                    keep_links.append(link)
            data.setdefault("links", {})["internal"] = keep_links
        # 映射：过滤键 -> data 中的字段
        filter_map = {
            'video': ['videos', 'video_sources', 'm3u8_urls'],
            'images': ['images'],
            'documents': ['documents'],
            'audio': ['audio'],
            'metadata': ['json_ld', 'og', 'meta'],
            'links': ['links'],
            'text_content': ['text'],
        }
        for filter_key, fields in filter_map.items():
            if not filters.get(filter_key, True):
                for field in fields:
                    if field in data:
                        # 修复：dict 类型清空为 {} 而非 []，保持类型一致性
                        if isinstance(data[field], dict):
                            data[field] = {}
                        elif isinstance(data[field], list):
                            data[field] = []
                        else:
                            data[field] = ''
        return data

    @staticmethod
    def _classify_error(e: Exception) -> str:
        """将异常分类为短键，用于自适应冷却映射表匹配"""
        error_name = type(e).__name__.lower()
        error_str = str(e).lower()
        if 'timeout' in error_name or 'timeout' in error_str:
            return 'timeout'
        if 'connection' in error_name or 'connection' in error_str:
            return 'connection_error'
        if 'ssl' in error_name or 'ssl' in error_str:
            return 'ssl_error'
        if 'captcha' in error_str or 'challenge' in error_str:
            return 'captcha'
        return error_name

    def _update_progress(self, status: str):
        """更新爬虫内存进度计数器"""
        try:
            p = self.crawler._progress
            p[status] = p.get(status, 0) + 1
            p['total'] = max(p.get('total', 0), p.get('done', 0) + p.get('pending', 0))
        except Exception:
            pass

    def _save_extracted_file(self, data: dict, url: str):
        """将提取的数据保存到磁盘JSON文件"""
        import hashlib
        import json
        from pathlib import Path
        from urllib.parse import urlparse
        try:
            dl = self.project.config.get('download_path', '')
            if not dl:
                return
            dest = Path(dl)
            domain = urlparse(url).netloc or 'unknown'
            # Windows 安全目录名：域名含端口(:)等非法字符 → 替换
            import re as _re
            domain = _re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', domain)[:120] or "unknown"
            ddir = dest / domain
            ddir.mkdir(parents=True, exist_ok=True)
            fname = hashlib.md5(url.encode()).hexdigest()[:12] + '.json'
            with open(ddir / fname, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.info(f"Saved: {ddir / fname}")
        except Exception as e:
            logger.error(f"Failed to save file for {url}: {e}")
