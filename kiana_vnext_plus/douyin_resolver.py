# -*- coding: utf-8 -*-
"""
抖音解析器 douyin_resolver.py —— 抖音无水印视频直链解析（v2.10.1）
yt-dlp 对抖音已失效（Unsupported URL——抖音改版 extractor 没跟上）→ 自定义解析：
  短链/分享链接 → aweme_id → 视频页 HTML 提取视频流 → playwm 替换 play（无水印）
用法: python douyin_resolver.py <抖音链接> [--json]
"""
import sys
import re
import json
import time
import os
import pathlib

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"


def _load_cookies():
    """从 Kiana cookies 加载 douyin 域 cookies（v2.10.5——多文件自填合并）
    路径优先级：KIANA_COOKIE_FILES（分号分隔多文件）→ KIANA_COOKIE_FILE（单文件）
    → 默认 LOCALAPPDATA\\KianaVnextPlus\\cookies.txt"""
    try:
        envs = os.environ.get("KIANA_COOKIE_FILES") or ""
        if envs:
            # [v2.18 P2-7] 统一走 parse_cookie_file_list（换行分隔/注释/引号原来静默失效）
            from .cookie_utils import parse_cookie_file_list
            srcs = parse_cookie_file_list(envs)
        else:
            srcs = [os.environ.get("KIANA_COOKIE_FILE") or
                    str(pathlib.Path(os.environ["LOCALAPPDATA"]) / "KianaVnextPlus" / "cookies.txt")]
        pairs = []
        for kf in srcs:
            kfp = pathlib.Path(kf)
            if not kfp.exists():
                continue
            for line in kfp.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 7 and "douyin" in parts[0]:
                    pairs.append(f"{parts[5]}={parts[6]}")
        return "; ".join(pairs)
    except Exception:
        return ""


_ALLOWED_HOST_SUFFIXES = ("douyin.com", "iesdouyin.com", "bytedance.com",
                          "douyinpic.com", "byteplover.com")


def _sanitize_http_url(url) -> str:
    """请求目标闸：复用 url_utils._http_target_ok（协议+私网+白名单+重定向逐跳），
    并强制升级 https。全部动态请求入口必须先过此闸，否则直接弃请求。"""
    from .url_utils import _http_target_ok
    if not _http_target_ok(url, _ALLOWED_HOST_SUFFIXES):
        return ""
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    return url


def http_get(url, timeout=15, referer=None, redirects=True, cookie_extra=""):
    from .url_utils import safe_urlopen
    url = _sanitize_http_url(url)
    if not url:
        return None, b"blocked: url target check failed"
    hdrs = {"User-Agent": UA, "Accept": "application/json,text/html,application/xhtml+xml,*/*",
            "Accept-Language": "zh-CN,zh;q=0.9"}
    if referer:
        hdrs["Referer"] = referer
    ck = _load_cookies()
    if cookie_extra:
        ck = "; ".join(filter(None, (ck, cookie_extra)))
    if ck:
        hdrs["Cookie"] = ck
    try:
        r = safe_urlopen(url, allowed_hosts=_ALLOWED_HOST_SUFFIXES,
                         headers=hdrs, timeout=timeout)
        if r is None:
            return None, b"blocked: request failed"
        return r.geturl(), r.read()
    except Exception as e:
        return None, str(e).encode()


def _gen_ttwid() -> str:
    """[v2.16.1] 抖音设备注册 cookie（ttwid）——请求链路弱登录补强（参考候选 C 的
    ttwid.bytedance.com/union/register 接口，重写实现；拿不到返回空串不阻塞）。"""
    from .url_utils import safe_urlopen
    url = _sanitize_http_url("https://ttwid.bytedance.com/ttwid/union/register/")
    if not url:
        return ""
    try:
        payload = json.dumps({
            "region": "cn", "aid": 1768, "union": True,
            "service": "www.ixigua.com",
            "deviceId": "", "platform": "desktop",
            "version": "0.6.0",
        }).encode()
        r = safe_urlopen(url, allowed_hosts=_ALLOWED_HOST_SUFFIXES,
                         headers={"User-Agent": UA, "Content-Type": "application/json"},
                         data=payload, timeout=8)
        if r is None:
            return ""
        ck = r.headers.get("Set-Cookie", "") or ""
        m = re.search(r"ttwid=([^;]+)", ck)
        if m:
            return f"ttwid={m.group(1)}"
        return ""
    except Exception:
        return ""


def _gen_fake_ms_token() -> str:
    """[v2.16.1] msToken 真 token 失败时的长度合格假 token（参考候选 C 的静默降级思路：
    解析链继续跑而不中断；真实 msToken 仍优先）。"""
    import random as _rnd
    import string as _st
    n = 126
    body = "".join(_rnd.choice(_st.ascii_letters + _st.digits + "-_") for _ in range(n))
    return body + "=="


def extract_aweme_id(url):
    """从任意抖音链接提取 aweme_id（视频 ID）"""
    # [FIXED & MODIFIED] v2.11 body 初始化：原仅短链分支赋值，非短链 URL 时
    # `if body:` 抛 UnboundLocalError → 被上层吞掉误判"解析异常"
    body = b""
    # 分享短链 v.douyin.com/xxx
    if "v.douyin.com" in url or "iesdouyin.com" in url:
        final, body = http_get(url, timeout=12)
        if final:
            url = final
    # douyin.com/video/<id> 或 share/forward/<id>
    m = re.search(r"(?:video|share/forward|note)/(\d{15,20})", url)
    if m:
        return m.group(1)
    m = re.search(r"item_ids=(\d{15,20})", url)
    if m:
        return m.group(1)
    # HTML 里找
    if body:
        m = re.search(r'"aweme_id"\s*:\s*"(\d{15,20})"', body.decode("utf-8", "ignore"))
        if m:
            return m.group(1)
    return None


def _gen_ms_token():
    """v2.10.3 生成真实 msToken（POST mssdk.bytedance.com/web/report）"""
    try:
        payload = json.dumps({
            "magic": 538969122, "version": 1, "dataType": 8,
            "strData": ("fWOdJTQR3/jwmZqBBsPO6tdNEc1jX7YTwPg0Z8CT+j3HScLFbj2Zm1XQ7/lqgSutntVKLJWaY3Hc/+vc0h+So9N1t6EqiImu5jKyUa+S4NPy6cNP0x9CUQQgb4+RRihCgsn4QyV8jivEFOsj3N5zFQbzXRyOV+9aG5B5EAnwpn8C70llsWq0zJz1VjN6y2KZiBZRyonAHE8feSGpwMDeUTllvq6BG3AQZz7RrORLWNCLEoGzM6bMovYVPRAJipuUML4Hq/568bNb5vqAo0eOFpvTZjQFgbB7f/CtAYYmnOYlvfrHKBKvb0TX6AjYrw2qmNNEer2ADJosmT5kZeBsogDui8rNiI/OOdX9PVotmcSmHOLRfw1cYXTgwHXr6cJeJveuipgwtUj2FNT4YCdZfUGGyRDz5bR5bdBuYiSRteSX12EktobsKPksdhUPGGv99SI1QRVmR0ETdWqnKWOj/7ujFZsNnfCLxNfqxQYEZEp9/U01CHhWLVrdzlrJ1v+KJH9EA4P1Wo5/2fuBFVdIz2upFqEQ11DJu8LSyD43qpTok+hFG3Moqrr81uPYiyPHnUvTFgwA/TIE11mTc/pNvYIb8IdbE4UAlsR90eYvPkI+rK9KpYN/l0s9ti9sqTth12VAw8tzCQvhKtxevJRQntU3STeZ3coz9Dg8qkvaSNFWuBDuyefZBGVSgILFdMy33//l/eTXhQpFrVc9OyxDNsG6cvdFwu7trkAENHU5eQEWkFSXBx9Ml54+fa3LvJBoacfPViyvzkJworlHcYYTG392L4q6wuMSSpYUconb+0c5mwqnnLP6MvRdm/bBTaY2Q6RfJcCxyLW0xsJMO6fgLUEjAg/dcqGxl6gDjUVRWbCcG1NAwPCfmYARTuXQYbFc8LO+r6WQTWikO9Q7Cgda78pwH07F8bgJ8zFBbWmyrghilNXENNQkyIzBqOQ1V3w0WXF9+Z3vG3aBKCjIENqAQM9qnC14WMrQkfCHosGbQyEH0n/5R2AaVTE/ye2oPQBWG1m0Gfcgs/96f6yYrsxbDcSnMvsA+okyd6GfWsdZYTIK1E97PYHlncFeOjxySjPpfy6wJc4UlArJEBZYmgveo1SZAhmXl3pJY3yJa9CmYImWkhbpwsVkSmG3g11JitJXTGLIfqKXSAhh+7jg4HTKe+5KNir8xmbBI/DF8O/+diFAlD+BQd3cV0G4mEtCiPEhOvVLKV1pE+fv7nKJh0t38wNVdbs3qHtiQNN7JhY4uWZAosMuBXSjpEtoNUndI+o0cjR8XJ8tSFnrAY8XihiRzLMfeisiZxWCvVwIP3kum9MSHXma75cdCQGFBfFRj0jPn1JildrTh2vRgwG+KeDZ33BJ2VGw9PgRkztZ2l/W5d32jc7H91FftFFhwXil6sA23mr6nNp6CcrO7rOblcm5SzXJ5MA601+WVicC/g3p6A0lAnhjsm37qP+xGT+cbCFOfjexDYEhnqz0QZm94CCSnilQ9B/HBLhWOddp9GK0SABIk5i3xAH701Xb4HCcgAulvfO5EK0RL2eN4fb+CccgZQeO1Zzo4qsMHc13UG0saMgBEH8SqYlHz2S0CVHuDY5j1MSV0nsShjM01vIynw6K0T8kmEyNjt1eRGlleJ5lvE8vonJv7rAeaVRZ06rlYaxrMT6cK3RSHd2liE50Z3ik3xezwWoaY6zBXvCzljyEmqjNFgAPU3gI+N1vi0MsFmwAwFzYqqWdk3jwRoWLp//FnawQX0g5T64CnfAe/o2e/8o5/bvz83OsAAwZoR48GZzPu7KCIN9q4GBjyrePNx5Csq2srblifmzSKwF5MP/RLYsk6mEE15jpCMKOVlHcu0zhJybNP3AKMVllF6pvn+HWvUnLXNkt0A6zsfvjAva/tbLQiiiYi6vtheasIyDz3HpODlI+BCkV6V8lkTt7m8QJ1IcgTfqjQBummyjYTSwsQji3DdNCnlKYd13ZQa545utqu"),
            "tspFromClient": int(time.time() * 1000),
        })
        # [v2.17 2.9] 目标闸对齐：原直接 urlopen 绕过 _sanitize_http_url（注释承诺
        # "全部动态请求入口必须先过此闸"）——改走 safe_urlopen（同一闸 + 重定向校验）
        from .url_utils import safe_urlopen
        r = safe_urlopen("https://mssdk.bytedance.com/web/report",
                         allowed_hosts=_ALLOWED_HOST_SUFFIXES,
                         headers={"User-Agent": UA, "Content-Type": "application/json"},
                         data=payload.encode(), timeout=10)
        if r is None:
            return ""
        ck = r.headers.get("Set-Cookie", "")
        m = re.search(r"msToken=([^;]+)", ck)
        return m.group(1) if m else ""
    except Exception:
        return ""


def _status_err(code):
    """[FIXED & MODIFIED] v2.11 抖音 detail API 错误码语义化
    [v2.16.1] 改用 api_errors 平台码表（新增 71=视频删除等），未命中回归可读兜底"""
    try:
        from .api_errors import error_hint
        return error_hint("douyin", code, default_message="detail API 风控/错误")
    except Exception:
        if code == 2155:
            return "抖音风控拦截 (status=2155，请求过频/IP 风控，稍后重试或换出口)"
        if code in (2190, 2192):
            return f"抖音登录态过期 (status={code}，请重新导出抖音 cookies)"
        if code == 8:
            return "抖音服务端错误 (status=8)"
        return f"detail API 风控/错误 (status={code})"


def _resolve_via_share_page(aweme_id):
    """[FIXED & MODIFIED] v2.11 分享页 HTML 兜底通道（detail API 被风控时的第二通道）
    抓 iesdouyin 分享页，从 window._ROUTER_DATA / RENDER_DATA JSON 或 playAddr 正则中提取直链。"""
    try:
        url = f"https://www.iesdouyin.com/share/video/{aweme_id}/"
        final, body = http_get(url, timeout=15, referer="https://www.douyin.com/")
        if not body:
            return None
        html = body.decode("utf-8", "ignore")
        data = None
        m = (re.search(r"window\._ROUTER_DATA\s*=\s*(\{.*?\})\s*</script>", html, re.S)
             or re.search(r'<script[^>]*id="RENDER_DATA"[^>]*>\s*(\{.*?\})\s*</script>', html, re.S))
        if m:
            try:
                data = json.loads(m.group(1))
            except Exception:
                data = None
        if data:
            def _walk(o):
                if isinstance(o, dict):
                    if isinstance(o.get("playAddr"), list) and o["playAddr"]:
                        return o["playAddr"][0].get("src")
                    pa = o.get("play_addr")
                    if isinstance(pa, dict) and (pa.get("url_list") or []):
                        return pa["url_list"][0]
                    for v2 in o.values():
                        r = _walk(v2)
                        if r:
                            return r
                elif isinstance(o, list):
                    for v2 in o:
                        r = _walk(v2)
                        if r:
                            return r
                return None
            src = _walk(data)
            if src:
                raw = src.replace("\\u0026", "&").replace("\\/", "/")
                _p = {"ok": True, "aweme_id": aweme_id, "video_url": raw.replace("playwm", "play"),
                      "uri": "", "method": "share_page", "candidates": 1}
                _p["media"] = _media_of(_p)
                return _p
        # JSON 解析失败时的最后手段：正则直抓 playAddr
        m2 = re.search(r'"playAddr":\s*\[\{"src":\s*"([^"]+)"', html)
        if m2:
            raw = m2.group(1).replace("\\u0026", "&").replace("\\/", "/")
            _p = {"ok": True, "aweme_id": aweme_id, "video_url": raw.replace("playwm", "play"),
                  "uri": "", "method": "share_page_regex", "candidates": 1}
            _p["media"] = _media_of(_p)
            return _p
        return None
    except Exception:
        return None

def _media_of(payload: dict) -> dict:
    """[v2.17 0-1] resolver 产物统一经 from_resolver 收口（旧 dict 兼容层保留）"""
    from .media_schema import from_resolver, to_dict
    return to_dict(from_resolver("douyin", payload))



def resolve(url):
    """解析抖音链接 → 无水印视频直链
    路径：aweme_id → a_bogus 签名 → aweme/detail API → play_addr（playwm→play 无水印）
    """
    aweme_id = extract_aweme_id(url)
    if not aweme_id:
        return {"ok": False, "error": "无法提取 aweme_id", "url": url}
    # v2.10.3: a_bogus 签名路径（本地生成签名——不再依赖浏览器/开源 API）
    # [FIXED & MODIFIED] v2.11 重试+风控语义化：detail 风控(2155)/服务端错误(8) 换
    # msToken+签名重试一次；登录态过期(2190/2192) 等确定性错误直接返回可读原因；
    # 双次尝试全败后走分享页 HTML 兜底通道
    try:
        from urllib.parse import quote
        try:
            from .douyin_abogus import ABogus  # 包内导入
        except ImportError:
            from douyin_abogus import ABogus  # 直接脚本运行
        params = {
            "device_platform": "webapp", "aid": "6383", "channel": "channel_pc_web",
            "pc_client_type": "1", "version_code": "290100", "version_name": "29.1.0",
            "cookie_enabled": "true", "screen_width": "1920", "screen_height": "1080",
            "browser_language": "zh-CN", "browser_platform": "Win32",
            "browser_name": "Chrome", "browser_version": "130.0.0.0",
            "browser_online": "true", "engine_name": "Blink", "engine_version": "130.0.0.0",
            "os_name": "Windows", "os_version": "10", "cpu_core_num": "12",
            "device_memory": "8", "platform": "PC", "downlink": "10",
            "effective_type": "4g", "from_user_page": "1", "locate_query": "false",
            "need_time_list": "1", "pc_libra_divert": "Windows",
            "publish_video_strategy_type": "2", "round_trip_time": "0",
            "show_live_replay_strategy": "1", "time_list_query": "0",
            "whale_cut_token": "", "update_version_code": "170400",
            "msToken": "", "aweme_id": aweme_id,
        }
        last_status = 0
        _ttwid = _gen_ttwid()  # [v2.16.1] 设备注册 cookie（弱登录补强；拿不到不阻塞）
        # [v2.17 2.7] 码表驱动的重试语义：2155/8（瞬时）可重试；2190/2192/71 等
        # 确定性错误第一次失败即返回，不再白耗第二次请求。
        # [v2.19 注释澄清] 原注释写"与 api_errors.retry_whitelist 一致，同步版"——
        # **不准确**：本处是"按响应里的 status_code 对照 PLATFORM_ERROR_CODES 码表决定
        # 是否 continue"的业务逻辑（与响应体解析交织）；而 retry_whitelist 是"异常驱动
        # 重试"的装饰器（且为 async），两者解决不同问题。无可复用关系，非重复实现。
        for _attempt in range(2):
            if _attempt > 0:
                time.sleep(1.0)
            params["msToken"] = _gen_ms_token() or _gen_fake_ms_token()  # 真 token 失败降级假 token
            try:
                bogus = ABogus().get_value(params)
            except Exception as e:
                return {"ok": False, "error": f"a_bogus 签名失败: {e}", "aweme_id": aweme_id}
            ab = quote(bogus, safe="")
            api = ("https://www.douyin.com/aweme/v1/web/aweme/detail/?"
                   + "&".join(f"{k}={v}" for k, v in params.items())
                   + f"&a_bogus={ab}")
            final, body = http_get(api, timeout=15, referer=f"https://www.douyin.com/video/{aweme_id}",
                                   cookie_extra=_ttwid)
            if not body:
                continue  # 网络失败/闸拒 → 重试
            try:
                d = json.loads(body.decode("utf-8", "ignore"))
            except Exception:
                continue
            sc = d.get("status_code") or 0
            if sc in (2155, 8):
                last_status = sc
                continue  # 风控/服务端瞬时 → 换签名重试一次（白名单外）
            if sc not in (0, None):
                return {"ok": False, "error": _status_err(sc), "aweme_id": aweme_id, "status": sc}
            item = d.get("aweme_detail") or {}
            v = item.get("video") or {}
            pa = v.get("play_addr") or {}
            urls = pa.get("url_list") or []
            uri = pa.get("uri", "")
            # [FIXED & MODIFIED] v2.10.5c 高码率选择：play_addr.url_list[0] 往往是
            # 最低档/试看段（实测 9467ms 视频只拿 396KB 低清）。bit_rate 数组含 15 档，
            # 选 bit_rate 最大档的 play_addr.url_list[0]（无水印直链最高画质）。
            _best = None
            _max_br = -1
            for br in v.get("bit_rate") or []:
                try:
                    _br = int(br.get("bit_rate") or 0)
                except Exception:
                    _br = 0
                if _br > _max_br:
                    _p2 = (br.get("play_addr") or {}).get("url_list") or []
                    if _p2:
                        _best = _p2[0]
                        _max_br = _br
            if _best:
                urls = [_best]
            if urls:
                pick = urls[0].replace("\\u0026", "&").replace("\\/", "/")
                no_watermark = pick.replace("playwm", "play")
                _p = {"ok": True, "aweme_id": aweme_id, "video_url": no_watermark,
                      "uri": uri, "method": "abogus", "candidates": len(urls),
                      "bitrate": _max_br, "duration_ms": item.get("duration", 0)}
                _p["media"] = _media_of(_p)
                return _p
            last_status = 0
        # [FIXED & MODIFIED] v2.11 分享页 HTML 兜底（detail 双次尝试全败后）
        sp = _resolve_via_share_page(aweme_id)
        if sp:
            return sp
        return {"ok": False, "error": f"detail API 无视频地址(status={last_status})，分享页兜底亦失败",
                "aweme_id": aweme_id}
    except Exception as e:
        return {"ok": False, "error": f"resolve 异常: {e}", "aweme_id": aweme_id}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python douyin_resolver.py <抖音链接>")
        sys.exit(1)
    r = resolve(sys.argv[1])
    if "--json" in sys.argv:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        if r["ok"]:
            print(f"✅ aweme_id: {r['aweme_id']}")
            print(f"✅ 无水印直链: {r['video_url'][:120]}")
        else:
            print(f"❌ {r.get('error')}")
    sys.exit(0 if r["ok"] else 1)
