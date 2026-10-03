# -*- coding: utf-8 -*-
"""脱敏链路回归（门禁第 13 项）

**这一项的由来**：门禁第 13 项上线时用**活体特征串**跑了一遍，当场抓到
`SESSDATA=…` / `bili_jct=…` 在日志里**原样输出**——`sanitize_text` 此前只覆盖
手机号/邮箱/IP，**不认会话 cookie**，而根 logger 过滤器正是靠它兜底的。

本文件锁三件事：
  ① **该抹的必须抹**：会话/令牌语义的 `名字=值`（含真实站点 cookie 名）；
  ② **不该抹的绝不能抹**：`author=` / `auth_mode=` / `login_time=` / `page=` ——
     子串匹配版会把它们一起抹掉，那是**数据损坏**不是脱敏（`author` 正是导出副本
     的常见字段）。这一条是本文件存在的另一半理由；
  ③ **活体链路**：往一个新 handler 打日志，输出里搜不到特征串；
     以及 `privacy_sanitize` 默认必须是开的（关掉它=脱敏整体失效）。
"""
import io
import logging
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kiana_vnext_plus.sanitizer import sanitize_text      # noqa: E402

# ① 必须被抹：真实站点会话 cookie + 通用令牌名
MUST_REDACT = (
    "SESSDATA=abc123", "bili_jct=deadbeef", "DedeUserID=12345",
    "PHPSESSID=xyz987", "access_token=zzz999", "csrf_token=qqq",
    "session_id=aaa111", "_abck=BBB~CC", "auth=BearerXYZ",
    "authorization=BearerXYZ", "token=a",
)

# ② 绝不能抹：普通字段（含"看起来像"的 auth*/login*）
MUST_KEEP = (
    "author=zhangsan", "author_name=zhangsan", "auth_mode=oauth2basic",
    "login_time=1712345", "page=2", "count=12345", "width=1920",
)

CANARIES = {
    "url_token": "CANARY_TOKEN_7f3a91d2",
    "cookie": "CANARY_COOKIE_4b8e10c5",
    "email": "canary_user@example.com",
}


class TestPrecision(unittest.TestCase):
    def test_secret_names_are_redacted(self):
        for s in MUST_REDACT:
            self.assertIn("[REDACTED]", sanitize_text(s), f"{s} 应被脱敏")

    def test_ordinary_fields_are_never_redacted(self):
        """**反过度脱敏**：子串匹配版会在这里全红——那是数据损坏，不是脱敏"""
        for s in MUST_KEEP:
            self.assertNotIn("[REDACTED]", sanitize_text(s),
                             f"{s} 是普通字段，被抹掉就是数据损坏")

    def test_cookie_value_redacted_inside_a_cookie_header(self):
        """真实形态：整条 Cookie 头里多个 cookie 都要抹，且分号结构保留"""
        out = sanitize_text(f"Cookie: SESSDATA={CANARIES['cookie']}; bili_jct=abc123")
        self.assertNotIn(CANARIES["cookie"], out)
        self.assertIn(";", out, "分号分隔结构应保留（否则日志不可读）")

    def test_existing_behaviours_unchanged(self):
        """手机号/邮箱/IP 的老行为不能被这次改动带坏"""
        self.assertIn("[手机号]", sanitize_text("联系13812345678"))
        self.assertIn("[邮箱]", sanitize_text("mail@example.com"))
        self.assertIn("[IP]", sanitize_text("来自 203.0.113.7 的请求"))


class TestLiveChain(unittest.TestCase):
    """③ 活体：真的打一条日志，看输出里还剩不剩"""

    def _capture(self):
        buf = io.StringIO()
        h = logging.StreamHandler(buf)
        h.setFormatter(logging.Formatter("%(message)s"))
        root = logging.getLogger()
        old, root.level = root.level, logging.DEBUG
        root.addHandler(h)
        try:
            import kiana_vnext_plus.config  # noqa: F401  (导入即装配脱敏)
            lg = logging.getLogger("kiana.test.canary")
            lg.warning("请求 https://api.example.com/x?token=%s&a=1", CANARIES["url_token"])
            lg.warning("Cookie: SESSDATA=%s", CANARIES["cookie"])
            lg.warning("联系 %s", CANARIES["email"])
        finally:
            root.removeHandler(h)
            root.level = old
        return buf.getvalue()

    def test_fixture_output_is_actually_captured(self):
        """夹具自证：捕获不到内容的话，下面的"搜不到"就是假绿"""
        out = self._capture()
        self.assertTrue(out.strip(), "没捕获到日志——后面的断言会假绿")
        self.assertIn("api.example.com", out)

    def test_no_canary_survives_the_logger_chain(self):
        out = self._capture()
        for name, secret in CANARIES.items():
            self.assertNotIn(secret, out, f"{name} 从日志链路泄漏了")

    def test_handler_autosanitize_wrapper_is_installed(self):
        import kiana_vnext_plus.config  # noqa: F401
        self.assertTrue(getattr(logging.Handler, "_kiana_autosanitized", False),
                        "logging.Handler 未被自动脱敏包装")

    def test_privacy_switch_defaults_on(self):
        """关掉 `privacy_sanitize` = 落盘/导出副本原样写出，必须默认开启"""
        from kiana_vnext_plus.config import DEFAULT_GLOBAL
        self.assertIs(DEFAULT_GLOBAL.get("privacy_sanitize"), True)


if __name__ == "__main__":
    unittest.main()
