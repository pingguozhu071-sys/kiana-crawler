# -*- coding: utf-8 -*-
"""身份捆绑轮换 identity_session.py（v2.17 E-P2，已接入主链路）

命名区分：本模块 SessionPool = 「身份捆绑虚拟用户池」（出口+cookie 绑定的轮换会话）；
session_pool.py 的 SessionPool = 「旧请求会话池」（protocol_engine 请求 cookie/头合并）。
两者同名不同物——检索/排错时以 import 来源区分。

参考 Crawlee SessionPool（Apache-2.0 可行，仅学"出口+cookie+指纹捆绑为虚拟用户、
被封锁整包退役"的语义，实现为本工程重写）：
  - IdentitySession：proxy_url + cookie_bundle + fingerprint_hint 三者捆绑；
  - SessionPool：按 domain 摊派会话；会话被封锁（mark_bad）→ 连续 2 次整包退役换新
    （新代理 + 新 cookie 组 + 新指纹）。
接线状态：**已接入主链路**（v6 据实修正——此前此处写"本模块当前仅逻辑层，
接线为下一轮"，而实际早已在用，属**过期陈述**，会误导排查）：
  - `crawler` 建池并挂 `protocol.identity_pool_provider`；
  - `protocol_engine._build_headers` 按 URL 域注入 Cookie；
  - `page_processor` 回传封锁（`mark_bad`）。
**如实标注未接线项**：`fingerprint_hint` 目前只生成、**全仓暂无消费者**——
不要把它当成"指纹已接线"。
"""
import dataclasses
import hashlib
import secrets
from typing import Dict, List
from urllib.parse import urlparse


@dataclasses.dataclass
class IdentitySession:
    """一个"虚拟用户"：出口代理 + cookie 包 + 指纹提示 捆绑（同生共死）"""
    proxy: str = ""
    cookie_bundle: dict = dataclasses.field(default_factory=dict)   # {"<域名>": "k=v; k2=v2", ...}
    fingerprint_hint: str = ""        # 指纹种子/提示（生成器入口）
    domain: str = ""
    bad_count: int = 0                # 连续封锁计数
    use_count: int = 0

    @property
    def disabled(self) -> bool:
        return self.bad_count >= 2     # 连续 2 次封锁 → 整包退役

    def cookies_for(self, domain: str) -> str:
        """[v2.17 E-P2] 按域取 cookie 头（bundle 内 key=域；子域也命中；__* 元键忽略）"""
        dom = str(domain or "").lower()
        parts = []
        for k, v in self.cookie_bundle.items():
            if not (isinstance(k, str) and isinstance(v, str)):
                continue
            if k.startswith("__"):
                continue
            if dom == k or dom.endswith("." + k):
                parts.append(v)
        return "; ".join(p for p in parts if p)


# 封禁域/错误语义 → 是否触发"整包退役"计数（纯函数，可单测）
BAD_SIGNALS = ("ipblockerror", "captcha", "challenge", "platformaccesserror",
               "noloading", "banned")
BAD_STATUSES = (403, 429, 410, 503)


def decide_bad(fail: bool, status=None, err: str = "") -> bool:
    """[v2.17 E-P2] 封锁判定：失败且 (状态码是风控/限流信号) 或 (异常类是 IP/账号风控信号)
    → True；成功 → False（清 bad 由 mark_good 处理）。保守：其余失败（超时/连接错）不算封锁，
    免误退役健康出口。"""
    if not fail:
        return False
    if isinstance(status, (int, float)) and int(status) in BAD_STATUSES:
        return True
    e = str(err or "").lower()
    return any(s in e for s in BAD_SIGNALS)


def load_cookie_groups(paths) -> list:
    """[v2.17 E-P2] 把 Netscape cookies.txt 文件集解析为 cookie 组列表（每文件一组）：
    [ {"<域名>": "k=v; k2=v2", ...}, ... ]。文件缺失/空 → 跳过；全空 → 空列表。
    与 cookie_health 同域逻辑一致（域前缀匹配），值为拼好的 Cookie 头串。"""
    import pathlib
    from .cookie_utils import parse_netscape_cookies
    groups = []
    for fp in (paths or []) if not isinstance(paths, str) else [paths]:
        p = pathlib.Path(str(fp))
        if not p.exists():
            continue
        try:
            text = p.read_text(encoding="utf-8-sig", errors="ignore")
            per_domain: dict = {}
            for c in parse_netscape_cookies(text):
                dom = str(c["domain"] or "").lower()
                if not dom:
                    continue
                per_domain.setdefault(dom, []).append(f"{c['name']}={c['value']}")
            bundled = {d: "; ".join(kvs) for d, kvs in per_domain.items()}
            if bundled:
                groups.append(bundled)
        except Exception:
            continue
    return groups


class SessionPool:
    """按域摊派会话池：封锁自动换新（无会话时新建）。

    API：
      get(domain) -> IdentitySession      （摊派/换新）
      mark_bad(domain, session)           （封锁：计数+退役换新）
      mark_good(domain, session)          （成功：清零 bad）
      cookies_for(url) -> str             （[v2.17] 按 URL 域命中活跃会话的 cookie 头）
    """

    def __init__(self, proxies=(), cookie_bundles=(), max_per_domain: int = 3):
        self.proxies = list(proxies or ())
        self.cookie_bundles = list(cookie_bundles or ())
        self.max_per_domain = max_per_domain
        self._pool: Dict[str, List[IdentitySession]] = {}

    def cookies_for(self, url: str) -> str:
        """[v2.17 E-P2] 请求注入点用：按 URL 域找活跃会话的 cookie 头（无则空串）。
        仅读活跃（未退役）会话；命中首个非空即返回（单域多会话时 cookie 同源，任一可用）。"""
        try:
            dom = (urlparse(url).hostname or "").lower()
        except Exception:
            return ""
        if not dom:
            return ""
        for sessions in self._pool.values():
            for s in sessions:
                if s.disabled:
                    continue
                ck = s.cookies_for(dom)
                if ck:
                    return ck
        return ""

    def get(self, domain: str) -> IdentitySession:
        dom_sessions = self._pool.setdefault(domain, [])
        # 释放已退役的
        live = [s for s in dom_sessions if not s.disabled]
        self._pool[domain] = live
        if live and len(live) < self.max_per_domain:
            if secrets.randbelow(100) < 15:  # 偶发轮换（长任务防粘性）
                live[0] = self._new(domain)
                return live[0]
            return secrets.choice(live)
        if not live:
            live.append(self._new(domain))
            return live[0]
        return live[-1]

    def _new(self, domain: str) -> IdentitySession:
        proxy = secrets.choice(self.proxies) if self.proxies else ""
        bundle = dict(secrets.choice(self.cookie_bundles)) if self.cookie_bundles else {}
        hint = hashlib.sha256(f"{proxy}|{list(bundle)[:3]}|{domain}".encode()).hexdigest()[:16]
        return IdentitySession(proxy=proxy, cookie_bundle=bundle,
                               fingerprint_hint=hint, domain=domain)

    def mark_bad(self, domain: str, session: IdentitySession):
        """封锁：bad_count+1；≥2 → 整包退役（下次 get 换新）"""
        session.bad_count += 1
        self._pool.setdefault(domain, [s if s is not session else session for s in
                                       self._pool.get(domain, [session])])

    def mark_good(self, domain: str, session: IdentitySession):
        session.bad_count = 0

    @property
    def total(self) -> int:
        return sum(len(v) for v in self._pool.values())
