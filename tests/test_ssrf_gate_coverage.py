# -*- coding: utf-8 -*-
"""SSRF 闸覆盖守卫（模式六 · 把"审计过一次"变成"不会悄悄回退"）

**审计结论（2026-09-30）**：SSRF 闸覆盖面**是扎实的**——
包内 55 处受限调用（`safe_get` / `safe_urlopen` / `is_private_url` / `_blocked`），
`03-终检与闭环方案` 第三组点名的路径逐条有着落：

| 路径 | 落点 |
|---|---|
| 主通道 | `protocol_engine.safe_urlopen` |
| 下载通道 | `media_downloader` / `universal_downloader`（含每一跳） |
| m3u8（分片/密钥/子表） | `m3u8_downloader._blocked` 逐处 |
| 订阅源 | `feed_source.safe_get` |
| robots / sitemap | `robots_policy.safe_urlopen` / `enhancements.safe_get` |
| 代理源 | `proxy_fetcher`（先判后取） |
| 图片 / 页内链接 | `page_processor.is_private_url` |
| 浏览器层 | `page_processor._fetch_with_identity` + `solver_service.gate` |

另有 **5 处裸 `session.get/post`**（不经闸），逐一核实**都是硬编码主机**、
用户输入只进**查询参数**，改不了主机 → **无 SSRF 面**：

`comment_danmaku`(×5) / `captcha_solver_extended`(×2) / `video_resolver`(×2) /
`protocol_engine` 的 DoH 端点。

**本文件把这 5 处钉住**：它们的 URL 必须仍是**字面量主机**。
一旦有人把它改成配置项或由页面内容拼出来（`f"https://{host}/…"`），这里立刻红——
那种改动**必须**改走过闸的路径，而不是悄悄绕过。

> 注：`llm_client` 的端点是 **LLM 服务**，可能就在本机（Ollama 等），
> **刻意**不过私网闸——它不是抓取目标，属登记在案的例外。
"""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "kiana_vnext_plus"

# 只借"清单与方法"——这里是本工程自己的落点登记
GATED_MODULES = (
    "protocol_engine.py", "media_downloader.py", "universal_downloader.py",
    "m3u8_downloader.py", "feed_source.py", "robots_policy.py",
    "enhancements.py", "proxy_fetcher.py", "page_processor.py",
    "solver_service.py", "url_utils.py", "fingerprint_probe.py",
    "douyin_resolver.py", "kuaishou_resolver.py", "music163_resolver.py",
    "xhs_resolver.py", "captcha_solver_extended.py",
)

# 裸 session 调用点：文件 → 允许出现的次数上限（多一处就要重新核实）
RAW_SESSION_SITES = {
    "comment_danmaku.py": 5,
    "captcha_solver_extended.py": 2,
    "video_resolver.py": 2,
    "protocol_engine.py": 1,
}

# 刻意不过闸的例外（不是抓取目标）
EXEMPT = {
    "llm_client.py": "LLM 服务端点，可能就在本机（Ollama 等），刻意不过私网闸",
}

_SESS_NAMES = ("session", "sess", "_session", "self.session")


def _src(name: str) -> str:
    return (PKG / name).read_text(encoding="utf-8", errors="ignore")


def _raw_calls(src: str):
    """AST 找**真实的**裸 session 调用 → `[(lineno, 第一个实参文本)]`。

    **必须用 AST 而不是正则**：`enhancements.py` / `universal_downloader.py` 里有
    `# [v2.19.7 安全·扫描发现] 原为裸 session.get(url)` 这类**注释**，
    文本匹配会把它们当成真调用（首版正则就误报了这两处）。
    """
    out = []
    try:
        tree = ast.parse(src)
    except Exception:
        return out
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr in ("get", "post", "request") and n.args):
            if ast.unparse(n.func.value) in _SESS_NAMES:
                out.append((n.lineno, ast.unparse(n.args[0])))
    return out


def _assignments(src: str) -> dict:
    """`名字 = 表达式` 的字面表（用于把 `api_url` 这类变量还原成 URL 文本）"""
    out = {}
    try:
        tree = ast.parse(src)
    except Exception:
        return out
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and len(n.targets) == 1 \
                and isinstance(n.targets[0], ast.Name):
            out.setdefault(n.targets[0].id, ast.unparse(n.value))
    return out


def _host_is_literal(url_expr: str) -> bool:
    """`://` 到下一个 `/` 之间不得出现 `{`（即主机不是拼出来的）"""
    if "://" not in url_expr:
        return False
    host = url_expr.split("://", 1)[1]
    for stop in ("/", "'", '"', "\\n"):
        host = host.split(stop)[0]
    return "{" not in host and bool(host)


class TestGatedModules(unittest.TestCase):
    def test_each_gated_module_still_uses_the_gate(self):
        """登记在册的落点必须真的还在过闸——登记表不许腐烂"""
        for name in GATED_MODULES:
            p = PKG / name
            self.assertTrue(p.exists(), f"登记的文件不在了: {name}")
            src = _src(name)
            self.assertTrue(
                any(k in src for k in ("safe_get(", "safe_urlopen(", "is_private_url(", "_blocked(")),
                f"{name} 不再引用任何闸函数——若是有意移除，请更新本文件")


class TestRawSessionSites(unittest.TestCase):
    def test_raw_sites_match_registry(self):
        """**反向扫描**：新出现一处裸 session 调用即红（必须先核实有无 SSRF 面）"""
        found = {}
        for p in sorted(PKG.glob("*.py")):
            n = len(_raw_calls(_src(p.name)))
            if n:
                found[p.name] = n
        stray = {k: v for k, v in found.items()
                 if k not in RAW_SESSION_SITES and k not in EXEMPT}
        self.assertEqual(stray, {},
                         f"发现未核实的裸 session 调用: {stray}——请核实主机是否可控，"
                         f"再登记进 RAW_SESSION_SITES（或改走过闸路径）")
        grew = {k: (RAW_SESSION_SITES[k], v) for k, v in found.items()
                if k in RAW_SESSION_SITES and v > RAW_SESSION_SITES[k]}
        self.assertEqual(grew, {}, f"裸调用变多了（登记值→实际）: {grew}")

    def test_every_raw_site_uses_a_literal_host(self):
        """**本文件最重要的一条**：这 5 处的主机必须是字面量。

        一旦有人把 URL 改成配置项、或从页面内容拼出来（`f"https://{host}/…"`），
        这里立刻红——那种改动**必须**改走过闸的路径，而不是悄悄绕过闸。
        """
        for name in RAW_SESSION_SITES:
            src = _src(name)
            assigns = _assignments(src)
            calls = _raw_calls(src)
            self.assertEqual(len(calls), RAW_SESSION_SITES[name],
                             f"{name} 的裸调用数与登记值不一致")
            for lineno, arg in calls:
                resolved = arg if "://" in arg else assigns.get(arg)
                self.assertIsNotNone(
                    resolved, f"{name}:{lineno} 解析不出 URL（{arg!r}）")
                self.assertIn("://", resolved,
                              f"{name}:{lineno} URL 不是字面量（{arg!r} → {resolved!r}）"
                              f"——动态 URL 必须走 safe_get/safe_urlopen")
                self.assertTrue(
                    _host_is_literal(resolved),
                    f"{name}:{lineno} 主机是拼出来的（{resolved!r}）"
                    f"——**必须**改走过闸路径")


if __name__ == "__main__":
    unittest.main()
