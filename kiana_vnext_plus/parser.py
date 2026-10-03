import json
import hashlib
import asyncio
import logging
from urllib.parse import urljoin, urlparse
# [实测 v2.19.8 实体解码] 显式 `from html import unescape`：本模块的解析函数
# **第一个形参就叫 `html`**（原始 HTML 文本），`import html` 的名字在里面会被
# 形参遮蔽 → `html.unescape` 变成对字符串取属性（AttributeError）。
from html import unescape as _html_unescape

# [FIXED & MODIFIED] v2.9.2 缺 logger 定义：v2.6.5 trafilatura 异常兜底用 logger.debug
# 但模块从未定义 logger → trafilatura 抛异常时 NameError 传播 → "Job failed: name 'logger'
# is not defined"（用户 GUI 实测 0 pages 根因）——B站风控页触发 trafilatura 异常路径
logger = logging.getLogger(__name__)

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

try:
    import trafilatura
except Exception:
    # [FIXED & MODIFIED] v2.6.6 捕获所有异常（不只 ImportError）：trafilatura 2.2.0 在
    # 打包环境（onefile _MEI 路径）可能 import 即抛 configparser.NoOptionError
    # （"No option 'min_extracted_size' in section: 'DEFAULT'"——用户 GUI 实测 0 pages 根因）
    trafilatura = None

if trafilatura is not None:
    try:
        # [FIXED & MODIFIED] v2.6.6 配置键兜底注入：打包环境 settings.cfg 可能未加载 →
        # DEFAULT_CONFIG 缺 min_extracted_size 等键 → extract() 内部 config.get 抛
        # NoOptionError → 整页处理失败（用户 BV16Uud6JEN5 日志实锤）。手动注入保证键存在。
        import trafilatura.settings as _traf_settings
        for _k, _v in (("min_extracted_size", "250"), ("min_extracted_comm_size", "1"),
                       ("min_output_size", "1"), ("min_output_comm_size", "1")):
            if not _traf_settings.DEFAULT_CONFIG.has_option("DEFAULT", _k):
                _traf_settings.DEFAULT_CONFIG.set("DEFAULT", _k, _v)
    except Exception:
        pass


def extract_metadata(html, url, base_url=None):
    """从 HTML 提取元数据：标题、描述、正文、链接、图片、视频等

    [实测 v2.19.8 短链 bug] `base_url` = **本次请求实际落到的地址**（跟随重定向后的
    终点），仅用于解析页面内的**相对链接**；`url` 仍是这次任务的请求 URL（`data["url"]`
    与落库/去重口径不变——它是任务的标识，不是解析基址）。

    为什么必须分开：短链种子（b23.tv/xxx → www.bilibili.com/video/BV…）下，页面里的
    `/video/BVxxx` 是**相对 www.bilibili.com 的**。以短链主机为基址会拼出
    `https://b23.tv/video/BVxxx`——这个地址**不存在**（短链服务只认它自己发的短码），
    于是每一页出链都 404（用户实测 19/31 页失败的唯一原因）。
    `base_url` 缺省/为空时退回 `url` —— **未发生重定向时行为与改前逐字一致**。
    """
    # 相对链接的解析基址：终到地址优先，缺失则退回请求 URL
    base = base_url or url
    data = {
        "url": url, "title": "", "description": "", "author": "", "date": "",
        "text": "", "canonical": None, "json_ld": [], "og": {}, "images": [],
        "links": {"internal": [], "external": []}, "videos": [], "torrents": [],
        # [FIXED & MODIFIED] v2.10.6 C4 字段扩展（13→17）：lang/og:site_name 顶层镜像/
        # published_time/favicon
        "lang": "", "site_name": "", "published_time": "", "favicon": "",
    }
    if not BeautifulSoup:
        return data
    soup = BeautifulSoup(html, "lxml")

    for ld in soup.find_all("script", type="application/ld+json"):
        try:
            data["json_ld"].append(json.loads(ld.string))
        except Exception:
            pass

    for meta in soup.find_all("meta"):
        if meta.get("property", "").startswith("og:"):
            data["og"][meta["property"][3:]] = meta.get("content", "")
    # og 顶层镜像：site_name/published_time 直接进 data（消费者少一层 .og）
    data["site_name"] = data["og"].get("site_name", "")
    data["published_time"] = data["og"].get("article:published_time", "") or \
        data["og"].get("published_time", "") or data["date"]

    # ── 标题：多级回退 [v6 修复·真机实测发现] ──────────────────────────
    # 原实现**只有 `<title>` 一个来源**：
    #     if soup.title and soup.title.string: data["title"] = ...
    # 但**微信公众号文章的 `<title>` 是空的**（标题由 JS 写入 `<h1 id="activity-name">`，
    # 而 `<h1>` 也是空的），真正的标题在 **`og:title`** 里。
    # 后果：真机抓两篇公众号文章，**正文都拿到了（773 / 546 字），标题却是空**，
    # 导出文件名成了 `untitled.md`。
    # 现在按"信息质量从高到低"回退，并在取到 og:title 时**不再被空 `<title>` 覆盖**。
    _title_candidates = []
    if soup.title and soup.title.string:
        _title_candidates.append(soup.title.string.strip())
    _og_title = (data["og"].get("title") or "").strip()
    if _og_title:
        _title_candidates.append(_og_title)
    for _m in soup.find_all("meta"):
        _key = (_m.get("name") or _m.get("property") or "").lower()
        if _key in ("twitter:title", "weibo:article:title"):
            _v = (_m.get("content") or "").strip()
            if _v:
                _title_candidates.append(_v)
    _h1 = soup.find("h1")
    if _h1:
        _v = _h1.get_text(strip=True)
        if _v:
            _title_candidates.append(_v)
    for _c in _title_candidates:
        if _c:
            data["title"] = _c
            break

    # html lang 属性（如 zh-CN）
    if soup.html and soup.html.get("lang"):
        data["lang"] = soup.html.get("lang")

    # favicon（link rel=icon / shortcut icon）
    for _l in soup.find_all("link", rel=True):
        _rel = " ".join(_l.get("rel")) if isinstance(_l.get("rel"), (list, tuple)) else str(_l.get("rel") or "")
        if "icon" in _rel.lower():
            try:
                data["favicon"] = urljoin(base, _html_unescape(_l.get("href", "")))
            except Exception:
                pass
            break

    canonical = soup.find("link", rel="canonical")
    # [v2.18 P1-9] 裸 <link rel="canonical">（无 href）曾让 urljoin(url, None)
    # TypeError 打穿整页解析（此段不在任何 try 内）
    if canonical and canonical.get("href"):
        try:
            data["canonical"] = urljoin(base, _html_unescape(canonical.get("href")))
        except Exception:
            pass

    desc = soup.find("meta", attrs={"name": "description"})
    if desc:
        data["description"] = desc.get("content", "")

    author = soup.find("meta", attrs={"name": "author"})
    if author:
        data["author"] = author.get("content", "")

    date = soup.find("meta", attrs={"name": "date"})
    if date:
        data["date"] = date.get("content", "")

    if trafilatura:
        # [FIXED & MODIFIED] v2.6.5 trafilatura 异常降级兜底——
        # trafilatura 2.2.0 内部 configparser 引用 options.min_extracted_size 配置缺失时
        # 抛 configparser.NoOptionError（No option 'min_extracted_size' in section: 'DEFAULT'）
        # 导致整个页面处理失败（用户实测 0 pages done）→ 兜底用 article/main 纯文本
        try:
            extracted = trafilatura.extract(html, output_format="json", url=url)
        except Exception as _e:
            logger.debug(f"trafilatura extract failed ({type(_e).__name__}): {_e}")
            extracted = None
        if extracted:
            try:
                data["text"] = json.loads(extracted).get("text", "")
            except json.JSONDecodeError:
                pass

    if not data["text"]:
        main = soup.find("article") or soup.find("main") or soup
        data["text"] = main.get_text(separator=" ", strip=True)[:5000]

    # ── 图片：[v6 修复·真机实测发现] 必须支持**懒加载**属性 ──────────────
    # 原实现是 `soup.find_all("img", src=True)` —— **只认 `src`**。
    # 但现代站点（尤其微信公众号）正文图片**全是懒加载**：
    # 真样本统计：页面 27 个 `<img>`，**只有 5 个有 `src`，19 个是 `data-src`**；
    # 正文区 18 张图 **全部** 是 `data-src` → 一张都没抓到（真机跑两篇公众号，
    # images 字段只有 2 条，其中一条还是页面 URL 本身）。
    # 现按优先级取第一个可用地址，跳过 `data:` 内联图与明显占位符。
    _LAZY_IMG_ATTRS = ("src", "data-src", "data-original", "data-lazy-src",
                       "data-echo", "data-url", "data-actualsrc")
    _seen_imgs = set()
    for img in soup.find_all("img"):
        _u = ""
        for _attr in _LAZY_IMG_ATTRS:
            _cand = (img.get(_attr) or "").strip()
            if _cand and not _cand.startswith("data:"):
                _u = _cand
                break
        if not _u:
            continue
        try:
            _abs = urljoin(base, _html_unescape(_u))
        except Exception:
            continue
        if _abs and _abs not in _seen_imgs:
            _seen_imgs.add(_abs)
            data["images"].append(_abs)

    # 修复：原来 find_all("src") 是错误的，应为 find_all("source")
    # <video> 标签内嵌 <source> 子标签，而非 <src> 标签
    for video in soup.find_all("video"):
        for src in video.find_all("source"):
            if src.get("src"):
                data["videos"].append(urljoin(base, _html_unescape(src["src"])))
        if video.get("src"):
            data["videos"].append(urljoin(base, _html_unescape(video["src"])))
    # [FIXED & MODIFIED] 补充 og:video 系列 meta 提取（og:video/og:video:secure_url/og:video:url）
    # 大量站点（新闻/影音站）以 og:video 作为主要视频入口，原实现漏掉
    for prop in ("og:video", "og:video:secure_url", "og:video:url"):
        meta = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        if meta and meta.get("content"):
            data["videos"].append(urljoin(base, _html_unescape(meta["content"])))
    # 去重（video 与 og:video 可能重复）
    seen = set()
    data["videos"] = [v for v in data["videos"] if not (v in seen or seen.add(v))]

    # [实测 v2.19.8 实体解码] `html.unescape`：`<img src>` 的实体由 lxml 顺手解了，
    # 但 **`<a href>` 不会**（实测同一份 HTML：img 的 `&amp;` → `&`，a 的 `&amp;` 原样留存）。
    # 不解的下场不是"多一个字符"而是**把 & 当普通字符百分号编码**：`?a=1&amp;b=2`
    # 原样拼进 URL → 规范化后变成 `?a=1%26amp%3Bb=2`（用户日志里的 `?amp%3Btrackid=`
    # 正是这个形状）→ **服务端收到一个不存在的参数名**，链接必 404。
    # 只解一层：与浏览器对 href 的处理一致（HTML 规范规定属性值按字符引用解码一次）。
    for a in soup.find_all("a", href=True):
        href = urljoin(base, _html_unescape(a["href"]))
        if href.startswith("magnet:") or href.endswith(".torrent"):
            data["torrents"].append(href)

    base_host = urlparse(base).netloc.lower()
    for a in soup.find_all("a", href=True):
        href = urljoin(base, _html_unescape(a["href"]))
        parsed = urlparse(href)
        if parsed.scheme not in ("http", "https"):
            continue
        if parsed.netloc.lower() == base_host:
            data["links"]["internal"].append(href)
        else:
            data["links"]["external"].append(href)

    return data


def flatten_json_ld(json_ld: list) -> list:
    """[v2.17 2-C] JSON-LD 实体扁平化：@type/@name/@author/@datePublished 进
    entities[]——下游检索/RAG/AI 直接用（纯函数，容错；非 dict/空 → []）。"""
    out = []
    for node in json_ld or []:
        if not isinstance(node, dict):
            continue
        t = node.get("@type")
        if isinstance(t, list):
            t = "|".join(str(x) for x in t)
        if not t:
            t = "Thing"
        name = node.get("name") or node.get("headline") or ""
        if name:
            out.append({"type": t, "name": str(name)[:200]})
        # [v2.18 P3-9] author 为单个对象（非数组）时原迭代 dict 产出键字符串 → 实体静默丢
        _authors = node.get("author")
        if isinstance(_authors, dict):
            _authors = [_authors]
        for a in _authors or []:
            if isinstance(a, dict) and a.get("name"):
                out.append({"type": "Person", "name": str(a["name"])[:100]})
        d = node.get("datePublished") or node.get("dateCreated") or ""
        if d:
            out.append({"type": "Date", "name": str(d)[:30]})
    return out[:50]


def compute_content_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def simhash_64(text):
    """[FIXED & MODIFIED] v2.11 64 位 SimHash（内容级去重——广撒网"能采能管"）
    对分词 token 的 64 位 sha256 哈希做加权投票；Hamming 距离 <=3 视为近似重复。"""
    if not text:
        return 0
    v = [0] * 64
    tokens = str(text).split()
    if not tokens:
        return 0
    for tok in tokens[:4096]:
        h = int.from_bytes(hashlib.sha256(tok.encode("utf-8", "ignore")).digest()[:8], "big")
        for i in range(64):
            if (h >> i) & 1:
                v[i] += 1
            else:
                v[i] -= 1
    out = 0
    for i in range(64):
        if v[i] > 0:
            out |= 1 << i
    # [FIXED & MODIFIED] v2.11 SQLite INTEGER 上限 2^63-1：最高位置 1 时原值溢出
    # → frontier flush 整批丢失（executemany 一行失败全批回滚）。钳制为 63 位。
    return out & ((1 << 63) - 1)


def simhash_hamming(a, b):
    """两个 SimHash 的 Hamming 距离（<=3 判定近似重复）"""
    return bin(a ^ b).count("1")


async def extract_metadata_async(html, url, base_url=None):
    # CPU 卸载：解析放到线程池执行
    # [实测 v2.19.8 短链 bug] base_url 一路透传（终到地址），缺省时 extract_metadata
    # 自行退回 url —— 既有调用方不传参时行为与改前逐字一致。
    return await asyncio.to_thread(extract_metadata, html, url, base_url)
