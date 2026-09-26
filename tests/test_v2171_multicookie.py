# -*- coding: utf-8 -*-
"""[v2.17.1] cookies 多文件解析：分号/换行/注释/去重，健康检查同规则"""
import os
import pathlib

from kiana_vnext_plus.cookie_utils import parse_cookie_file_list
from kiana_vnext_plus import cookie_health


class TestParseCookieFileList:
    def test_semicolon(self):
        assert parse_cookie_file_list("a.txt;  b.txt ;c.txt") == ["a.txt", "b.txt", "c.txt"]

    def test_newline_and_crlf(self):
        text = "one.txt\r\n# 注释行\r\n;two.txt\n\n three.txt"
        assert parse_cookie_file_list(text) == ["one.txt", "two.txt", "three.txt"]

    def test_dedup_keep_order(self):
        assert parse_cookie_file_list("x;a;x;b;a") == ["x", "a", "b"]

    def test_quotes_stripped(self):
        assert parse_cookie_file_list('"d:/a b/c.txt"; \'e.txt\'') == ["d:/a b/c.txt", "e.txt"]

    def test_empty_none(self):
        assert parse_cookie_file_list("") == []
        assert parse_cookie_file_list(None) == []
        assert parse_cookie_file_list("  ;\n# x\n") == []


class TestCookieHealthFiles:
    def test_env_newline_split(self, monkeypatch):
        monkeypatch.setenv("KIANA_COOKIE_FILES", "a;\nb\n#c\n")
        assert cookie_health._cookie_files() == ["a", "b"]

    def test_env_fallback(self, monkeypatch):
        monkeypatch.delenv("KIANA_COOKIE_FILES", raising=False)
        monkeypatch.setenv("KIANA_COOKIE_FILE", "single.txt")
        assert cookie_health._cookie_files() == ["single.txt"]

    def test_default_only_if_exists(self, monkeypatch):
        monkeypatch.delenv("KIANA_COOKIE_FILES", raising=False)
        monkeypatch.delenv("KIANA_COOKIE_FILE", raising=False)
        monkeypatch.setenv("LOCALAPPDATA", str(pathlib.Path("this_should_not_exist_xyz")))
        assert cookie_health._cookie_files() == []
