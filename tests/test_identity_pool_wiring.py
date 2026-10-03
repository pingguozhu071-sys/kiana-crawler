# -*- coding: utf-8 -*-
"""P1「按站身份池」首页开关 —— 六跳接线 + 身份库路径唯一性

## 为什么专门写这个测试文件

「身份池」这个功能**已经坏过一次**，而且坏得**完全没有症状**：

### 病一：六跳缺一跳（第 36 轮，历史）
`cookie_armory_enabled` 当时**缺 `gcfg` 那一跳**（`launcher_v8.EngineBridge`
的翻译表里没有这个键）→ 引擎侧代码全在、`DEFAULT_GLOBAL` 也有，
**但没有任何 GUI 路径能启用它**。用户看到的是一个"能填但永远不生效"的开关。

### 病二：存的和读的不是同一个库（v6 修）
```python
crawler.py    CookieArmory(self.project.get_db_path(), ...)
              # = <输出目录>\cli_<URL哈希>\frontier.db   ← 随首个 URL 变
cli.py        CookieArmory(ProjectIdentity(args.project).get_db_path(), ...)
              # = projects\default\frontier.db            ← 另一个文件
```
两条路**永远算不到同一个文件** ⇒ `cookie-add` 存进去的账号，
爬取**永远读不到**；而且爬取那份随 URL 变，**每个新 URL 都是一个空库**。
用户看到的是"存成功了、开关也开了，但抓取行为和没开一样"。

修法是给 `cookie_armory.armory_db_path()` 一个**唯一实现**
（用户级固定位置 `<data_root>/armory.db`），所有调用方都必须走它。

**这个文件的作用就是让这两种病都不可能悄悄复发。**
"""
import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PKG = ROOT / "kiana_vnext_plus"

KEY = "cookie_armory_enabled"

# 六跳：(说明, 文件, 必须出现的片段)
SIX_HOPS = [
    ("① 首页控件 create", "launcher_v9.py", "self.sw_armory = self._switch"),
    ("② _wire 接线", "launcher_v9.py", "sw_armory.checkedChanged.connect"),
    ("③ _start 的 cfg", "launcher_v9.py", 'cookie_armory_enabled": bool(h.sw_armory'),
    ("④ EngineBridge 翻译表", "launcher_v8.py", KEY),
    ("⑤ run_crawler 的 gcfg", "run_crawler.py", KEY),
    ("⑥ DEFAULT_GLOBAL 默认源", "kiana_vnext_plus/config.py", KEY),
]


class TestSixHopsWired(unittest.TestCase):
    """六跳必须**每一跳都在**——缺一跳就是"能填不生效"（第 36 轮原病）"""

    def test_all_six_hops_present(self):
        missing = []
        for label, rel, needle in SIX_HOPS:
            text = (ROOT / rel).read_text(encoding="utf-8")
            if needle not in text:
                missing.append(f"{label}（{rel} 里找不到 {needle!r}）")
        self.assertEqual(missing, [], "六跳接线缺跳（功能会静默失效）: " + "；".join(missing))

    def test_switch_defaults_off(self):
        """风险敏感特性必须**默认关**——用你的账号跑机器量级访问，要用户显式点头"""
        text = (ROOT / "launcher_v9.py").read_text(encoding="utf-8")
        self.assertIn('cfg.get("cookie_armory_enabled", False)', text,
                      "首页开关的默认值不是 False")
        cfg = (PKG / "config.py").read_text(encoding="utf-8")
        self.assertRegex(cfg, rf'"{KEY}"\s*:\s*False',
                         "DEFAULT_GLOBAL 里的默认值不是 False")

    def test_status_line_exists_and_is_refreshed(self):
        """状态行必须存在且被刷 —— 开着但库里空，界面要看得见（不许静默降级）"""
        text = (ROOT / "launcher_v9.py").read_text(encoding="utf-8")
        self.assertIn("self.armory_status", text, "没有身份池状态行")
        self.assertIn("_refresh_armory_status", text, "状态行没有被刷新")
        self.assertIn("库里没有任何身份", text,
                      "空库时必须明确提示——否则用户以为'开了就生效了'")


class TestArmoryPathIsSingleSource(unittest.TestCase):
    """身份库路径只能有**一个**实现，且必须与任务目录无关"""

    def test_helper_exists(self):
        from kiana_vnext_plus.cookie_armory import armory_db_path
        self.assertTrue(callable(armory_db_path))

    def test_path_is_user_level_not_task_level(self):
        """**核心**：必须是用户级固定位置，不能随任务/URL 变"""
        from kiana_vnext_plus.cookie_armory import armory_db_path
        p1 = armory_db_path()
        p2 = armory_db_path()
        self.assertEqual(p1, p2, "两次调用结果不同——说明还是算出来的动态路径")
        self.assertTrue(p1.endswith("armory.db"), f"落点不对: {p1}")
        # 不能落在某个任务的输出目录里
        self.assertNotIn("cli_", p1, "身份库落在任务目录里了——会随 URL 变，等于每次都是空库")

    def test_crawler_and_cli_use_the_helper(self):
        """两个**曾经各算各的**调用方，现在都必须走唯一实现"""
        crawler = (PKG / "crawler.py").read_text(encoding="utf-8")
        cli = (PKG / "cli.py").read_text(encoding="utf-8")
        self.assertIn("armory_db_path", crawler, "crawler 还在自己拼路径")
        self.assertIn("armory_db_path", cli, "cli 还在自己拼路径")
        # 不许再给 CookieArmory 传 project 的 db 路径
        self.assertNotRegex(crawler, r"CookieArmory\(\s*\n?\s*self\.project\.get_db_path\(\)",
                            "crawler 又把任务库路径传给身份库了")

    def test_gui_reads_the_same_db(self):
        """**界面数的库必须就是引擎读的库** —— 否则状态行是摆设"""
        gui = (ROOT / "launcher_v9.py").read_text(encoding="utf-8")
        tree = ast.parse(gui)
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "_refresh_armory_status"), None)
        self.assertIsNotNone(fn, "找不到 _refresh_armory_status")
        body = ast.unparse(fn)
        self.assertIn("armory_db_path", body, "状态行没走唯一路径实现，数的是别的库")
        # 真相：状态行必须用真实 API（list_accounts），不许猜方法名
        self.assertIn("list_accounts", body,
                      "状态行用的不是 CookieArmory 的真实方法（曾经猜成 list_identities → 永远走异常分支）")
        self.assertIn("_read_master_password", body,
                      "构造 CookieArmory 必须给 master password（少参数 → 永远抛异常）")


class TestSiteKeyIsNormalized(unittest.TestCase):
    """**存的键与查的键必须是同一个** —— 否则"存了却查不到"（真机踩过）

    真机病象：导入时存 `bilibili.com`，引擎抓取时查 `www.bilibili.com` →
    精确匹配失败 → 日志刷
    `[身份] 域 www.bilibili.com 不可用（not_provided）：该站未入库任何身份`
    —— 而库里**明明躺着**那条身份。整条身份池功能等于没做。
    """

    def test_subdomains_collapse_to_one_key(self):
        from kiana_vnext_plus.cookie_armory import normalize_site
        # 同一套 cookies 能同时用在这些子域上 → 必须归到同一个键
        for s in ("bilibili.com", "www.bilibili.com", "space.bilibili.com",
                  "m.bilibili.com", "player.bilibili.com", "WWW.Bilibili.COM"):
            self.assertEqual(normalize_site(s), "bilibili.com", f"{s} 没归一")

    def test_different_sites_stay_separate(self):
        """归一化**不许**把不同站点混成一个（那会让号互相串）"""
        from kiana_vnext_plus.cookie_armory import normalize_site
        self.assertEqual(normalize_site("www.douyin.com"), "douyin.com")
        self.assertEqual(normalize_site("tieba.baidu.com"), "tieba.baidu.com")
        self.assertNotEqual(normalize_site("www.douyin.com"),
                            normalize_site("www.bilibili.com"))

    def test_url_shaped_input(self):
        """整条 URL 也能归一（先剥协议再切路径 —— 反过来会得到 `https`）"""
        from kiana_vnext_plus.cookie_armory import normalize_site
        self.assertEqual(normalize_site("https://www.bilibili.com/video/BV1x"), "bilibili.com")
        self.assertEqual(normalize_site("www.bilibili.com:443"), "bilibili.com")

    def test_store_and_lookup_actually_match(self):
        """**端到端**：存 `bilibili.com`，用 `www.bilibili.com` 必须取得到"""
        import os
        import tempfile
        from kiana_vnext_plus.cookie_armory import CookieArmory
        db = os.path.join(tempfile.mkdtemp(), "armory.db")
        arm = CookieArmory(db, "test-master")
        self.assertTrue(arm.add_account("bilibili.com", "主号", "SESSDATA=x; bili_jct=y"))
        got = arm.acquire_identity("www.bilibili.com")
        self.assertEqual(type(got).__name__, "Acquired",
                         f"存了却查不到 —— 归一化没生效（拿到 {type(got).__name__}）")
        self.assertEqual(getattr(got, "name", ""), "主号")
        # 异站仍然取不到（不许因为归一化而串站）
        miss = arm.acquire_identity("www.douyin.com")
        self.assertEqual(type(miss).__name__, "Unavailable", "异站竟然取到了身份——串站了")

    def test_no_second_implementation_of_subdomain_stripping(self):
        """**不许有第二份归一化实现** —— GUI/CLI 都得走 `normalize_site`"""
        for rel in ("launcher_v9.py", "run_crawler.py"):
            src = (ROOT / rel).read_text(encoding="utf-8")
            self.assertNotRegex(
                src, r're\.sub\(r"\^\(www\|m\|mobile\|api\)',
                f"{rel} 里又自己写了一遍剥子域 —— 与 normalize_site 不一致就会回到'存了查不到'")
            self.assertIn("normalize_site", src, f"{rel} 没走唯一实现")


if __name__ == "__main__":
    unittest.main()
