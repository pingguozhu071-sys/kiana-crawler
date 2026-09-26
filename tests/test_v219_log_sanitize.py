"""v2.19 安全回归：日志脱敏（挂 handler 根治）+ URL 参数表扩容 + HTTP 缓存脱敏

对应 v2.19 优化批次 1.4 / 1.5。
核心背景：Filter 原挂在 root **logger** 上，而 Python logging 只调用祖先的
**handler**、不调用祖先 logger 的 filter → 子 logger 记录完全未脱敏。
全部离线。
"""
import logging
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiana_vnext_plus.config import attach_log_sanitizer
from kiana_vnext_plus.sanitizer import sanitize_url, sanitize_text


class _RecHandler(logging.Handler):
    """收集 handler 收到的已格式化消息"""

    def __init__(self):
        super().__init__()
        self.msgs = []
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record):
        self.msgs.append(record.getMessage())


class TestLogSanitizerAttachedToHandler(unittest.TestCase):
    def test_child_logger_sanitized(self):
        """子 logger 的敏感 URL 经挂 Filter 的 handler 后必须被脱敏（原实现失效点）"""
        h = _RecHandler()
        attach_log_sanitizer(h)
        lg = logging.getLogger("kiana.t219.child")
        lg.setLevel(logging.INFO)
        lg.addHandler(h)
        lg.propagate = False
        try:
            lg.warning("fetch https://api.example.com/v1?token=SECRET123&api_key=KEYABC done")
        finally:
            lg.removeHandler(h)
        self.assertEqual(len(h.msgs), 1)
        msg = h.msgs[0]
        self.assertNotIn("SECRET123", msg, "token 必须被脱敏")
        self.assertNotIn("KEYABC", msg, "api_key 必须被脱敏")
        self.assertIn("[REDACTED]", msg)

    def test_idempotent_attach(self):
        """重复挂载不得叠加 Filter"""
        h = _RecHandler()
        attach_log_sanitizer(h)
        attach_log_sanitizer(h)
        n = sum(1 for f in h.filters if type(f).__name__ == "_SanitizeLogFilter")
        self.assertEqual(n, 1, "同一 handler 上脱敏 Filter 只应有一个")

    def test_attach_none_safe(self):
        attach_log_sanitizer(None)  # 不得抛异常

    def test_phone_and_email_masked(self):
        h = _RecHandler()
        attach_log_sanitizer(h)
        lg = logging.getLogger("kiana.t219.pii")
        lg.setLevel(logging.INFO)
        lg.addHandler(h)
        lg.propagate = False
        try:
            lg.info("contact 13812345678 or a.b@example.com")
        finally:
            lg.removeHandler(h)
        self.assertNotIn("13812345678", h.msgs[0])
        self.assertNotIn("a.b@example.com", h.msgs[0])


class TestSanitizeUrlParams(unittest.TestCase):
    def test_expanded_param_table(self):
        """扩容后的参数表：api_key/sig/sign/secret/ticket/csrf 等均应脱敏"""
        cases = [
            "https://x.com/a?api_key=K1",
            "https://x.com/a?apikey=K2",
            "https://x.com/a?sig=S1",
            "https://x.com/a?sign=S2",
            "https://x.com/a?signature=S3",
            "https://x.com/a?secret=S4",
            "https://x.com/a?ticket=T1",
            "https://x.com/a?csrf=C1",
            "https://x.com/a?access_token=A1",
            "https://x.com/a?token=T2",
        ]
        for u in cases:
            out = sanitize_url(u)
            self.assertIn("[REDACTED]", out, f"应脱敏: {u}")
            self.assertNotIn("=K1", out)

    def test_non_secret_params_untouched(self):
        u = "https://x.com/search?q=hello&page=2&code=12345"
        self.assertEqual(sanitize_url(u), u, "普通参数不应被误伤")

    def test_bad_input_safe(self):
        self.assertEqual(sanitize_url(None), None)
        sanitize_url(12345)  # 不得抛异常


class TestFindEmailsReuse(unittest.TestCase):
    def test_shared_linear_regex(self):
        """sanitizer.find_emails 供 adaptive_v2 复用（消除旧 O(n²) 版本分叉）"""
        from kiana_vnext_plus.sanitizer import find_emails
        got = find_emails("联系 a.b@example.com 或 c@d.org，非邮箱 not-an-email")
        self.assertIn("a.b@example.com", got)
        self.assertIn("c@d.org", got)

    def test_adaptive_uses_shared(self):
        import inspect
        from kiana_vnext_plus.adaptive_v2 import DataCleaner
        src = inspect.getsource(DataCleaner.extract_structured)
        self.assertIn("find_emails", src,
                      "adaptive_v2 应复用 sanitizer.find_emails（不得保留旧版正则）")
        self.assertNotIn(r"[a-zA-Z0-9._%+\-]+@", src)


class TestAutoSanitizeOnNewHandlers(unittest.TestCase):
    """[v2.19.3 质检加固] 任何**新建** handler 应自动获得脱敏（无需显式调用）。

    动机：显式 attach 是脆弱约定——漏挂一次的代价是密钥明文落盘且无人察觉。"""

    def test_new_handler_auto_sanitized(self):
        import logging
        from kiana_vnext_plus import config  # noqa: F401 - 导入即启用自动脱敏
        recs = []

        class _H(logging.Handler):
            def emit(self, r):
                recs.append(r.getMessage())

        h = _H()
        h.setFormatter(logging.Formatter("%(message)s"))
        n = sum(1 for f in h.filters if type(f).__name__ == "_SanitizeLogFilter")
        self.assertEqual(n, 1, "新建 handler 必须自动带上脱敏 Filter")

        lg = logging.getLogger("kiana.t219.auto")
        lg.setLevel(logging.INFO)
        lg.addHandler(h)
        lg.propagate = False
        try:
            lg.warning("fetch https://api.x.com/v1?token=SECRET999&api_key=KEY777 ok")
        finally:
            lg.removeHandler(h)
        self.assertNotIn("SECRET999", recs[0])
        self.assertNotIn("KEY777", recs[0])

    def test_sanitize_idempotent(self):
        """重复脱敏不得损坏文本（自动挂载后可能跑两遍）"""
        from kiana_vnext_plus.config import _SanitizeLogFilter
        import logging as _lg
        rec = _lg.LogRecord("x", _lg.WARNING, "", 0, "already [REDACTED] text", None, None)
        _SanitizeLogFilter().filter(rec)
        _SanitizeLogFilter().filter(rec)
        self.assertEqual(rec.getMessage(), "already [REDACTED] text")

    def test_patch_applied_once(self):
        import logging
        from kiana_vnext_plus import config  # noqa: F401
        self.assertTrue(getattr(logging.Handler, "_kiana_autosanitized", False),
                        "Handler.__init__ 应只被包装一次（防重复包装）")


class TestHttpCacheSanitized(unittest.TestCase):
    def test_page_processor_stores_sanitized(self):
        """主路径缓存必须写入脱敏后的内容（否则 PII 明文落盘 http_cache）"""
        import inspect
        from kiana_vnext_plus.page_processor import PageProcessor
        src = inspect.getsource(PageProcessor.process_job)
        flat = " ".join(src.split())
        self.assertIn("http_cache.store(url, html_sanitized", flat,
                      "主路径缓存必须存入脱敏后的 html_sanitized（否则 PII 明文落盘）")

    def test_sanitize_precedes_store(self):
        """脱敏必须先于缓存写入"""
        import inspect
        from kiana_vnext_plus.page_processor import PageProcessor
        src = inspect.getsource(PageProcessor.process_job)
        i_san = src.find("to_thread(sanitize_text")
        i_store = src.find("store(url, html_sanitized")
        self.assertGreater(i_san, -1)
        self.assertGreater(i_store, -1)
        self.assertLess(i_san, i_store, "sanitize 必须先于缓存写入")


if __name__ == "__main__":
    unittest.main()
