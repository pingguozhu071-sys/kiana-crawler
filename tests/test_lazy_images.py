# -*- coding: utf-8 -*-
"""图片提取必须支持**懒加载属性**（`data-src` 等）

## 真机实测发现的 bug

```python
for img in soup.find_all("img", src=True):     # ← 只找有 src 的
    data["images"].append(urljoin(base, _html_unescape(img["src"])))
```

现代站点（**尤其微信公众号**）正文图片**全是懒加载**。用真样本（3.6MB 公众号页）统计：

| | 数量 |
|---|---|
| 页面 `<img>` 总数 | 27 |
| 其中有 `src` | **5** |
| 其中有 `data-src` | **19** |
| **正文区图片** | 18 —— **全部是 `data-src`** |

后果：真机跑两篇公众号，`images` 字段只有 2 条，**其中一条还是页面 URL 本身**，
正文配图**一张都没抓到**。

修后同一份样本：**图片数 2 → 19**，全是真实的 `mmbiz.qpic.cn` 正文图。

## 顺带钉住的两件事

- `data:` 内联图（base64）不能进 `images` —— 那是占位符，不是资源；
- 同一个 URL 只记一次（懒加载属性常与 `src` 放同一张图，容易重复计数）。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kiana_vnext_plus.parser import extract_metadata      # noqa: E402

URL = "https://mp.weixin.qq.com/s/xxxx"


def _imgs(body: str):
    return extract_metadata(f"<html><body>{body}</body></html>", URL)["images"]


class TestLazyLoadedImages(unittest.TestCase):
    def test_data_src_is_picked_up(self):
        """**核心回归钉**：只有 `data-src` 的图（微信正文的形态）必须被抓到"""
        got = _imgs('<img data-src="https://mmbiz.qpic.cn/a.jpg" />')
        self.assertEqual(got, ["https://mmbiz.qpic.cn/a.jpg"])

    def test_plain_src_still_works(self):
        """不许改坏原有行为"""
        got = _imgs('<img src="https://example.com/a.png" />')
        self.assertEqual(got, ["https://example.com/a.png"])

    def test_src_wins_over_data_src(self):
        """两者都有时以 `src` 为准（它是实际加载的那个）"""
        got = _imgs('<img src="https://example.com/real.jpg" '
                    'data-src="https://example.com/lazy.jpg" />')
        self.assertEqual(got, ["https://example.com/real.jpg"])

    def test_other_lazy_attributes(self):
        for attr in ("data-original", "data-lazy-src", "data-echo", "data-actualsrc"):
            with self.subTest(attr=attr):
                got = _imgs(f'<img {attr}="https://example.com/x.jpg" />')
                self.assertEqual(got, ["https://example.com/x.jpg"])

    def test_relative_lazy_src_is_absolutized(self):
        got = _imgs('<img data-src="/img/a.jpg" />')
        self.assertEqual(got, ["https://mp.weixin.qq.com/img/a.jpg"])

    def test_data_uri_is_skipped(self):
        """base64 内联图是占位符，不该进 images（也不该把后面的真图挤掉）"""
        got = _imgs('<img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=" />'
                    '<img data-src="https://example.com/real.jpg" />')
        self.assertEqual(got, ["https://example.com/real.jpg"])

    def test_duplicates_collapsed(self):
        """同一张图有 `src` 又有 `data-src` 时**只记一次**"""
        got = _imgs('<img src="https://example.com/a.jpg" data-src="https://example.com/a.jpg" />')
        self.assertEqual(got, ["https://example.com/a.jpg"])

    def test_img_without_any_url_is_ignored(self):
        got = _imgs('<img alt="没有地址" /><img data-src="https://example.com/b.jpg" />')
        self.assertEqual(got, ["https://example.com/b.jpg"])

    def test_order_preserved(self):
        body = "".join(f'<img data-src="https://example.com/{i}.jpg" />' for i in range(5))
        got = _imgs(body)
        self.assertEqual(got, [f"https://example.com/{i}.jpg" for i in range(5)])


if __name__ == "__main__":
    unittest.main()
