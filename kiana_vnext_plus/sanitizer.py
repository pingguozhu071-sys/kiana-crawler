import re

SENSITIVE_HEADERS = {
    "authorization", "cookie", "set-cookie",
    "proxy-authorization", "x-api-key",
}


# [v2.19 安全] 敏感查询参数表扩容（原仅 token/key/auth/session/sid/password/…）：
# 增补 api_key/apikey/api-key/sig/sign/signature/secret/ticket/csrf/nonce——
# 代理源 URL 常带 ?token=，API 调用常带 api_key/sign，此前均不在表内而原样入日志。
# 不纳入过于通用的 code/uid（会误伤电商商品码、正常业务参数）。
_URL_SECRET_RE = re.compile(
    r"([?&](?:token|key|auth|session|sid|password|passwd|access_token|refresh_token"
    r"|api_key|apikey|api-key|sig|sign|signature|secret|ticket|csrf|nonce)=)[^&#]+",
    re.IGNORECASE)


def sanitize_url(url):
    """脱敏 URL 中的 token、key、session 等敏感参数"""
    try:
        return _URL_SECRET_RE.sub(r"\1[REDACTED]", url)
    except Exception:
        return url


def find_emails(text) -> list:
    """[v2.19] 邮箱**查找**（不脱敏）——统一复用上面的有上界正则，供其他模块调用。

    背景：`adaptive_v2.py` 曾自行维护一份**无量词上界**的旧版邮箱正则（即 v2.18 已判定
    为 O(n²) 的那版），与本模块修复后的版本分叉。抽公共函数避免再次分叉。"""
    try:
        return _EMAIL_RE.findall(str(text or ""))
    except Exception:
        return []


def sanitize_headers(headers):
    """脱敏敏感 HTTP 头"""
    return {k: "[REDACTED]" if k.lower() in SENSITIVE_HEADERS else v for k, v in headers.items()}


def sanitize_proxy(proxy):
    """脱敏代理 URL 中的 user:pass@ 凭据（root 日志过滤器覆盖不到代理串）"""
    try:
        from urllib.parse import urlsplit
        s = urlsplit(proxy)
        if s.username or s.password:
            host = s.hostname or ""
            port = f":{s.port}" if s.port else ""
            return f"{s.scheme}://***@{host}{port}"
        return proxy
    except Exception:
        return proxy


# [v2.18 P1-11] 邮箱正则预编译 + 量词封顶：原 `[A-Za-z0-9._%+-]+@` 无上界，
# 1MB 连续合法字符（base64/长 token，每页正文都过这里）对每个起点向后扫到串尾
# → O(n²) 卡死 worker 数分钟。local 部分封顶 64（RFC 5321 上限）、domain 255 后
# 单起点扫描 O(1)~O(64)，整体线性；另加 "@" 快速通道，无 @ 文本直接跳过。
_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,}"
    r"(?![A-Za-z0-9._%+-])")
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_IP_RE = re.compile(
    r"(?<![.\d])(?:25[0-5]|2[0-4]\d|1\d{2}|[1-9]?\d)(?:\.(?:25[0-5]|2[0-4]\d|1\d{2}|[1-9]?\d)){3}(?![.\d])")


def sanitize_text(text):
    """脱敏文本中的手机号、邮箱、IP 地址（隐私保护核心功能）"""
    # [FIXED & MODIFIED] v2.10.4 手机号/邮箱边界修复：原 \b 在「汉字↔数字」之间不成立
    # （\w 含 CJK），「联系13812345678或」漏脱敏 → 隐私泄漏。改为 (?<!\d)/(?!\d) 纯数字边界。
    text = _PHONE_RE.sub("[手机号]", text)
    if "@" in text:
        text = _EMAIL_RE.sub("[邮箱]", text)
    # [FIXED & MODIFIED] IP 段范围校验（0-255）+ 独立边界（前后不能是 . 或数字，
    # 彻底排除 1.2.3.4.5 五段版本号 / 小数 1.2.3.4.5 中间子串误伤）
    text = _IP_RE.sub("[IP]", text)
    return text


# ════════════════════════════════════════════════════════════════
# [v2.19.7 安全·扫描发现] 提取结果**副本**脱敏（导出/落盘用）
#   背景：导出链此前只对 `data['text']` 脱敏——`url`（常带 ?token=/session=）、
#   `images[].src`（签名 CDN 链接）、`author`/`description`/`entities` 原样进
#   extracted 表 + jsonl/csv/markdown 快照，而这些产物正是会被分享/检索/进 AI 管线的部分。
#   ⚠️ 边界（重要）：本函数只用于**导出副本**。爬虫运行态数据（frontier.normalized_url、
#   video_downloads.video_url）**绝不能**过这里——那些 URL 是"稍后还要再请求一次"的
#   功能数据，把 ?token= 的值抹成 [REDACTED] 会让续爬/续下直接 403 失败。
# ════════════════════════════════════════════════════════════════
_URLISH_KEYS = frozenset({
    "url", "final_url", "canonical", "canonical_url", "link", "permalink", "href", "src",
    "srcset", "poster", "next_url", "next", "image", "img", "images", "video", "videos",
    "audio", "source", "thumbnail", "thumb", "cover", "avatar", "author_url", "site_url",
    "referer", "referrer", "location", "download_url", "file_url",
})
_TEXTISH_KEYS = frozenset({
    "title", "description", "author", "site_name", "summary", "keywords", "caption",
    "alt", "name", "username", "nickname", "label", "headline", "subtitle",
})
# 这些容器里的**任意**字符串都按文本脱敏（结构化数据 key 不可枚举）
_DEEP_TEXT_CONTAINERS = frozenset({"entities", "json_ld", "structured", "metadata", "extra"})
_REC_MAX_DEPTH = 6


def sanitize_record(data, *, max_depth: int = _REC_MAX_DEPTH):
    """返回一条提取结果的**脱敏副本**（不改动原对象；异常一律退回原值）。

    - 像 URL 的值（键名命中 _URLISH_KEYS 或以 http(s):// 开头）→ `sanitize_url`
    - 文本键（title/description/author/...）与结构容器（entities/json_ld/...）→ `sanitize_text`
    - 其余值原样透传；深度封顶防病态嵌套；`data` 非 dict/list 时原样返回。
    """
    if not isinstance(data, (dict, list)):
        return data

    def _one(key, val, depth, deep_text=False):
        if depth > max_depth:
            return val
        if isinstance(val, dict):
            return {k: _one(k, v, depth + 1, deep_text) for k, v in val.items()}
        if isinstance(val, (list, tuple)):
            return [_one(key, v, depth + 1, deep_text) for v in val]
        if isinstance(val, str) and val:
            lk = str(key or "").lower()
            if val.startswith(("http://", "https://")) or lk in _URLISH_KEYS:
                return sanitize_url(val)
            if deep_text or lk in _TEXTISH_KEYS:
                return sanitize_text(val)
        return val

    try:
        if isinstance(data, list):
            return [_one(None, v, 1) for v in data]
        return {k: _one(k, v, 1, str(k or "").lower() in _DEEP_TEXT_CONTAINERS)
                for k, v in data.items()}
    except Exception:
        return data
