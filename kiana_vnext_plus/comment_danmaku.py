"""B站 评论/弹幕采集（v2.11）

WBI 签名（mixinKey 表 + md5）→ 评论 x/v2/reply/wbi/main + 弹幕 comment.bilibili.com/{cid}.xml。
挂视频下载后置任务（crawler._collect_bili_comments）：仅当 URL 含 BV/av 号时触发，
b23.tv 短链无法静态提取 BV → 跳过（诚实降级）。
风控/未登录（-412/-352）返回带 error 的部分结果，不硬绕。
"""
import os
import re
import time
import hashlib
import pathlib
import logging
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

# WBI mixinKey 64 位映射表（B站官方算法公开常量）
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"


def _bili_cookies() -> dict:
    """从用户自填 cookies 文件提取 bilibili 域 cookie 字典（密钥绝不落日志）"""
    jar = {}
    envs = os.environ.get("KIANA_COOKIE_FILES") or ""
    # [v2.18 P2-7] 统一走 parse_cookie_file_list（换行分隔/注释/引号原来静默失效）
    if envs:
        from .cookie_utils import parse_cookie_file_list
        srcs = parse_cookie_file_list(envs)
    else:
        srcs = []
    if not srcs:
        one = os.environ.get("KIANA_COOKIE_FILE")
        if one:
            srcs = [one]
    if not srcs:
        default = pathlib.Path(os.environ.get("LOCALAPPDATA", "")) / "KianaVnextPlus" / "cookies.txt"
        if default.exists():
            srcs = [str(default)]
    for kf in srcs:
        p = pathlib.Path(kf)
        if not p.exists():
            continue
        try:
            for line in p.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                parts = s.split("\t")
                if len(parts) >= 7 and "bilibili" in (parts[0] or "").lower():
                    jar[parts[5]] = parts[6]
        except Exception:
            continue
    return jar


def get_mixin_key(orig: str) -> str:
    """WBI mixinKey：img_key + sub_key 按 64 位映射表取前 32 位"""
    return "".join(orig[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def sign_wbi(params: dict, mixin_key: str) -> dict:
    """WBI 签名：wts 时间戳 + 键字典序排序 + 过滤 '!()*' 字符 + md5(query + mixinKey)"""
    p = dict(params)
    p["wts"] = int(time.time())
    filtered = {}
    for k in sorted(p.keys()):
        filtered[k] = "".join(ch for ch in str(p[k]) if ch not in "!'()*")
    query = urlencode(filtered)
    p["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
    return p


# [v2.17 3.6.3] WBI keys 进程内缓存（45 分钟 TTL）：同任务多次视频后置采集免重复 nav
_WBI_CACHE = {"key": None, "ts": 0}
_WBI_TTL = 45 * 60


async def _wbi_keys(session) -> tuple:
    """从 nav API 取 wbi_img 密钥对（img_key, sub_key；45 分钟缓存；失效自动重取）"""
    now = time.time()
    if _WBI_CACHE["key"] is not None and now - _WBI_CACHE["ts"] < _WBI_TTL:
        return _WBI_CACHE["key"]
    r = await session.get("https://api.bilibili.com/x/web-interface/nav",
                          headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"},
                          cookies=_bili_cookies(), timeout=10)
    d = r.json()
    wbi = (d.get("data") or {}).get("wbi_img") or {}
    img = (wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
    sub = (wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
    if not img or not sub:
        _WBI_CACHE["key"] = None  # 失效清缓存（下次重取）
        return None, None
    _WBI_CACHE["key"] = (img, sub)
    _WBI_CACHE["ts"] = now
    return img, sub


async def fetch_bili_replies(session, aid, root_comment_id, headers, cookies,
                             mixin, max_pages: int = 2) -> list:
    """[v2.16.1] 某条主评论下的二级回复（x/v2/reply/reply，parent=root；逐页）。
    双级骨架（主楼分页 → 楼中楼按需展开），接口与实现为本工程重写：
    单条主评论的子评论失败只断该条、不断整链。返回 [{"root_id", "uname", "message", "like", "ctime"}]"""
    out = []
    for pn in range(1, max_pages + 1):
        try:
            params = sign_wbi({
                "oid": aid, "type": 1, "root": root_comment_id,
                "ps": 20, "pn": pn, "plat": 1, "web_location": 1315875,
            }, mixin)
            r = await session.get("https://api.bilibili.com/x/v2/reply/reply",
                                  params=params, headers=headers, cookies=cookies)
            d = r.json()
            if d.get("code") != 0:
                break  # 风控/未登录：保留已取部分（诚实降级）
            reps = ((d.get("data") or {}).get("replies") or [])[:100]
            for rep in reps:
                out.append({
                    "root_id": root_comment_id,
                    "uname": (rep.get("member") or {}).get("uname", ""),
                    "message": (rep.get("content") or {}).get("message", ""),
                    "like": rep.get("like", 0), "ctime": rep.get("ctime", 0),
                })
            if len(reps) < 20:
                break
        except Exception:
            break
    return out


def danmaku_to_ass(xml_text: str, width: int = 1920, height: int = 1080):
    """[v2.16.1] 弹幕 XML → ASS 字幕（可选依赖 biliass——未安装返回 None 并日志，
    主流程不受影响。danmaku2ass 的用法为公开惯例，仅学模式）。"""
    try:
        import biliass  # 可选依赖：缺则降级
    except ImportError:
        logger.info("biliass 未安装——跳过弹幕 ASS 转换（弹幕 XML 数据仍保留）")
        return None
    try:
        conv = biliass.Danmaku2ASS(
            input_str=xml_text, input_type="xml", output_format="ass",
            width=width, height=height)
        return (conv.get("output_str") or "").strip() or None
    except Exception as e:
        logger.debug(f"弹幕 ASS 转换失败: {e}")
        return None


async def collect_bili_video_data(bvid: str, max_comment_pages: int = 3,
                                  max_danmaku: int = 3000,
                                  max_sub_roots: int = 3, want_ass: bool = False) -> dict:
    """采集 B站视频的评论+弹幕（view → aid/cid → reply 分页 + 弹幕 XML；
    [v2.16.1] 主评论前 max_sub_roots 条拉二级回复；want_ass 时弹幕转 ASS（可选依赖 biliass））
    成功返回 {"ok": True, "bvid", "title", "aid", "cid", "comments": [...], "danmaku": [...]}
    风控/未登录返回 {"ok": False, "error": ...}——诚实降级，不硬绕。"""
    from curl_cffi.requests import AsyncSession
    session = AsyncSession(timeout=15)
    try:
        cookies = _bili_cookies()
        hdrs = {"User-Agent": UA, "Referer": f"https://www.bilibili.com/video/{bvid}"}
        # 1) view：aid/cid/title
        r = await session.get("https://api.bilibili.com/x/web-interface/view",
                              params={"bvid": bvid}, headers=hdrs, cookies=cookies)
        d = r.json()
        if d.get("code") != 0:
            return {"ok": False, "bvid": bvid, "type": "bili_comments_danmaku",
                    "error": f"view API code={d.get('code')}"}
        data = d.get("data") or {}
        result = {"ok": False, "bvid": bvid, "type": "bili_comments_danmaku",
                  "title": data.get("title", ""), "aid": data.get("aid"),
                  "cid": data.get("cid"), "owner": (data.get("owner") or {}).get("name", "")}
        img_key, sub_key = await _wbi_keys(session)
        if not img_key:
            result["error"] = "wbi 密钥获取失败（nav API 未返回 wbi_img）"
            return result
        mixin = get_mixin_key(img_key + sub_key)
        # [v2.18 P3-3] aid 缺失（非普通视频页/解析失败）时不再把 "oid=None" 签名发出
        # （服务端 code=-400，根因不可见）——提前诚实报错
        _aid = data.get("aid")
        if not _aid:
            result["error"] = "页面未解析出 aid（非普通视频页或风控壳页），跳过评论采集"
            return result
        # 2) 评论分页
        # [FIXED & MODIFIED] v2.11 实测通道选择：x/v2/reply/wbi/main 首页带 pagination_str
        # 触发 -403；旧版 x/v2/reply + mode=2 + sort=2（热评）WBI 签名后 code=0（20 条/页）
        comments, total = [], 0
        for pn in range(1, max_comment_pages + 1):
            params = sign_wbi({
                "oid": _aid, "type": 1, "mode": 2, "sort": 2,
                "plat": 1, "web_location": 1315875, "pn": pn, "ps": 20,
            }, mixin)
            r2 = await session.get("https://api.bilibili.com/x/v2/reply",
                                   params=params, headers=hdrs, cookies=cookies)
            d2 = r2.json()
            if d2.get("code") != 0:
                # -412 未登录 / -352 风控 → 诚实降级（已有评论保留）
                result["error"] = f"reply API code={d2.get('code')}"
                break
            dd = d2.get("data") or {}
            total = (dd.get("page") or {}).get("count", total)
            # [v2.16.1] 主评论循环顺手收集 rpid —— 之后对前 max_sub_roots 条拉二级回复
            for rep in (dd.get("replies") or [])[:100]:
                comments.append({
                    "uname": (rep.get("member") or {}).get("uname", ""),
                    "message": (rep.get("content") or {}).get("message", ""),
                    "like": rep.get("like", 0), "ctime": rep.get("ctime", 0),
                    "rpid": rep.get("rpid"),
                })
            if not (dd.get("replies") or []):
                break
        # [v2.16.1] 二级回复（对该页前 max_sub_roots 条主评论；单条失败只断该条）
        _roots_todo = [c["rpid"] for c in comments if c.get("rpid")][:max_sub_roots]
        for _rid in _roots_todo:
            try:
                subs = await fetch_bili_replies(session, data.get("aid"), _rid,
                                                hdrs, cookies, mixin, max_pages=2)
                comments.extend(subs)  # 二级条目带 root_id，数据里天然可区分
            except Exception:
                continue
        result["comments"] = comments
        result["comment_count"] = len(comments)
        result["comment_total"] = total
        # 3) 弹幕 XML
        danmaku = []
        xml_raw = ""
        cid = data.get("cid")
        if cid:
            try:
                r3 = await session.get(f"https://comment.bilibili.com/{cid}.xml",
                                       headers={"User-Agent": UA,
                                                "Referer": f"https://www.bilibili.com/video/{bvid}"})
                xml = r3.text or ""
                xml_raw = xml
                danmaku = [m.group(1) for m in
                           re.finditer(r'<d p="[^"]*">([^<]*)</d>', xml)][:max_danmaku]
            except Exception as e:
                logger.debug(f"弹幕抓取失败({bvid}): {e}")
        result["danmaku"] = danmaku
        result["danmaku_count"] = len(danmaku)
        # [v2.16.1] 弹幕 ASS 副产物（可选依赖 biliass——缺失仅日志，不影响主流程）
        if want_ass and danmaku and xml_raw:
            _ass = danmaku_to_ass(xml_raw)
            if _ass:
                result["danmaku_ass"] = _ass
        result["ok"] = True
        result["_ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
        return result
    except Exception as e:
        return {"ok": False, "bvid": bvid, "type": "bili_comments_danmaku",
                "error": f"collect 异常: {e}"}
    finally:
        try:
            await session.close()
        except Exception:
            pass
