# -*- coding: utf-8 -*-
"""指纹「地理/平台」一致性测试 —— 本机会话**不许伪装成外国**

## 真机实测发现的 bug（两个，同一个根源：不该伪装的地方伪装了）

**① `_pick_geo` 子串匹配 → 本机会话恒判为德国**

```python
for code in TIMEZONE_MAP:
    if code.lower() in proxy_url.lower():   # ← 子串匹配
        return code
```

哨兵串 `"default_local_session"` 里同时含有
**`DE`**(**de**fault) / **`AU`**(def**au**lt) / **`CA`**(lo**ca**l)，
循环取第一个 → **DE**。注释写着"默认美国"，与实际完全不符。

后果：**每一次不走代理的爬取**都伪装成
`de-DE` 语言 + `Europe/Berlin` 时区 + 德语 Linux UA ——
而真实出口是**中国大陆 IP**。风控交叉比对 IP 地理与浏览器语言/时区，**当场识破**。

**② `platform` 与 geo 无关地随机选** —— `default_local_session` 这个种子
偏偏抽到 `Linux x86_64`，而真实机器是 Windows。

## 为什么这比"不伪装"更糟

真实浏览器不会自相矛盾。**一个中国 IP 上的德语 Linux 浏览器**，
比一个诚实的 `zh-CN / Asia/Shanghai / Win32` 显眼得多。

## 真机证据（8 条 B 站短链回归）

| 指标 | 修复前 | 修复后 |
|---|---|---|
| 验证码挑战 | **7** | **0** |
| GeeTest 求解失败 | **14** | **0** |
| 耗时 | 47s | 16s |
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kiana_vnext_plus.fingerprint_consistency import (      # noqa: E402
    LOCAL_GEO, LOCAL_SESSION, TIMEZONE_MAP, _pick_geo, compute_fingerprint_from_ip,
)


class TestGeoSentinelIsNotSubstringMatched(unittest.TestCase):
    """**核心回归钉**：本机会话的哨兵串不许被子串匹配成某个国家"""

    def test_local_session_is_local_geo(self):
        self.assertEqual(_pick_geo(LOCAL_SESSION), LOCAL_GEO,
                         "本机会话必须返回本机地理，不能是 DE/AU/CA 之类")

    def test_empty_proxy_is_local_geo(self):
        self.assertEqual(_pick_geo(""), LOCAL_GEO)
        self.assertEqual(_pick_geo(None), LOCAL_GEO)

    def test_sentinel_substrings_do_not_win(self):
        """把当年的三个"幸运子串"逐个钉死——任何一个胜出都是 bug"""
        for bad in ("DE", "AU", "CA"):
            self.assertNotEqual(_pick_geo(LOCAL_SESSION), bad,
                                f"{bad} 又靠子串匹配赢了（哨兵串里有它）")

    def test_all_geo_codes_tried_as_sentinel(self):
        """**任意** geo 代码都不该在哨兵串上被误匹配"""
        wrong = [c for c in TIMEZONE_MAP if _pick_geo(LOCAL_SESSION) == c and c != LOCAL_GEO]
        self.assertEqual(wrong, [], f"哨兵串被误判成: {wrong}")

    def test_real_proxy_hints_still_work(self):
        """修子串匹配**不能**把正常功能修坏：代理 URL 里的国家提示仍要生效"""
        self.assertEqual(_pick_geo("http://de.proxy:8080"), "DE")
        self.assertEqual(_pick_geo("http://user:pass@jp.proxy:8080"), "JP")
        self.assertEqual(_pick_geo("socks5://hk.example:1080"), "HK")

    def test_no_hint_falls_back_to_us(self):
        """代理但无国家提示 → 沿用原注释承诺的默认美国"""
        self.assertEqual(_pick_geo("http://1.2.3.4:8080"), "US")


class TestLocalFingerprintIsSelfConsistent(unittest.TestCase):
    """本机指纹必须**自洽**：语言/时区/平台三者不许互相矛盾"""

    def setUp(self):
        self.fp = compute_fingerprint_from_ip(LOCAL_SESSION)

    def test_geo_and_language_match(self):
        expected_tz, _off, expected_lang = TIMEZONE_MAP[LOCAL_GEO]
        self.assertEqual(self.fp["geo_code"], LOCAL_GEO)
        self.assertEqual(self.fp["language"], expected_lang)
        self.assertEqual(self.fp["timezone"], expected_tz)

    def test_languages_list_starts_with_language(self):
        self.assertEqual(self.fp["languages"][0], self.fp["language"])

    def test_platform_matches_real_machine(self):
        """平台必须跟**真实系统**一致——伪装成别的 OS 只会引入矛盾"""
        import sys as _sys
        want = ("Win32" if _sys.platform.startswith("win")
                else "MacIntel" if _sys.platform == "darwin" else "Linux x86_64")
        self.assertEqual(self.fp["platform"], want)

    def test_ua_family_matches_platform(self):
        """UA 里的 OS 段必须与 platform 对得上（这是最容易被交叉比对的矛盾点）"""
        ua, plat = self.fp["user_agent"], self.fp["platform"]
        if plat == "Win32":
            self.assertIn("Windows NT", ua)
            self.assertNotIn("Linux", ua)
            self.assertNotIn("Macintosh", ua)
        elif plat == "MacIntel":
            self.assertIn("Macintosh", ua)
            self.assertNotIn("Linux", ua)
        else:
            self.assertIn("Linux", ua)

    def test_no_headless_marker_in_ua(self):
        """UA 里不许出现 HeadlessChrome —— 那是白送的自动化标签"""
        self.assertNotIn("Headless", self.fp["user_agent"])

    def test_proxy_session_still_disguises(self):
        """代理场景**不该**被这次修复影响——那里伪装是对的（出口 IP 确实在外）"""
        de = compute_fingerprint_from_ip("http://de.proxy:8080")
        self.assertEqual(de["geo_code"], "DE")
        self.assertEqual(de["language"], "de-DE")


if __name__ == "__main__":
    unittest.main()
