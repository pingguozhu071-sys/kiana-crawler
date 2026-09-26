

class ResponseAdapter:
    """统一响应适配器，封装不同引擎的 HTTP 响应"""

    def __init__(self, status_code: int, url: str, headers: dict, raw_text: str = None,
                 raw_bytes: bytes = None, too_big: bool = False):
        self._status_code = status_code
        self._url = url
        self._headers = {k.lower(): v for k, v in headers.items()}
        self._raw_text = raw_text
        self._raw_bytes = raw_bytes
        self.too_big = too_big  # [v2.14] 超过 max_body_bytes 被截断（页处理器据此拒解析）

    @property
    def status_code(self):
        return self._status_code

    @property
    def url(self):
        return self._url

    @property
    def headers(self):
        return self._headers

    @property
    def ok(self):
        return self._status_code == 200

    async def text(self):
        return self._raw_text if self._raw_text is not None else ""

    async def content_bytes(self):
        return self._raw_bytes if self._raw_bytes is not None else b""

    async def json(self):
        import json
        text = await self.text()
        try:
            return json.loads(text) if text else {}
        except json.JSONDecodeError:
            return {}
