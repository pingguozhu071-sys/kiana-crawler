import json
import hashlib
import asyncio
import logging
from urllib.parse import urljoin, urlparse

# [FIXED & MODIFIED] v2.9.2 缺 logger 定义：v2.6.5 trafilatura 异常兜底用 logger.debug
# 但模块从未定义 logger → trafilatura 抛异常时 NameError 传播 → "Job failed: name 'logger'
# is not defined"（作者 GUI 实测 0 pages 根因）——B站风控页触发 trafilatura 异常路径
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
    # （"No option 'min_extracted_size' in section: 'DEFAULT'"——作者 GUI 实测 0 pages 根因）
    trafilatura = None

if trafilatura is not None:
    try:
        # [FIXED & MODIFIED] v2.6.6 配置键兜底注入：打包环境 settings.cfg 可能未加载 →
        # DEFAULT_CONFIG 缺 min_extracted_size 等键 → extract() 内部 config.get 抛
        # NoOptionError → 整页处理失败（作者 BV16Uud6JEN5 日志实锤）。手动注入保证键存在。
        import trafilatura.settings as _traf_settings
        for _k, _v in (("min_extracted_size", "250"), ("min_extracted_comm_size", "1"),
                       ("min_output_size", "1"), ("min_output_comm_size", "1")):
            if not _traf_settings.DEFAULT_CONFIG.has_option("DEFAULT", _k):
                _traf_settings.DEFAULT_CONFIG.set("DEFAULT", _k, _v)
    except Exception:
        pass


def extract_metadata(html, url):
    """从 HTML 提取元数据：标题、描述、正文、链接、图片、视频等"""
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

    if soup.title and soup.title.string:
        data["title"] = soup.title.string.strip()

    # html lang 属性（如 zh-CN）
    if soup.html and soup.html.get("lang"):
        data["lang"] = soup.html.get("lang")

    # favicon（link rel=icon / shortcut icon）
    for _l in soup.find_all("link", rel=True):
        _rel = " ".join(_l.get("rel")) if isinstance(_l.get("rel"), (list, tuple)) else str(_l.get("rel") or "")
        if "icon" in _rel.lower():
            try:
                data["favicon"] = urljoin(url, _l.get("href", ""))
            except Exception:
                pass
            break

    canonical = soup.find("link", rel="canonical")
    # [v2.18 P1-9] 裸 <link rel="canonical">（无 href）曾让 urljoin(url, None)
    # TypeError 打穿整页解析（此段不在任何 try 内）
    if canonical and canonical.get("href"):
        try:
            data["canonical"] = urljoin(url, canonical.get("href"))
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
        # 导致整个页面处理失败（作者实测 0 pages done）→ 兜底用 article/main 纯文本
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

    for img in soup.find_all("img", src=True):
        data["images"].append(urljoin(url, img["src"]))

    # 修复：原来 find_all("src") 是错误的，应为 find_all("source")
    # <video> 标签内嵌 <source> 子标签，而非 <src> 标签
    for video in soup.find_all("video"):
        for src in video.find_all("source"):
            if src.get("src"):
                data["videos"].append(urljoin(url, src["src"]))
        if video.get("src"):
            data["videos"].append(urljoin(url, video["src"]))
    # [FIXED & MODIFIED] 补充 og:video 系列 meta 提取（og:video/og:video:secure_url/og:video:url）
    # 大量站点（新闻/影音站）以 og:video 作为主要视频入口，原实现漏掉
    for prop in ("og:video", "og:video:secure_url", "og:video:url"):
        meta = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        if meta and meta.get("content"):
            data["videos"].append(urljoin(url, meta["content"]))
    # 去重（video 与 og:video 可能重复）
    seen = set()
    data["videos"] = [v for v in data["videos"] if not (v in seen or seen.add(v))]

    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"])
        if href.startswith("magnet:") or href.endswith(".torrent"):
            data["torrents"].append(href)

    base_host = urlparse(url).netloc.lower()
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"])
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


async def extract_metadata_async(html, url):
    # CPU 卸载：解析放到线程池执行
    return await asyncio.to_thread(extract_metadata, html, url)
