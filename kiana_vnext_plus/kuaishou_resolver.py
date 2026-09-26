# -*- coding: utf-8 -*-
"""快手解析器 kuaishou_resolver.py（v2.16.1 阶段3；v2.17 2.1 占位兑现）

通道策略：
  页面状态通道（已实现）：短链 → 视频页 → pageData/__INITIAL_STATE__/srcNoMark 提取
  无水印直链。
  签名通道（骨架）：prepare_signatures() 为未来"页内签名环境捕获"（CDP/浏览器上下文
  里 Object.defineProperty(caver) 截获 __ks_realm → $encode 生成 __NS_hxfalcon）的
  稳定出口——当前恒返回 {}（= 无签名，走页面状态通道）；签名签名不可用时调用方
  走诚实降级（返回可读错误，绝不静默失败）。
  候选 B 的 srcNoMark/pageData 字段名仅作情报对照。

用法: python kuaishou_resolver.py <快手链接>
输出与 douyin_resolver 同构：{ok, video_id, video_url, images, method, ...}
"""
import json
import re
import sys

def _media_of(payload: dict) -> dict:
    """[v2.17 0-1] resolver 产物统一经 from_resolver 收口（旧 dict 兼容层保留）"""
    from .media_schema import from_resolver, to_dict
    return to_dict(from_resolver("kuaishou", payload))


def prepare_signatures(video_id, browser=None, api_uri="", query="", body="") -> dict:
    """[v2.17 2.1] 快手页内签名占位/同步契约：当前恒返回空 dict（无签名——页面状态通道自足）。
    签名真实现见 resolve_async（需 solver 浏览器上下文，async 环境）；捕获脚本与
    _sign_in_browser 已就绪（3.3）。绝不抛异常。"""
    return {}


# [v2.17 3.3] 页内签名环境捕获脚本（自研写法——仅"捕获+暴露"两个动作，不复制任何
# 平台/第三方算法实现；算法执行由平台页面自身 JS 承担=零逆向、零许可风险）
KS_SIGN_CAPTURE_SCRIPT = """
(() => {
  window.__ks_realm = null;
  try {
    Object.defineProperty(Object.prototype, 'caver', {
      configurable: true,
      set: function (v) {
        if (v && (v.$encode || v['$encode'])) { window.__ks_realm = v; }
      },
    });
  } catch (e) {}
})();
"""


async def _sign_in_browser(solver, url: str, video_id, api_uri="", query="", body=""):
    """[v2.17 3.3] 借用 solver 浏览器上下文生成 __NS_hxfalcon 签名（实验）。
    注入捕获脚本 → goto 视频页（登录态由池上下文 cookies 承载）→ $encode(uri,query,body)。
    捕获不到（未登录/页面未初始化）→ None；调用方走页面状态通道兜底。"""
    try:
        import json as _json
        capture = KS_SIGN_CAPTURE_SCRIPT
        expr = ("(() => {"
                "  const r = window.__ks_realm;"
                "  if (!r || typeof r.$encode !== 'function') return null;"
                f"  return r.$encode({_json.dumps(api_uri or '')}, "
                f"{_json.dumps(query or '')}, {_json.dumps(body or '')});"
                "})()")
        out = await solver.run_in_page(url, init_script=capture, eval_expr=expr, wait_ms=2500)
        if out and isinstance(out, str):
            return {"__NS_hxfalcon": out, "caver": 2}
        return None
    except Exception:
        return None


async def resolve_async(url, solver=None, sign_enabled=False, api_uri="", query="", body=""):
    """[v2.17 3.3] 快手 async 通道：sign_enabled 且 solver 可用 → 先尝试页内签名
    （签名结果附入返回，供未来 REST 通道消费）；页面状态通道恒兜底。
    输出与 resolve 同构（多 signature 字段）。"""
    result = resolve(url)
    if sign_enabled and solver is not None and result.get("ok"):
        vid = extract_video_id(url)
        try:
            sig = await _sign_in_browser(solver, url, vid, api_uri=api_uri,
                                         query=query, body=body)
            if sig:
                result["signature"] = sig
        except Exception:
            pass
    return result

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
_ALLOWED_HOSTS = ("kuaishou.com", "gifshow.com", "kspkg.com", "kscdn.com")


def extract_video_id(url):
    """快手链接 → video_id：/short-video/<id> / f/<short> / /profile/...<id>"""
    if not isinstance(url, str):
        return None
    m = re.search(r'(?:short-video|f|photo)/([0-9A-Za-z_-]{8,30})', url)
    if m:
        return m.group(1)
    m = re.search(r'([0-9]{8,25})', url)
    if m:
        return m.group(1)
    return None


def _fetch_html(url, referer="https://www.kuaishou.com/"):
    from .url_utils import safe_urlopen
    r = safe_urlopen(url, allowed_hosts=_ALLOWED_HOSTS,
                     headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9",
                              "Referer": referer}, timeout=15)
    if r is None:
        return ""
    try:
        return r.read().decode("utf-8", "ignore")
    except Exception:
        return ""


def _extract_state(html):
    """快手页面数据载体提取：window.pageData / __INITIAL_STATE__ / window.__APOLLO_STATE__"""
    for pat in (r'window\.pageData\s*=\s*(\{.*?\})\s*;?\s*</script>',
                r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>',
                r'window\.__APOLLO_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>'):
        m = re.search(pat, html, re.S)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                continue
    return None


def _media_from_state(state):
    """状态树深搜：srcNoMark（无水印）→ urlList/主播放地址；封面图 imageUrls。"""
    video, images = "", set()
    if not state:
        return video, images

    def walk(o, d=0):
        nonlocal video
        if d > 14:
            return
        if isinstance(o, dict):
            if isinstance(o.get("srcNoMark"), str) and o["srcNoMark"].startswith("http"):
                if not video:
                    video = o["srcNoMark"]
            elif isinstance(o.get("photoUrl"), str) and "http" in o["photoUrl"] and not video:
                video = o["photoUrl"]
            for k, v in o.items():
                if k in ("imageUrls", "coverUrls", "imgUrlList") and isinstance(v, list):
                    for u in v:
                        if isinstance(u, str) and u.startswith("http"):
                            images.add(u)
                walk(v, d + 1)
        elif isinstance(o, list):
            for v in o:
                walk(v, d + 1)

    walk(state)
    return video, list(images)[:20]


def resolve(url):
    """解析快手视频→无水印直链（页面状态通道；签名通道未接入时诚实报错）"""
    try:
        vid = extract_video_id(url)
        # [v2.17 2.1] 签名出口（当前恒 {}——签名通道未接入；接入后带签名走 REST）
        _sig = prepare_signatures(vid or "")
        final_url = url
        # 短链（v.kuaishou.com）——safe_urlopen 自动跟随重定向并校验每一跳
        if "v.kuaishou.com" in str(url):
            from .url_utils import safe_urlopen
            r = safe_urlopen(url, allowed_hosts=_ALLOWED_HOSTS,
                             headers={"User-Agent": UA}, timeout=15)
            if r is not None:
                try:
                    final_url = r.geturl()
                except Exception:
                    pass
        html = _fetch_html(final_url)
        if not html:
            return {"ok": False, "error": "视频页抓取失败（风控/网络；快手建议导 cookies）",
                    "video_id": vid, "url": url}
        state = _extract_state(html)
        video_url, images = _media_from_state(state)
        if not video_url:
            # 最后一招：HTML 内直接正则（页面结构改版前常见）
            m = re.search(r'"srcNoMark"\s*:\s*"([^"]+)"', html)
            if m:
                video_url = m.group(1)
        if not video_url:
            return {"ok": False,
                    "error": "未提取到无水印直链（页面已改版或未登录——页内签名通道待接入，先用浏览器打开视频页验证登录态）",
                    "video_id": vid, "url": url}
        _p = {"ok": True, "video_id": vid or "", "video_url": video_url,
              "images": images, "method": "page_state", "url": url}
        _p["media"] = _media_of(_p)
        return _p
    except Exception as e:
        return {"ok": False, "error": f"kuaishou resolve 异常: {e}", "url": url}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python kuaishou_resolver.py <快手链接>")
        sys.exit(1)
    r = resolve(sys.argv[1])
    print(json.dumps(r, ensure_ascii=False, indent=2))
    sys.exit(0 if r["ok"] else 1)
