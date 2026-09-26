# -*- coding: utf-8 -*-
"""URL 评分/链接过滤扩展点（v2.17 B3，P2 架构项）

把 page_processor 内联的 `_calc_priority`（URL 打分）与 `_is_junk_link`/`_is_static_link`
（链接过滤）搬为**可注册的扩展点**——默认实现 = 原逻辑逐字迁移（零行为变化），
第三方/未来能力（热度信号、LLM 评分器、规则库过滤）以 register_* 挂入，无需改主链路。

设计：
  score_url(url, domain) -> int     scorer 按注册顺序执行，首个返回非 None 者生效
  link_ok(url) -> bool              过滤链全部通过才保留（任一 False 即丢弃）
  register_scorer(name, fn, front=False) / register_filter(name, fn)   插件入口
  set_label_provider(fn)            路由标签提供器（crawler.url_router.extract_label 包装）

使用方：PageProcessor._calc_priority / _discover_links（委托本模块，语义与原内联一致）。
"""
import re
from urllib.parse import urlparse

SCORERS: dict = {}    # name -> fn(url: str, domain: str) -> int|None（None=交给下一个）
FILTERS: dict = {}    # name -> fn(url: str) -> bool（True=通过/保留）
_ORDER: list = []     # scorer 执行顺序（name 列表）
_LABEL_PROVIDER = None


def register_scorer(name, fn, front=False):
    """注册打分器；front=True 插到最前（优先级覆盖）。同名覆盖。"""
    SCORERS[name] = fn
    if front:
        if name in _ORDER:
            _ORDER.remove(name)
        _ORDER.insert(0, name)
    elif name not in _ORDER:
        _ORDER.append(name)


def register_filter(name, fn):
    """注册链接过滤器（全链通过才保留）。同名覆盖。"""
    FILTERS[name] = fn


def set_label_provider(fn):
    """设置 URL 路由标签提供器（PageProcessor.__init__ 注入；
    返回 None 表示无路由标签——走路径关键词回退）。"""
    global _LABEL_PROVIDER
    _LABEL_PROVIDER = fn


def score_url(url, domain="") -> int:
    """URL 打分：按注册顺序首个非 None 生效；无则回退 2（延续原默认语义）。"""
    for name in _ORDER:
        try:
            val = SCORERS[name](url, domain)
        except Exception:
            val = None
        if val is not None:
            return int(val)
    return 2


def link_ok(url) -> bool:
    """链接过滤链：任一过滤器返回 False（丢弃）→ 不通过；全过 → 通过。"""
    for name, fn in FILTERS.items():
        try:
            if not fn(url):
                return False
        except Exception:
            return False
    return True


# ═══ 默认实现（原 page_processor 内联逻辑逐字迁移）═══

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


def _scorer_basic(url: str, domain: str):
    """基础语义打分（原 `_calc_priority` 逐字）：路由标签 > 路径关键词 > 默认 2。"""
    label = None
    if _LABEL_PROVIDER is not None:
        try:
            label = _LABEL_PROVIDER(url)
        except Exception:
            label = None
    if label is not None:
        _label_priority = {'ARTICLE': 3, 'PRODUCT': 4, 'VIDEO': 3, 'SEARCH': 5,
                           'LISTING': 5, 'API': 1, 'AUTH': 6, 'STATIC': 6, 'PAGE': 2}
        return _label_priority.get(label, 2)
    path = (urlparse(url).path or "").lower()
    if any(kw in path for kw in ["article", "post", "detail", "news"]):
        return 3
    if "/page/" in path or "page=" in path:
        return 5
    return 2


_ADJUNK = ("installads.net", "snai.it", "bd742.com", "hotlog.ru", "exodus.gr",
           "doubleclick.net", "googlesyndication.com", "googleadservices.com",
           "adservice", "amazon-adsystem.com", "adnxs.com", "pubmatic.com",
           "criteo.com", "taboola.com", "outbrain.com", "propellerads.com",
           "media.net", "matomo", "piwik", "sentry.io", "newrelic.com",
           "mixpanel.com", "segment.io", "hotjar.com", "fullstory.com",
           "clarity.ms", "mouseflow.com", "pingdom.net", "disqus.com",
           "addthis.com", "sharethis.com", "platform.twitter.com",
           "connect.facebook.net", "facebook.com/tr", "platform.linkedin.com",
           "addtoany.com", "platform.tumblr.com")


def _filter_junk(url: str) -> bool:
    """垃圾链接过滤（原 `_is_junk_link` 逐字）：形态兜底 + 域名黑名单 + /api/ 路径。"""
    # 形态兜底：URL 只能含 ASCII 可见字符（无空白/控制符/尖括号/引号/汉字）
    if re.search(r'[\s<>"\'`\u4e00-\u9fff]', url):
        return False
    try:
        _up = urlparse(url)
        host = (_up.hostname or "").lower()
        if host.startswith(("api.", "cm.", "ad.", "ads.", "track.", "tracking.", "log.",
                            "logs.", "stat.", "stats.", "metrics.", "beacon.", "sentry.",
                            "analytics.", "report.", "img.", "static.", "cms.")):
            return False
        if any(k in host or host.endswith(k) for k in _ADJUNK):
            return False
        if "/api/" in (_up.path or "").lower():
            return False
        return True
    except Exception:
        return False


def _filter_static(url: str) -> bool:
    """静态资源/媒体流过滤（原 `_is_static_link` 逐字）。"""
    try:
        _up = urlparse(url)
        path = (_up.path or "").lower()
        ext = path[path.rfind("."):] if "." in path else ""
        if ext in _STATIC_EXTS:
            return False
        host = (_up.netloc or "").lower()
        if any(host == d or host.endswith("." + d) for d in _MEDIA_CDN_DOMAINS):
            return False
    except Exception:
        pass
    return True


def priority_delta_from_data(data) -> int:
    """[v2.17 B4b] 证据驱动优先级微调（dynamic_priority 开启时主链路调用）：
    规则命中 → -1（提前处理）；无标题无正文（空壳/占位）→ +3（后排，等解析证据更
    完整的页面/或直接低质流）。默认 0 = 不动。"""
    if data is None:
        return 0
    d = data
    if d.get('rule'):
        return -1
    if not (d.get('title') or d.get('text')):
        return 3
    return 0


register_scorer("builtin_basic", _scorer_basic)
register_filter("builtin_junk", _filter_junk)
register_filter("builtin_static", _filter_static)
