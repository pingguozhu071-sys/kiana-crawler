# -*- coding: utf-8 -*-
"""TLS 指纹一致性守卫：下载类会话**必须显式 impersonate**（模式六 · 同类缺陷防复发）

**为什么单独立一条**：v2.19.8 修过一个真缺陷——下载器建 curl_cffi 会话时**不传
`impersonate`**，于是落在"不模拟指纹"的默认档：既慢（实测同 CDN 同代理
4.7–8.2 MB/s vs 带指纹 12–25 MB/s），又与协议通道的指纹**不一致**。

但那次**只修了两处，漏了第三处**：`m3u8_downloader.init_session` 与
`media_downloader.init_session` 是**同一段代码的拷贝**（连注释都一样、timeout 都是 120），
却被漏掉。本文件就是防这类"**修了 A、漏了 B**"的复发：

  ① **下载类**会话（注册表 DOWNLOADERS）**必须**带 `impersonate=`——少一个就红；
  ② **反向扫描**：包内任何 `AsyncSession(timeout=大值)` 却不带 impersonate 的新会话
     都必须被这条挡住（要么修，要么显式登记进 REVIEWED 并写明理由）；
  ③ 下载类的指纹必须取自**同一个池**（`TLS_IMPERSONATE_POOL`），
     不能各写各的字面量——混用不同指纹本身就是可检测信号。

**只做静态核对**：不联网、不起浏览器。
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "kiana_vnext_plus"

# ① 下载类：媒体字节流走这几条链，**必须**显式 impersonate
DOWNLOADERS = ("media_downloader.py", "universal_downloader.py", "m3u8_downloader.py")

# ② 已复核、**本次不改**的其它会话（面向站点但非字节流下载）。
#    留下理由，避免"没登记 = 没人想过"。改不改它们属独立决策。
REVIEWED = {
    "comment_danmaku.py": "B站弹幕 API（小请求）",
    "enhancements.py": "增强解析的小请求",
    "feed_source.py": "RSS/订阅源拉取",
    "proxy_fetcher.py": "代理源列表拉取",
    "video_resolver.py": "视频地址解析请求",
    "captcha_solver_extended.py": "打码服务 API",
    "llm_client.py": "LLM 服务 API（非目标站点）",
    "protocol_engine.py": "含池化主通道（已带 impersonate）与内部健康检查",
    "cookie_health.py": "探测请求（用 requests.get(impersonate=...) 传参，非会话）",
}

_SESSION_RE = re.compile(r"AsyncSession\s*\(", re.M)


def _src(name: str) -> str:
    return (PKG / name).read_text(encoding="utf-8", errors="ignore")


def _session_calls(src: str):
    """粗切出每个 `AsyncSession(...)` 调用的文本（按括号配平）。"""
    out = []
    for m in _SESSION_RE.finditer(src):
        i = m.end() - 1
        depth, j = 0, i
        while j < len(src):
            if src[j] == "(":
                depth += 1
            elif src[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        out.append(src[m.start():j + 1])
    return out


class TestDownloadersCarryImpersonate(unittest.TestCase):
    def test_each_downloader_session_has_impersonate(self):
        """① 下载类会话必须显式 impersonate —— 这是 v2.19.8 漏修第三处的直接教训"""
        for name in DOWNLOADERS:
            calls = _session_calls(_src(name))
            self.assertTrue(calls, f"{name} 里找不到 AsyncSession 调用（登记表过期？）")
            for c in calls:
                self.assertIn("impersonate", c,
                              f"{name} 的下载会话没有 impersonate —— "
                              f"会落在无指纹默认档，且与协议通道指纹不一致")

    def test_downloaders_share_one_pool(self):
        """③ 下载类的指纹必须取自同一个池，不许各写各的字面量"""
        for name in DOWNLOADERS:
            src = _src(name)
            self.assertIn("TLS_IMPERSONATE_POOL", src,
                          f"{name} 应复用工程统一的 TLS 池（混用指纹本身可检测）")
            self.assertNotRegex(src, r'impersonate\s*=\s*["\']chrome',
                                f"{name} 写死了 impersonate 字面量——应从 TLS_IMPERSONATE_POOL 取")


class TestNoUnreviewedSession(unittest.TestCase):
    def test_every_session_site_is_registered(self):
        """② 反向扫描：出现**未登记**的会话创建点就红。

        这条就是模式六要的"**有没有第二份实现**"——答案固定在这张表里，
        而不是靠每次 grep 再人肉拼。
        """
        known = set(DOWNLOADERS) | set(REVIEWED)
        found = set()
        for p in PKG.glob("*.py"):
            if "AsyncSession" in p.read_text(encoding="utf-8", errors="ignore"):
                found.add(p.name)
        unregistered = sorted(found - known)
        self.assertEqual(unregistered, [],
                         f"发现未登记的 AsyncSession 使用点: {unregistered}——"
                         f"请判断它是否属下载类，并登记进 DOWNLOADERS 或 REVIEWED（附理由）")

    def test_registry_entries_still_exist(self):
        """登记表不许腐烂：文件没了/不再建会话就该更新表"""
        for name in REVIEWED:
            self.assertTrue((PKG / name).exists(), f"登记的文件不在了: {name}")


if __name__ == "__main__":
    unittest.main()
