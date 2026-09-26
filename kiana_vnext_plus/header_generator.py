"""真实浏览器请求头生成器

生成与 Chrome 浏览器一致的请求头，包括 Client Hints (sec-ch-ua)、
Sec-Fetch 系列、Accept 系列等。
"""
import random
from typing import Dict, Optional

# ═══ UA 池（Chrome 120-134 各版本真实 UA，供 SessionPool 轮换使用） ═══
UA_POOL: list = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
]


def random_ua() -> str:
    """从 UA 池随机选取一个 UA"""
    return random.choice(UA_POOL)


def generate_chrome_headers(
    chrome_version: int,
    platform: str = "Win32",
    language: str = "en-US",
    is_navigation: bool = True,
    referer: Optional[str] = None,
) -> Dict[str, str]:
    """生成 Chrome 浏览器的完整请求头

    Args:
        chrome_version: Chrome 主版本号 (120-131)
        platform: navigator.platform 值
        language: 浏览器语言
        is_navigation: 是否为导航请求（影响 sec-fetch-* 头）
        referer: Referer URL

    Returns:
        完整的请求头字典
    """
    # 确定 sec-ch-ua-platform 值
    if "Win" in platform:
        ch_platform = '"Windows"'
        sec_chua_mobile = "?0"
    elif "Mac" in platform:
        ch_platform = '"macOS"'
        sec_chua_mobile = "?0"
    elif "Linux" in platform:
        ch_platform = '"Linux"'
        sec_chua_mobile = "?0"
    else:
        ch_platform = '"Windows"'
        sec_chua_mobile = "?0"

    # sec-ch-ua 格式（Chrome 120+ 使用 GREASE 值）
    grease = random.choice(['"Not_A Brand";v="8"', '"Not.A/Brand";v="8"', '"Not/A)Brand";v="24"', '"Not)A;Brand";v="99"'])
    sec_chua = f'{grease}, "Chromium";v="{chrome_version}", "Google Chrome";v="{chrome_version}"'

    # Sec-Fetch 系列
    if is_navigation:
        sec_fetch_site = "none"
        sec_fetch_mode = "navigate"
        sec_fetch_user = "?1"
        sec_fetch_dest = "document"
    else:
        sec_fetch_site = "same-origin"
        sec_fetch_mode = "no-cors"
        sec_fetch_user = None
        sec_fetch_dest = "empty"

    headers = {
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8,"
            "application/signed-exchange;v=b3;q=0.7"
        ),
        "Accept-Language": f"{language},{language.split('-')[0]};q=0.9",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "sec-ch-ua": sec_chua,
        "sec-ch-ua-mobile": sec_chua_mobile,
        "sec-ch-ua-platform": ch_platform,
        "Sec-Fetch-Site": sec_fetch_site,
        "Sec-Fetch-Mode": sec_fetch_mode,
        "Sec-Fetch-Dest": sec_fetch_dest,
        "Upgrade-Insecure-Requests": "1" if is_navigation else None,
    }

    # 移除 None 值
    headers = {k: v for k, v in headers.items() if v is not None}

    if sec_fetch_user:
        headers["Sec-Fetch-User"] = sec_fetch_user

    if referer:
        headers["Referer"] = referer

    return headers


def generate_subresource_headers(
    chrome_version: int,
    resource_type: str = "script",
    referer: Optional[str] = None,
    language: str = "en-US",
) -> Dict[str, str]:
    """生成子资源请求头（JS/CSS/图片等）

    Args:
        chrome_version: Chrome 主版本号
        resource_type: 资源类型 (script/style/image/font/xhr/fetch)
        referer: 页面 Referer
        language: 浏览器语言
    """
    accept_map = {
        "script": "*/*;q=0.8",
        "style": "text/css,*/*;q=0.1",
        "image": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "font": "*/*",
        "xhr": "*/*",
        "fetch": "*/*",
    }
    dest_map = {
        "script": "script",
        "style": "style",
        "image": "image",
        "font": "font",
        "xhr": "empty",
        "fetch": "empty",
    }

    grease = random.choice(['"Not_A Brand";v="8"', '"Not.A/Brand";v="8"', '"Not/A)Brand";v="24"'])
    sec_chua = f'{grease}, "Chromium";v="{chrome_version}", "Google Chrome";v="{chrome_version}"'

    headers = {
        "Accept": accept_map.get(resource_type, "*/*"),
        "Accept-Language": f"{language},{language.split('-')[0]};q=0.9",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "sec-ch-ua": sec_chua,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Site": "same-origin" if referer else "cross-site",
        "Sec-Fetch-Mode": "no-cors" if resource_type in ("image", "font") else "cors",
        "Sec-Fetch-Dest": dest_map.get(resource_type, "empty"),
    }

    if referer:
        headers["Referer"] = referer

    if resource_type in ("xhr", "fetch"):
        headers["X-Requested-With"] = "XMLHttpRequest"

    return headers


def generate_api_headers(
    chrome_version: int,
    content_type: str = "application/json",
    language: str = "en-US",
    referer: Optional[str] = None,
    origin: Optional[str] = None,
) -> Dict[str, str]:
    """生成 API 请求头（POST/PUT 等）

    Args:
        chrome_version: Chrome 主版本号
        content_type: 内容类型
        language: 浏览器语言
        referer: Referer URL
        origin: Origin URL
    """
    grease = random.choice(['"Not_A Brand";v="8"', '"Not.A/Brand";v="8"', '"Not/A)Brand";v="24"'])
    sec_chua = f'{grease}, "Chromium";v="{chrome_version}", "Google Chrome";v="{chrome_version}"'

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": f"{language},{language.split('-')[0]};q=0.9",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Content-Type": content_type,
        "sec-ch-ua": sec_chua,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Site": "same-origin" if referer else "cross-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }

    if referer:
        headers["Referer"] = referer

    if origin:
        headers["Origin"] = origin

    return headers


def merge_headers(base: Dict[str, str], extra: Dict[str, str]) -> Dict[str, str]:
    """合并请求头，extra 优先级更高"""
    result = dict(base)
    result.update(extra)
    return result
