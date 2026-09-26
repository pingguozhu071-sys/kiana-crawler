"""Kiana Vnext Plus — v2.17 B4a：ItemPipeline 链化测试。

默认链 = 原内联步骤逐字拆分（去重/清洗/校验语义锁定）；set_chain 整链编排。
全离线。
"""
import sys, os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault('KIANA_CRYPTO_KEY', 'kv_test')

from kiana_vnext_plus.p1_enhancements import ItemPipeline


class TestDefaultChainParity:
    """默认链行为 = 旧内联实现（去重/清洗+结构保留/校验）"""

    def test_clean_and_static_keys(self):
        p = ItemPipeline()
        out = p.process({"title": "  A  ", "text": "  b  ", "desc": "",
                         "canonical": "", "images": []}, "http://x/1")
        assert out["title"] == "A" and out["text"] == "b"
        assert "desc" not in out                    # 非结构空值剔除
        assert "canonical" in out and "images" in out  # 结构字段保留（旧语义）

    def test_dup_dropped(self):
        p = ItemPipeline()
        assert p.process({"title": "A"}, "http://x/1") is not None
        assert p.process({"title": "A"}, "http://x/1") is None
        assert p.stats["dropped_dup"] == 1

    def test_invalid_dropped(self):
        p = ItemPipeline()
        assert p.process({"desc": "只有描述"}, "http://x/2") is None
        assert p.stats["dropped_invalid"] == 1

    def test_custom_processor_old_api(self):
        p = ItemPipeline()
        p.add_processor(lambda d: d | {"extra": 1})
        out = p.process({"title": "T", "text": "x"}, "http://x/9")
        assert out["extra"] == 1
        # None 返回保留原文（旧语义）
        p.add_processor(lambda d: None)
        assert p.process({"title": "T", "text": "x"}, "http://x/10")["title"] == "T"


class TestSetChain:
    """整链编排：替换默认步骤"""

    def test_custom_chain_replaces_default(self):
        p = ItemPipeline()

        def keep_title(item, url):
            return {"title": item.get("title", "")}

        p.set_chain([keep_title])
        out = p.process({"title": "T", "text": "x", "desc": "y"}, "http://x/1")
        assert out == {"title": "T"}

    def test_step_none_drops(self):
        p = ItemPipeline()

        def deny(item, url):
            return None

        p.set_chain([deny])
        assert p.process({"title": "T"}, "http://x/1") is None

    def test_customs_still_apply_after_chain(self):
        p = ItemPipeline()
        p.set_chain([lambda item, url: dict(item)])
        p.add_processor(lambda d: d | {"z": 9})
        assert p.process({"title": "T"}, "http://x/2")["z"] == 9
