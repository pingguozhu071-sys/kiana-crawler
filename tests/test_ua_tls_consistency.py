# -*- coding: utf-8 -*-
"""UA ↔ TLS 同源守卫（本工程红线："改了下载/网络必须核对指纹一致性"）

**发现的真问题**：主通道早在 v2.10.5 就把 UA 主版本绑定到 TLS 伪装版本
（`fingerprint_consistency` 里那条注释称之为"**反爬第一自杀行为**"：
"UA 说 Chrome/138 实际 TLS=chrome120，JA3/UA 交叉比对自曝"）。
**但下载链没跟上**——它们的会话 `impersonate=TLS_IMPERSONATE_POOL[0]`（= **chrome136**），
而发出的 UA 是：

  · `media_downloader`           硬编码 `Chrome/120.0`  → 版本失配 16 个主版本
  · `universal_downloader` 会话头  **截断串**（`…AppleWebKit/537.36` 后直接结束）
  · `universal_downloader` 下载头  硬编码 `Chrome/120.0`

而下载链恰恰是**防盗链最紧**的一条链。

**本文件锁两件事**：
  ① **同源不变式**：凡设置了 `impersonate=` 的会话，其 UA 必须由
     `user_agent_for(同一个 impersonate)` 生成——不许再硬编码版本号；
  ② **截断 UA 只许减少不许新增**：`…AppleWebKit/537.36` 后没有版本号的 UA
     **没有任何真实浏览器会发**。存量登记在案（可能是为兼容性刻意保留的最小 UA，
     改动需真机验证），但**新增一处就是红**。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kiana_vnext_plus.fingerprint_consistency import (      # noqa: E402
    TLS_IMPERSONATE_POOL, user_agent_for,
)

PKG = ROOT / "kiana_vnext_plus"

# ① 设了 impersonate **且发 UA** 的会话，其 UA 必须同源。
#    （`m3u8_downloader` 不发 UA —— 用会话自带的，故不在列）
SESSION_UA_MODULES = ("media_downloader.py", "universal_downloader.py")

# ② 已知「截断 UA」的存量（**只许减不许增**）——AST 实测值，非正则估算。
#    为什么本次不改：`universal_downloader` 里记着"部分站点对 Chrome UA + 无 cookie
#    请求 403（w3.org 实测仅 Referer/无头反而 200）——多组合尝试兜底"，
#    即这些最小 UA **可能是为兼容性刻意保留的**，改动需真机跑一遍下载回归才能确认。
KNOWN_TRUNCATED = {
    "universal_downloader.py": 2,     # yt-dlp opts 的 http_headers / 图片下载
    "video_resolver.py": 1,
    "captcha_solver_extended.py": 1,  # 打码服务 API，非目标站点
}

def _src(name: str) -> str:
    return (PKG / name).read_text(encoding="utf-8", errors="ignore")


def _ua_literals(name: str):
    """**AST** 取出 UA 字面量。

    不用正则在源码文本上扫：`fingerprint_consistency.py` 的注释里就写着
    "截断串（`…AppleWebKit/537.36` 后直接结束）"，文本匹配会把**注释**当成真 UA
    （本文件首版就误报了它，和第 21 轮同一个坑）。AST 只看真实字符串常量。
    """
    import ast
    out = []
    try:
        tree = ast.parse(_src(name))
    except Exception:
        return out
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) \
                and n.value.startswith("Mozilla/5.0"):
            out.append(n.value)
    return out


def _is_truncated(ua: str) -> bool:
    """截断 = 以 `AppleWebKit/537.36` 收尾且**没有任何后续版本令牌**"""
    return ua.strip().endswith("AppleWebKit/537.36")


class TestHelperContract(unittest.TestCase):
    def test_version_is_bound(self):
        """UA 主版本必须跟着 impersonate 走"""
        for imp in ("chrome136", "chrome120", "chrome116"):
            ver = imp.replace("chrome", "")
            self.assertIn(f"Chrome/{ver}.", user_agent_for(imp))
            self.assertIn("AppleWebKit/537.36 (KHTML, like Gecko)", user_agent_for(imp))

    def test_never_emits_a_version_less_ua(self):
        """**关键**：解析不出主版本时回退，绝不发"没有版本号"的 UA"""
        for bad in ("", "bogus", "chrome", "firefox120"):
            ua = user_agent_for(bad)
            self.assertRegex(ua, r"Chrome/\d{3}\.",
                             f"{bad!r} 生成了没有版本号的 UA: {ua}")

    def test_default_matches_pool_head(self):
        """回退值必须与下载链实际用的 TLS 版本一致（否则等于换了个失配）"""
        self.assertIn(f"Chrome/{TLS_IMPERSONATE_POOL[0].replace('chrome', '')}.",
                      user_agent_for(""))
        self.assertEqual(TLS_IMPERSONATE_POOL[0], "chrome136")


class TestSameOriginInvariant(unittest.TestCase):
    def test_session_modules_derive_ua_from_helper(self):
        """① 设了 impersonate 的会话，UA 必须由 user_agent_for 生成"""
        for name in SESSION_UA_MODULES:
            self.assertIn("user_agent_for", _src(name),
                          f"{name} 有 impersonate 会话却不使用 user_agent_for —— "
                          f"硬编码 UA 会与 TLS 版本失配")

    def test_no_hardcoded_chrome_version_in_session_modules(self):
        """不许再出现硬编码的 `Chrome/<版本>` UA 字面量（helper 之外的）。

        同样走 **AST**（`_ua_literals`）——注释里提到 `Chrome/120.0` 不算数，
        只有真实字符串常量才算。
        """
        for name in SESSION_UA_MODULES:
            for ua in _ua_literals(name):
                self.assertNotRegex(
                    ua, r"Chrome/\d+\.",
                    f"{name} 仍有硬编码版本号的 UA 字面量: {ua[:70]}…——"
                    f"应改为 user_agent_for(TLS_IMPERSONATE_POOL[0])")


class TestTruncatedUaBudget(unittest.TestCase):
    def test_no_new_truncated_ua(self):
        """② 截断 UA 只许减少不许新增（**反向扫描**，AST 判定）"""
        found = {}
        for p in sorted(PKG.glob("*.py")):
            n = sum(1 for ua in _ua_literals(p.name) if _is_truncated(ua))
            if n:
                found[p.name] = n
        grew = {k: (KNOWN_TRUNCATED.get(k, 0), v) for k, v in found.items()
                if v > KNOWN_TRUNCATED.get(k, 0)}
        self.assertEqual(grew, {},
                         f"新增了截断 UA（登记值→实际）: {grew}——"
                         f"没有任何真实浏览器会发'…AppleWebKit/537.36'后直接结束的 UA")
        shrunk = {k: (KNOWN_TRUNCATED[k], found.get(k, 0))
                  for k in KNOWN_TRUNCATED if found.get(k, 0) < KNOWN_TRUNCATED[k]}
        if shrunk:
            self.fail(f"截断 UA 已减少 {shrunk}——请把 KNOWN_TRUNCATED 调小（只许降）")

    def test_fixed_sites_are_clean(self):
        """本轮修过的地方必须已经干净（会话头与下载头都不得再有截断 UA）"""
        for name in SESSION_UA_MODULES:
            left = [ua for ua in _ua_literals(name) if _is_truncated(ua)]
            self.assertEqual(len(left), KNOWN_TRUNCATED.get(name, 0),
                             f"{name} 的截断 UA 数与登记不符: {left}")


if __name__ == "__main__":
    unittest.main()
