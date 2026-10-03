"""电商商品解析器（E-commerce Product Parser）v2.5.0
从淘宝/京东/拼多多/闲鱼页面内嵌 JSON 中提取商品字段并归一化。

数据源（各平台页面内嵌状态，无需接口签名）：
  淘宝搜索页   : window.__INIT_DATA__ / __INITIAL_STATE__（itemList 商品列表）
  淘宝详情页   : window.__INIT_DATA__（itemInfo 详情）
  京东搜索页   : window.pageConfig（product 商品列表）
  京东详情页   : pageConfig.product（标题/参数）+ #spec-list（SKU）
  拼多多       : window.rawData（mobile 端搜索/详情）
  闲鱼         : window.__INIT_DATA__

归一化字段：title / price / original_price / sales / shop / rating / location /
           images[] / url / sku_params / raw（原始片段）
"""
import json
import logging
import re
from typing import List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# 平台识别
PLATFORM_MARKERS = {
    "taobao": ("taobao.com", "tmall.com", "e.tb.cn"),  # [FIXED & MODIFIED] e.tb.cn 淘宝分享短链（跳转源）
    "jd": ("jd.com", "360buy.com"),
    "pdd": ("yangkeduo.com", "pinduoduo.com"),
    "xianyu": ("2.taobao.com", "goofish.com"),
}


def detect_platform(url: str) -> Optional[str]:
    """按 URL 域名识别电商平台。

    [v6 修复] 原实现是 `any(m in host for m in markers)` 且**按字典顺序取第一个命中**，
    有两个真缺陷（均已实测复现）：

    ① **顺序覆盖**：`xianyu` 的标记 `2.taobao.com` **永远命中不了**——`taobao` 先被检查，
       而 `"taobao.com"` 是 `"2.taobao.com"` 的子串。于是
       `https://2.taobao.com/item.htm` → `'taobao'`（应为 `'xianyu'`），
       **闲鱼商品会被用淘宝的解析逻辑处理**。

    ② **子串匹配 = 域名后缀伪造**：标记是**域名**，却按子串比——
       `nottaobao.com`、`fake-jd.com`、`taobao.com.evil.com` 全被判成对应平台。

    现改为：**按域名边界匹配**（`host == m` 或 `host.endswith("." + m)`）
    + **最长标记优先**（不再依赖字典顺序；将来加 `item.jd.com` 这类更具体的标记也不会被覆盖）。
    """
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return None
    if not host:
        return None
    best: Optional[str] = None
    best_len = 0
    for plat, markers in PLATFORM_MARKERS.items():
        for m in markers:
            m = m.lower()
            if (host == m or host.endswith("." + m)) and len(m) > best_len:
                best, best_len = plat, len(m)
    return best


def _json_from_script(html: str, var_name: str) -> Optional[dict]:
    """从 window.xxx = {...}; 提取 JSON 对象（鲁棒：非贪婪到行尾分号）"""
    for pat in (
        rf'window\.{var_name}\s*=\s*(\{{.*?\}});?\s*</script>',
        rf'window\.{var_name}\s*=\s*(\{{.*?\}});',
        rf'var\s+{var_name}\s*=\s*(\{{.*?\}});',
    ):
        m = re.search(pat, html, re.S)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                continue
    return None


def _num(value) -> Optional[float]:
    """字符串/数字 → float（去掉 ¥,￥,元,万,逗号）"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).replace(",", "").replace("¥", "").replace("￥", "").replace("元", "").strip()
    if not s or s in ("-", "--", "暂无"):
        return None
    try:
        if "万" in s:
            return round(float(s.replace("万", "")) * 10000, 1)
        return float(s)
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════
# 淘宝 / 天猫
# ═══════════════════════════════════════════════════════════════
def parse_taobao(html: str, url: str) -> List[dict]:
    """淘宝/天猫：搜索页 __INIT_DATA__（itemList）或详情页 itemInfo"""
    items: List[dict] = []
    data = _json_from_script(html, "__INIT_DATA__") or _json_from_script(html, "__INITIAL_STATE__")
    if not data:
        return items

    # 详情页：itemInfo / item
    info = data.get("itemInfo") or data.get("item") or {}
    if info:
        items.append(_norm({
            "title": info.get("title"),
            "price": info.get("price"),
            "original_price": info.get("originalPrice") or info.get("marketPrice"),
            "sales": info.get("sellCount") or info.get("totalSoldQuantity"),
            "shop": (info.get("seller") or {}).get("shopName") or (info.get("shopInfo") or {}).get("title"),
            "rating": (info.get("ratings") or {}).get("avgRate"),
            "location": (info.get("delivery") or {}).get("from"),
            "images": [info.get("mainPic") or info.get("picUrl")],
            "sku_params": info.get("skuInfo"),
            "url": url,
            "raw": info,
        }))

    # 搜索页：itemList
    for node in _walk(data, "itemList"):
        if not isinstance(node, dict):
            continue
        if "title" in node or "picUrl" in node or "price" in node:
            items.append(_norm({
                "title": node.get("title") or node.get("titleText"),
                "price": node.get("price"),
                "original_price": node.get("originalPrice"),
                "sales": node.get("sellCount") or node.get("saleCount"),
                "shop": node.get("shopName") or (node.get("seller") or {}).get("shopName"),
                "rating": node.get("rateScore") or node.get("rating"),
                "location": node.get("provcity") or node.get("location"),
                "images": [node.get("picUrl") or node.get("imageUrl")],
                "url": _abs(node.get("itemUrl") or node.get("url"), "https://www.taobao.com"),
                "raw": node,
            }))
    return items


# ═══════════════════════════════════════════════════════════════
# 京东
# ═══════════════════════════════════════════════════════════════
def parse_jd(html: str, url: str) -> List[dict]:
    """京东：搜索页 pageConfig.product（wareInfoList）/ 详情页 pageConfig.product"""
    items: List[dict] = []
    cfg = _json_from_script(html, "pageConfig")
    if not cfg:
        return items

    prod = cfg.get("product") or {}
    # 详情页：product 对象本身是商品
    if isinstance(prod, dict) and (prod.get("name") or prod.get("skuName")):
        items.append(_norm({
            "title": prod.get("name") or prod.get("skuName"),
            "price": prod.get("price") or (prod.get("price") or {}).get("p") if isinstance(prod.get("price"), dict) else prod.get("price"),
            "original_price": None,
            "sales": prod.get("sales") or prod.get("saleCount"),
            "shop": (prod.get("shopInfo") or {}).get("shopName"),
            "rating": (prod.get("commentInfo") or {}).get("commentScoreStr"),
            "location": None,
            "images": [prod.get("image") or prod.get("imagePath")],
            "sku_params": prod.get("specification") or prod.get("spec"),
            "url": url,
            "raw": prod,
        }))
    # 搜索页：wareInfoList
    for node in _walk(prod, "wareInfoList"):
        if isinstance(node, dict) and ("title" in node or "skuId" in node):
            items.append(_norm({
                "title": node.get("title") or node.get("name"),
                "price": node.get("price") or (node.get("priceShow") or {}).get("price") if isinstance(node.get("priceShow"), dict) else node.get("priceShow"),
                "original_price": node.get("originalPrice"),
                "sales": node.get("goodComments") or node.get("sales"),
                "shop": node.get("shopName"),
                "rating": node.get("commentScore"),
                "location": None,
                "images": [node.get("image") or node.get("imagePath")],
                "url": f"https://item.jd.com/{node.get('skuId')}.html" if node.get("skuId") else None,
                "raw": node,
            }))
    # 详情页备选：window.pageConfig 外联 JS 数据（price 接口由接口层处理）
    return items


# ═══════════════════════════════════════════════════════════════
# 拼多多
# ═══════════════════════════════════════════════════════════════
def parse_pdd(html: str, url: str) -> List[dict]:
    """拼多多：window.rawData（搜索 goodsList / 详情 goods）"""
    items: List[dict] = []
    data = _json_from_script(html, "rawData")
    if not data:
        return items

    for node in _walk(data, ("goodsList", "goods", "store", "initDataObj")):
        if not isinstance(node, dict):
            continue
        if any(k in node for k in ("goods_name", "goodsName", "price", "hdThumb")):
            items.append(_norm({
                "title": node.get("goods_name") or node.get("goodsName"),
                "price": node.get("price") or node.get("min_group_price"),
                "original_price": node.get("market_price") or node.get("max_group_price"),
                "sales": node.get("sales_tip") or node.get("cnt") or node.get("sold_quantity"),
                "shop": node.get("mall_name") or node.get("mallName"),
                "rating": node.get("goods_rate") or node.get("rating"),
                "location": None,
                "images": [node.get("hdThumb") or node.get("thumbUrl")],
                "url": f"https://mobile.yangkeduo.com/goods.html?goods_id={node.get('goods_id') or node.get('goodsId')}" if (node.get("goods_id") or node.get("goodsId")) else None,
                "raw": node,
            }))
    return items


# ═══════════════════════════════════════════════════════════════
# 闲鱼
# ═══════════════════════════════════════════════════════════════
def parse_xianyu(html: str, url: str) -> List[dict]:
    items: List[dict] = []
    data = _json_from_script(html, "__INIT_DATA__") or _json_from_script(html, "__INITIAL_STATE__")
    if not data:
        return items
    for node in _walk(data, ("itemList", "items", "resultList")):
        if not isinstance(node, dict):
            continue
        if any(k in node for k in ("title", "itemName", "price")):
            items.append(_norm({
                "title": node.get("title") or node.get("itemName"),
                "price": node.get("price"),
                "original_price": node.get("originalPrice"),
                "sales": node.get("soldCount") or node.get("dealAmount"),
                "shop": node.get("sellerNick") or node.get("shopName"),
                "rating": None,
                "location": node.get("location"),
                "images": [node.get("picUrl") or node.get("mainPicUrl")],
                "url": _abs(node.get("itemUrl"), "https://www.goofish.com"),
                "raw": node,
            }))
    return items


# ═══════════════════════════════════════════════════════════════
# 统一入口
# ═══════════════════════════════════════════════════════════════
def parse_ecommerce(html: str, url: str) -> List[dict]:
    """电商页面商品提取统一入口（页面内嵌数据，无需接口签名）"""
    plat = detect_platform(url)
    if not plat or not html:
        return []
    try:
        if plat == "taobao":
            return parse_taobao(html, url)
        if plat == "jd":
            return parse_jd(html, url)
        if plat == "pdd":
            return parse_pdd(html, url)
        if plat == "xianyu":
            return parse_xianyu(html, url)
    except Exception as e:
        logger.debug(f"ecommerce parse failed ({plat}): {e}")
    return []


# ═══════════════════════════════════════════════════════════════
# 工具
# ═══════════════════════════════════════════════════════════════
def _norm(d: dict) -> dict:
    """字段归一化：价格/销量转数值，去 None"""
    out = {
        "title": (d.get("title") or "").strip()[:300] or None,
        "price": _num(d.get("price")),
        "original_price": _num(d.get("original_price")),
        "sales": d.get("sales") if isinstance(d.get("sales"), str) else _num(d.get("sales")),
        "shop": d.get("shop"),
        "rating": _num(d.get("rating")),
        "location": d.get("location"),
        "images": [i for i in (d.get("images") or []) if i][:10],
        "url": d.get("url"),
        "sku_params": d.get("sku_params"),
        "raw": d.get("raw"),
    }
    return {k: v for k, v in out.items() if v not in (None, [], {}, "")}


def _abs(u: Optional[str], base: str) -> Optional[str]:
    if not u:
        return None
    if u.startswith("http"):
        return u
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("/"):
        return base + u
    return base + "/" + u


def _walk(obj, keys) -> List:
    """深度优先遍历 JSON，收集所有名为 keys（或 key 列表之一）的节点。
    [FIXED & MODIFIED] list 值自动展开（itemList/goodsList 等数组整体命中时逐个展开）"""
    if isinstance(keys, str):
        keys = (keys,)
    found = []
    def rec(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in keys:
                    if isinstance(v, list):
                        found.extend(v)  # 数组展开为多个节点
                    else:
                        found.append(v)
                else:
                    rec(v)
        elif isinstance(o, list):
            for v in o:
                rec(v)
    rec(obj)
    return found
