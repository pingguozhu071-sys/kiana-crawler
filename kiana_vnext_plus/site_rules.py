"""YAML 站点规则层（v2.15 阶段 4——体检项 4/41 可扩展性/低代码的正解）

声明式站点接入：不改一行 Python，新增 rules/sites/<域名>.yaml 即可让 Kiana
按自定义选择器抽取列表页/详情页字段/媒体/翻页。

规则格式（rules/sites/example.yaml 有完整示例）:
    name: mysite
    match: [mysite.com]          # 域名后缀匹配
    item_type: article
    list:
      selector: "div.post"
      fields:
        title: {sel: "h2 a", text: true}
        url:   {sel: "h2 a", attr: href}
    detail:
      fields:
        price: {sel: ".price", text: true, transform: parse_price}
    media:
      images: {sel: "img.thumb", attr: src}
    pagination:
      next: {sel: "a.next", attr: href}

设计参考 Jormungandr adapters/generic.py 的思路（自研实现）：
单规则文件失败仅 warning 不拖垮整体（RuleLoader 容错）；
本模块不 import 任何重依赖（bs4/pyyaml 惰性）。
"""
import os
import re
import logging
from pathlib import Path
from typing import Optional
from datetime import datetime
from urllib.parse import urljoin

logger = logging.getLogger(__name__)

try:
    import yaml
    _YAML_OK = True
except Exception:
    _YAML_OK = False

try:
    from bs4 import BeautifulSoup
    _BS4_OK = True
except Exception:
    _BS4_OK = False

_RULES_DIR = Path(__file__).parent.parent / "rules" / "sites"
_TRANSFORMS = ("strip", "int", "float", "parse_price", "date_parse", "url_extract")


def _transform(value, name):
    """规则字段 transform 钩子"""
    if value is None:
        return None
    s = str(value).strip()
    try:
        if name == "strip":
            return s
        if name == "int":
            digits = "".join(ch for ch in s if ch.isdigit() or ch == "-")
            return int(digits) if digits else None
        if name == "float":
            digits = "".join(ch for ch in s if ch.isdigit() or ch in "-.")
            return float(digits) if digits else None
        if name == "parse_price":
            m = "".join(ch for ch in s if ch.isdigit() or ch == ".")
            return float(m) if m else None
        if name == "date_parse":
            # [v2.17 5.1] 常见日期格式 → ISO；无法解析诚实返回原串（不丢数据）
            for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M",
                        "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d",
                        "%Y年%m月%d日 %H:%M", "%Y年%m月%d日", "%m-%d %H:%M",
                        "%y-%m-%d %H:%M:%S", "%y-%m-%d %H:%M"):
                try:
                    return datetime.strptime(s, fmt).isoformat()
                except ValueError:
                    continue
            return s
        if name == "url_extract":
            # [v2.17 5.1] 从文本提取第一个 http(s) URL（无则 None）
            m = re.search(r'https?://[^\s<>"\']+', s)
            return m.group(0) if m else None
    except Exception:
        return None
    return s


class SiteRule:
    """单站点规则（加载自 YAML）"""

    def __init__(self, data: dict):
        self.name = str(data.get("name", "unnamed"))
        self.match = [str(d).lower() for d in (data.get("match") or [])]
        self.item_type = str(data.get("item_type", "article"))
        self.list_cfg = data.get("list") or {}
        self.detail_fields = (data.get("detail") or {}).get("fields") or {}
        self.media_images = (data.get("media") or {}).get("images")
        self.pagination_next = (data.get("pagination") or {}).get("next")
        self.link_scope = str(data.get("link_scope", "same_domain"))
        # [v2.17 0-2] exclude：子串排除列表（命中即不匹配）——防规则层与视频/音乐通道抢 URL
        # （netease-news 曾覆盖 music.163.com、sohu-news 曾覆盖 tv.sohu.com）
        self.exclude = [str(x).lower() for x in (data.get("exclude") or [])]

    def matches(self, url: str) -> bool:
        from urllib.parse import urlparse
        u = str(url or "").lower()
        if any(x in u for x in self.exclude):
            return False
        host = (urlparse(u).hostname or "").lower()
        return any(host == d or host.endswith("." + d) for d in self.match)

    @classmethod
    def from_yaml(cls, path: Path) -> "SiteRule":
        with open(path, encoding="utf-8-sig") as f:
            data = yaml.safe_load(f) or {}
        return cls(data)


class RuleLoader:
    """规则目录加载器：sorted(glob *.yaml)，单文件失败仅 warning"""

    def __init__(self, rules_dir: Path = _RULES_DIR):
        self.dir = Path(rules_dir)
        self._rules: Optional[list] = None

    def load(self) -> list:
        if self._rules is not None:
            return self._rules
        rules: list = []
        if not (_YAML_OK and self.dir.is_dir()):
            self._rules = rules
            return rules
        for f in sorted(self.dir.glob("*.yaml")):
            try:
                rules.append(SiteRule.from_yaml(f))
            except Exception as e:
                logger.warning(f"规则文件加载失败（跳过）{f.name}: {e}")
        self._rules = rules
        logger.info(f"站点规则已加载: {len(rules)} 条（{self.dir}）")
        return rules

    def invalidate(self):
        self._rules = None  # 热加载失效（下次 load 重新读盘）


_loader = RuleLoader()


def find_rule(url: str) -> Optional[SiteRule]:
    """URL → 命中的站点规则（无则 None）"""
    for r in _loader.load():
        if r.matches(url):
            return r
    return None


def _split_selector(sel: str) -> list:
    """顶层逗号切分（忽略 :not(...)/[...] 内的逗号）——字段选择器候选列表用"""
    parts, depth, cur = [], 0, []
    for ch in str(sel):
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur).strip())
    return [p for p in parts if p]


def _select_first(soup, sel):
    """[FIXED & MODIFIED] v2.17 真机冒烟：字段选择器逗号列表 = *候选回退*（首个有匹配者生效）。
    bs4 select_one 对逗号列表是"并集按文档序取一"——'h1' 兜底会压过 h1.main-title 等特异候选
    （新浪文章页两个 h1，导航 channel-logo 在前，标题被抽成"新闻中心"）。字段抽取是单值场景，
    回退语义才符合规则设计（'article, .content, #content' 意为"按顺序试"）。"""
    for part in _split_selector(sel):
        try:
            node = soup.select_one(part)
        except Exception:
            continue
        if node is not None:
            return node
    return None


def _extract_fields(soup, fields_cfg) -> dict:
    out: dict = {}
    for key, cfg in (fields_cfg or {}).items():
        if not isinstance(cfg, dict):
            continue
        sel = cfg.get("sel")
        if not sel:
            continue
        node = _select_first(soup, sel)
        if node is None:
            out[key] = None
            continue
        if cfg.get("attr"):
            value = node.get(cfg["attr"])
        else:
            value = node.get_text(" ", strip=True)
        if cfg.get("transform") in _TRANSFORMS:
            value = _transform(value, cfg["transform"])
        out[key] = value
    return out


def apply_rule(html: str, url: str, base_url: Optional[str] = None) -> Optional[dict]:
    """按命中规则抽取页面 → dict（未被规则命中/解析失败返回 None）。
    返回结构：{name, item_type, fields, images, next_url, list_items}

    [实测 v2.19.8 短链 bug] `base_url` = **跟随重定向后的终到地址**，只用于把页面里的
    **相对链接**（list_items 的 item_link / 翻页 next_url）补成绝对 URL。短链种子下
    若按请求地址补，会补出 `https://b23.tv/page/2` 这种不存在的地址——入队即 404，
    与 parser 里那批 urljoin 是同一个坑。`rule` 仍按 `url` 匹配（口径不变），
    `base_url` 缺省时与改前逐字一致。"""
    if not (_YAML_OK and _BS4_OK):
        return None
    rule = find_rule(url)
    if rule is None:
        return None
    base = base_url or url
    try:
        soup = BeautifulSoup(html or "", "lxml")
        result: dict = {"name": rule.name, "item_type": rule.item_type,
                        "fields": {}, "images": [], "next_url": None, "list_items": []}

        # 列表页：块选择器 + 相对字段
        # [v2.18 P2-6] 各段独立容错：一处非法 CSS 选择器原来让整条规则静默作废
        # （已解析出的 list_items 也一并丢），现逐段隔离
        try:
            if rule.list_cfg.get("selector"):
                blocks = soup.select(rule.list_cfg["selector"])[:200]
                for b in blocks:
                    item = _extract_fields(b, rule.list_cfg.get("fields"))
                    # [v2.17 5.1] item_link 保留键：相对链接提取入队 detail（补全为绝对 URL）
                    if item.get("item_link"):
                        item["item_link"] = urljoin(base, str(item["item_link"]))
                    if any(v for v in item.values()):
                        result["list_items"].append(item)
        except Exception as e:
            logger.debug(f"规则列表段跳过: {e}")

        # 详情/全局字段
        try:
            if rule.detail_fields:
                result["fields"].update(_extract_fields(soup, rule.detail_fields))
        except Exception as e:
            logger.debug(f"规则字段段跳过: {e}")

        # 媒体图片
        try:
            if rule.media_images and rule.media_images.get("sel"):
                attr = rule.media_images.get("attr", "src")
                for img in soup.select(rule.media_images["sel"])[:100]:
                    src = img.get(attr)
                    if src and str(src).startswith("http"):
                        result["images"].append(str(src))
        except Exception as e:
            logger.debug(f"规则图片段跳过: {e}")

        # 翻页（[v2.17 5.1] next 支持列表：多个候选选择器按顺序试用，命中即停）
        _next_cfg = rule.pagination_next
        _cands = _next_cfg if isinstance(_next_cfg, list) else ([_next_cfg] if _next_cfg else [])
        for _c in _cands:
            if not (_c and _c.get("sel")):
                continue
            try:
                nxt = soup.select_one(_c["sel"])
            except Exception as e:
                logger.debug(f"规则翻页选择器跳过: {e}")
                continue
            if not nxt:
                continue
            href = str(nxt.get(_c.get("attr", "href")) or "")
            if href:
                result["next_url"] = urljoin(base, href)
                break

        # title 兜底
        if not result["fields"].get("title") and soup.title:
            result["fields"]["title"] = soup.title.get_text(strip=True)
        return result
    except Exception as e:
        logger.debug(f"规则抽取失败({rule.name}): {e}")
        return None


RULE_TEMPLATE = """# Kiana 站点规则（{domain}）
# 编辑保存后下次爬取自动生效（热加载）。字段说明见 rules/README 或项目文档。
name: {domain.replace('.', '_')}
match: [{domain}]
# exclude: [子串列表——命中即不匹配（如 music.163.com / /song，防与 yt-dlp 通道抢 URL）]
item_type: article

detail:
  fields:
    title: {{sel: "h1", text: true}}
    content: {{sel: "article, .content, #content", text: true}}

media:
  images: {{sel: "img", attr: src}}

pagination:
  next: {{sel: "a.next", attr: href}}
"""


def new_rule_template(domain: str) -> str:
    """生成规则骨架文本（CLI rule-new 用）"""
    safe = domain.replace(".", "_")
    return (RULE_TEMPLATE
            .replace("{domain.replace('.', '_')}", safe)
            .replace("{domain}", domain))


def validate_rule_file(path) -> tuple:
    """[v2.17 5.2] rule-validate 核心：YAML 解析 + 必需键校验 → (ok, messages)。
    坏 YAML 带行号（yaml.YAMLError problem_mark），定位到行。"""
    messages = []
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8-sig")) or {}
    except Exception as e:
        mark = getattr(getattr(e, "problem_mark", None), "line", None)
        where = f"第 {mark + 1} 行: " if mark is not None else ""
        return False, [f"YAML 解析失败 {where}{e}"]
    if not isinstance(data, dict):
        return False, ["规则根必须是映射（name/match 等键）"]
    ok = True
    if not data.get("name"):
        ok = False
        messages.append("缺少必需键 name")
    match = data.get("match")
    if not (isinstance(match, list) and match and all(str(m).strip() for m in match)):
        ok = False
        messages.append("match 必须是非空列表（域名后缀匹配，如 [example.com]）")
    try:
        SiteRule(data)
    except Exception as e:
        ok = False
        messages.append(f"规则对象校验失败: {e}")
    if ok:
        messages.append(f"规则有效: name={data.get('name')} match={match}")
    return ok, messages


def test_rule_url(url: str, html: str, path) -> dict:
    """[v2.17 5.2] rule-test 核心：本地 HTML 样例跑 apply_rule。
    html 为空时仅完成语法校验（无网络场景）；返回 {ok, messages, result}。"""
    ok, msgs = validate_rule_file(path)
    if not ok:
        return {"ok": False, "messages": msgs, "result": None}
    if not (html or "").strip():
        return {"ok": True, "messages": msgs + ["未提供 --html 样例，仅完成语法校验"],
                "result": None}
    try:
        res = apply_rule(html, url)
    except Exception as e:
        return {"ok": False, "messages": msgs + [f"抽取异常: {e}"], "result": None}
    if res is None:
        return {"ok": True, "messages": msgs + ["规则未命中该 URL（检查 match 域名）或抽取出错"],
                "result": None}
    return {"ok": True, "messages": msgs, "result": res}
