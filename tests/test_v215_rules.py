"""Kiana Vnext Plus — v2.15 阶段 4：YAML 站点规则层回归"""
import sys, os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

HTML = """
<html><head><title>示例页</title></head><body>
<article>
  <h1 class="t">商品标题甲</h1>
  <div class="price">￥1,299.00</div>
  <div id="content">正文内容段落。</div>
  <img src="https://cdn.example.com/a.jpg">
  <a class="next" href="/list?page=2">下一页</a>
  <div class="item"><h3><a href="/p/1">条目一</a></h3><span class="s">摘要一</span></div>
  <div class="item"><h3><a href="/p/2">条目二</a></h3><span class="s">摘要二</span></div>
</article>
</body></html>
"""


def _write_rule(tmp_path, text):
    rules_dir = tmp_path / "rules" / "sites"
    rules_dir.mkdir(parents=True, exist_ok=True)
    (rules_dir / "test.yaml").write_text(text, encoding="utf-8")
    return rules_dir


RULE = """
name: example.com
match: [example.com]
item_type: product
list:
  selector: "div.item"
  fields:
    title: {sel: "h3 a", text: true}
    url:   {sel: "h3 a", attr: href}
detail:
  fields:
    title: {sel: "h1.t", text: true}
    price: {sel: "div.price", text: true, transform: parse_price}
media:
  images: {sel: "img", attr: src}
pagination:
  next: {sel: "a.next", attr: href}
"""


class TestSiteRules:
    def _load_with_dir(self, tmp_path, text=RULE, monkeypatch=None):
        from kiana_vnext_plus import site_rules as sr
        # [v2.16.1] 测试隔离：用 pytest monkeypatch（自动恢复）——原直接赋值模块级
        # _loader 单例且不恢复，全量跑时污染后续测试（find_rule 指向 tmp 规则目录）
        monkeypatch.setattr(sr, "_loader", sr.RuleLoader(_write_rule(tmp_path, text)))
        return sr

    def test_match_and_extract(self, tmp_path, monkeypatch):
        sr = self._load_with_dir(tmp_path, monkeypatch=monkeypatch)
        r = sr.apply_rule(HTML, "https://www.example.com/product/1")
        assert r is not None and r["name"] == "example.com"
        assert r["fields"]["title"] == "商品标题甲"
        assert r["fields"]["price"] == 1299.0        # transform: parse_price
        assert "https://cdn.example.com/a.jpg" in r["images"]
        assert r["next_url"] == "https://www.example.com/list?page=2"  # urljoin
        assert len(r["list_items"]) == 2
        assert r["list_items"][0]["title"] == "条目一"

    def test_no_rule_returns_none(self, tmp_path, monkeypatch):
        sr = self._load_with_dir(tmp_path, monkeypatch=monkeypatch)
        assert sr.apply_rule(HTML, "https://other-site.com/") is None

    def test_broken_yaml_skipped(self, tmp_path, monkeypatch):
        """坏规则文件仅跳过不拖垮（RuleLoader 容错）"""
        sr = self._load_with_dir(tmp_path, monkeypatch=monkeypatch)
        d = tmp_path / "rules" / "sites"
        (d / "bad.yaml").write_text(":::not yaml [[[", encoding="utf-8")
        sr._loader.invalidate()
        rules = sr._loader.load()
        assert len(rules) == 1  # 坏文件被跳过，好文件仍在

    def test_template_generation(self):
        from kiana_vnext_plus.site_rules import new_rule_template
        t = new_rule_template("mysite.org")
        assert "match: [mysite.org]" in t
        assert "{sel:" in t  # 字段格式保留
