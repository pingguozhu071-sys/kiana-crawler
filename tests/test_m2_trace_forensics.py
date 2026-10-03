# -*- coding: utf-8 -*-
"""trace 取证脱敏回归（M2-d）

**对抗性验证**：构造一份"看起来就像真 trace"的假归档——含签名 URL、Cookie 头、
Authorization 头、以及非 JSON 的裸头行——然后断言**每一个特征串都被抹掉**。

判据不是"函数没抛异常"，而是：把产出归档**整包解压后全盘搜索**，一个都搜不到。
（对齐工程既有的"明文扫描"做法：用特征串当 cookie 值跑完整流程，再全盘搜。）

本文件另有一条**夹具自证**：先证明原始假 trace **确实含**这些特征串——
否则"搜不到"可能只是因为压根没放进去，那样的测试是假绿。
"""
import json
import os
import shutil
import tempfile
import unittest
import zipfile

from kiana_vnext_plus import trace_forensics as tf

# 特征串：任一个出现在产出里就是脱敏失效
SECRETS = ("TOPSECRET_TOKEN_123", "TOPSECRET_COOKIE_456", "TOPSECRET_AUTH_789",
           "TOPSECRET_RAWHDR_000", "TOPSECRET_KEY_111")

TRACE_ENTRIES = {
    "trace.trace": "\n".join([
        json.dumps({"type": "beforeRequest",
                    "url": f"https://api.example.com/x?token={SECRETS[0]}&a=1"}),
        json.dumps({"type": "request",
                    "headers": [{"name": "cookie", "value": f"SESSDATA={SECRETS[1]}"},
                                {"name": "user-agent", "value": "UA/1.0"}]}),
        json.dumps({"type": "request",
                    "headers": {"authorization": f"Bearer {SECRETS[2]}"}}),
        f"Cookie: {SECRETS[3]}",                                  # 非 JSON 行
        f"plain line with url ?key={SECRETS[4]}",                 # 非 JSON 行 + 签名参数
    ]),
    "trace.network": json.dumps({"url": f"https://cdn.example.com/a?sign={SECRETS[0]}"}),
    "resources/shot.png": "\x89PNG\x00binary\x00payload",          # 二进制资源
}


def _make_zip(path):
    with zipfile.ZipFile(path, "w") as z:
        for name, content in TRACE_ENTRIES.items():
            z.writestr(name, content)


def _all_text(path):
    """整包解压后拼成一坨文本——用于全盘搜特征串"""
    buf = []
    with zipfile.ZipFile(path) as z:
        for n in z.namelist():
            buf.append(z.read(n).decode("utf-8", "ignore"))
    return "\n".join(buf)


class TestSanitizeTraceText(unittest.TestCase):
    def test_url_signature_params_are_redacted(self):
        out = tf.sanitize_trace_text(f"see https://a.com/p?token={SECRETS[0]}&x=1 end")
        self.assertNotIn(SECRETS[0], out)
        self.assertIn("x=1", out, "非敏感参数应保留（否则诊断价值被抹光）")

    def test_playwright_header_array_form(self):
        line = json.dumps({"headers": [{"name": "cookie", "value": f"S={SECRETS[1]}"},
                                       {"name": "user-agent", "value": "UA"}]})
        out = tf.sanitize_trace_text(line)
        self.assertNotIn(SECRETS[1], out)
        self.assertIn("UA", out, "无关头必须保留")

    def test_dict_header_form(self):
        line = json.dumps({"headers": {"authorization": f"Bearer {SECRETS[2]}"}})
        self.assertNotIn(SECRETS[2], tf.sanitize_trace_text(line))

    def test_non_json_fallback_still_redacts(self):
        """解析不了的行走保守兜底——宁可多抹，不可漏抹"""
        out = tf.sanitize_trace_text(f"Cookie: {SECRETS[3]}")
        self.assertNotIn(SECRETS[3], out)

    def test_empty_input_is_safe(self):
        self.assertEqual(tf.sanitize_trace_text(""), "")


class TestArchiveSanitization(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kiana_trace_")
        self.src = os.path.join(self.tmp, "raw.zip")
        self.dst = os.path.join(self.tmp, "clean.zip")
        _make_zip(self.src)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fixture_really_contains_secrets(self):
        """**夹具自证**：原始假 trace 必须真的含这些特征串，否则下面的"搜不到"是假绿。"""
        raw = _all_text(self.src)
        for s in SECRETS:
            self.assertIn(s, raw, f"夹具不含 {s} —— 脱敏测试会变成假绿")

    def test_no_secret_survives_in_output_archive(self):
        """**本文件最重要的一条**：产出归档整包解压后，一个特征串都搜不到。"""
        stats = tf.sanitize_trace_archive(self.src, self.dst)
        self.assertGreaterEqual(stats["text_sanitized"], 2)
        out = _all_text(self.dst)
        for s in SECRETS:
            self.assertNotIn(s, out, f"脱敏后仍能搜到 {s}")

    def test_binary_resources_copied_untouched(self):
        tf.sanitize_trace_archive(self.src, self.dst)
        with zipfile.ZipFile(self.src) as a, zipfile.ZipFile(self.dst) as b:
            self.assertEqual(a.read("resources/shot.png"), b.read("resources/shot.png"))
            self.assertEqual(sorted(a.namelist()), sorted(b.namelist()), "条目集合不得变化")

    def test_original_is_not_modified(self):
        before = open(self.src, "rb").read()
        tf.sanitize_trace_archive(self.src, self.dst)
        self.assertEqual(open(self.src, "rb").read(), before, "原始 trace 只读，不得被改写")

    def test_non_secret_content_is_preserved(self):
        """别把诊断价值一起抹了：无关字段要留着"""
        tf.sanitize_trace_archive(self.src, self.dst)
        out = _all_text(self.dst)
        self.assertIn("user-agent", out)
        self.assertIn("UA/1.0", out)


class TestArchiveLocation(unittest.TestCase):
    def test_default_dir_is_outside_repo_or_gitignored(self):
        """红线③：归档不得落进仓库（便携模式下的 KianaData/ 已被 .gitignore 覆盖）"""
        self.assertTrue(tf.archive_is_outside_repo(),
                        f"归档目录落在仓库内且未被忽略: {tf.trace_archive_dir()}")

    def test_archive_trace_writes_sanitized_copy_and_keeps_original(self):
        tmp = tempfile.mkdtemp(prefix="kiana_arch_")
        try:
            src = os.path.join(tmp, "raw.zip")
            _make_zip(src)
            out_root = os.path.join(tmp, "archives")
            res = tf.archive_trace(src, root=out_root, name="case1")
            self.assertIsNotNone(res)
            self.assertTrue(os.path.exists(res["path"]))
            self.assertTrue(os.path.exists(src), "默认保留原始 trace（keep_original=True）")
            for s in SECRETS:
                self.assertNotIn(s, _all_text(res["path"]))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_missing_source_returns_none_without_crashing(self):
        missing = os.path.join(tempfile.gettempdir(), "definitely_missing_xyz.zip")
        self.assertIsNone(tf.archive_trace(missing),
                          "源缺失应返回 None 并告警，不许抛异常拖垮任务")


if __name__ == "__main__":
    unittest.main()
