"""Kiana 管线/分类/缓存件（自研实现，随版本演进）

组成：URL 模式路由、声明式 ItemPipeline（链化）、限流/页面分类器/磁盘 HTTP 缓存/
信号总线。各件为工程通用模式（路由/分类/缓存均为业界常规概念）的 Kiana 自研实现——
具体结构/接口细节（如 HttpCache 容量上限+TTL 语义、ItemPipeline 链化骨架）均在本仓库
commit 链内逐版演进，无外部代码对应关系。
来源与合规声明见 docs/来源与合规声明.md。"""
import asyncio
import logging
import time
import re
import hashlib
import json
from dataclasses import dataclass,field
from typing import Callable,Optional
from collections import defaultdict
from pathlib import Path
logger=logging.getLogger(__name__)

# ═══ P1-1: 路由系统（按模式把 URL 分派给不同处理器）═══
class Router:
    """Route URLs to different handlers based on pattern matching"""
    def __init__(self):
        self._routes:list[tuple[str,Callable]]=[]
        self._default:Optional[Callable]=None

    def route(self, pattern: str):
        """Decorator: register handler for URL pattern"""
        def decorator(fn):
            self._routes.append((pattern,fn))
            return fn
        return decorator

    def default(self, fn: Callable):
        """Register default handler"""
        self._default=fn
        return fn

    def match(self, url: str) -> Callable:
        for pattern,fn in reversed(self._routes):
            if re.search(pattern,url,re.IGNORECASE):
                return fn
        return self._default

    def extract_label(self, url: str) -> str:
        """Auto-detect page type from URL pattern"""
        patterns=[
            (r'/article[s]?/|/post[s]?/|/blog/|/news/','ARTICLE'),
            (r'/product[s]?/|/item/|/goods/|/detail/','PRODUCT'),
            (r'/video[s]?/|/watch|/play/|/live/','VIDEO'),
            (r'/search|/find|/query|\\?.*q=|\\?.*s=','SEARCH'),
            (r'/category/|/tag/|/topic/|/list/|/page/\\d+','LISTING'),
            (r'/api/|/graphql|/rest/','API'),
            (r'/login|/signin|/auth','AUTH'),
            (r'/about|/contact|/faq|/terms|/privacy','STATIC'),
            (r'.*','PAGE'),
        ]
        for pat,label in patterns:
            if re.search(pat,url,re.IGNORECASE):
                return label
        return 'PAGE'

# ═══ P1-2: 声明式条目管道（字段声明 + 逐级处理）═══
@dataclass
class PipelineItem:
    raw: dict
    cleaned: dict = field(default_factory=dict)
    validated: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    dropped: bool = False

class ItemPipeline:
    """链式处理器：dedup → clean → validate → [custom...]（v2.17 B4a 链化）。

    默认链 = 原内联步骤拆分（语义逐字保持：URL 去重 / 空白清洗+结构字段保留 /
    空标题空正文校验）；`set_chain` 可整链编排（每步 (item, url) -> dict|None，
    None=丢弃——与默认步骤同约定）；`add_processor` 沿用旧 API（None 保留原文语义）。"""
    def __init__(self):
        self._custom: list = []
        self._seen_keys: set = set()
        self.stats = {'total': 0, 'dropped_dup': 0, 'dropped_invalid': 0, 'stored': 0}
        self._chain = [self._step_dedup, self._step_clean, self._step_validate]

    def _step_dedup(self, item: dict, url: str) -> Optional[dict]:
        """按 URL 哈希去重（已见 → None=丢弃并计数）"""
        key = hashlib.md5(url.encode()).hexdigest()
        if key in self._seen_keys:
            self.stats['dropped_dup'] += 1
            return None
        self._seen_keys.add(key)
        return item

    def _step_clean(self, item: dict, url: str) -> Optional[dict]:
        """空白清洗 + 结构字段保留（空值剔除仅限非结构字段）"""
        cleaned = dict(item)
        for k in ['title', 'description', 'text']:
            if k in cleaned:
                cleaned[k] = re.sub(r'\s+', ' ', str(cleaned[k])).strip()
        _STRUCT_KEYS = {'json_ld', 'og', 'links', 'images', 'videos', 'torrents',
                        'canonical', 'description', 'author', 'date', 'page_type'}
        return {k: v for k, v in cleaned.items() if v or k in _STRUCT_KEYS}

    def _step_validate(self, item: dict, url: str) -> Optional[dict]:
        """空标题且空正文 → 无效丢弃"""
        if not item.get('title') and not item.get('text'):
            self.stats['dropped_invalid'] += 1
            return None
        return item

    def set_chain(self, steps: list):
        """整链编排（替换默认 dedup/clean/validate）——每步 (item, url) -> dict|None。"""
        self._chain = list(steps)
        return self

    def add_processor(self, fn: Callable):
        """追加自定义处理器（沿用旧 API；None 返回值保留原文——旧语义）"""
        self._custom.append(fn)
        return self

    def process(self, item: dict, url: str = "") -> Optional[dict]:
        self.stats['total'] += 1
        cur = item
        for step in self._chain:
            # 链元素约定：fn(item, url) -> dict|None（None=丢弃；默认步骤为绑定方法，
            # 自定义 callable 同签名——无需感知 pipeline 实例）
            try:
                cur = step(cur, url)
            except Exception:
                cur = None
            if cur is None:
                return None
        for proc in self._custom:
            try:
                cur = proc(cur) or cur
            except Exception:
                pass
        self.stats['stored'] += 1
        return cur


# ═══ 域级自适应延迟（Kiana 自研：目标延迟闭环，min/max 钳制）═══
# [v2.19 清理] 原 `AutoThrottleV2`（按域自适应延迟）已删除：全仓**零引用**，
# 且与 enhancements.AutoThrottle（被 tests/test_core.py 使用）职责重复——
# 自适应节流的实际生效路径是 rate_limiter（双档令牌桶 + 指数退避）。
# 保留会造成"两份实现、改一处漏一处"的维护陷阱。


class BrowserPool:
    """Fixed-size pool of browser contexts with warming and recycling"""
    def __init__(self,min_size:int=1,max_size:int=5,lifetime_seconds:int=1800):
        self.min_size=min_size;self.max_size=max_size;self.lifetime=lifetime_seconds
        self._instances:list[dict]=[]
        self._lock=asyncio.Lock()

    async def acquire(self, domain: str):
        async with self._lock:
            # Try to reuse same-domain context
            for inst in self._instances:
                if inst['status']=='IDLE' and inst.get('domain')==domain:
                    if time.time()-inst['born']<self.lifetime:
                        inst['status']='BUSY'
                        return inst
            # Find any IDLE instance
            for inst in self._instances:
                if inst['status']=='IDLE':
                    inst['status']='BUSY';inst['domain']=domain
                    return inst
            # Create new if under max
            if len(self._instances)<self.max_size:
                inst={'status':'BUSY','domain':domain,'born':time.time()}
                self._instances.append(inst)
                return inst
        # Wait for IDLE
        while True:
            await asyncio.sleep(0.5)
            for inst in self._instances:
                if inst['status']=='IDLE':
                    async with self._lock:
                        inst['status']='BUSY';inst['domain']=domain
                        return inst

    async def release(self, inst: dict):
        async with self._lock:
            inst['status']='IDLE'
            # Kill old instances
            if time.time()-inst['born']>self.lifetime:
                self._instances.remove(inst)

# ═══ P1-5: Page Type Auto-Classifier ═══
class PageClassifier:
    """页面类型分类（URL + HTML 特征启发式；Kiana 自研启发集）"""
    @staticmethod
    def classify(url: str, html: str = "") -> str:
        patterns=[
            (r'/article|/post|/blog|/news|/story','ARTICLE'),
            (r'/product|/item|/goods|/shop|/p/\\d+','PRODUCT'),
            (r'/video|/watch|/play|/live|/bilibili|/youtube','VIDEO'),
            (r'/search|/find|/query|\\?q=|\\?s=|\\?search','SEARCH'),
            (r'/category|/tag|/topic|/list|/catalog|/page/\\d+','LISTING'),
            (r'/login|/signin|/auth|/register','AUTH'),
            (r'/api/|/graphql|/rest/|/v\\d+/','API'),
            (r'/about|/contact|/faq|/help|/terms|/privacy','STATIC'),
        ]
        for pat,label in patterns:
            if re.search(pat,url,re.IGNORECASE):
                return label
        if html:
            if '<article' in html or 'articleBody' in html:return 'ARTICLE'
            if 'og:type" content="product"' in html:return 'PRODUCT'
            if 'og:type" content="video"' in html or '<video' in html:return 'VIDEO'
        return 'PAGE'

# ═══ 磁盘 HTTP 缓存（Kiana 自研：ETag/Last-Modified 协商 + TTL + 容量上限）═══
class HttpCache:
    """ETag + Last-Modified based disk cache to avoid redundant requests
    [FIXED & MODIFIED] v2.14 阶段3 容量上限：原只写不删（TTL 只影响读）→ 磁盘慢性泄漏。
    条目上限 400（超限按 mtime 删最旧 10%）。"""
    MAX_ENTRIES = 400

    def __init__(self, cache_dir: Optional[Path] = None, ttl: int = 3600):
        # [v2.16.1] 便携版补全：默认目录接 config.data_root()（原写死 ~/.kiana_cache——
        # KIANA_PORTABLE 下不随行，绿色版"换机即用"半接线）
        try:
            from .config import data_root
            _default = Path(data_root()) / 'http_cache'
        except Exception:
            _default = Path.home() / '.kiana_cache'
        self.dir = Path(cache_dir or _default)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl  # [v2.16 M6] 304 协商 TTL 可配（config.http_cache_ttl）

    def get(self, url: str) -> Optional[dict]:
        key=hashlib.sha256(url.encode()).hexdigest()[:16]
        meta_file=self.dir/f'{key}.meta'
        content_file=self.dir/f'{key}.html'
        if meta_file.exists() and content_file.exists():
            try:
                meta=json.loads(meta_file.read_text(encoding='utf-8'))
                age=time.time()-meta.get('fetched_at',0)
                if age<self.ttl:  # [v2.16] TTL 可配（默认 1h）
                    return {'content':content_file.read_text(encoding='utf-8'),'meta':meta}
                # [FIXED & MODIFIED] v2.15 阶段2 stale 返回（真增量爬取）：过期条目带
                # stale 标记返回——调用方持 ETag/Last-Modified 发条件请求，304 复用
                return {'content':content_file.read_text(encoding='utf-8'),'meta':meta,'stale':True}
            except Exception:pass
        return None

    def store(self, url: str, content: str, headers: dict = None):
        key=hashlib.sha256(url.encode()).hexdigest()[:16]
        (self.dir/f'{key}.html').write_text(content,encoding='utf-8')
        # [v2.19.7 安全·扫描发现] meta 里的 url 只是人读的调试信息（查表用的是上面
        # sha256(url) 键，get() 从不用 meta['url'] 发请求）→ 落盘前抹掉 ?token=/session=
        # 这类参数值，缓存目录被翻/被同步走时不再泄漏可复用凭据。
        try:
            from .sanitizer import sanitize_url
            _disp_url = sanitize_url(url)
        except Exception:
            _disp_url = url
        meta={'url':_disp_url,'fetched_at':time.time(),'etag':str(headers.get('etag','')) if headers else '',
              'last_modified':str(headers.get('last-modified','')) if headers else ''}
        (self.dir/f'{key}.meta').write_text(json.dumps(meta, ensure_ascii=False),encoding='utf-8')
        self._enforce_cap()

    def _enforce_cap(self):
        """超 MAX_ENTRIES 删最旧 10%（浅层防泄漏，不做精确 LRU）"""
        try:
            files = list(self.dir.glob("*.html"))
            if len(files) <= self.MAX_ENTRIES:
                return
            files.sort(key=lambda p: p.stat().st_mtime)
            for p in files[:max(1, len(files) // 10)]:
                p.unlink(missing_ok=True)
                p.with_suffix(".meta").unlink(missing_ok=True)
        except Exception:
            pass

# ═══ P1-7: 信号总线 + 生命周期钩子 ═══
class SignalBus:
    """Publish-subscribe event bus for crawler lifecycle"""
    def __init__(self):
        self._handlers:dict[str,list[Callable]]=defaultdict(list)

    def on(self, event: str):
        def decorator(fn):
            self._handlers[event].append(fn)
            return fn
        return decorator

    async def emit(self, event: str, **kwargs):
        for fn in self._handlers.get(event,[]):
            try:
                if asyncio.iscoroutinefunction(fn):
                    await fn(**kwargs)
                else:fn(**kwargs)
            except Exception as e:
                logger.error(f"Signal {event} handler error: {e}")
