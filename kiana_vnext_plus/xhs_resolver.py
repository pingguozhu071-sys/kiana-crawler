# -*- coding: utf-8 -*-
"""小红书解析器 xhs_resolver.py（v2.16.1 阶段3）

通道策略（零逆向：只读公开页面，不依赖任何签名组件）：
  window.__INITIAL_STATE__ / __NUXT__ 一类页面内嵌状态属公开页面数据结构，
  取其媒体地址无需签名。实现：xhslink / xiaohongshu 短链 → 详情页 HTML →
  __INITIAL_STATE__ JSON 深度行走，提取无水印图片 / 视频直链。
  [配合 api_errors.xhs 码表——若未来接入签名 API 通道，错误可语义化]

用法: python xhs_resolver.py <小红书链接>
输出与 douyin_resolver 同构：{ok, note_id, title, images, video_url, method, ...}
"""
import base64
import json
import re
import sys

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
_ALLOWED_HOSTS = ("xiaohongshu.com", "xhslink.com", "xhs-img.com", "xhscdn.com")

def _media_of(payload: dict) -> dict:
    """[v2.17 0-1] resolver 产物统一经 from_resolver 收口（旧 dict 兼容层保留）"""
    from .media_schema import from_resolver, to_dict
    return to_dict(from_resolver("xhs", payload))


def extract_note_id(url):
    """小红书链接 → note_id（/explore/<id> / discovery/item/<id> / #笔记 短链走重定向）"""
    if not isinstance(url, str):
        return None
    m = re.search(r'(?:explore|discovery/item|browse)/?[?#/]?([0-9a-f]{16,32})', url)
    if m:
        return m.group(1)
    m = re.search(r'#?/?([0-9a-f]{16,32})', url)
    if m:
        return m.group(1)
    return None


def _fetch_html(url, referer=""):
    from .url_utils import safe_urlopen
    hdrs = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept": "text/html,application/xhtml+xml"}
    if referer:
        hdrs["Referer"] = referer
    r = safe_urlopen(url, allowed_hosts=_ALLOWED_HOSTS, headers=hdrs, timeout=15)
    if r is None:
        return None
    try:
        return r.read().decode("utf-8", "ignore")
    except Exception:
        return None


def _resolve_short(url):
    """xhslink 短链 → 终链（safe_urlopen 自动跟随重定向；返回最终 URL）"""
    from .url_utils import safe_urlopen
    r = safe_urlopen(url, allowed_hosts=_ALLOWED_HOSTS,
                     headers={"User-Agent": UA}, timeout=15)
    if r is not None:
        try:
            return r.geturl()
        except Exception:
            pass
    return url


def _init_state(html):
    """页面 JSON 状态提取（window.__INITIAL_STATE__ / RENDER_DATA 双格式）。
    [v2.17 2.3] 兼容真实页面三种形态：纯 JSON / decodeURIComponent("%7B...") /
    base64 包裹——通用捕获后按候选序列净化解析（全部失败返回 None）。"""
    m = (re.search(r'window\.__INITIAL_STATE__\s*=\s*([^\n;<]{2,});?\s*</script>', html, re.S)
         or re.search(r'<script[^>]*id="RENDER_DATA"[^>]*>\s*([^\n;<]{2,}?)\s*</script>', html, re.S))
    if not m:
        return None
    raw = m.group(1).strip()
    # 剥 decodeURIComponent("...") 外壳（真实页面常见形态）
    _dec = re.match(r'decodeURIComponent\(\s*[\'"]?([^\'"]+)[\'"]?\s*\)', raw)
    if _dec:
        raw = _dec.group(1)
    import urllib.parse as _up
    candidates = []
    if raw.startswith(("%7B", "%7b")):
        candidates.append(_up.unquote(raw))
    candidates.append(raw)
    try:
        candidates.append(base64.b64decode(raw).decode("utf-8", "ignore"))
    except Exception:
        pass
    for cand in candidates:
        if not cand or not cand.lstrip().startswith("{"):
            continue
        try:
            return json.loads(cand)
        except Exception:
            continue
    return None


def _media_extract(state):
    """页面状态深搜提取（一次遍历）：title / 图片直链（urlDefault 无水印）/ 视频直链
    （master_url/m4s_url 为无水印主链）。"""
    title, video_url = "", ""
    images = {}

    def walk(o, d=0):
        nonlocal title, video_url
        if d > 12:
            return
        if isinstance(o, dict):
            if not title and isinstance(o.get("title"), str) and len(o["title"]) > 2:
                title = o["title"]
            if not video_url:
                for k in ("master_url", "m4s_url"):
                    v = o.get(k)
                    if isinstance(v, str) and v.startswith("http"):
                        video_url = v
                        break
            u = o.get("urlDefault")
            if isinstance(u, str) and u.startswith("http"):
                images[u] = True
            for v2 in o.values():
                walk(v2, d + 1)
        elif isinstance(o, list):
            for v2 in o:
                walk(v2, d + 1)

    walk(state)
    return title, list(images.keys())[:20], video_url


def resolve(url):
    """解析小红书笔记→无水印图片/视频直链（页面状态通道，零逆向）"""
    try:
        _u = _resolve_short(url) if ("xhslink.com" in str(url)) else url
        note_id = extract_note_id(_u) or extract_note_id(url)
        html = _fetch_html(_u, referer="https://www.xiaohongshu.com/")
        if not html:
            return {"ok": False, "error": "详情页抓取失败（风控/网络；未登录可先导 cookies）",
                    "note_id": note_id, "url": url}
        state = _init_state(html)
        title, images, video_url = _media_extract(state) if state else ("", [], "")
        if not images and not video_url:
            return {"ok": False, "error": "页面状态未提取到媒体（页面改版或未登录重定向）",
                    "note_id": note_id, "url": url}
        desc = ""
        m = re.search(r'<meta[^>]+name="description"[^>]+content="([^"]*)"', html, re.I)
        if m:
            desc = m.group(1)
        _p = {"ok": True, "note_id": note_id or "", "title": title, "desc": desc,
              "images": images, "video_url": video_url,
              "method": "page_state", "url": url}
        _p["media"] = _media_of(_p)
        return _p
    except Exception as e:
        return {"ok": False, "error": f"xhs resolve 异常: {e}", "url": url}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python xhs_resolver.py <小红书链接>")
        sys.exit(1)
    r = resolve(sys.argv[1])
    print(json.dumps(r, ensure_ascii=False, indent=2))
    sys.exit(0 if r["ok"] else 1)
