# -*- coding: utf-8 -*-
"""M2-e 新增的两个站点规则：**在真实样本上实测**，不是"看着像"

## 这两个规则是怎么来的

2026-10-03 按 `03-终检与闭环方案` 的 M2-e 要求（真实站点规则 10 → 12），
用 `tools/capture_sample.py` 抓了两个真实页面：

| 站点 | 样本 URL | 样本文件 |
|---|---|---|
| 什么值得买 | `https://post.smzdm.com/p/apqmlo27/` | `smzdm_post_sample_sample.html` |
| 微信公众号 | `https://mp.weixin.qq.com/s/LOIkTrBj-H5xeXxWEJfnsw` | `wechat_article_sample.html` |

**选择器全部是在样本上逐个核对出来的**（工程纪律：禁止"瞎选选择器"），
反例也记录在规则 YAML 的注释里。本文件把这些结论钉住。

## 两条值得单独钉住的实测发现

1. **smzdm 的正文类名是 `.m-contant`** —— 站方把 `content` 拼成了 `contant`。
   谁"顺手改成 `.m-content`"，规则就一条都匹配不到。
2. **公众号正文图片 10 张全是 `data-src`** —— `src` 是占位图。
   工程 `parser.py` 里早就记录了这条真机发现（"正文区 18 张图全部是 data-src"），
   本样本把它复现并固定下来。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ASSETS = ROOT / "tests" / "assets"


def _apply(rule_name, sample, url):
    from kiana_vnext_plus.site_rules import apply_rule
    html = (ASSETS / sample).read_text(encoding="utf-8", errors="ignore")
    return apply_rule(html, url, url)


class TestSmzdmRule(unittest.TestCase):
    """什么值得买社区文章页"""

    URL = "https://post.smzdm.com/p/apqmlo27/"
    SAMPLE = "smzdm_post_sample_sample.html"

    def test_rule_hits_and_extracts(self):
        r = _apply("smzdm", self.SAMPLE, self.URL)
        self.assertIsNotNone(r, "规则没命中")
        self.assertEqual(r["name"], "smzdm-post")
        f = r.get("fields") or {}
        # 标题要从样本里那个真标题来（不是空、不是占位）
        self.assertIn("充电", f.get("title", ""), f"标题不对: {f.get('title')!r}")
        self.assertTrue(f.get("author", "").strip(), "作者为空")
        self.assertGreater(len(f.get("content", "")), 300,
                           f"正文太短({len(f.get('content',''))} 字)，选择器可能错了")

    def test_content_selector_keeps_the_site_typo(self):
        """**`.m-contant` 是站方自己的拼写**，不许被"修正"成 `.m-content`"""
        y = (ROOT / "rules" / "sites" / "smzdm-post.yaml").read_text(encoding="utf-8")
        self.assertIn(".m-contant", y, "站方拼写被改掉了 —— 那样一条都匹配不到")
        # 顺带确认没被同时写成正确拼写（那会让人以为两个都行）
        self.assertNotIn('sel: ".m-content"', y)


class TestWechatRule(unittest.TestCase):
    """微信公众号文章"""

    URL = "https://mp.weixin.qq.com/s/LOIkTrBj-H5xeXxWEJfnsw"
    SAMPLE = "wechat_article_sample.html"

    def test_rule_hits_and_extracts(self):
        r = _apply("wechat", self.SAMPLE, self.URL)
        self.assertIsNotNone(r, "规则没命中")
        self.assertEqual(r["name"], "wechat-article")
        f = r.get("fields") or {}
        self.assertIn("素养", f.get("title", ""), f"标题不对: {f.get('title')!r}")
        self.assertGreater(len(f.get("content", "")), 1000,
                           f"正文太短({len(f.get('content',''))} 字)")

    def test_title_is_not_read_from_tag_title(self):
        """公众号的 `<title>` 基本是空的 —— 标题必须来自 `h1#activity-name`

        工程 `parser.py` 记录过真机现象：
        "真机抓两篇公众号文章，**正文都拿到了，标题却是空**"。
        """
        import re
        html = (ASSETS / self.SAMPLE).read_text(encoding="utf-8", errors="ignore")
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S)
        tag_title = (m.group(1).strip() if m else "")
        h1 = re.search(r'<h1[^>]*id="activity-name"[^>]*>(.*?)</h1>', html, re.S)
        self.assertTrue(h1, "样本里没有 h1#activity-name —— 样本坏了？")
        h1_text = re.sub(r"<[^>]+>", "", h1.group(1)).strip()
        # **实测结论**：`<title>` 与真标题**不是一回事**（公众号 title 基本为空/站点名）。
        # 这条断言把"为什么必须优先 h1#activity-name"变成一个可失败的判据：
        # 若哪天两者变相等，说明站点行为变了，**该回来重新取证**，而不是继续沿用旧结论。
        self.assertNotEqual(tag_title, h1_text,
                            "`<title>` 竟然等于真标题了 —— 公众号行为可能已变，需重新取证")
        y = (ROOT / "rules" / "sites" / "wechat-article.yaml").read_text(encoding="utf-8")
        self.assertIn("h1#activity-name", y, "规则没优先用 h1#activity-name")
        self.assertLess(y.index("h1#activity-name"), y.index("h1\", text"),
                        "`<title>`/h1 兜底不该排在 h1#activity-name 前面")

    def test_lazy_loaded_images_use_data_src(self):
        """**正文图片全是懒加载** —— 只读 `src` 一张都拿不到

        工程 `parser.py` 记录："现代站点（尤其微信公众号）正文图片全是懒加载……
        正文区 18 张图**全部**是 `data-src` → 一张都没抓到"。
        """
        from bs4 import BeautifulSoup
        html = (ASSETS / self.SAMPLE).read_text(encoding="utf-8", errors="ignore")
        soup = BeautifulSoup(html, "html.parser")
        imgs = soup.select("div#js_content img")
        self.assertGreater(len(imgs), 0, "正文里没有图 —— 样本坏了？")
        lazy = [i for i in imgs if i.get("data-src")]
        self.assertEqual(len(lazy), len(imgs),
                         f"并非全部懒加载({len(lazy)}/{len(imgs)}) —— 结论要更新")
        # 规则必须读 data-src
        y = (ROOT / "rules" / "sites" / "wechat-article.yaml").read_text(encoding="utf-8")
        self.assertIn("attr: data-src", y, "规则没读 data-src —— 会一张图都拿不到")


class TestSampleHygiene(unittest.TestCase):
    """两个新样本必须是**干净且不臃肿**的（它们会进公开仓库）"""

    def test_no_obvious_personal_data(self):
        """不得含合法邮箱 / 合法 IP / 身份证号

        ⚠️ 11 位数字**不作为判据** —— 实测两个样本里的"手机号"命中
        （`track_no` 埋点号、`data-ratio` 小数、图片文件名数字）**全是误报**。
        这里只查那些**误报率极低**的模式。
        """
        import re
        for s in ("smzdm_post_sample_sample.html", "wechat_article_sample.html"):
            t = (ASSETS / s).read_text(encoding="utf-8", errors="ignore")
            self.assertEqual(re.findall(r"[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}", t), [],
                             f"{s} 含邮箱样字符串")
            # 合法 IPv4（八位组 0-255）—— 不合法的一串数字是误报
            ip = re.findall(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}"
                            r"(?:25[0-5]|2[0-4]\d|1?\d?\d)\b", t)
            self.assertEqual(ip, [], f"{s} 含合法 IP: {ip[:3]}")
            self.assertEqual(re.findall(r"\b\d{17}[\dXx]\b", t), [],
                             f"{s} 含身份证号样字符串")

    def test_samples_are_not_bloated(self):
        """样本 >2MB 会拖慢测试（工具自己的告警）"""
        for s in ("smzdm_post_sample_sample.html", "wechat_article_sample.html"):
            size = (ASSETS / s).stat().st_size
            self.assertLess(size, 2 * 1024 * 1024,
                            f"{s} 有 {size/1024/1024:.1f}MB，超过 2MB 上限")


if __name__ == "__main__":
    unittest.main()
