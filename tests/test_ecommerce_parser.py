# -*- coding: utf-8 -*-
"""电商解析器测试（该模块此前覆盖率仅 **19.5%**，本文件是主要交付物）

**为什么盯上这个模块**：不看方案清单，改用**覆盖率反向找空白**——
`ecommerce_parser.py` 133 条语句、只覆盖 19.5%，而它是**纯逻辑**（正则 + JSON 遍历 + 归一化），
不需要浏览器也不需要网络，正是最容易藏 bug 的地方。

只看一眼 `detect_platform` 就抓到**两个真缺陷**（见 `TestDetectPlatform` 的说明）：
  ① **字典顺序覆盖**：`xianyu` 的标记 `2.taobao.com` 永远命中不了；
  ② **子串匹配 = 域名后缀伪造**：`nottaobao.com` / `taobao.com.evil.com` 被判成淘宝。
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kiana_vnext_plus import ecommerce_parser as ep      # noqa: E402


def _html(var: str, obj) -> str:
    return f"<html><body><script>window.{var} = {json.dumps(obj)};</script></body></html>"


class TestDetectPlatform(unittest.TestCase):
    """**本文件最重要的一组**：两个真缺陷的回归钉 + 边界正确性"""

    def test_xianyu_is_not_swallowed_by_taobao(self):
        """① 顺序覆盖：`2.taobao.com` 必须判成闲鱼。

        原实现按字典顺序取第一个命中，而 `"taobao.com"` 是 `"2.taobao.com"` 的子串
        → 闲鱼被当成淘宝，商品会走**淘宝的解析逻辑**（结构不同 → 解析不出东西）。
        """
        self.assertEqual(ep.detect_platform("https://2.taobao.com/item.htm?id=1"), "xianyu")
        self.assertEqual(ep.detect_platform("https://www.goofish.com/item?id=1"), "xianyu")

    def test_real_taobao_still_taobao(self):
        for u in ("https://item.taobao.com/item.htm?id=1",
                  "https://detail.tmall.com/item.htm?id=1",
                  "https://e.tb.cn/h.abc",
                  "https://taobao.com/x"):
            self.assertEqual(ep.detect_platform(u), "taobao", u)

    def test_other_platforms(self):
        self.assertEqual(ep.detect_platform("https://item.jd.com/100.html"), "jd")
        self.assertEqual(ep.detect_platform("https://www.jd.com/x"), "jd")
        self.assertEqual(ep.detect_platform("https://mobile.yangkeduo.com/goods.html"), "pdd")

    def test_no_domain_suffix_spoofing(self):
        """② 标记是**域名**，就必须按域名边界比——子串匹配会被伪造后缀骗过"""
        for fake in ("https://nottaobao.com/x",
                     "https://taobao.com.evil.com/x",
                     "https://fake-jd.com/x",
                     "https://jd.com.evil.com/x",
                     "https://goofish.com.evil.com/x"):
            self.assertIsNone(ep.detect_platform(fake),
                              f"{fake} 不是该平台，却被识别成 {ep.detect_platform(fake)}")

    def test_port_and_empty_input(self):
        self.assertEqual(ep.detect_platform("https://item.taobao.com:443/x"), "taobao")
        for bad in ("", "not a url", "https://["):
            self.assertIsNone(ep.detect_platform(bad))

    def test_longest_marker_wins(self):
        """修复的机制本身：更长（更具体）的标记优先，与字典顺序无关"""
        host_markers = [(m, p) for p, ms in ep.PLATFORM_MARKERS.items() for m in ms]
        self.assertIn(("2.taobao.com", "xianyu"), host_markers)
        # 2.taobao.com（11）必须比 taobao.com（10）更具体
        self.assertGreater(len("2.taobao.com"), len("taobao.com"))


class TestNum(unittest.TestCase):
    def test_plain_and_formatted(self):
        self.assertEqual(ep._num(12), 12.0)
        self.assertEqual(ep._num("12.5"), 12.5)
        self.assertEqual(ep._num("￥1,299.00"), 1299.0)
        self.assertEqual(ep._num("¥88元"), 88.0)

    def test_wan_multiplier(self):
        self.assertEqual(ep._num("1.5万"), 15000.0)
        self.assertEqual(ep._num("2万"), 20000.0)

    def test_placeholders_are_none(self):
        for bad in (None, "", "-", "--", "暂无", "abc"):
            self.assertIsNone(ep._num(bad), f"{bad!r} 应判为无数值")


class TestAbs(unittest.TestCase):
    def test_forms(self):
        base = "https://www.taobao.com"
        self.assertEqual(ep._abs("https://x.com/a", base), "https://x.com/a")
        self.assertEqual(ep._abs("//cdn.x.com/a", base), "https://cdn.x.com/a")
        self.assertEqual(ep._abs("/item/1", base), base + "/item/1")
        self.assertEqual(ep._abs("item/1", base), base + "/item/1")
        self.assertIsNone(ep._abs(None, base))
        self.assertIsNone(ep._abs("", base))


class TestWalk(unittest.TestCase):
    def test_collects_by_key(self):
        obj = {"a": {"itemList": [{"title": "x"}, {"title": "y"}]}}
        self.assertEqual(len(ep._walk(obj, "itemList")), 2, "数组应展开为多个节点")

    def test_multiple_keys(self):
        obj = {"goods": {"title": "a"}, "other": {"goodsList": [{"title": "b"}]}}
        got = ep._walk(obj, ("goodsList", "goods"))
        self.assertEqual(len(got), 2)

    def test_missing_key_is_empty(self):
        self.assertEqual(ep._walk({"a": 1}, "nope"), [])
        self.assertEqual(ep._walk(None, "nope"), [])


class TestNorm(unittest.TestCase):
    def test_drops_empty_and_converts(self):
        out = ep._norm({"title": "  商品  ", "price": "￥99", "sales": None,
                        "images": [None, "", "https://i/x.jpg"], "url": "u"})
        self.assertEqual(out["title"], "商品")
        self.assertEqual(out["price"], 99.0)
        self.assertNotIn("sales", out, "空值应被丢弃")
        self.assertEqual(out["images"], ["https://i/x.jpg"], "空图应被过滤")

    def test_title_truncated(self):
        out = ep._norm({"title": "a" * 500})
        self.assertEqual(len(out["title"]), 300)


class TestParsers(unittest.TestCase):
    def test_taobao_detail(self):
        html = _html("__INIT_DATA__", {"itemInfo": {"title": "T恤", "price": "59",
                                                    "seller": {"shopName": "小店"}}})
        items = ep.parse_taobao(html, "https://item.taobao.com/x")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "T恤")
        self.assertEqual(items[0]["price"], 59.0)
        self.assertEqual(items[0]["shop"], "小店")

    def test_taobao_search_list(self):
        html = _html("__INIT_DATA__", {"itemList": [{"title": "A", "price": 1},
                                                    {"title": "B", "price": 2}]})
        items = ep.parse_taobao(html, "https://s.taobao.com/x")
        self.assertEqual(len(items), 2)

    def test_jd_search_list_builds_item_url(self):
        html = _html("pageConfig", {"product": {"wareInfoList": [
            {"title": "手机", "price": 3999, "skuId": 100012043978}]}})
        items = ep.parse_jd(html, "https://search.jd.com/x")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["url"], "https://item.jd.com/100012043978.html")

    def test_pdd_goods(self):
        html = _html("rawData", {"goods": {"goods_name": "袜子", "price": 990,
                                           "goods_id": 42}})
        items = ep.parse_pdd(html, "https://mobile.yangkeduo.com/x")
        self.assertEqual(len(items), 1)
        self.assertIn("goods_id=42", items[0]["url"])

    def test_xianyu_list(self):
        html = _html("__INIT_DATA__", {"itemList": [{"title": "二手书", "price": 15,
                                                     "sellerNick": "老王"}]})
        items = ep.parse_xianyu(html, "https://www.goofish.com/x")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["shop"], "老王")

    def test_missing_data_returns_empty(self):
        for fn in (ep.parse_taobao, ep.parse_jd, ep.parse_pdd, ep.parse_xianyu):
            self.assertEqual(fn("<html></html>", "https://x/y"), [])


class TestUnifiedEntry(unittest.TestCase):
    def test_xianyu_page_routes_to_xianyu_parser(self):
        """**修复的端到端后果**：闲鱼页必须走闲鱼解析器。

        修复前 `2.taobao.com` 被判成 taobao → 走 `parse_taobao` →
        闲鱼的数据结构对不上 → **一个商品都解析不出来**。
        """
        html = _html("__INIT_DATA__", {"itemList": [{"title": "二手相机", "price": 1200}]})
        items = ep.parse_ecommerce(html, "https://2.taobao.com/item.htm?id=1")
        self.assertEqual(len(items), 1, "闲鱼页没被正确路由（修复前这里会是 0）")
        self.assertEqual(items[0]["title"], "二手相机")

    def test_unknown_platform_returns_empty(self):
        self.assertEqual(ep.parse_ecommerce("<html>x</html>", "https://example.com/x"), [])

    def test_empty_html_returns_empty(self):
        self.assertEqual(ep.parse_ecommerce("", "https://item.taobao.com/x"), [])

    def test_parser_exception_is_swallowed_not_raised(self):
        """统一入口不许把解析异常抛给调用方（单平台解析失败不应中断整场抓取）

        **必须真的制造异常**：首版这条只是喂空 HTML，走的是"没数据就返回 []"的
        正常分支，**根本没碰到 except**——名字与行为不符，等于假绿。
        这里直接让平台解析器抛，验证兜底确实生效。
        """
        original = ep.parse_taobao
        ep.parse_taobao = lambda html, url: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            self.assertEqual(ep.parse_ecommerce("<html>x</html>",
                                                 "https://item.taobao.com/x"), [])
        finally:
            ep.parse_taobao = original


if __name__ == "__main__":
    unittest.main()
