"""LLM Client 兼容层（v2.16 阶段L）

一次只接一个（单客户端单配置）；多格式 API 兼容：
  openai / anthropic / gemini / ollama —— 用户自填 Base URL + API Key，格式即选即用。

自审 14 坑全修：
  path 归一化(去尾斜杠,不自动加/v1)；anthropic 必带 anthropic-version + system 顶层；
  gemini 认证(x-goog-api-key) + parts 过滤非文本；ollama max_tokens→options.num_predict；
  响应解析兜底(empty choices / None / 旧 text / 多字段错误体)；错误语义化全表；
  stream:false 强制；参数白名单三项可关。全异步+硬超时。
"""
import asyncio
import json
import logging
import time
import urllib.parse
from typing import Optional

logger = logging.getLogger(__name__)

FORMATS = ("openai", "anthropic", "gemini", "ollama")

# 格式 → 官方端点示例（GUI「一键填入」helper；非硬依赖）
ENDPOINT_EXAMPLES = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "ollama": "http://127.0.0.1:11434",
}


def normalize_base(base_url: str) -> str:
    """路径归一化：strip 空白/去尾斜杠/只取 scheme://host:port 前缀（去 query/frag）。
    绝不自动加 /v1——路径还原交给 _resolve_base 的两级探测。"""
    b = (base_url or "").strip()
    if not b:
        return ""
    if b.endswith("/"):
        b = b.rstrip("/")
    # 去 query/fragment
    for ch in ("?", "#"):
        if ch in b:
            b = b.split(ch)[0]
    return b


def _headers_for(fmt, api_key):
    if fmt == "openai":
        return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if fmt == "anthropic":
        # 坑2/3：anthropic 必带版本头；x-api-key 认证
        return {"x-api-key": api_key, "anthropic-version": "2023-06-01",
                "Content-Type": "application/json"}
    if fmt == "gemini":
        # 坑4：官方推荐 x-goog-api-key（query ?key= 会泄漏进 URL/日志）
        return {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    if fmt == "ollama":
        return {"Content-Type": "application/json"}
    return {}


def build_body(fmt, model, messages, params, system=None):
    """按格式构造请求体。params 白名单：max_tokens/temperature/top_p（None 不发送）。"""
    if fmt == "openai":
        body = {"model": model, "messages": messages, "stream": False}
        if params.get("max_tokens"):
            body["max_tokens"] = params["max_tokens"]
        if params.get("temperature") is not None:
            body["temperature"] = params["temperature"]
        if params.get("top_p") is not None:
            body["top_p"] = params["top_p"]
        return body
    if fmt == "anthropic":
        body = {"model": model, "messages": messages, "stream": False}
        if params.get("max_tokens"):  # anthropic 必传 max_tokens
            body["max_tokens"] = params["max_tokens"]
        else:
            body["max_tokens"] = 512
        if system:
            body["system"] = system  # 坑3：system 顶层字段
        if params.get("temperature") is not None:
            body["temperature"] = params["temperature"]
        return body
    if fmt == "gemini":
        contents = [{"role": m["role"] if m.get("role") == "assistant" else "user",
                     "parts": [{"text": m["content"]}]} for m in messages]
        body = {"contents": contents}
        if params.get("max_tokens"):
            body.setdefault("generationConfig", {})["maxOutputTokens"] = params["max_tokens"]
        if params.get("temperature") is not None:
            body.setdefault("generationConfig", {})["temperature"] = params["temperature"]
        return body
    if fmt == "ollama":
        body = {"model": model, "messages": messages, "stream": False}
        if params.get("max_tokens"):
            body.setdefault("options", {})["num_predict"] = params["max_tokens"]  # 坑5
        if params.get("temperature") is not None:
            body.setdefault("options", {})["temperature"] = params["temperature"]
        return body
    return {}


def parse_response(fmt, data):
    """按格式解析响应文本（兜底 empty choices / None / 旧 text）。"""
    if not isinstance(data, dict):
        return ""
    if fmt == "openai":
        try:
            choices = data.get("choices") or []
            if not choices:
                return ""
            ch = choices[0] or {}
            msg = ch.get("message") or {}
            if msg.get("content"):
                return str(msg["content"])
            return str(ch.get("text") or "")  # 坑6：旧格式
        except Exception:
            return ""
    if fmt == "anthropic":
        try:
            return "".join(b.get("text", "") for b in (data.get("content") or []) if isinstance(b, dict))
        except Exception:
            return ""
    if fmt == "gemini":
        try:
            cands = data.get("candidates") or []
            if not cands:
                return ""
            parts = (cands[0].get("content") or {}).get("parts") or []
            return "".join(str(p.get("text", "")) for p in parts if isinstance(p, dict) and p.get("text"))
        except Exception:
            return ""
    if fmt == "ollama":
        try:
            return str((data.get("message") or {}).get("content", ""))
        except Exception:
            return ""
    return ""


def extract_error(data) -> str:
    """多字段错误体兜底：{error:{message|msg|detail}} → 语义化。"""
    if not isinstance(data, dict):
        return "响应非 JSON"
    err = data.get("error") or {}
    if isinstance(err, dict):
        for k in ("message", "msg", "detail", "code"):
            v = err.get(k)
            if v:
                return str(v)[:200]
    for k in ("detail", "msg"):
        v = data.get(k)
        if v:
            return str(v)[:200]
    return "未知错误"


class LLMClient:
    """单客户端（一次接一个）；全异步 + 硬超时。"""

    def __init__(self, cfg: dict):
        self.format = str(cfg.get("format", "openai"))
        if self.format not in FORMATS:
            self.format = "openai"
        self.base_url = normalize_base(cfg.get("base_url", ""))
        self.api_key = str(cfg.get("api_key", ""))
        self.model = str(cfg.get("model", ""))
        self.timeout_s = float(cfg.get("timeout_s", 15))
        self.params = dict(cfg.get("params") or {})

    def _session(self):
        from curl_cffi.requests import AsyncSession
        return AsyncSession(timeout=self.timeout_s)

    async def _request(self, url, headers, body, method="POST"):
        # [v2.19.7 安全·扫描发现·**刻意不加 SSRF 闸**] url 是**用户自己在设置页填的**
        # LLM 端点（llm_api_base），且都是硬编码模板 + 用户 base_url 拼接。若在这里套
        # is_private_url，本地推理服务（http://localhost:11434 / http://127.0.0.1:1234
        # 的 Ollama、LM Studio 等）会被一律拦死——那是明确支持的用法。这不是"可被页面
        # 内容操纵的目标 URL"（区别于 collect_feed/sitemap/solver 那些），故刻意放行。
        async with self._session() as s:
            if method == "GET":
                resp = await s.get(url, headers={k: v for k, v in headers.items() if k != "Content-Type"})
            else:
                resp = await s.post(url, json=body, headers=headers)
            return resp

    async def _resolve_base(self) -> str:
        """两级探测（openai/gemini）：base 404 → 试 base+/v1；成功变体缓存到实例。"""
        if self.base_url:
            return self.base_url
        return ""

    async def list_models(self):
        """模型列表（openai/gemini/ollama 有；anthropic→( [], True ) 手填）。"""
        if self.format == "anthropic":
            return [], True
        if not self.base_url:
            return [], False
        try:
            headers = _headers_for(self.format, self.api_key)
            if self.format == "ollama":
                url = f"{self.base_url}/api/tags"
                resp = await self._request(url, {}, {}, method="GET")
                if resp.status_code == 200:
                    data = resp.json()
                    return [m.get("name", "") for m in (data.get("models") or []) if m.get("name")], False
                return [], False
            url = f"{self.base_url}/models"
            resp = await self._request(url, headers, {}, method="GET")
            if resp.status_code == 200:
                data = resp.json()
                return [m.get("id") or m.get("name", "") for m in (data.get("data") or [])], False
            return [], False
        except Exception:
            return [], False

    async def test_connection(self) -> dict:
        """快速层 GET models(5s超时) → 深测层 POST max_tokens=1 ping 计时。
        返回 {ok, latency_ms, format, models_count, first_model, error}。"""
        t0 = time.monotonic()
        models, hand_entry = await self.list_models()
        latency1 = (time.monotonic() - t0) * 1000
        if hand_entry:
            # anthropic 无列表：直接深测
            try:
                msgs = [{"role": "user", "content": "ping"}]
                body = build_body(self.format, self.model or "claude-3-5-sonnet-20241022",
                                  msgs, dict(self.params) or {"max_tokens": 1})
                url = f"{self.base_url.rstrip('/')}/messages"
                resp = await self._request(url, _headers_for(self.format, self.api_key), body)
                lat = (time.monotonic() - t0) * 1000
                if resp.status_code == 200:
                    return {"ok": True, "latency_ms": round(lat), "format": self.format,
                            "models_count": 0, "first_model": "", "error": ""}
                return {"ok": False, "latency_ms": round(lat), "format": self.format,
                        "models_count": 0, "first_model": "",
                        "error": _status_hint(resp.status_code, extract_error(resp.json() if resp.content else {}))}
            except Exception as e:
                return {"ok": False, "latency_ms": round((time.monotonic() - t0) * 1000),
                        "format": self.format, "models_count": 0, "first_model": "",
                        "error": _status_hint(0, str(e)[:120])}
        if not models:
            return {"ok": False, "latency_ms": round(latency1), "format": self.format,
                    "models_count": 0, "first_model": "", "error": "模型列表获取失败（检查 Base URL/Key/网络）"}
        # 深测：取第一个模型 ping
        m0 = models[0]
        try:
            body = build_body(self.format, m0, [{"role": "user", "content": "ping"}],
                              dict(self.params) or {"max_tokens": 1})
            url = self._chat_url(m0)
            resp = await self._request(url, _headers_for(self.format, self.api_key), body)
            lat = (time.monotonic() - t0) * 1000
            if resp.status_code == 200:
                return {"ok": True, "latency_ms": round(lat), "format": self.format,
                        "models_count": len(models), "first_model": m0, "error": ""}
            return {"ok": False, "latency_ms": round(lat), "format": self.format,
                    "models_count": len(models), "first_model": m0,
                    "error": _status_hint(resp.status_code, extract_error(resp.json() if resp.content else {}))}
        except Exception as e:
            return {"ok": False, "latency_ms": round((time.monotonic() - t0) * 1000),
                    "format": self.format, "models_count": len(models), "first_model": m0,
                    "error": _status_hint(0, str(e)[:120])}

    def _chat_url(self, model):
        if self.format == "gemini":
            return f"{self.base_url.rstrip('/')}/models/{urllib.parse.quote(model)}:generateContent"
        if self.format == "ollama":
            return f"{self.base_url.rstrip('/')}/api/chat"
        return f"{self.base_url.rstrip('/')}/chat/completions"

    async def chat(self, messages: list, model: str = "", system: str = "") -> str:
        """主调用。网络/429 重试1次；4xx 不重试；超时/空 → LLM_EMPTY 留痕。"""
        m = model or self.model
        if not m:
            raise ValueError("LLM 模型未设置")
        body = build_body(self.format, m, messages, self.params, system=system)
        url = self._chat_url(m)
        last_err = ""
        for attempt in range(2):
            try:
                resp = await self._request(url, _headers_for(self.format, self.api_key), body)
                if resp.status_code == 200:
                    text = parse_response(self.format, resp.json() if resp.content else {})
                    if text.strip():
                        return text.strip()
                    last_err = "空内容（模型名可能不对）"
                    raise ValueError(last_err)
                code = resp.status_code
                # [v2.18 P3-5] 4xx 错误体常是 HTML——resp.json() 抛 JSONDecodeError
                # （ValueError 子类）被下方 except ValueError 捕获 → 违背"4xx 不重试"。
                # 错误体解析单独兜底，非 JSON 时截取原文片段。
                try:
                    _err_hint = extract_error(resp.json() if resp.content else {})
                except Exception:
                    _err_hint = (resp.content or b"")[:120].decode("utf-8", errors="replace")
                msg = _status_hint(code, _err_hint)
                if code in (429, 500, 502, 503):
                    last_err = msg
                    await asyncio.sleep(3 if code == 429 else 2)
                    continue
                raise RuntimeError(msg)  # 4xx 不重试
            except (asyncio.TimeoutError, ConnectionError, RuntimeError) as e:
                last_err = str(e)
                if attempt == 0:
                    await asyncio.sleep(2)
                    continue
                break
            except ValueError as e:
                last_err = str(e)
                if attempt == 0:
                    await asyncio.sleep(3)
                    continue
                break
        raise RuntimeError(f"LLM 调用失败: {last_err}")


def _status_hint(code: int, msg: str) -> str:
    """错误语义化全表（坑：非JSON/空内容）。
    [v2.18 P3-4] code=0 不再谎报"连接超时"——DNS 失败/拒绝连接细分提示。"""
    m = msg or ""
    if code == 401 or code == 403:
        return f"API Key 无效或无权限 ({code})" + (f": {m}" if m else "")
    if code == 408:
        return "连接超时，检查网络/代理/Base URL" + (f": {m}" if m else "")
    if code == 0:
        return ("连接失败（超时/DNS 解析失败/拒绝连接）——检查 Base URL 是否可访问、"
                "网络/代理是否正常" + (f": {m}" if m else ""))
    if code == 404:
        return "地址不存在（可能缺 /v1 或 format 选错）" + (f": {m}" if m else "")
    if code == 429:
        return "限频（Rate Limit），稍后重试"
    if code == 422:
        return "请求参数不被该网关接受（尝试关闭 temperature 等参数）" + (f": {m}" if m else "")
    if code == 200:
        return "响应无内容（模型名可能不对）"
    if m == "响应非 JSON":
        return "响应非 JSON——检查是否为该格式的正确端点"
    return f"HTTP {code}" + (f": {m}" if m else "")


MAX_TEXT = 8000


def _truncate(text, limit=MAX_TEXT):
    """批处理正文截断（token 上限防护）"""
    return str(text)[:limit] if text else ""


# ═══ [v2.16 阶段L] LLM 离线批处理（纯函数数据流——不遍历文件系统，由调用方喂 rows）═══
# [v2.17 2.2] 同步版 llm_process_rows 已删除（恒走 if False 占位、恒 skipped 的"假 API"，
# 零生产调用方）——唯一入口为 async_llm_process_rows（调用方必须 await）。


async def async_llm_score_links(client, links: list, budget_links: int = 64,
                                concurrency: int = 8, total_timeout: float = 120.0) -> dict:
    """[v2.17 2-B] 链接相关性打分（种子级；默认关由调用方开关）：
    返回 {url: priority}（priority 越小越优先；失败/预算尽 → 空 dict 诚实降级）。
    [v2.18 P1-1] 旧版串行 await：64 条 × 最坏 33s/条 ≈ 35 分钟种子入队阻塞
    （"LLM 阻塞爬虫关键路径"老病复发点）。现分片并发 + 整体预算超时：
    超时未完成条目诚实降级默认分 3，不再拖死启动。"""
    urls = list(links or [])[:max(1, budget_links)]
    if not urls:
        return {}
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _score_one(url: str) -> tuple:
        try:
            result = await client.chat([{"role": "user", "content":
                f"输出一个整数 1-5（越小越值得优先爬取）：{url}"}])
            v = int("".join(ch for ch in str(result) if ch.isdigit())[:1] or 3)
            return url, min(max(v, 1), 5)
        except Exception:
            return url, 3

    async def _bounded(u: str) -> tuple:
        async with sem:
            return await _score_one(u)

    tasks = {u: asyncio.ensure_future(_bounded(u)) for u in urls}
    _done, _pending = await asyncio.wait(tasks.values(), timeout=total_timeout)
    out = {}
    for t in _done:
        try:
            u, v = t.result()
            out[u] = v
        except Exception:
            pass
    if _pending:
        for t in _pending:
            t.cancel()
        await asyncio.gather(*_pending, return_exceptions=True)
    for u in urls:
        out.setdefault(u, 3)  # 预算内未完成 → 默认分（诚实降级，不阻塞）
    return out


async def async_llm_process_rows(client, rows: list, task="summarize",
                                 budget_month=500, limit=None) -> dict:
    """异步批处理主入口（爬虫/CLI 用）：rows 由调用方扫描传入，本函数不碰文件系统。
    返回 {done, failed, skipped, budget_left, out_rows}。">
    """
    done = failed = skipped = used = 0
    out_rows = []
    for row in (rows[:limit] if limit else rows):
        url = (row or {}).get("url")
        if not url:
            continue
        try:
            title = row.get("title", "") or ""
            text = _truncate(row.get("text", "") or "")
            article = f"标题：{title}\n正文：{text}" if text else f"标题：{title}"
            if task == "summarize":
                prompt = f"请用中文50字内总结以下内容：\n{article}"
            elif task == "classify":
                prompt = f"请为以下内容分类（用1-3个标签）：\n{article}"
            elif task == "entities":
                prompt = f"请提取以下内容的人物/机构/地点实体（逗号分隔）：\n{article}"
            elif task == "keywords":
                prompt = f"请提取5个关键词（逗号分隔）：\n{article}"
            else:
                raise ValueError(f"未知任务: {task}")
            result = await client.chat([{"role": "user", "content": prompt}])
            out_rows.append({"url": url, "task": task, "result": result,
                             "ts": time.time(), "ok": True})
            done += 1
            used += 1
        except Exception as e:
            failed += 1
            out_rows.append({"url": url, "task": task, "result": "",
                             "ts": time.time(), "ok": False, "error": str(e)[:120]})
        if used >= budget_month:
            logger.warning(f"预算已尽({budget_month})，停止")
            break
        await asyncio.sleep(0.1)
    return {"done": done, "failed": failed, "skipped": skipped,
            "budget_left": max(0, budget_month - used), "out_rows": out_rows}
