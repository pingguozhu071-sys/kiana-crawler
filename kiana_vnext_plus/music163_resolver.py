# -*- coding: utf-8 -*-
"""网易云音乐 weapi 解析器 music163_resolver.py（v2.16.1 阶段3）

算法参考 02-spiders 候选工程 extractor/music163/encrypt.py（声明"copy 自
CharlesPikachu/Music-Downloader"，来源/许可不清晰——按公开算法重写实现）：
  weapi = JSON → AES-CBC(key=固定 nonce 0CoJUm6Qyw8W8jud, iv=0102030405060708)
          → base64 → AES-CBC(key=随机 16 字符) → base64 得 params；
  secKey 逆序 + 教科书 RSA（e=010001, 固定 modulus）→ hex(256 位) 得 encSecKey。
只用已 pin 的 cryptography 库（AES-CBC/PKCS7，协议要求的固定 IV）与 Python 原生 pow。
请求统一走 url_utils.safe_urlopen（协议+私网+白名单+重定向逐跳过闸）。

用法: python music163_resolver.py <音乐/视频链接> [--json]
输出与 douyin_resolver 同构：{ok, type, media_url, song_id, method, ...}
"""
import base64
import json
import re
import secrets
import sys

MODULUS = ("00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b72515"
           "2b3ab17a876aea8a5aa76d2e417629ec4ee341f56135fccf695280104e0312ecb"
           "da92557c93870114af6c9d05c4f7f0c3685b7a46bee255932575cce10b424d81"
           "3cfe4875d3e82047b97ddef52741d546b8e289dc6935b3ece0462db0a22b8e7")
PUBKEY = 0x010001
NONCE = b"0CoJUm6Qyw8W8jud"
IV = b"0102030405060708"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

def _media_of(payload: dict) -> dict:
    """[v2.17 0-1] resolver 产物统一经 from_resolver 收口（旧 dict 兼容层保留）"""
    from .media_schema import from_resolver, to_dict
    return to_dict(from_resolver("music163", payload))


def _aes_cbc_encrypt(data: bytes, key: bytes) -> bytes:
    """AES-CBC(PKCS7) 加密（cryptography 实现；iv 为 weapi 协议固定值）"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7
    padder = PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(IV)).encryptor()
    return enc.update(padded) + enc.finalize()


def _rsa_encrypt(sec_key: str) -> str:
    """教科书 RSA（weapi 协议专用：无填充，sec_key 逆序后 m^e mod n → 256 位 hex）"""
    m = int.from_bytes(sec_key[::-1].encode("utf-8"), "big")
    return format(pow(m, PUBKEY, int(MODULUS, 16)), "x").zfill(256)


def build_weapi_body(payload: dict, sec_key: str = None) -> dict:
    """payload → {params, encSecKey}（weapi 请求体）。sec_key 缺省生成随机 16 字符。
    流程：json → AES(nonce) → base64 → 该 b64 串 → AES(secKey) → base64 = params"""
    sec_key = sec_key or secrets.token_hex(8)[:16]
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    step1 = base64.b64encode(_aes_cbc_encrypt(text.encode("utf-8"), NONCE))
    params = base64.b64encode(_aes_cbc_encrypt(step1, sec_key.encode("utf-8"))).decode()
    return {"params": params, "encSecKey": _rsa_encrypt(sec_key)}


def extract_id(url):
    """从网易云链接提取 ID（歌曲/视频）：music.163.com/#/song?id= / /song/123 / /mv/ / #/video?id="""
    if not isinstance(url, str):
        return None
    m = (re.search(r'[?&#/](?:id|song|mv|video)[=\s/]*(\d{3,12})', url)
         or re.search(r'#?/?(?:song|mv|video)/(\d{3,12})', url))
    return m.group(1) if m else None


def weapi_post(url: str, payload: dict, timeout=12, referer="https://music.163.com/"):
    """weapi POST（url_utils.safe_urlopen 统一过闸：协议+私网+白名单+重定向逐跳；
    返回响应 JSON 或 None）
    [v2.17 2.5] 请求体改标准 form 编码（params=..&encSecKey=..）——原 json 序列化 +
    form Content-Type 不匹配；与网易 weapi 惯例一致（离线加密向量不变）。"""
    from urllib.parse import urlencode
    from .url_utils import safe_urlopen
    body = urlencode(build_weapi_body(payload)).encode("utf-8")
    r = safe_urlopen(url, allowed_hosts=("music.163.com", "163.com"),
                     headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded",
                              "Referer": referer, "Cookie": "os=pc; appver=2.9.9; NMTID=1"},
                     data=body, timeout=timeout)
    if r is None:
        return None
    try:
        return json.loads(r.read().decode("utf-8", "ignore"))
    except Exception:
        return None


def resolve(url):
    """解析网易云歌曲/视频直链（weapi；无登录只拿所供档位，诚实降级）"""
    song_id = extract_id(url)
    if not song_id:
        return {"ok": False, "error": "无法提取网易云 ID", "url": url}
    is_mv = "/mv/" in str(url) or "#/video" in str(url) or "/video" in str(url)
    try:
        if is_mv:
            data = weapi_post("https://music.163.com/weapi/song/enhance/play/mv/url?csrf_token=",
                              {"id": song_id, "r": "1080", "csrf_token": ""})
            raw = (data or {}).get("data") or {}
            media = raw.get("url") or ""
            mtype = "mv"
        else:
            data = weapi_post("https://music.163.com/weapi/song/enhance/player/url?csrf_token=",
                              {"ids": f"[{song_id}]", "level": "exhigh",
                               "encodeType": "aac", "csrf_token": ""})
            arr = (data or {}).get("data") or []
            item = arr[0] if arr else {}
            media = item.get("url") or ""
            mtype = "song"
        if not media:
            return {"ok": False, "error": "无直链（权限/试听限制——需登录态或会员档位）",
                    "song_id": song_id, "type": mtype}
        _p = {"ok": True, "song_id": song_id, "media_url": media,
              "type": mtype, "method": "weapi"}
        _p["media"] = _media_of(_p)
        return _p
    except Exception as e:
        return {"ok": False, "error": f"网易云解析异常: {e}", "song_id": song_id}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python music163_resolver.py <网易云链接>")
        sys.exit(1)
    r = resolve(sys.argv[1])
    if "--json" in sys.argv:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(r, ensure_ascii=False))
    sys.exit(0 if r["ok"] else 1)
