# -*- coding: utf-8 -*-
"""代理源拉取器 proxy_fetcher.py（v2.17 3-C，默认关）

ExitManager 至今只吃配置/env 手填代理；本模块让"代理服务商 API 自动注池"成为可选项：
  1. 解析 `proxy_source` 配置串（逗号分隔多条源）：
     - `api:https://provider/api?token=xxx`   返回 JSON 列表（[{url,country?}] 或 ["url"...]）
     - `ip:port` / `socks5://ip:port`          直排队（静态源，适合自购/白名单）
  2. 拉取结果逐条 exit_mgr.add_node（带国别元数据时一并给 geo）；
  3. 每次拉取前清空由本模块注入的旧节点（避免越积越多）——只动"源注入"节点，
     不发散手动配置（退出时刻记忆：add_node 无来源标记——记录注入 url 集合按批清）。

默认关（proxy_fetcher_enabled=False）；开启后 schedule 间隔拉取（默认 600s）。
"""
import json
import logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def parse_proxy_source(src: str) -> list:
    """配置串 → 待拉取条目 [{api|static, value, country}]。
    api: 前缀 → JSON 端点；其余（host:port / scheme://host:port）→ 静态直排。"""
    out = []
    for item in str(src or "").split(","):
        item = item.strip()
        if not item:
            continue
        if item.lower().startswith("api:"):
            out.append({"kind": "api", "value": item[4:].strip(),
                        "country": None})
        else:
            out.append({"kind": "static", "value": item, "country": None})
    return out


def parse_proxy_payload(text) -> list:
    """服务商 API 返回 JSON → [{url, country|None}]。
    兼容列表字符串 / [{url:...}] / {data:{proxies:[...]}} 三种形态；失败 → [] 诚实降级。"""
    try:
        data = json.loads(text)
    except Exception:
        return []
    raw = []
    if isinstance(data, list):
        raw = data
    elif isinstance(data, dict):
        raw = data.get("data", {}).get("proxies") or data.get("proxies") \
            or data.get("items") or data.get("result") or []
    out = []
    for p in raw:
        if isinstance(p, str) and p.strip():
            out.append({"url": p.strip(), "country": None})
        elif isinstance(p, dict) and p.get("url"):
            out.append({"url": str(p["url"]).strip(),
                        "country": p.get("country") or p.get("geo") or None})
    return out


def validate_proxy(url: str) -> bool:
    """形态校验：scheme(http/https/socks5)或 host:port 形态；私网/空值拒绝。
    [v2.18 P2-9] 三处宽松口收紧：大写 scheme（Socks5://）绕过 → 先小写归一；
    dns_check=False 放行"解析到私网的域名" → 改 True；except 无条件放行 →
    仅 is_private_url 模块缺失时才放行（DNS 失败仍放行交由连接超时兜底）。"""
    if not url:
        return False
    u = url.strip()
    if "://" in u:
        _sch, _rest = u.split("://", 1)
        u = _sch.lower() + "://" + _rest
    if not (u.startswith(("http://", "https://", "socks5://", "socks4://"))):
        # host:port 形态兜底
        if ":" not in u.split("@")[-1]:
            return False
        u = "http://" + u
    try:
        from .url_utils import is_private_url
    except Exception:
        return True  # 仅 SSRF 模块缺失时宽松放行（不应发生）
    try:
        # is_private_url 对非 http(s) 协议保守拒绝（ssrf 语义）——socks 校验前协议归一
        _check = u.replace("socks5://", "http://").replace("socks4://", "http://")
        if is_private_url(_check, dns_check=True):
            return False
    except Exception:
        pass
    return True


async def fetch_source(session, entry: dict) -> list:
    """单源拉取：api → GET+解析；static → 直排返回

    [v2.19.7 安全·扫描发现] 原为裸 `session.get`（follows redirects）。这里的源 URL 来自
    环境变量 KIANA_PROXY_SOURCES，虽是用户自填，但重定向链完全在外部控制下（代理源被
    劫持/302 到内网 = 把内网响应喂进代理池）。统一走 safe_get 的逐跳校验。"""
    if entry["kind"] == "api":
        from .url_utils import safe_get
        r = await safe_get(session, entry["value"], timeout=15)
        if r is None or r.status_code != 200:
            logger.warning(f"代理源 API 非 200/被拦截: "
                           f"{r.status_code if r is not None else 'blocked'}（{entry['value'][:50]}）")
            return []
        return parse_proxy_payload(r.text)
    return [{"url": entry["value"], "country": entry.get("country")}]


async def fetch_all(entries) -> tuple:
    """遍历所有源 → (可用代理 [{url,country}...], 失败数)。网络层由调用方会话注入
    （crawler 侧持有 curl AsyncSession；测试注入假 session）。"""
    from curl_cffi.requests import AsyncSession
    ok, failed = [], 0
    async with AsyncSession(timeout=15) as session:
        for e in entries:
            try:
                items = await fetch_source(session, e)
            except Exception as ex:
                logger.warning(f"代理源拉取失败: {e['value'][:40]} {ex}")
                failed += 1
                continue
            ok.extend(i for i in items if validate_proxy(i.get("url", "")))
    return ok, failed
