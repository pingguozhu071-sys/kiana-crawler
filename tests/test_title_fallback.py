# -*- coding: utf-8 -*-
"""标题提取的**多级回退**测试

## 真机实测发现的 bug

`extract_metadata` 原先**只有 `<title>` 一个标题来源**：

```python
if soup.title and soup.title.string:
    data["title"] = soup.title.string.strip()
```

但**微信公众号文章的 `<title>` 是空的** —— 标题由 JS 写进 `<h1 id="activity-name">`
（那个 `<h1>` 抓下来也是空的），真正的标题在 **`og:title`** 里。

真机后果：抓两篇公众号文章，**正文都拿到了（773 / 546 字），标题却是空**，
导出文件名成了 `untitled.md`。

修后真机复跑，导出名变成真标题：
```
20261001_235553_Vtuber时雨羽衣联动童装被炎上，终止合作后网友并不买账.md
```

## 回退顺序的取舍

**`<title>` 仍排第一** —— 这是**刻意的保守选择**：原来的行为不能被改坏
（很多站点的 `<title>` 才是对的，且带站点后缀的形态各处不同）。
新增的 `og:title` / `twitter:title` / `<h1>` **只在 `<title>` 为空时才生效**。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kiana_vnext_plus.parser import extract_metadata      # noqa: E402

URL = "https://mp.weixin.qq.com/s/xxxx"


class TestTitleFallback(unittest.TestCase):
    def test_og_title_used_when_title_tag_is_empty(self):
        """**核心回归钉**：微信形态——`<title>` 空、标题在 og:title"""
        html = ('<html><head><title></title>'
                '<meta property="og:title" content="茅洲河流域河湖底泥与通沟污泥处理处置的实践及经验">'
                '</head><body><h1 id="activity-name"></h1><p>正文</p></body></html>')
        self.assertEqual(extract_metadata(html, URL)["title"],
                         "茅洲河流域河湖底泥与通沟污泥处理处置的实践及经验")

    def test_og_title_used_when_title_tag_missing(self):
        html = ('<html><head>'
                '<meta property="og:title" content="没有 title 标签的页面">'
                '</head><body>x</body></html>')
        self.assertEqual(extract_metadata(html, URL)["title"], "没有 title 标签的页面")

    def test_title_tag_still_wins(self):
        """**不许改坏原有行为**：两者都有时，仍以 `<title>` 为准"""
        html = ('<html><head><title>站点标题</title>'
                '<meta property="og:title" content="og 标题">'
                '</head><body>x</body></html>')
        self.assertEqual(extract_metadata(html, URL)["title"], "站点标题")

    def test_twitter_title_fallback(self):
        html = ('<html><head><title></title>'
                '<meta name="twitter:title" content="推特标题">'
                '</head><body>x</body></html>')
        self.assertEqual(extract_metadata(html, URL)["title"], "推特标题")

    def test_h1_fallback(self):
        """最后兜底：`<h1>`（很多文章页正文里就有）"""
        html = ('<html><head><title></title></head>'
                '<body><h1>正文里的大标题</h1><p>x</p></body></html>')
        self.assertEqual(extract_metadata(html, URL)["title"], "正文里的大标题")

    def test_no_title_anywhere_stays_empty(self):
        """**真的没有标题**时保持空 —— 不许瞎编（例如拿 description 当标题）"""
        html = ('<html><head><meta name="description" content="这只是摘要，不是标题">'
                '</head><body><p>正文</p></body></html>')
        self.assertEqual(extract_metadata(html, URL)["title"], "")

    def test_whitespace_only_title_falls_through(self):
        """只含空白/换行的 `<title>` 等于没有 —— 要能穿透到下一级"""
        html = ('<html><head><title>   \n  </title>'
                '<meta property="og:title" content="真正的标题">'
                '</head><body>x</body></html>')
        self.assertEqual(extract_metadata(html, URL)["title"], "真正的标题")

    def test_empty_og_title_does_not_blank_a_real_title(self):
        """og:title 为空时**不许**把已取到的 `<title>` 清掉"""
        html = ('<html><head><title>好标题</title>'
                '<meta property="og:title" content="">'
                '</head><body>x</body></html>')
        self.assertEqual(extract_metadata(html, URL)["title"], "好标题")


if __name__ == "__main__":
    unittest.main()
