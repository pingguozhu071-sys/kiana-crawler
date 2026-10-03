# -*- coding: utf-8 -*-
"""规则样本采集工具回归（M2-e 前置）

要锁死的：
  ① 默认**只落运行期数据根，不碰仓库**——样本会进仓库、会被公开，这一步必须显式；
  ② 落盘前过脱敏（夹具自证 + 脱敏后搜不到）；
  ③ 抓取被闸拦下 / URL 非法 → **可读错误且不落任何文件**（不留半截产物）；
  ④ 推不出名字也能有安全回退，且文件名不会带路径分隔符（防目录穿越）。
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import capture_sample as cs          # noqa: E402

HTML_WITH_EMAIL = ("<html><body><div class='note'><p>联系 user@example.com 投稿</p>"
                   "</div></body></html>")


class TestDeriveName(unittest.TestCase):
    def test_targets_from_the_pending_list(self):
        self.assertEqual(cs.derive_name("https://post.smzdm.com/p/a1b2c3/"), "smzdm")
        self.assertEqual(cs.derive_name("https://mp.weixin.qq.com/s/AbCdEf"), "weixin")

    def test_generic_subdomain_dropped(self):
        self.assertEqual(cs.derive_name("https://www.example.com/x"), "example")

    def test_never_contains_path_separators(self):
        """防目录穿越：推出来的名字只允许字母数字与 _-"""
        for url in ("https://a.b.c/../../evil", "https://x.com/%2e%2e%2fboom",
                    "https:///", "not a url at all", ""):
            nm = cs.derive_name(url)
            self.assertNotIn("/", nm)
            self.assertNotIn("\\", nm)
            self.assertNotIn("..", nm)
            self.assertTrue(nm)


class TestCaptureLocation(unittest.TestCase):
    def test_default_dir_is_outside_repo(self):
        """① 默认落运行期数据根——样本**不得**默认写进仓库"""
        d = Path(cs.capture_dir()).resolve()
        repo = ROOT.resolve()
        self.assertFalse(str(d).startswith(str(repo)), f"默认目录落在仓库内: {d}")

    def test_capture_writes_to_given_root(self):
        tmp = tempfile.mkdtemp(prefix="kiana_cap_")
        try:
            res = cs.capture("https://post.smzdm.com/p/x", root=tmp,
                             fetcher=lambda u, t, p: HTML_WITH_EMAIL.encode())
            self.assertTrue(res["ok"], res)
            self.assertTrue(os.path.exists(res["path"]))
            self.assertTrue(res["path"].startswith(tmp), "必须写到指定根，不许外溢")
            self.assertEqual(res["name"], "smzdm")
            self.assertTrue(res["path"].endswith("smzdm_sample.html"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_capture_does_not_touch_repo_assets(self):
        """采集默认**不得**在 tests/assets/ 留任何东西"""
        assets = ROOT / "tests" / "assets"
        before = sorted(os.listdir(assets))
        tmp = tempfile.mkdtemp(prefix="kiana_cap2_")
        try:
            cs.capture("https://post.smzdm.com/p/x", root=tmp,
                       fetcher=lambda u, t, p: b"<html><body><p>x</p></body></html>")
            self.assertEqual(sorted(os.listdir(assets)), before,
                             "默认采集不许改动 tests/assets/")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestSanitization(unittest.TestCase):
    def test_fixture_really_has_email(self):
        self.assertIn("user@example.com", HTML_WITH_EMAIL)

    def test_email_is_scrubbed_before_saving(self):
        tmp = tempfile.mkdtemp(prefix="kiana_cap3_")
        try:
            res = cs.capture("https://post.smzdm.com/p/x", root=tmp,
                             fetcher=lambda u, t, p: HTML_WITH_EMAIL.encode())
            self.assertTrue(res["ok"], res)
            saved = Path(res["path"]).read_text(encoding="utf-8")
            self.assertNotIn("user@example.com", saved, "落盘样本里仍有邮箱")
            self.assertIn("note", saved, "结构必须保留（抹了结构规则就没法写）")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestFailurePaths(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kiana_cap4_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_blocked_fetch_is_readable_and_writes_nothing(self):
        res = cs.capture("https://post.smzdm.com/p/x", root=self.tmp,
                         fetcher=lambda u, t, p: None)
        self.assertFalse(res["ok"])
        self.assertIn("闸", res["error"])
        self.assertEqual(os.listdir(self.tmp), [], "失败时不许留半截产物")

    def test_bad_scheme_rejected(self):
        for bad in ("ftp://x/y", "file:///etc/passwd", "", "javascript:alert(1)"):
            res = cs.capture(bad, root=self.tmp)
            self.assertFalse(res["ok"], f"{bad!r} 不该被接受")
        self.assertEqual(os.listdir(self.tmp), [])

    def test_fetcher_exception_never_raises(self):
        def boom(u, t, p):
            raise RuntimeError("connection reset")
        res = cs.capture("https://post.smzdm.com/p/x", root=self.tmp, fetcher=boom)
        self.assertFalse(res["ok"])
        self.assertIn("抓取异常", res["error"])

    def test_empty_body_is_readable_error(self):
        res = cs.capture("https://post.smzdm.com/p/x", root=self.tmp,
                         fetcher=lambda u, t, p: b"   ")
        self.assertFalse(res["ok"])
        self.assertIn("空", res["error"])


class TestInstall(unittest.TestCase):
    def test_install_copies_into_repo_assets(self):
        tmp = tempfile.mkdtemp(prefix="kiana_cap5_")
        name = "zzz_tmp_install_probe"
        try:
            res = cs.capture("https://post.smzdm.com/p/x", name=name, root=tmp,
                             fetcher=lambda u, t, p: b"<html><body><p>probe</p></body></html>")
            self.assertTrue(res["ok"], res)
            dst = cs.install(name, res["path"])
            try:
                self.assertTrue(os.path.exists(dst))
                self.assertIn("tests", dst)
                self.assertTrue(dst.replace("\\", "/").endswith(f"assets/{name}_sample.html"))
            finally:
                os.remove(dst)          # 测试不许在仓库留垃圾
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
