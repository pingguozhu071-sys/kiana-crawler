"""cookies 登录态自检（v2.10.5）

用户自填 cookies 后，GUI/CLI 可调此模块验证各站登录态是否有效（过期/失效时提前提示）。
仅暴露健康状态，绝不输出任何 cookie 值。
"""
import pathlib
import logging

logger = logging.getLogger(__name__)

# ── 探针三态：**只有 expired 才允许判死** ──────────────────────────────────
# 原实现把"网络超时/连不上"与"登录态失效"**都**返回 ok=False，二者不可区分。
# 一旦拿它去做冷却，一次线路抖动就会把健康身份误杀——这正是工程反复踩的
# "看起来有防护、实际误杀"。故显式区分第三态 unknown，并提供判定助手。
PROBE_OK = "ok"            # 登录态有效
PROBE_EXPIRED = "expired"  # **明确**未登录（服务端明确回复）→ 可以冷却/退役
PROBE_UNKNOWN = "unknown"  # 网络异常/超时/风控码/解析失败 → **不得判死**，只记录


def probe_should_punish(status: str) -> bool:
    """探针三态 → 是否允许判死（冷却/退役）。**只有明确 expired 才允许**。

    把这条做成函数而不是散在各调用点的 `if`，是为了让"超时不判死"这条纪律
    可测试、并防止后来者顺手写成 `if not ok: 冷却`。
    """
    return status == PROBE_EXPIRED


def _cookie_files() -> list:
    """cookies 文件路径列表 —— **委托给唯一入口 `cookie_utils.cookie_source_files()`**。

    [v6 修复·**同一能力两份实现**] 这段逻辑原来在**本函数**与
    `universal_downloader._cookie_sources()` 里**逐字重复**了一遍
    （都是 KIANA_COOKIE_FILES → KIANA_COOKIE_FILE → 默认 cookies.txt）。
    两处各写一份的后果是**每次改动都得改两遍**，漏一处就静默分叉。
    现统一到 `cookie_utils.cookie_source_files()`，本函数只转发。

    顺带：唯一入口里**新增了持久化配置档的自动发现**
    （`profiles/*/cookies.txt`），本函数因此也自动受益 ——
    用户用 `tools/cookie_login.py` 登录过之后，这里就能看见了。
    """
    from .cookie_utils import cookie_source_files
    return cookie_source_files()


def _domain_cookies(cookie_files: list, domain: str) -> dict:
    """从多个 Netscape cookie 文件收集指定域的 cookies（不返回值，只返回是否含核心字段）"""
    # [v6] 本处过滤是"域名字符串包含"（比域后缀匹配宽），保留原样
    from .cookie_utils import parse_netscape_cookies
    names = set()
    for kf in cookie_files:
        p = pathlib.Path(kf)
        if not p.exists():
            continue
        try:
            text = p.read_text(encoding="utf-8-sig", errors="ignore")
            for c in parse_netscape_cookies(text):
                if domain in str(c["domain"] or "").lower():
                    names.add(str(c["name"]))
        except Exception:
            pass
    return names


def check_bilibili(cookie_files: list) -> dict:
    """B站登录态自检：nav API 验证（仅返回状态，不打印 cookie）

    返回 `{"ok": bool, "status": "ok"|"expired"|"unknown", "msg": str, "fields": [...]}`。

    **三态语义（调用方必须遵守，用 `probe_should_punish` 判定）**：
      ok       → 登录态有效
      expired  → **明确**未登录（服务端明确回复）→ 可以冷却
      unknown  → 网络异常 / 超时 / 风控码 / 解析失败 → **不得判死**

    `ok` 字段为兼容既有 GUI 显示而保留（unknown 与 expired 均为 False），
    **不要**用 `ok` 单独决策是否惩罚身份。
    """
    try:
        from curl_cffi import requests
        from .cookie_utils import parse_netscape_cookies
        jar = {}
        for kf in cookie_files:
            p = pathlib.Path(kf)
            if not p.exists():
                continue
            text = p.read_text(encoding="utf-8-sig", errors="ignore")
            for c in parse_netscape_cookies(text):
                if "bilibili" in str(c["domain"] or "").lower():
                    jar[str(c["name"])] = str(c["value"])
        if not jar:
            return {"ok": False, "status": PROBE_UNKNOWN,
                    "msg": "未找到 B站 cookies（无内容可判定）", "fields": []}

        try:
            r = requests.get("https://api.bilibili.com/x/web-interface/nav",
                             cookies=jar, impersonate="chrome", timeout=10)
        except Exception as e:
            # 线路/超时问题：**不是**登录态结论
            return {"ok": False, "status": PROBE_UNKNOWN,
                    "msg": f"B站自检未能完成（网络/超时，登录态未判定）: {type(e).__name__}",
                    "fields": []}

        try:
            d = r.json()
        except Exception as e:
            return {"ok": False, "status": PROBE_UNKNOWN,
                    "msg": f"B站自检响应无法解析（未判定）: {type(e).__name__}", "fields": []}

        code = d.get("code")
        data = d.get("data") or {}
        if code == 0 and data.get("isLogin"):
            return {"ok": True, "status": PROBE_OK,
                    "msg": f"B站登录态有效 (会员={data.get('vipStatus')})",
                    "fields": sorted(jar.keys())}
        if code == 0 or code == -101:
            # 服务端明确回复"未登录"——这是唯一可以判死的情形
            return {"ok": False, "status": PROBE_EXPIRED,
                    "msg": f"B站登录态失效（服务端明确未登录，code={code}）", "fields": []}
        # 其余状态码（如 -412 风控）不构成登录态结论 → 不判死
        return {"ok": False, "status": PROBE_UNKNOWN,
                "msg": f"B站自检未判定（服务端返回 code={code}，可能是风控）", "fields": []}
    except Exception as e:
        return {"ok": False, "status": PROBE_UNKNOWN,
                "msg": f"B站自检异常（未判定）: {type(e).__name__}", "fields": []}


# 真正做了**在线登录态验证**的站点 → 探针函数。
# 不在此表的站点只有"域级存在性弱检查"（见 check_sites 的 note），
# **不得**把它们当成登录态结论，更不得据此冷却身份。
PROBE_REGISTRY = {"B站": check_bilibili}


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
