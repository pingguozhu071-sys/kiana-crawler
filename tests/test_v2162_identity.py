"""Kiana Vnext Plus — v2.17 E-P2 身份捆绑 Session 主链路接线测试。

覆盖：Netscape cookie 组解析、按域 cookie 头匹配（含子域）、封锁判定纯函数、
整包退役语义（连续 2 次 bad→换新）、协议层 cookie 注入、零影响路径（池关闭=noop）。
全离线：无网络、无真实 cookies、无浏览器。
"""
import sys, os, asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault('KIANA_CRYPTO_KEY', 'kv_test')

from kiana_vnext_plus.identity_session import (
    SessionPool, IdentitySession, load_cookie_groups, decide_bad,
)


class TestLoadCookieGroups:
    """Netscape cookie 文件 → 域→Cookie 头串 分组"""

    def test_parse_groups_by_domain(self, tmp_path):
        f = tmp_path / "c.txt"
        f.write_text(
            "# Netscape\n"
            "a.com\tTRUE\t/\tFALSE\t0\tk1\tv1\n"
            "a.com\tTRUE\t/\tFALSE\t0\tk2\tv2\n"
            "sub.b.com\tTRUE\t/\tFALSE\t0\tsk\tsv\n"
            "c.com\tFALSE\t/\tTRUE\t2147483647\tonly\tx\n",
            encoding="utf-8")
        groups = load_cookie_groups([str(f)])
        assert len(groups) == 1
        g = groups[0]
        assert g["a.com"] == "k1=v1; k2=v2"
        assert g["sub.b.com"] == "sk=sv"
        assert g["c.com"] == "only=x"

    def test_missing_and_empty_files_skipped(self, tmp_path):
        assert load_cookie_groups([str(tmp_path / "nope.txt")]) == []
        empty = tmp_path / "e.txt"
        empty.write_text("# only comments\n\n", encoding="utf-8")
        assert load_cookie_groups([str(empty), str(tmp_path / "nope.txt")]) == []

    def test_str_path_supported(self, tmp_path):
        f = tmp_path / "c.txt"
        f.write_text("a.com\tTRUE\t/\tFALSE\t0\tk\tv\n", encoding="utf-8")
        assert load_cookie_groups(str(f))[0]["a.com"] == "k=v"


class TestCookiesForDomain:
    """会话/池按域取 Cookie 头（子域命中；__ 元键忽略）"""

    def test_session_cookies_for_subdomain(self):
        s = IdentitySession(cookie_bundle={
            "a.com": "k=v; k2=v2",
            "__default__": True,
            "other.com": "y=1",
        })
        assert s.cookies_for("a.com") == "k=v; k2=v2"
        assert s.cookies_for("sub.a.com") == "k=v; k2=v2"
        assert s.cookies_for("sub.a.com.cn") == ""    # 反向后缀不命中
        assert s.cookies_for("x.com") == ""

    def test_pool_cookies_for_url(self):
        pool = SessionPool(proxies=[], cookie_bundles=[{"a.com": "k=v"}])
        pool.get("a.com")  # 建会话（get 会换新兜底）
        assert pool.cookies_for("http://a.com/x") == "k=v"
        assert pool.cookies_for("http://sub.a.com/x") == "k=v"
        # 未建会话的域：无 cookie → 空串（不是抛错）
        assert pool.cookies_for("http://b.com/x") == ""

    def test_disabled_session_not_injected(self):
        s = IdentitySession(cookie_bundle={"a.com": "k=v"}, bad_count=2)
        pool = SessionPool(proxies=[], cookie_bundles=[{"a.com": "k=v"}])
        pool._pool.setdefault("a.com", []).append(s)
        assert pool.cookies_for("http://a.com/x") == ""


class TestDecideBad:
    """封锁判定：风控信号才整包退役计数；超时/连接错不误伤"""

    def test_bad_statuses(self):
        assert decide_bad(True, status=403) is True
        assert decide_bad(True, status=429) is True
        assert decide_bad(True, status=503) is True
        assert decide_bad(True, status=410) is True
        assert decide_bad(True, status=500) is False     # 5xx 服务端错≠出口封锁
        assert decide_bad(True, status=404) is False

    def test_bad_error_signals(self):
        assert decide_bad(True, err="ipblockerror") is True
        assert decide_bad(True, err="captcha") is True
        assert decide_bad(True, err="challenge") is True
        assert decide_bad(True, err="platformaccesserror") is True
        assert decide_bad(True, err="timeout") is False
        assert decide_bad(True, err="connection_error") is False
        assert decide_bad(True, err="ssl_error") is False

    def test_success_never_bad(self):
        assert decide_bad(False, status=403, err="ipblockerror") is False


class TestRetireSemantics:
    """连续 2 次封锁 → 整包退役；mark_good 清零"""

    def test_two_bad_retires_and_get_replaces(self):
        pool = SessionPool(proxies=["http://p1", "http://p2"],
                           cookie_bundles=[{"a.com": "k=v"}])
        s1 = pool.get("a.com")
        assert s1.proxy.startswith("http://")
        pool.mark_bad("a.com", s1)
        assert not s1.disabled
        pool.mark_bad("a.com", s1)
        assert s1.disabled
        s2 = pool.get("a.com")
        assert s2 is not s1 and not s2.disabled

    def test_good_resets_bad(self):
        pool = SessionPool(proxies=[], cookie_bundles=[{"a.com": "k=v"}])
        s = pool.get("a.com")
        pool.mark_bad("a.com", s)
        pool.mark_good("a.com", s)
        assert s.bad_count == 0 and not s.disabled


class TestFeedbackWiringNoop:
    """零影响路径：池未构建时 _identity_feedback / _acquire_identity 直接空转"""

    def test_feedback_noop_without_pool(self):
        from kiana_vnext_plus.crawler import Crawler
        c = object.__new__(Crawler)
        c._identity_pool = None
        c._identity_feedback("d", None, fail=True, status=403)   # 不抛、无副作用
        c._identity_feedback("d", object(), fail=False)

    def test_acquire_returns_empty_without_pool(self):
        from kiana_vnext_plus.crawler import Crawler
        c = object.__new__(Crawler)
        c._identity_pool = None
        proxy, sess = asyncio.run(c._acquire_identity("a.com"))
        assert proxy == "" and sess is None

    def test_feedback_marks_pool_session(self):
        from kiana_vnext_plus.crawler import Crawler
        c = object.__new__(Crawler)
        pool = SessionPool(proxies=["http://p1", "http://p2"],
                           cookie_bundles=[{"a.com": "k=v"}])
        c._identity_pool = pool
        sess = pool.get("a.com")
        c._identity_feedback("a.com", sess, fail=True, status=403)
        assert sess.bad_count == 1
        c._identity_feedback("a.com", sess, fail=False)
        assert sess.bad_count == 0


class TestProtocolCookieInjection:
    """identity_pool_provider 挂钩：命中时注入 Cookie；未命中不动 headers"""

    class _FakeProvider:
        def __init__(self, out):
            self.out = out

        def cookies_for(self, url):
            return self.out

    def test_injects_when_provider_has_cookie(self):
        from kiana_vnext_plus.protocol_engine import ProtocolEngine
        eng = ProtocolEngine()
        eng.identity_pool_provider = self._FakeProvider("k=v; k2=v2")
        headers = eng._build_headers("http://a.com/x", None, None)
        assert headers.get("Cookie") == "k=v; k2=v2"

    def test_no_cookie_key_when_provider_empty(self):
        from kiana_vnext_plus.protocol_engine import ProtocolEngine
        eng = ProtocolEngine()
        eng.identity_pool_provider = self._FakeProvider("")
        headers = eng._build_headers("http://a.com/x", None, None)
        assert "Cookie" not in {k.lower() for k in headers}

    def test_no_provider_noop(self):
        from kiana_vnext_plus.protocol_engine import ProtocolEngine
        eng = ProtocolEngine()
        headers = eng._build_headers("http://a.com/x", None, None)
        assert "Cookie" not in {k.lower() for k in headers}

    def test_identity_single_source_over_old_pool(self):
        """[v2.17 0-3] 身份捆绑开启时旧 session_pool 不得注入（出口与 cookie 异源失配纠偏）"""
        from kiana_vnext_plus.protocol_engine import ProtocolEngine
        eng = ProtocolEngine()
        eng.identity_pool_provider = self._FakeProvider("NEW=1")

        class _OldPool:
            def get_session(self, url):
                return {"cookies": "OLD=1"}

        eng.session_pool = _OldPool()
        headers = eng._build_headers("http://a.com/x", None, None)
        assert headers.get("Cookie") == "NEW=1", "身份开启时单源=身份池 cookie"
        # 关闭身份：旧池照常
        eng2 = ProtocolEngine()
        eng2.session_pool = _OldPool()
        headers2 = eng2._build_headers("http://a.com/x", None, None)
        assert headers2.get("Cookie") == "OLD=1"
