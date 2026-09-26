"""Kiana Vnext Plus — v2.16.1 阶段1 安全与正确性加固回归

覆盖：SSRF 闸字面变体加固（十进制/十六进制/八进制 IP，旧实现 ipaddress ValueError
当域名放行）+ DNS 解析校验（可关、带宽缓存）；CSV 表头重复（f.tell()==0 追加模式恒真）；
cli.py cookie-list 残留 NameError。
"""
import sys, os, socket
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault('KIANA_CRYPTO_KEY', 'kv_test')

from kiana_vnext_plus import url_utils
from kiana_vnext_plus.url_utils import is_private_url


class TestPrivateUrlLiteralVariants:
    """v2.16.1：ipaddress.ip_address 对 '2130706433'/'127.1'/'0x7f000001' 抛 ValueError
    → 旧实现当域名放行（curl/浏览器实际解析为 127.0.0.1）——现在用系统解析器判定。"""

    def test_decimal_integer(self):
        assert is_private_url("http://2130706433/") is True

    def test_short_dotted(self):
        assert is_private_url("http://127.1/") is True

    def test_hex_ipv4(self):
        assert is_private_url("http://0x7f000001/") is True

    def test_octal_ipv4(self):
        assert is_private_url("http://0177.0.0.1/") is True

    def test_private_ipv6(self):
        assert is_private_url("http://[::1]/") is True
        assert is_private_url("http://[fe80::1]/") is True

    def test_public_ip_literal_allowed(self):
        assert is_private_url("http://8.8.8.8/") is False


class TestPrivateUrlDnsCheck:
    def test_domain_to_private_ip_blocked(self, monkeypatch):
        calls = {}

        def fake_getaddrinfo(host, port, **kw):
            if kw.get("flags") == socket.AI_NUMERICHOST:
                raise socket.gaierror("not numeric")  # 域名 → 字面判定失败
            calls[host] = calls.get(host, 0) + 1
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 80))]

        monkeypatch.setattr(url_utils._socket, "getaddrinfo", fake_getaddrinfo)
        assert is_private_url("http://evil-xip.example.com/") is True
        assert calls.get("evil-xip.example.com") == 1

    def test_public_domain_allowed(self, monkeypatch):
        def fake_getaddrinfo(host, port, **kw):
            if kw.get("flags") == socket.AI_NUMERICHOST:
                raise socket.gaierror("not numeric")
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]

        monkeypatch.setattr(url_utils._socket, "getaddrinfo", fake_getaddrinfo)
        assert is_private_url("http://example-site.test/") is False

    def test_dns_failure_allows(self, monkeypatch):
        def fake_getaddrinfo(host, port, **kw):
            raise socket.gaierror("NXDOMAIN")

        monkeypatch.setattr(url_utils._socket, "getaddrinfo", fake_getaddrinfo)
        assert is_private_url("http://down-site.test/") is False  # 解析失败按放行（防误伤）

    def test_dns_check_toggle_off(self, monkeypatch):
        def fake_getaddrinfo(host, port, **kw):
            if kw.get("flags") == socket.AI_NUMERICHOST:
                raise socket.gaierror("not numeric")
            raise AssertionError("DNS_CHECK_ENABLED=False 时不应做 DNS 解析校验")

        monkeypatch.setattr(url_utils._socket, "getaddrinfo", fake_getaddrinfo)
        url_utils.DNS_CHECK_ENABLED = False
        try:
            assert is_private_url("http://anything.test/") is False
        finally:
            url_utils.DNS_CHECK_ENABLED = True

    def test_host_cache_used(self, monkeypatch):
        calls = []

        def fake_getaddrinfo(host, port, **kw):
            if kw.get("flags") == socket.AI_NUMERICHOST:
                raise socket.gaierror("not numeric")
            calls.append(host)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 80))]

        monkeypatch.setattr(url_utils._socket, "getaddrinfo", fake_getaddrinfo)
        url_utils._HOST_CACHE.clear()
        is_private_url("http://cache-site.test/")
        is_private_url("http://cache-site.test/")
        assert len(calls) == 1  # 第二次命中缓存不发解析


class TestCsvHeaderOnce:
    """v2.16.1：CSV 表头重复——f.tell()==0 在追加模式恒真，每次 flush 都重写表头。"""

    def test_header_once_per_file(self, tmp_path):
        from kiana_vnext_plus.enhancements import DataExporter
        ex = DataExporter(tmp_path)
        for i in range(60):  # 50 条触发首次自动 flush + 剩余 10 条 flush_all 第二次
            ex.add({"url": f"http://a/{i}", "title": f"t{i}", "n": i}, "a.test.com")
        ex.flush_all()
        csvs = list((tmp_path / "a.test.com").glob("*.csv"))
        assert csvs, "应有 CSV 产出"
        headers = 0
        rows = 0
        for p in csvs:
            lines = p.read_text(encoding="utf-8-sig").splitlines()
            for l in lines:
                if l.startswith("n,title,url"):  # keys 排序后的表头
                    headers += 1
                elif l.strip():
                    rows += 1
        assert headers == len(csvs), f"每个 CSV 文件只允许一个表头（实际 {headers}/{len(csvs)}）"
        assert rows == 60


class TestCliNoNameErrorResidue:
    """v2.16.1：cli.py cookie-list 分支残留 asyncio.run(do_export())（do_export 仅
    export 分支定义 → 必 NameError）。"""

    def test_no_asyncio_run_do_export_in_cookie_list(self):
        src = (Path(__file__).parent.parent / "kiana_vnext_plus" / "cli.py").read_text(encoding="utf-8")
        # [FIXED & MODIFIED] v2.17 真机冒烟：原断言"全源码不含"被推翻——export 分支此前就是
        # 缺少调用而死代码（do_export 定义了从未 run，exit=0 无输出无文件）。精确语义：
        # 调用必须存在且恰一处、位于 export 分支之内、不得残留于其后分支（cookie-list 等）
        assert src.count("asyncio.run(do_export())") == 1, \
            "do_export 调用须恰好一处（export 分支；曾整体丢失致子命令死代码）"
        call = src.find("asyncio.run(do_export())")
        export_beg = src.find('elif args.command == "export"')
        cookie_list = src.find('elif args.command == "cookie-list"')
        assert export_beg >= 0 and call > export_beg
        assert cookie_list < 0 or call < cookie_list, "cookie-list 残留调用必须清除"
