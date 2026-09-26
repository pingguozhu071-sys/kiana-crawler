"""Kiana Vnext Plus — v2.16.1 阶段5 LLM 开关与接线回归

覆盖：默认关（llm_enabled=False 不构建客户端）；Key/地址不齐不构建；LLMClient 构造；
enrich_project 批量增强（幂等：已增强行跳过；回写原子且文件仍为有效 JSON）；
GUI 设置页开关存在且默认关（源码级断言——GUI 冒烟另跑）。
"""
import sys, os, asyncio, json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault('KIANA_CRYPTO_KEY', 'kv_test')

from run_crawler import _maybe_llm_enhancer
from kiana_vnext_plus import llm_enrich
from kiana_vnext_plus.llm_enrich import enrich_project


class TestMaybeLlmEnhancer:
    def test_default_off_returns_none(self):
        assert _maybe_llm_enhancer({"llm_enabled": False}) is None
        assert _maybe_llm_enhancer({}) is None  # GUI 旧配置无键 = 默认关

    def test_enabled_but_incomplete_returns_none(self):
        assert _maybe_llm_enhancer({"llm_enabled": True}) is None  # 无 key/base 不构建
        assert _maybe_llm_enhancer({"llm_enabled": True, "llm_key": "k"}) is None

    def test_enabled_complete_builds(self):
        c = _maybe_llm_enhancer({"llm_enabled": True, "llm_key": "sk-x",
                                 "llm_api_base": "https://api.deepseek.com/v1",
                                 "llm_model": "deepseek-v4-flash"})
        assert c is not None
        assert c.base_url == "https://api.deepseek.com/v1"
        assert c.model == "deepseek-v4-flash"


class TestEnrichProject:
    def test_enrich_and_idempotent(self, tmp_path, monkeypatch):
        d = tmp_path / "export" / "data" / "a.example.com"
        d.mkdir(parents=True)
        f = d / "20260829_100000.jsonl"
        f.write_text(
            json.dumps({"url": "http://a/1", "title": "t1", "text": "xx"}, ensure_ascii=False) + "\n"
            + json.dumps({"url": "http://a/2", "title": "t2", "text": "yy",
                          "_llm": {"task": "summarize", "result": "old"}}, ensure_ascii=False) + "\n",
            encoding="utf-8")

        async def fake_process(client, rows, task="summarize", budget_month=500, limit=None):
            return {"done": 1, "failed": 0, "skipped": 0, "budget_left": 499,
                    "out_rows": [{"summary": "AI 摘要甲"}, None]}

        async def flow():
            return await enrich_project(tmp_path / "export", object(),
                                        task="summarize", budget_month=500)

        monkeypatch.setattr(llm_enrich, "async_llm_process_rows", fake_process)
        stats = asyncio.run(flow())
        assert stats["done"] == 1 and stats["files"] == 1
        lines = f.read_text(encoding="utf-8").splitlines()
        recs = [json.loads(l) for l in lines]  # 回写后仍必须是有效 JSON
        assert recs[0]["_llm"]["result"]["summary"] == "AI 摘要甲"
        assert recs[1]["_llm"]["result"] == "old"  # 已增强行未被覆盖（幂等）

    def test_no_data_returns_zeros(self, tmp_path):
        stats = asyncio.run(enrich_project(tmp_path, object(), budget_month=500))
        assert stats["done"] == 0 and stats["files"] == 0

    def test_skips_rejected_and_hollow_rows(self, tmp_path, monkeypatch):
        """[v2.17 0-5a] 质量闸拒收页（rejected）与空文本行不进 LLM——烧预算纠偏"""
        d = tmp_path / "export" / "data" / "a.example.com"
        d.mkdir(parents=True)
        f = d / "20260830_100000.jsonl"
        f.write_text(
            json.dumps({"url": "http://a/1", "title": "t1", "text": "xx"}, ensure_ascii=False) + "\n"
            + json.dumps({"url": "http://a/2", "rejected": True,
                          "title": "空壳", "quality": 0.1}, ensure_ascii=False) + "\n"
            + json.dumps({"url": "http://a/3"}, ensure_ascii=False) + "\n",
            encoding="utf-8")
        captured = {}

        async def fake_process(client, rows, task="summarize", budget_month=500, limit=None):
            captured["rows"] = rows
            return {"done": 1, "failed": 0, "skipped": 0, "budget_left": 499,
                    "out_rows": [{"summary": "仅甲"}]}

        monkeypatch.setattr(llm_enrich, "async_llm_process_rows", fake_process)
        stats = asyncio.run(enrich_project(tmp_path / "export", object(), budget_month=500))
        assert len(captured["rows"]) == 1, f"只应送有效行: {captured['rows']}"
        assert captured["rows"][0]["url"] == "http://a/1"
        assert stats["skipped"] == 2

    def test_no_client_crash_safe(self, tmp_path, monkeypatch):
        async def boom(*a, **k):
            raise RuntimeError("network down")
        monkeypatch.setattr(llm_enrich, "async_llm_process_rows", boom)
        d = tmp_path / "export" / "data" / "b.com"
        d.mkdir(parents=True)
        (d / "1.jsonl").write_text(
            json.dumps({"url": "http://b/1", "title": "t", "text": "z"}, ensure_ascii=False) + "\n",
            encoding="utf-8")
        stats = asyncio.run(enrich_project(tmp_path / "export", object()))
        assert stats["failed"] >= 0  # 异常被吞，任务不受影响


class TestLlmLinkScoring:
    """2-B LLM 链接打分：批量、钳制 1-5、异常降级 3"""

    def test_scores_and_clamps(self, monkeypatch):
        from kiana_vnext_plus import llm_client

        class _Fake:
            async def chat(self, messages):
                prompt = messages[0]["content"]
                if "url-a" in prompt:
                    return "2"
                if "url-c" in prompt:
                    return "9"
                raise ValueError("boom")

        async def flow():
            return await llm_client.async_llm_score_links(
                _Fake(), ["https://x/url-a", "https://x/url-b", "https://x/url-c"])

        out = asyncio.run(flow())
        assert out["https://x/url-a"] == 2
        assert out["https://x/url-b"] == 3   # 异常 → 中性 3
        assert out["https://x/url-c"] == 5   # 9 钳制 5

    def test_budget_limits(self):
        from kiana_vnext_plus import llm_client
        calls = []

        class _Fake:
            async def chat(self, messages):
                calls.append(1)
                return "3"

        asyncio.run(llm_client.async_llm_score_links(
            _Fake(), [f"https://x/{i}" for i in range(10)], budget_links=3))
        assert len(calls) == 3


class TestGuiSwitchSource:
    def test_llm_switch_in_settings_default_off(self):
        src = (Path(__file__).parent.parent / "launcher_v9.py").read_text(encoding="utf-8")
        assert 'self.sw_llm = SwitchButton("启用（任务完成后' in src
        assert 'cfg.get("llm_enabled", False)' in src or (
            'config.get("llm_enabled", False)' in src)
        assert "_save_llm_settings" in src

    def test_engine_bridge_forwards_keys(self):
        src = (Path(__file__).parent.parent / "launcher_v8.py").read_text(encoding="utf-8")
        assert '"llm_enabled": bool(cfg.get("llm_enabled", False))' in src
        assert '"llm_key": str(cfg.get("llm_key", ""))' in src

    def test_v8_no_longer_pops_llm_keys(self):
        src = (Path(__file__).parent.parent / "launcher_v8.py").read_text(encoding="utf-8")
        assert 'd.pop("llm_enabled"' not in src  # 旧清洗已收窄，键可持久化
