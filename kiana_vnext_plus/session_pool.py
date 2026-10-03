"""会话池（强化版）— Session 身份管理 + 域名分组 + 轮转 + 封禁标记 + UA 池

接口兼容：
  - get_session(url) -> dict            （旧协议引擎用）
  - update_session(url, cookies, headers)（旧协议引擎/求解引擎用）
  - get_stealth_headers(url) / get_tls_profile(idx)（引擎路由回退层级用）
  - get(domain) -> Session               （Crawlee 风格新接口）
  - mark_blocked / cleanup_expired / invalidate
"""
import time
import random
import threading
import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional, Dict, List

from .header_generator import random_ua


@dataclass
class Session:
    """单会话身份：Cookie Jar + UA + 生命周期统计"""
    id: str
    domain: str
    ua: str = field(default_factory=random_ua)
    cookies: dict = field(default_factory=dict)        # name -> value
    created_at: float = field(default_factory=time.time)
    usage_count: int = 0
    error_count: int = 0
    is_blocked: bool = False
    max_usage: int = 200            # 单会话最大请求数，超限自动轮换
    max_age: float = 1800.0         # 会话最大存活时长（秒）
    headers: dict = field(default_factory=dict)
    last_used: float = field(default_factory=time.time)

    @property
    def expired(self) -> bool:
        return (time.time() - self.created_at) > self.max_age

    @property
    def exhausted(self) -> bool:
        return self.usage_count >= self.max_usage

    def touch(self):
        self.last_used = time.time()
        self.usage_count += 1

    def to_dict(self) -> dict:
        """转换为旧接口 dict 视图"""
        return {
            "cookies": "; ".join(f"{k}={v}" for k, v in self.cookies.items()) if self.cookies else None,
            "headers": dict(self.headers),
        }


class SessionPool:
    """线程安全的域名分组会话池（轮转策略：最少使用优先）"""

    def __init__(self, ttl=300, max_per_domain: int = 3, max_usage: int = 200, max_age: float = 1800.0):
        self._sessions: Dict[str, List[Session]] = defaultdict(list)
        self._ttl = ttl
        self._max_per_domain = max_per_domain
        self._default_max_usage = max_usage
        self._default_max_age = max_age
        self._lock = threading.Lock()

    @staticmethod
    def _domain_of(url: str) -> str:
        """[v6 修复] 改为委托 `url_utils.extract_domain`（**域名提取的唯一实现**）。

        原实现与 `extract_domain` 几乎一致，**只差异常处理**：它对畸形 URL
        （如 `https://[`）会**抛 ValueError**，而 `extract_domain` 不会。
        同一能力的第三份拷贝，故一并收口。
        """
        from .url_utils import extract_domain
        return extract_domain(url)

    # ── 新接口：Crawlee 风格 ────────────────────────────
    def get(self, domain: str) -> Session:
        """获取/创建会话（轮转策略：优先未过期未封禁且使用最少者）"""
        domain = self._domain_of(domain)
        with self._lock:
            pool = self._sessions[domain]
            candidates = [s for s in pool if not s.is_blocked and not s.expired and not s.exhausted]
            if candidates:
                s = min(candidates, key=lambda x: (x.usage_count, x.last_used))
                s.touch()
                return s
            # 需要新建：先清理超限会话，保持池上限
            if len(pool) >= self._max_per_domain:
                pool.sort(key=lambda x: x.created_at)
                pool.pop(0)
            sid = hashlib.md5(f"{domain}{time.time()}{random.random()}".encode()).hexdigest()[:12]
            s = Session(id=sid, domain=domain,
                        max_usage=self._default_max_usage, max_age=self._default_max_age)
            pool.append(s)
            s.touch()
            return s

    def invalidate(self, session: Session):
        """废弃一个会话（调用方标记不可用）"""
        with self._lock:
            session.is_blocked = True

    # ── 旧接口：协议引擎兼容 ────────────────────────────
    def get_session(self, url) -> Optional[Dict]:
        """旧接口：按域名返回会话 dict（headers/cookies）"""
        if not url:
            return None
        domain = self._domain_of(url)
        with self._lock:
            pool = self._sessions.get(domain)
            if not pool:
                return None
            candidates = [s for s in pool if not s.is_blocked and not s.expired and not s.exhausted]
            if not candidates:
                return None
            s = min(candidates, key=lambda x: (x.usage_count, x.last_used))
            s.touch()
            return s.to_dict()

    # 响应头中不可复用为请求头的成员（HTTP/2 伪头 + hop-by-hop/实体头——回写再发送
    # 会触发 curl 43 CURLE_BAD_FUNCTION_ARGUMENT 或服务器 400）
    _RESPONSE_ONLY_HEADERS = (
        "content-length", "content-encoding", "transfer-encoding", "keep-alive",
        "connection", "proxy-connection", "date", "server", "alt-svc", "via",
        "strict-transport-security", "content-type", "set-cookie", "etag",
        "last-modified", "expires", "cache-control", "vary", "age", "location",
    )

    @classmethod
    def _sanitize_reusable_headers(cls, headers) -> dict:
        """过滤响应头 → 可复用请求头（丢弃伪头/hop-by-hop/实体头；值含非法字符丢弃）"""
        out = {}
        if not isinstance(headers, dict):
            return out
        for k, v in headers.items():
            try:
                if not k or str(k).startswith(":"):
                    continue  # HTTP/2 伪头（:status 等）
                if k.lower() in cls._RESPONSE_ONLY_HEADERS:
                    continue
                sv = v if isinstance(v, str) else ", ".join(str(x) for x in v)
                if any(ord(c) < 0x20 or ord(c) > 0x7E for c in sv):
                    continue
                out[str(k)] = sv
            except Exception:
                continue
        return out

    def update_session(self, url, cookies=None, headers=None):
        """旧接口：更新会话 cookies/headers。兼容 dict 整体传入与 kwargs 两种形式
        [FIXED & MODIFIED] v2.11 出厂级 bug 修复：solver 渲染后把响应头整包写回
        （含 :status 伪头/content-length/transfer-encoding）→ 同域后续请求带垃圾头
        → curl 43 全灭（压测实测 ruanyifeng 内页全挂）。现入池前过滤。"""
        if not url:
            return
        domain = self._domain_of(url)
        with self._lock:
            pool = self._sessions.setdefault(domain, [])  # defaultdict 语义（.get 会在新域 None.append 崩）
            if isinstance(cookies, dict) and headers is None and "cookies" in cookies:
                # 调用方传入整个 sess dict（协议引擎 cookie 同步路径）
                data = cookies
                headers = data.get("headers")
                cookies = data.get("cookies")
            if not pool:
                s = Session(id=hashlib.sha256(f"{domain}{time.time()}".encode()).hexdigest()[:12],
                            domain=domain)
                pool.append(s)
            else:
                s = pool[-1]
            if cookies:
                if isinstance(cookies, str):
                    s.cookies = dict(pair.split("=", 1) for pair in cookies.split(";") if "=" in pair)
                elif isinstance(cookies, dict):
                    s.cookies.update(cookies)
            if headers:
                s.headers.update(self._sanitize_reusable_headers(headers))
            s.last_used = time.time()

    # ── 封禁与清理 ──────────────────────────────────────
    def mark_blocked(self, domain, session_id: Optional[str] = None):
        """标记会话为封禁（不指定 id 则封禁该域名全部会话）"""
        domain = self._domain_of(domain)
        with self._lock:
            for s in self._sessions.get(domain, []):
                if session_id is None or s.id == session_id:
                    s.is_blocked = True
                    s.error_count += 1

    def cleanup_expired(self) -> int:
        """清理过期/超限/封禁会话，返回清理数量"""
        removed = 0
        now = time.time()
        with self._lock:
            for domain in list(self._sessions.keys()):
                alive = [s for s in self._sessions[domain]
                         if not s.is_blocked and not s.expired and not s.exhausted
                         and (now - s.last_used) <= self._ttl]
                removed += len(self._sessions[domain]) - len(alive)
                if alive:
                    self._sessions[domain] = alive
                else:
                    del self._sessions[domain]
        return removed

    def stats(self) -> dict:
        """池状态统计"""
        with self._lock:
            total = sum(len(v) for v in self._sessions.values())
            blocked = sum(1 for v in self._sessions.values() for s in v if s.is_blocked)
            return {"domains": len(self._sessions), "sessions": total, "blocked": blocked}

    # ── 引擎路由回退层级支持 ────────────────────────────
    def get_stealth_headers(self, url) -> dict:
        """隐身请求头（配合引擎路由 Tier 2）

        [FIXED & MODIFIED] v2.10.6 A2 补 Client Hints：原只有 UA/Accept-Language/Sec-Fetch
        五头——sec-ch-ua/platform/mobile 全裸（CH 检测必曝）。版本号必须从所发 UA 主版本提取
        并与 TLS 伪装版本同源（避免 UA/TLS 再失配——见 fingerprint_consistency P0-1 同源绑定）。
        """
        _ua = random_ua()
        _major = "0"
        try:
            _m = __import__("re").search(r"Chrome/(\d+)\.", _ua or "")
            if _m:
                _major = _m.group(1)
        except Exception:
            pass
        _plat = '"Windows"' if "Windows" in (_ua or "") else '"macOS"' if "Macintosh" in (_ua or "") else '"Linux"'
        return {
            "User-Agent": _ua,
            "Accept-Language": random.choice(["zh-CN,zh;q=0.9,en;q=0.8", "en-US,en;q=0.9",
                                              "ja-JP,ja;q=0.9,en;q=0.8", "zh-TW,zh;q=0.9,en;q=0.8"]),
            # Client Hints（版本与 UA 同源）
            "sec-ch-ua": f'"Chromium";v="{_major}", "Google Chrome";v="{_major}", ";Not-A-Brand";v="99"',
            "sec-ch-ua-platform": _plat,
            "sec-ch-ua-mobile": "?0",
            "Sec-Fetch-Site": random.choice(["same-origin", "none"]),
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }

    def get_tls_profile(self, idx: int = 0) -> str:
        """[FIXED & MODIFIED] v2.17 0-7 TLS 一致性：轮换版本池与
        fingerprint_consistency.TLS_IMPERSONATE_POOL 同源——原先硬编码
        chrome123/chrome133a 不在池内：①主客户端池缺该版本（init 只建池内版本）
        ②UA 主版本由 pick_tls_impersonate 决定，Tier3 绕过即 UA→TLS 版本失配。"""
        from .fingerprint_consistency import TLS_IMPERSONATE_POOL
        profiles = list(TLS_IMPERSONATE_POOL)
        return profiles[idx % len(profiles)]
