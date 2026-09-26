"""Kiana Vnext Plus — v2.16 阶段L：LLM 兼容层回归（纯函数 + 多格式）"""
import sys, os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from kiana_vnext_plus.llm_client import normalize_base, build_body, _headers_for, \
    parse_response, extract_error, _status_hint, LLMClient


class TestNormalizeBase:
    def test_strip_and_tail_slash(self):
        assert normalize_base("  https://api.x.com/v1/  ") == "https://api.x.com/v1"
        assert normalize_base("https://api.x.com") == "https://api.x.com"

    def test_no_auto_add_v1(self):
        assert normalize_base("https://api.x.com") == "https://api.x.com"  # 绝不自动加

    def test_strip_query_frag(self):
        assert normalize_base("https://x.com/v1?token=abc#frag") == "https://x.com/v1"

    def test_empty(self):
        assert normalize_base("") == ""
        assert normalize_base(None) == ""


class TestBuildBody:
    def test_openai(self):
        b = build_body("openai", "m1", [{"role": "user", "content": "hi"}],
                       {"max_tokens": 100, "temperature": 0.5})
        assert b["model"] == "m1" and b["stream"] is False
        assert b["max_tokens"] == 100 and b["temperature"] == 0.5

    def test_openai_params_whitelist(self):
        # None 不发送
        b = build_body("openai", "m", [{"role": "user", "content": "x"}],
                       {"max_tokens": None, "temperature": None, "top_p": None})
        assert "max_tokens" not in b and "temperature" not in b and "top_p" not in b

    def test_anthropic_system_top_level(self):
        b = build_body("anthropic", "claude", [{"role": "user", "content": "hi"}],
                       {"max_tokens": 512}, system="你是助手")
        assert b["system"] == "你是助手"          # 坑3：system 顶层
        assert b["max_tokens"] == 512

    def test_anthropic_default_max_tokens(self):
        b = build_body("anthropic", "c", [{"role": "user", "content": "hi"}], {})
        assert b["max_tokens"] == 512             # 必传

    def test_gemini(self):
        b = build_body("gemini", "g", [{"role": "user", "content": "hi"}],
                       {"max_tokens": 100, "temperature": 0.3})
        assert b["contents"][0]["parts"][0]["text"] == "hi"
        assert b["generationConfig"]["maxOutputTokens"] == 100

    def test_ollama_max_tokens_to_num_predict(self):
        b = build_body("ollama", "llama", [{"role": "user", "content": "hi"}],
                       {"max_tokens": 200})
        assert b["options"]["num_predict"] == 200  # 坑5
        assert "max_tokens" not in b


class TestHeaders:
    def test_anthropic_version(self):
        h = _headers_for("anthropic", "k")
        assert h["anthropic-version"] == "2023-06-01"   # 坑2 必带
        assert h["x-api-key"] == "k"

    def test_gemini_header_not_query(self):
        h = _headers_for("gemini", "k")
        assert h["x-goog-api-key"] == "k"               # 坑4 认证头（不进 URL）
        assert "key" not in h


class TestParseResponse:
    def test_openai_content(self):
        assert parse_response("openai", {"choices": [{"message": {"content": "你好"}}]}) == "你好"

    def test_openai_empty_choices(self):
        assert parse_response("openai", {"choices": []}) == ""

    def test_openai_legacy_text(self):
        assert parse_response("openai", {"choices": [{"text": "旧"}]}) == "旧"   # 坑6

    def test_anthropic_content_list(self):
        assert parse_response("anthropic", {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}) == "ab"

    def test_gemini_parts_filter_non_text(self):
        data = {"candidates": [{"content": {"parts": [{"text": "hi"}, {"functionCall": {"name": "f"}}, {"text": "!"}]}}]}
        assert parse_response("gemini", data) == "hi!"

    def test_ollama(self):
        assert parse_response("ollama", {"message": {"content": "yo"}}) == "yo"


class TestExtractError:
    def test_error_message(self):
        assert extract_error({"error": {"message": "bad"}}) == "bad"

    def test_detail(self):
        assert extract_error({"error": {"detail": "no"}}) == "no"

    def test_top_level_msg(self):
        assert extract_error({"msg": "top"}) == "top"

    def test_non_dict(self):
        assert extract_error("garbage") == "响应非 JSON"


class TestStatusHint:
    def test_401(self):
        assert "API Key 无效" in _status_hint(401, "")
    def test_timeout(self):
        # [v2.18 P3-4] code 0 与 408 语义拆分：0=连接失败（含 DNS/拒绝连接），408=纯超时
        assert "连接失败" in _status_hint(0, "")
        assert "连接超时" in _status_hint(408, "")
    def test_404(self):
        assert "缺 /v1" in _status_hint(404, "")
    def test_429(self):
        assert "限频" in _status_hint(429, "")
    def test_422(self):
        assert "temperature" in _status_hint(422, "")
    def test_200_empty(self):
        assert "无内容" in _status_hint(200, "")
    def test_non_json(self):
        assert "响应非 JSON" in _status_hint(0, "响应非 JSON")


class TestClientBasic:
    def test_init_defaults(self):
        c = LLMClient({})
        assert c.format == "openai"
        assert c.timeout_s == 15
    def test_bad_format_fallback(self):
        c = LLMClient({"format": "xxx"})
        assert c.format == "openai"
    def test_chat_url(self):
        c = LLMClient({"base_url": "https://x.com/v1", "format": "openai"})
        assert c._chat_url("m") == "https://x.com/v1/chat/completions"
        c2 = LLMClient({"base_url": "https://x.com/v1", "format": "ollama"})
        assert c2._chat_url("m") == "https://x.com/v1/api/chat"
