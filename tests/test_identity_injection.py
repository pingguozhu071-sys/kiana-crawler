# -*- coding: utf-8 -*-
"""身份注入接线回归（M1-c）

要锁死的核心行为：**注入失败不许静默**。

原实现是 `except Exception: pass`——注入失败会静默退化成"无 cookie 请求"，
于是登录墙的表现成了"页面内容不对"，而不是可读的登录态错误，排查时几乎无法定位。

另外锁死一条**脱敏**要求：注入失败的日志**只许记域、绝不记 URL**
（查询串可能带签名参数，与工程既有的脱敏纪律一致）。
"""
import logging

from kiana_vnext_plus.protocol_engine import ProtocolEngine


class _OkProvider:
    def cookies_for(self, url):
        return "SESSDATA=abc; buvid=xyz"


class _BoomProvider:
    def cookies_for(self, url):
        raise RuntimeError("provider 内部炸了")


def test_provider_injects_cookie():
    eng = ProtocolEngine()
    eng.identity_pool_provider = _OkProvider()
    h = eng._build_headers("https://example.com/a", None, None)
    assert h.get("Cookie") == "SESSDATA=abc; buvid=xyz"
    assert eng.identity_inject_failures == 0


def test_no_provider_is_zero_impact():
    """默认（不挂 provider）必须零影响：不加 Cookie、不计失败。"""
    eng = ProtocolEngine()
    h = eng._build_headers("https://example.com/a", None, None)
    assert "Cookie" not in h
    assert eng.identity_inject_failures == 0
    assert eng.identity_pool_provider is None


def test_injection_failure_is_not_silent(caplog):
    """注入失败：不抛异常、不发 Cookie，但**必须可观测**（计数 + ERROR 日志）。"""
    eng = ProtocolEngine()
    eng.identity_pool_provider = _BoomProvider()
    with caplog.at_level(logging.ERROR):
        h = eng._build_headers("https://example.com/a", None, None)   # 不得抛异常
    assert "Cookie" not in h
    assert eng.identity_inject_failures == 1, "失败必须可观测（原实现静默吞掉）"
    assert any(r.levelno >= logging.ERROR for r in caplog.records), "必须有 ERROR 级日志"


def test_failure_log_does_not_leak_url(caplog):
    """只记域、绝不记 URL——查询串可能带签名参数。"""
    eng = ProtocolEngine()
    eng.identity_pool_provider = _BoomProvider()
    secret = "TOPSECRETSIGNATURE123"
    with caplog.at_level(logging.ERROR):
        eng._build_headers(f"https://example.com/a?token={secret}", None, None)
    assert secret not in caplog.text, "日志**泄漏了 URL 查询串**"
    assert "example.com" in caplog.text, "但要能定位到域"


def test_failure_counter_accumulates():
    eng = ProtocolEngine()
    eng.identity_pool_provider = _BoomProvider()
    for _ in range(3):
        eng._build_headers("https://example.com/a", None, None)
    assert eng.identity_inject_failures == 3


def test_user_supplied_cookie_wins_over_provider():
    """用户自填 cookies 优先级不变：已有 Cookie 头时身份池不得覆盖。"""
    eng = ProtocolEngine()
    eng.identity_pool_provider = _OkProvider()
    h = eng._build_headers("https://example.com/a", None, {"Cookie": "USER=1"})
    assert h.get("Cookie") == "USER=1"
