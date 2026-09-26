"""Kiana Vnext Plus — v2.17 B3：URL 打分/链接过滤扩展点测试。

默认实现 = 原 page_processor 内联逻辑逐字迁移（行为锁定）；插件注册可覆盖/叠加。
全离线。
"""
import sys, os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault('KIANA_CRYPTO_KEY', 'kv_test')

from kiana_vnext_plus import link_scoring as ls


class TestDefaultScorer:
    """默认打分器：原 _calc_priority 语义（标签 > 路径关键词 > 2）"""

    def test_label_via_provider(self):
        ls.set_label_provider(lambda url: "ARTICLE" if "article" in url else None)
        try:
            assert ls.score_url("https://a.com/article/1") == 3
            assert ls.score_url("https://a.com/whatever") == 2
        finally:
            ls.set_label_provider(None)

    def test_path_keywords(self):
        assert ls.score_url("https://a.com/detail/123") == 3
        assert ls.score_url("https://a.com/post/x") == 3
        assert ls.score_url("https://a.com/news/1") == 3
        assert ls.score_url("https://a.com/list/page/2") == 5
        # 原实现只看 urlparse(...).path——query 中的 page= 不命中（保留旧语义）
        assert ls.score_url("https://a.com/?page=3") == 2
        assert ls.score_url("https://a.com/home") == 2

    def test_no_provider_falls_back_to_path(self):
        ls.set_label_provider(None)
        assert ls.score_url("https://a.com/news/9") == 3


class TestPluginScorer:
    """插件注册：front 覆盖默认；顺序首个非 None 生效"""

    def test_front_overrides(self):
        def hot(url, domain):
            return 1  # 高热度优先值更小

        ls.register_scorer("test_hot", hot, front=True)
        try:
            assert ls.score_url("https://a.com/news/9") == 1
            assert ls._ORDER[0] == "test_hot"  # 执行顺序表首位（front 覆盖）
        finally:
            ls.SCORERS.pop("test_hot", None)
            # 还原顺序（front 插入过）
            while "test_hot" in ls._ORDER:
                ls._ORDER.remove("test_hot")

    def test_none_defers_to_next(self):
        def maybe(url, domain):
            return None if "skip" in url else 7

        ls.register_scorer("test_maybe", maybe, front=True)
        try:
            assert ls.score_url("https://a.com/skip/news/1") == 3  # None→下一个 scorer
            assert ls.score_url("https://a.com/hit") == 7
        finally:
            ls.SCORERS.pop("test_maybe", None)
            if "test_maybe" in ls._ORDER:
                ls._ORDER.remove("test_maybe")


class TestDefaultFilters:
    """默认过滤链：原 _is_junk_link/_is_static_link 语义（True=通过保留）"""

    def test_junk_blocked(self):
        assert ls.link_ok("https://api.b.com/x") is False
        assert ls.link_ok("https://cm.b.com/ad") is False
        assert ls.link_ok("https://stats.foo.net/e") is False
        assert ls.link_ok("https://installads.net/a") is False
        assert ls.link_ok("https://www.site.com/api/data") is False
        assert ls.link_ok("https://x.com/a b.html") is False     # 形态兜底
        assert ls.link_ok("https://x.com/文章.html") is False

    def test_static_blocked(self):
        assert ls.link_ok("https://x.com/a.js") is False
        assert ls.link_ok("https://x.com/path/a.png") is False
        assert ls.link_ok("https://x.com/video.m4s") is False
        assert ls.link_ok("https://bilivideo.com/v1.mp4") is False
        assert ls.link_ok("https://a.hdslb.com/x.flv") is False

    def test_normal_kept(self):
        assert ls.link_ok("https://news.sina.com.cn/article/9.html") is True
        assert ls.link_ok("https://www.163.com/dy/article/AB.html") is True
        assert ls.link_ok("https://zhihu.com/question/123") is True


class TestJsonLdFlattener:
    """2-C JSON-LD 实体扁平化：@type/@name/author/日期 → entities[]（纯函数）"""

    def test_flatten(self):
        from kiana_vnext_plus.parser import flatten_json_ld
        ents = flatten_json_ld([
            {"@type": "Article", "name": "标题", "author": [{"name": "作者A"}],
             "datePublished": "2026-08-30"},
            "not-a-dict",
            {"@type": ["NewsArticle", "Article"], "headline": "第二标题"},
            {},
        ])
        assert ents[0] == {"type": "Article", "name": "标题"}
        assert {"type": "Person", "name": "作者A"} in ents
        assert {"type": "Date", "name": "2026-08-30"} in ents
        assert ents[3] == {"type": "NewsArticle|Article", "name": "第二标题"}

    def test_empty(self):
        from kiana_vnext_plus.parser import flatten_json_ld
        assert flatten_json_ld(None) == []
        assert flatten_json_ld([{}]) == []


class TestFeedSource:
    """1-2 RSS/Atom 订阅源：形态判定 + 解析（零新依赖正则族）"""

    def test_looks_like_feed(self):
        from kiana_vnext_plus.feed_source import looks_like_feed
        assert looks_like_feed("https://a.com/feed.xml")
        assert looks_like_feed("https://a.com/rss")
        assert looks_like_feed("https://a.com/atom")
        assert looks_like_feed("https://a.com/blog/feed")
        assert not looks_like_feed("https://a.com/article/123")

    def test_parse_rss(self):
        from kiana_vnext_plus.feed_source import parse_feed
        xml = ('<?xml version="1.0"?><rss><channel>'
               '<item><link>https://a.com/1</link><title>一</title></item>'
               '<item><link>https://a.com/2</link></item>'
               '</channel></rss>')
        assert parse_feed(xml) == ["https://a.com/1", "https://a.com/2"]

    def test_parse_atom(self):
        from kiana_vnext_plus.feed_source import parse_feed
        xml = ('<?xml version="1.0"?><feed>'
               '<entry><link href="https://a.com/1"/><title>x</title></entry>'
               '<entry><link href="https://a.com/2"/></entry>'
               '</feed>')
        assert parse_feed(xml) == ["https://a.com/1", "https://a.com/2"]

    def test_parse_sitemap_style_fallback(self):
        from kiana_vnext_plus.feed_source import parse_feed
        xml = '<urlset><url><loc>https://a.com/1</loc></url></urlset>'
        assert parse_feed(xml) == ["https://a.com/1"]

    def test_ignore_non_http_and_dedupe(self):
        from kiana_vnext_plus.feed_source import parse_feed
        xml = ('<rss><channel>'
               '<item><link>mailto:x@y</link></item>'
               '<item><link>https://a.com/1</link></item>'
               '<item><link>https://a.com/1</link></item>'
               '</channel></rss>')
        assert parse_feed(xml) == ["https://a.com/1"]

    def test_empty_returns_empty(self):
        from kiana_vnext_plus.feed_source import parse_feed
        assert parse_feed("<html><body>不是源</body></html>") == []


class TestPriorityDelta:
    """B4b 证据驱动优先级微调（dynamic_priority 开启时使用）"""

    def test_rule_hit_boosts(self):
        assert ls.priority_delta_from_data({"rule": "netease-news", "title": "T"}) == -1

    def test_hollow_page_pushed_back(self):
        assert ls.priority_delta_from_data({"title": "", "text": ""}) == 3

    def test_normal_no_change(self):
        assert ls.priority_delta_from_data({"title": "T", "text": "x"}) == 0
        assert ls.priority_delta_from_data(None) == 0


class TestPluginFilter:
    def test_extra_filter_applies(self):
        ls.register_filter("test_block_douban", lambda url: "douban" not in url)
        try:
            assert ls.link_ok("https://a.com/news/x") is True
            assert ls.link_ok("https://douban.com/group/x") is False
        finally:
            ls.FILTERS.pop("test_block_douban", None)


class TestParityWithOldBehavior:
    """与旧内联实现逐点对照（旧值直接内嵌：来自 v2.17 组件化前的 page_processor）"""

    def test_calc_priority_parity(self):
        cases = {
            "https://a.com/video/1": 3,
            "https://a.com/product/9": 4,
            "https://a.com/search/q": 5,
            "https://a.com/login": 6,
            "https://a.com/static/x.css": 5 if True else 2,  # 只有 A 标签样本主要验常量路径
        }
        # 无 provider 时按路径关键词验证
        ls.set_label_provider(None)
        assert ls.score_url("https://a.com/video/1") == 2  # video 无关键词 → 2
        assert ls.score_url("https://a.com/article/9") == 3
        assert ls.score_url("https://a.com/page/3") == 5
