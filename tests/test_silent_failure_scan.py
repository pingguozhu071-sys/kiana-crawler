# -*- coding: utf-8 -*-
"""热路径静默失败扫描回归（门禁第 12 项）

要锁死的：
  ① **写清理由的忽略 = 合法**（`pass  # 说明` 不计入）——否则这条纪律会逼着人
     把合理的忽略也改掉，反而伤代码；
  ② **不写理由的忽略 = 计入基线**，而基线**只许降不许升**；
  ③ 扫描**只在热路径**上生效（不误伤全仓其它位置）。
"""
import sys
import tempfile
import shutil
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import silent_failure_scan as sfs      # noqa: E402


def _write(tmp: Path, name: str, body: str) -> Path:
    p = tmp / name
    p.write_text(body, encoding="utf-8")
    return p


class TestScanFile(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="kiana_sfs_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_pass_without_reason_is_flagged(self):
        p = _write(self.tmp, "a.py", "try:\n    x()\nexcept Exception:\n    pass\n")
        self.assertEqual(len(sfs.scan_file(p)), 1)

    def test_pass_with_reason_comment_is_legal(self):
        """① 写清理由的忽略是工程实践，不是债——**不得**计入"""
        p = _write(self.tmp, "b.py",
                   "try:\n    x()\nexcept Exception:\n    pass  # 域名不是 IP 字面，交给 DNS\n")
        self.assertEqual(sfs.scan_file(p), [], "有理由注释的忽略不该被扫出来")

    def test_reason_comment_above_pass_also_counts(self):
        p = _write(self.tmp, "c.py",
                   "try:\n    x()\nexcept Exception:\n    # 清理路径，失败可忽略\n    pass\n")
        self.assertEqual(sfs.scan_file(p), [])

    def test_non_pass_body_is_not_flagged(self):
        """只扫 `pass`——`return None` / 记日志等形态语义不同，不在这里判"""
        p = _write(self.tmp, "d.py", "try:\n    x()\nexcept Exception:\n    return None\n")
        self.assertEqual(sfs.scan_file(p), [])

    def test_multiline_handler_body_is_not_pass_only(self):
        p = _write(self.tmp, "e.py",
                   "try:\n    x()\nexcept Exception:\n    pass\n    y()\n")
        self.assertEqual(sfs.scan_file(p), [])

    def test_unparsable_file_is_skipped_not_crashing(self):
        p = _write(self.tmp, "f.py", "def broken(:\n")
        self.assertEqual(sfs.scan_file(p), [])

    def test_bare_except_is_reported(self):
        p = _write(self.tmp, "g.py", "try:\n    x()\nexcept:\n    pass\n")
        hits = sfs.scan_file(p)
        self.assertEqual(hits[0][1], "BARE", "裸 except 更要报出来")


class TestCompare(unittest.TestCase):
    def test_increase_is_regression(self):
        cmp = sfs.compare({"a.py": [1]}, {"a.py": [[1, "E"], [2, "E"]]})
        self.assertEqual(cmp["worse"], [("a.py", 1, 2)])

    def test_decrease_is_improvement(self):
        cmp = sfs.compare({"a.py": [1, 2]}, {"a.py": [[1, "E"]]})
        self.assertEqual(cmp["worse"], [])
        self.assertEqual(cmp["better"], [("a.py", 2, 1)])

    def test_new_file_with_hits_is_regression(self):
        """新文件里的静默失败同样是新增——不能因为"基线里没有这个文件"就放过"""
        cmp = sfs.compare({}, {"new.py": [[1, "E"]]})
        self.assertEqual(cmp["worse"], [("new.py", 0, 1)])

    def test_cleared_file_is_improvement(self):
        cmp = sfs.compare({"a.py": [1]}, {})
        self.assertEqual(cmp["better"], [("a.py", 1, 0)])


class TestScopeAndBaseline(unittest.TestCase):
    def test_only_hot_files_are_scanned(self):
        """③ 只扫热路径——不误伤全仓其它位置"""
        for rel in sfs.HOT_FILES:
            self.assertTrue(rel.startswith("kiana_vnext_plus/"), rel)
        self.assertEqual(len(set(sfs.HOT_FILES)), len(sfs.HOT_FILES), "不得有重复项")

    def test_verified_legit_site_is_not_flagged(self):
        """实证：`url_utils` 里那条写了理由的忽略必须**不**被扫出来"""
        hits = sfs.scan_file(ROOT / "kiana_vnext_plus" / "url_utils.py")
        lines = {n for n, _ in hits}
        self.assertNotIn(197, lines, "197 行有理由注释（'交给 DNS 解析校验'），不该计入")

    def test_baseline_matches_current_tree(self):
        """基线必须与当前树一致——否则门禁会误报（要么假绿要么假红）"""
        cur = sfs.scan()
        base = sfs.load_baseline()
        self.assertTrue(base, "基线缺失——跑 --write-baseline")
        self.assertEqual(sfs.compare(base, cur)["worse"], [],
                         "当前树的静默失败数超过了基线")

    def test_baseline_is_checked_in_asset(self):
        self.assertTrue(sfs.BASELINE.exists())
        self.assertIn("tests", str(sfs.BASELINE))


if __name__ == "__main__":
    unittest.main()
