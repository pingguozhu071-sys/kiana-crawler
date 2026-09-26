try:
    import orjson

    def json_dumps(obj) -> str:
        return orjson.dumps(obj).decode()

    def json_loads(s: str) -> dict:
        return orjson.loads(s)
except ImportError:
    import json

    def json_dumps(obj) -> str:
        return json.dumps(obj, ensure_ascii=False)

    def json_loads(s: str) -> dict:
        return json.loads(s)
