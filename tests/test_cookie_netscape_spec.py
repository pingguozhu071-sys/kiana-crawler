# -*- coding: utf-8 -*-
"""cookies.txt 的**读写规格** —— 用 yt-dlp 自己的解析器当裁判

## 为什么值得单独钉

`cookies.txt` 是**事实标准**，但有两套格式在流通：

| 格式 | 列数 | 谁用 |
|---|---|---|
| **Netscape / yt-dlp** | **7 列** | yt-dlp、本工程 |
| curl 的 cookies.txt | 9 列（多两列） | curl |

**写成 9 列喂 yt-dlp → `LoadError: invalid length`**，整份 cookie 直接不生效。
（调研资料里就有一处把 7 写成 9，我们差点跟着写错。）

## 还有一条更隐蔽的

HttpOnly 的 cookie 在域名前有 `#HttpOnly_` 前缀 ——
**而 B站的 `SESSDATA` 恰好是 HttpOnly**。
很多解析器（含本工程此前的 **9 处**）把"以 `#` 开头"一律当注释跳过
⇒ **登录态被静默丢掉**，表现为"文件看着是好的、服务端却说未登录"。

本文件用**真实解析器**（yt-dlp 的 `YoutubeDLCookieJar`）当裁判，
而不是自己写个断言糊弄自己。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SESSDATA = {"name": "SESSDATA", "value": "AAA%2CBBB", "domain": ".bilibili.com",
            "path": "/", "secure": True, "http_only": True, "expires": 1806300464}
BILI_JCT = {"name": "bili_jct", "value": "deadbeef", "domain": ".bilibili.com",
            "path": "/", "secure": False, "http_only": False, "expires": 0}


def _ytdlp_read(path):
    """用 **yt-dlp 真正的解析器**读，返回 {name: value}。

    yt-dlp 不在环境里就跳过 —— 但本工程把它当下载层依赖，正常情况下一定有。
    """
    from yt_dlp.cookies import YoutubeDLCookieJar
    jar = YoutubeDLCookieJar(path)
    jar.load(ignore_discard=True, ignore_expires=True)
    return {c.name: c.value for c in jar}


class TestWriteNetscape(unittest.TestCase):
    def _write(self, cookies):
        from kiana_vnext_plus.cookie_utils import write_netscape_cookies
        d = tempfile.mkdtemp()
        p = os.path.join(d, "ck.txt")
        write_netscape_cookies(cookies, p)
        return p

    def test_ytdlp_can_read_what_we_write(self):
        """**决定性**：写出来的文件，yt-dlp 必须能完整读回（含 HttpOnly 的 SESSDATA）"""
        p = self._write([SESSDATA, BILI_JCT])
        try:
            got = _ytdlp_read(p)
        except ImportError:
            self.skipTest("环境里没有 yt-dlp")
        self.assertIn("SESSDATA", got,
                      "HttpOnly 的 SESSDATA 丢了 —— 8 成是进了注释行")
        self.assertEqual(got["SESSDATA"], "AAA%2CBBB")
        self.assertEqual(got["bili_jct"], "deadbeef")

    def test_httponly_prefix_present(self):
        """HttpOnly 的 cookie **必须**带 `#HttpOnly_` 前缀（curl 等严格解析器靠它）"""
        p = self._write([SESSDATA])
        raw = Path(p).read_bytes().decode("utf-8")
        self.assertIn("#HttpOnly_.bilibili.com", raw)

    def test_non_httponly_has_no_prefix(self):
        p = self._write([BILI_JCT])
        raw = Path(p).read_bytes().decode("utf-8")
        self.assertNotIn("#HttpOnly_", raw)

    def test_seven_columns_not_nine(self):
        """**7 列** —— 写成 9 列 yt-dlp 会 `LoadError: invalid length`"""
        p = self._write([BILI_JCT])
        data_lines = [ln for ln in Path(p).read_text(encoding="utf-8").splitlines()
                      if ln and not ln.startswith("#")]
        self.assertTrue(data_lines, "没写出数据行")
        for ln in data_lines:
            self.assertEqual(len(ln.split("\t")), 7,
                             f"列数不是 7：{ln!r}")

    def test_crlf_line_endings(self):
        """行尾必须 CRLF（与 yt-dlp 官方 fixture 一致）"""
        p = self._write([BILI_JCT])
        raw = Path(p).read_bytes()
        self.assertIn(b"\r\n", raw)
        # 不许有落单的 LF
        self.assertNotRegex(raw, rb"(?<!\r)\n")

    def test_header_first_line(self):
        p = self._write([BILI_JCT])
        first = Path(p).read_text(encoding="utf-8").splitlines()[0]
        self.assertEqual(first, "# Netscape HTTP Cookie File")

    def test_tab_in_value_does_not_break_columns(self):
        """值里混进 TAB 会**撕裂列结构** —— 必须剔除，且剔除后仍能被读回"""
        bad = dict(BILI_JCT, value="has\ttab\nand newline")
        p = self._write([bad])
        try:
            got = _ytdlp_read(p)
        except ImportError:
            self.skipTest("环境里没有 yt-dlp")
        self.assertIn("bili_jct", got, "含 TAB 的值把整行搞成畸形行了")
        self.assertNotIn("\t", got["bili_jct"])

    def test_empty_name_skipped(self):
        """无名 cookie 写出去也是畸形行 —— 直接跳过"""
        from kiana_vnext_plus.cookie_utils import write_netscape_cookies
        txt = write_netscape_cookies([{"name": "", "value": "x", "domain": ".a.com"}])
        data = [ln for ln in txt.splitlines() if ln and not ln.startswith("#")]
        self.assertEqual(data, [])


class TestReadNetscape(unittest.TestCase):
    """读的那一侧（含本工程此前 9 处都漏掉的 HttpOnly）"""

    SAMPLE = (
        "# Netscape HTTP Cookie File\n"
        "#HttpOnly_.bilibili.com\tTRUE\t/\tTRUE\t1806300464\tSESSDATA\tAAAA\n"
        ".bilibili.com\tTRUE\t/\tFALSE\t1817533624\tbuvid3\tXYZ\n"
    )

    def test_httponly_is_data_not_comment(self):
        from kiana_vnext_plus.cookie_utils import netscape_to_header
        h = netscape_to_header(self.SAMPLE)
        self.assertIn("SESSDATA=AAAA", h, "HttpOnly 行被当注释吃掉了")
        self.assertIn("buvid3=XYZ", h)

    def test_roundtrip_through_ytdlp(self):
        """**我们写 → yt-dlp 读 → 我们读**，三方对得上"""
        from kiana_vnext_plus.cookie_utils import (write_netscape_cookies,
                                                   netscape_to_header)
        d = tempfile.mkdtemp()
        p = os.path.join(d, "ck.txt")
        write_netscape_cookies([SESSDATA, BILI_JCT], p)
        try:
            via_ytdlp = _ytdlp_read(p)
        except ImportError:
            self.skipTest("环境里没有 yt-dlp")
        via_us = {}
        for kv in netscape_to_header(Path(p).read_text(encoding="utf-8")).split("; "):
            if "=" in kv:
                k, _, v = kv.partition("=")
                via_us[k] = v
        self.assertEqual(via_ytdlp, via_us, "两条读取路径结果不一致")


class TestPlaywrightShapeIntegration(unittest.TestCase):
    """**Playwright/patchright 的 `ctx.cookies()` 直接喂进来，不许丢东西**

    这一组是两个**实测踩出来的**集成坑。它们都属于"换个入口，同一个 bug 又回来了"。

    ## 坑 1：键名风格不同

    实测真起 patchright 读回，`ctx.cookies()` 的第一条是：
    ```
    {'domain': '.example.com', 'expires': 1800000000, 'httpOnly': True,
     'name': 'T', 'path': '/', 'sameSite': 'Lax', 'secure': True, 'value': 'V'}
    ```
    **`httpOnly` 是驼峰**，而本工程内部用的是下划线 `http_only`。
    只认后者 ⇒ **HttpOnly 标记丢失** ⇒ `SESSDATA` 没有 `#HttpOnly_` 前缀
    ⇒ **严格解析器整行丢掉** —— 正是本工程刚花几轮修掉的那个 bug。

    ## 坑 2：session cookie 的 expires 是 **-1**

    Playwright 对 session cookie 返回 `expires = -1`（不是 0、不是空）。
    而 yt-dlp 对 expires 的校验是 `[0-9]+(\\.[0-9]+)?` —— **负数不合法**，
    它会 `WARNING: skipping cookie file entry due to invalid expires at -1`
    **把整行跳过**。实测复现过。
    """

    # **真实形状**（起 patchright 读回后打印出来的，不是编的）
    PW_COOKIES = [
        {"domain": ".bilibili.com", "expires": 1806300464, "httpOnly": True,
         "name": "SESSDATA", "path": "/", "sameSite": "Lax", "secure": True,
         "value": "AAA%2CBBB"},
        {"domain": ".bilibili.com", "expires": -1, "httpOnly": False,
         "name": "buvid3", "path": "/", "sameSite": "Lax", "secure": False,
         "value": "XYZ"},
    ]

    def _write(self):
        from kiana_vnext_plus.cookie_utils import write_netscape_cookies
        d = tempfile.mkdtemp()
        p = os.path.join(d, "ck.txt")
        write_netscape_cookies(self.PW_COOKIES, p)
        return p

    def test_camel_case_httponly_is_recognized(self):
        """坑 1：`httpOnly`（驼峰）必须被认出来"""
        raw = Path(self._write()).read_text(encoding="utf-8")
        self.assertIn("#HttpOnly_.bilibili.com", raw,
                      "驼峰 `httpOnly` 没被认 —— SESSDATA 会丢前缀")

    def test_session_cookie_expires_minus_one_normalized(self):
        """坑 2：`expires = -1` 必须归 0（写 -1 出去 yt-dlp 会跳过整行）"""
        raw = Path(self._write()).read_text(encoding="utf-8")
        for ln in raw.splitlines():
            if ln and not ln.startswith("#"):
                self.assertRegex(ln.split("\t")[4], r"^[0-9]+$",
                                 f"expires 不是非负整数（yt-dlp 会跳过该行）: {ln!r}")

    def test_ytdlp_reads_both_cookies_back(self):
        """**总验收**：两条都要能被 yt-dlp 读回（一条 HttpOnly、一条 session）"""
        try:
            got = _ytdlp_read(self._write())
        except ImportError:
            self.skipTest("环境里没有 yt-dlp")
        self.assertIn("SESSDATA", got, "HttpOnly 那条丢了")
        self.assertIn("buvid3", got, "session 那条丢了（expires=-1？）")
        self.assertEqual(len(got), 2, f"该有 2 条，实际 {got}")

    def test_both_key_spellings_work(self):
        """**容忍两套写法** —— 内部用下划线、外部用驼峰，两边都要能用"""
        from kiana_vnext_plus.cookie_utils import write_netscape_cookies
        for key in ("http_only", "httpOnly"):
            raw = write_netscape_cookies(
                [{"name": "S", "value": "V", "domain": ".a.com", key: True}])
            self.assertIn("#HttpOnly_", raw, f"键名 {key!r} 没被认")

    def test_non_dict_entries_skipped(self):
        """混进非 dict 的条目不许把整次导出搞崩"""
        from kiana_vnext_plus.cookie_utils import write_netscape_cookies
        raw = write_netscape_cookies(
            [None, "junk", {"name": "S", "value": "V", "domain": ".a.com"}])
        self.assertIn("S\tV", raw.replace("#HttpOnly_.a.com\tTRUE\t/\tFALSE\t0\t", ""))


if __name__ == "__main__":
    unittest.main()
