"""cookies 登录态自检（v2.10.5）

用户自填 cookies 后，GUI/CLI 可调此模块验证各站登录态是否有效（过期/失效时提前提示）。
仅暴露健康状态，绝不输出任何 cookie 值。
"""
import os
import pathlib
import logging

logger = logging.getLogger(__name__)


def _cookie_files() -> list:
    """解析用户自填 cookies 文件路径（KIANA_COOKIE_FILES 分号或换行 / KIANA_COOKIE_FILE / 默认）"""
    from .cookie_utils import parse_cookie_file_list
    envs = os.environ.get("KIANA_COOKIE_FILES") or ""
    if envs:
        return parse_cookie_file_list(envs)
    one = os.environ.get("KIANA_COOKIE_FILE")
    if one:
        return [one]
    default = pathlib.Path(os.environ.get("LOCALAPPDATA", "")) / "KianaVnextPlus" / "cookies.txt"
    return [str(default)] if default.exists() else []


def _domain_cookies(cookie_files: list, domain: str) -> dict:
    """从多个 Netscape cookie 文件收集指定域的 cookies（不返回值，只返回是否含核心字段）"""
    names = set()
    for kf in cookie_files:
        p = pathlib.Path(kf)
        if not p.exists():
            continue
        try:
            for line in p.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                parts = s.split("\t")
                if len(parts) < 7:
                    continue
                host = (parts[0] or "").lower()
                if domain in host:
                    names.add(parts[5])
        except Exception:
            pass
    return names


def check_bilibili(cookie_files: list) -> dict:
    """B站登录态自检：nav API 验证（仅返回状态，不打印 cookie）"""
    try:
        from curl_cffi import requests
        jar = {}
        for kf in cookie_files:
            p = pathlib.Path(kf)
            if not p.exists():
                continue
            for line in p.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                parts = s.split("\t")
                if len(parts) >= 7 and "bilibili" in (parts[0] or "").lower():
                    jar[parts[5]] = parts[6]
        if not jar:
            return {"ok": False, "msg": "未找到 B站 cookies", "fields": []}
        r = requests.get("https://api.bilibili.com/x/web-interface/nav",
                         cookies=jar, impersonate="chrome", timeout=10)
        d = r.json()
        data = d.get("data") or {}
        if d.get("code") == 0 and data.get("isLogin"):
            return {"ok": True, "msg": f"B站登录态有效 (会员={data.get('vipStatus')})",
                    "fields": sorted(jar.keys())}
        return {"ok": False, "msg": f"B站登录态失效 (code={d.get('code')})", "fields": []}
    except Exception as e:
        return {"ok": False, "msg": f"B站自检异常: {type(e).__name__}", "fields": []}


def check_sites() -> dict:
    """自检所有站点（用户 cookies 覆盖的；快手/小红书为域级存在性弱检查）"""
    files = _cookie_files()
    if not files:
        return {"sites": [], "msg": "未配置 cookies（默认位置不存在）"}
    result = {"sites": [], "msg": ""}
    # 断言只报域级存在性
    bili = _domain_cookies(files, "bilibili")
    douyin = _domain_cookies(files, "douyin")
    tieba = _domain_cookies(files, "tieba.baidu")
    ks = _domain_cookies(files, "kuaishou")
    xhs = _domain_cookies(files, "xiaohongshu")
    result["sites"].append({"site": "B站", "has_cookie": bool(bili),
                            "fields": sorted(bili)[:5], "health": check_bilibili(files)})
    result["sites"].append({"site": "抖音", "has_cookie": bool(douyin),
                            "fields": sorted(douyin)[:5]})
    result["sites"].append({"site": "贴吧", "has_cookie": bool(tieba),
                            "fields": sorted(tieba)[:5]})
    # [v2.17 4.4] 快手/小红书：弱检查（仅域级存在性——两站均为动态签名风控，
    # 静态 cookie 在不在≠登录态有效，诚实标注，不做在线验证断言）
    result["sites"].append({"site": "快手", "has_cookie": bool(ks),
                            "fields": sorted(ks)[:5],
                            "note": "弱检查：存在即健康态（动态签名风控，未在线验证）"})
    result["sites"].append({"site": "小红书", "has_cookie": bool(xhs),
                            "fields": sorted(xhs)[:5],
                            "note": "弱检查：存在即健康态（地理/风控强依赖，未在线验证）"})
    return result


if __name__ == "__main__":
    import json
    sys = __import__("sys")
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(check_sites(), ensure_ascii=False, indent=2))
