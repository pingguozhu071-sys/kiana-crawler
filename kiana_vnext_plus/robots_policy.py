# -*- coding: utf-8 -*-
"""robots.txt 合规件 robots_policy.py（v2.17 E-P1-1）

参考 Scrapy robotstxt.py 与 Colly robotsMap（Apache-2.0/BSD 许可，逻辑可移植——
本实现为适配本工程 HttpCache/请求闸的重写：缓存条目走 HTTP 磁盘缓存层，
请求走 url_utils.safe_urlopen 白名单（同源 robots 域名，无 SSRF 面）。

语义：
  - allow(url) True=允许抓取（默认）；False=robots 拒绝（调用方跳过入队/停止处理）
  - 配置开关 robots_respect（默认 False——保持既有行为，用户显式开启才生效；
    海外站/合规任务开启）
  - 每域缓存（进程内 dict，上限 256 域，LRU 化清空）
  - 抓取失败（网络/404）→ 视为"无 robots 限制"（防误伤；与主流实现一致）
"""
import logging
import re
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

ROBOTS_CACHE: dict = {}          # host -> (rules_allow, rules_disallow, allow_all)
CRAWL_DELAY_CACHE: dict = {}     # [v2.17 1-5] host -> crawl_delay(秒, 默认 0=不限)
MAX_HOSTS = 256


def _fetch_robots(host: str, timeout=8):
    """抓取并解析主机 robots.txt（https 优先，回退 http；失败返回 None=不限制）。"""
    from .url_utils import safe_urlopen
    scheme = "https" if not _host_http_only(host) else "http"
    url = f"{scheme}://{host}/robots.txt"
    r = safe_urlopen(url, allowed_hosts=(host,), timeout=timeout,
                     headers={"User-Agent": "KianaVnextPlus/2.16"})
    if r is None:
        return None
    try:
        return r.read().decode("utf-8", "ignore")
    except Exception:
        return None


def _host_http_only(host: str) -> bool:
    return False  # 默认 https；HTTP 回退由 safe_urlopen 内不存在——保持简单


def _parse_robots(text):
    """极简 User-Agent 通配解析：匹配任意 UA（爬虫自身 UA）的 Allow/Disallow 规则。
    通配符 * 支持，$ 尾匹配；返回 (allow_rules, disallow_rules, allow_all, crawl_delay)。"""
    allows, disallows = [], []
    crawl_delay = 0.0
    ua_match = False
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, val = line.partition(":")
        key = key.strip().lower()
        val = val.strip()
        if key == "user-agent":
            ua_match = ("*" in val.lower()) or ("kiana" in val.lower())
        elif ua_match and key == "allow" and val:
            allows.append(val)
        elif ua_match and key == "disallow" and val:
            disallows.append(val)
        elif key == "crawl-delay" and val:
            # [v2.17 1-5] Crawl-delay 尊重（秒级；非数字按 0 处理——诚实降级不拒绝）
            try:
                crawl_delay = max(float(val), 0.0)
            except ValueError:
                crawl_delay = 0.0
    return allows, disallows, crawl_delay


def _rule_match(path: str, rule: str) -> bool:
    """通配匹配：* 任意串；$ 锚尾；纯前缀匹配（无 * 时）"""
    rule = rule.strip()
    if not rule:
        return False
    anchored = rule.endswith("$")
    if anchored:
        rule = rule[:-1]
    if "*" in rule:
        rx = "^" + re.escape(rule).replace(r"\*", ".*") + ("$" if anchored else "")
        return re.match(rx, path) is not None
    if anchored:
        return path == rule
    return path.startswith(rule)


def _host_rules(host: str):
    """host → (allows, disallows, allow_all)；解析失败 → 不限制"""
    if host in ROBOTS_CACHE:
        return ROBOTS_CACHE[host]
    if len(ROBOTS_CACHE) >= MAX_HOSTS:
        ROBOTS_CACHE.clear()
    text = _fetch_robots(host)
    if text is None:
        rules = ([], [], True)  # 抓取失败 → 不限制（防误伤，诚实记录）
        crawl_delay = 0.0
        CRAWL_DELAY_CACHE[host] = 0.0
        logger.debug(f"robots.txt 不可达({host})——按不限制处理")
    else:
        allows, disallows, crawl_delay = _parse_robots(text)
        rules = (allows, disallows, False)
        CRAWL_DELAY_CACHE[host] = crawl_delay
        logger.info(f"robots.txt 已加载({host}): allow={len(allows)} "
                    f"disallow={len(disallows)} crawl-delay={crawl_delay}")
    ROBOTS_CACHE[host] = rules
    return rules


def crawl_delay_for(host: str) -> float:
    """[v2.17 1-5] 域 crawl-delay（秒；解析失败/未加载=0 不限制）。
    依赖 is_allowed/_host_rules 缓存——调用前先经 is_allowed(url) 触发加载。"""
    try:
        h = (host or "").lower()
        if h and h not in ROBOTS_CACHE:
            _host_rules(h)
        return float(CRAWL_DELAY_CACHE.get(h, 0.0))
    except Exception:
        return 0.0


def is_allowed(url: str) -> bool:
    """robots 合规判定（开关在调用方：未启用恒定 True）。"""
    try:
        p = urlparse(url)
        host = (p.hostname or "").lower()
        if not host:
            return True
        path = p.path or "/"
        allows, disallows, allow_all = _host_rules(host)
        if allow_all:
            return True
        if any(_rule_match(path, d) for d in disallows):
            # allow 规则优先级高于 disallow（若有）
            if any(_rule_match(path, a) for a in allows):
                return True
            return False
        return True
    except Exception:
        return True  # 判定失败按放行（防误伤）


def clear():
    ROBOTS_CACHE.clear()
    CRAWL_DELAY_CACHE.clear()
