# -*- coding: utf-8 -*-
"""配置档取登录态的**纯逻辑**测试 —— **一个浏览器都不启动**（CI 里跑不了）。

## 这个文件为什么这么切

`cookie_profile` 里会开浏览器的只有一个函数（`open_profile_for_login`），
其余全是"把凭据放到正确位置、正确格式"的**纯逻辑**。CI 里没有浏览器，
但**格式错了才是真正会静默出事的地方** —— 所以这里全部火力对准逻辑：

1. **站点 → 配置档目录映射**（每站一档、不与日常浏览器同目录、仓库外）；
2. **cookies → Netscape 的转换**（`httpOnly` 驼峰改名的坑、分区 cookie、会话 `-1`）；
3. **storage_state 的读写与结构**（含 B站 `ac_time_value` 那类 localStorage）；
4. **多文件合并**；
5. **CLI 真能独立跑**（子进程跑 `--dry-run` / `--list-sites`，只测不开窗的路径）。

## 判据用"真解析器"，不是自己写断言

导出那一侧一律用 **yt-dlp 的 `YoutubeDLCookieJar`** 当裁判
（写法照 `tests/test_cookie_netscape_spec.py`）。自己写个 `assert "SESSDATA" in text`
是**假绿**：文件里出现这四个字母，和解析器能不能把它读回来，是两件事。

## 还钉了三条"源码级不变量"

配置档这套东西最容易在后续维护里被"顺手改坏"，
而改坏之后**测试全绿、真机静默失效**（本工程的老毛病）。故用 `ast` 直接钉住：
`headless` 不许变、不许出现自定义 UA、不许自己拼 cookies.txt 的列。
"""
import ast
import inspect
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MODULE_SRC = ROOT / "kiana_vnext_plus" / "cookie_profile.py"

# ── 假 cookie 数据：形状**照 `ctx.cookies()` 的真实返回**（驼峰 `httpOnly`）──
# 这里刻意用驼峰而不是 `write_netscape_cookies` 认的 `http_only`：
# **转换函数要处理的正是这个差异**，用驼峰喂进去才测得到真东西。
SESSDATA = {"name": "SESSDATA", "value": "AAA%2CBBB", "domain": ".bilibili.com",
            "path": "/", "expires": 1806300464.5, "httpOnly": True, "secure": True,
            "sameSite": "None"}
BILI_JCT = {"name": "bili_jct", "value": "deadbeef", "domain": ".bilibili.com",
            "path": "/", "expires": 0, "httpOnly": False, "secure": False,
            "sameSite": "Lax"}
# 会话 cookie：patchright 用 **-1** 表示"会话期"，不是 0
SESSION_CK = {"name": "sid", "value": "sess", "domain": ".douyin.com",
              "path": "/", "expires": -1, "httpOnly": True, "secure": True,
              "sameSite": "Lax"}


def _fake_state(cookies=None, ac_time_value="ac-time-token-123"):
    """造一份 `ctx.storage_state()` 形状的假数据（cookies + origins/localStorage）。"""
    return {
        "cookies": [dict(c) for c in (cookies if cookies is not None else [SESSDATA, BILI_JCT])],
        "origins": [{
            "origin": "https://www.bilibili.com",
            "localStorage": [
                # B站续期凭据就在这个键里 —— 手动导出的 cookies.txt **没有它**
                {"name": "ac_time_value", "value": ac_time_value},
                {"name": "buvid_fp_plain", "value": "abcdef"},
            ],
        }],
    }


def _ytdlp_read(path):
    """用 **yt-dlp 真正的解析器**读回 `{name: value}`；环境里没装就跳过。"""
    from yt_dlp.cookies import YoutubeDLCookieJar
    jar = YoutubeDLCookieJar(str(path))
    jar.load(ignore_discard=True, ignore_expires=True)
    return {c.name: c.value for c in jar}


def _tmp_root():
    return Path(tempfile.mkdtemp(prefix="kiana_profile_test_"))


# ══════════════════════════════════════════════════════════════════════
class TestSiteRegistry(unittest.TestCase):
    """站点枚举与"不许自创一套键"的约束。"""

    def test_registry_shape(self):
        from kiana_vnext_plus import cookie_profile as cp
        self.assertTrue(cp.SITE_PROFILES)
        for key, meta in cp.SITE_PROFILES.items():
            self.assertIn(".", key, f"键应是可注册域形态：{key}")
            for field in ("site_key", "label", "domains", "login_url", "login_cookies"):
                self.assertIn(field, meta, f"{key} 缺字段 {field}")
            self.assertTrue(str(meta["login_url"]).startswith("https://"),
                            f"{key} 的登录页必须是 https（登录动作不能被明文劫持）")
            self.assertIsInstance(meta["login_cookies"], tuple)

    def test_registry_keys_are_normalizer_stable(self):
        """登记表的键必须**归一后还是自己**，短名必须**不被归一改动**。

        这条是"不自创一套站点键"的判据：将来"按站身份池"要按站查东西时，
        两边的键不同族就会复现 `cookie_armory:130` 那个"存进去查不到"的坑
        （存的是 `bilibili.com`、查的是 `www.bilibili.com`）。

        ⚠️ **`site_key` 不要求等于 `normalize_site(键)`**（这里我第一版就写错了）：
        `normalize_site("bilibili.com")` 原样返回 `bilibili.com`（它是**可注册域**，
        无子域前缀可剥），而目录短名是 `bilibili` —— 两者本来就不该相等。
        真正要钉的是：`site_key` 经归一**不许被改掉**（写成 `www.bilibili` 就会
        被剥成 `bilibili`，将来和别的键撞车）。
        """
        from kiana_vnext_plus import cookie_profile as cp
        from kiana_vnext_plus.cookie_armory import normalize_site
        for key, meta in cp.SITE_PROFILES.items():
            self.assertEqual(normalize_site(key), key,
                             f"登记表键 {key} 不是归一稳定形态（会被 normalize_site 改写）")
            self.assertEqual(normalize_site(meta["site_key"]), meta["site_key"],
                             f"{key} 的 site_key={meta['site_key']} 带子域前缀，"
                             "归一时会被剥掉 → 与其他站撞车")

    def test_all_required_sites_present(self):
        """用户要抓的那 6 个平台一个都不能少（少一个就是功能没做完）"""
        from kiana_vnext_plus import cookie_profile as cp
        keys = {m["site_key"] for m in cp.SITE_PROFILES.values()}
        for want in ("bilibili", "douyin", "tieba", "kuaishou", "xiaohongshu", "wechat_mp"):
            self.assertIn(want, keys)

    def test_aliases_resolve_to_one_key(self):
        """**短名/域名/大小写/带路径都归到同一个键** —— 否则同一个站会有多份配置档"""
        from kiana_vnext_plus import cookie_profile as cp
        for alias in ("bilibili", "bilibili.com", "www.bilibili.com", "BILIBILI",
                      "https://www.bilibili.com/x", "https://bilibili.com/"):
            self.assertEqual(cp.resolve_site(alias), "bilibili.com", f"{alias} 没归一到 B站")
        self.assertEqual(cp.resolve_site("v.douyin.com"), "douyin.com")
        self.assertEqual(cp.resolve_site("tieba.baidu.com"), "tieba.baidu.com")
        self.assertEqual(cp.resolve_site("mp.weixin.qq.com"), "mp.weixin.qq.com")

    def test_unknown_site_raises_and_lists_options(self):
        """拼错的站名**必须报错**（而不是静默建个错误目录），且把可用站名列出来"""
        from kiana_vnext_plus import cookie_profile as cp
        with self.assertRaises(ValueError) as cm:
            cp.resolve_site("bilibli")
        msg = str(cm.exception)
        self.assertIn("bilibili", msg)
        with self.assertRaises(ValueError):
            cp.resolve_site("")
        with self.assertRaises(ValueError):
            cp.resolve_site("example.com")


class TestProfileDirectories(unittest.TestCase):
    """目录映射：每站一档、**不与日常浏览器同目录**、在仓库外。"""

    def test_per_site_directories_are_distinct(self):
        """**每站独立** —— 共用一个配置档会让会话/指纹互串，storage_state 也会混成一份"""
        from kiana_vnext_plus import cookie_profile as cp
        root = _tmp_root()
        seen = {}
        for meta in cp.site_options():
            d = cp.profile_dir(meta["key"], root=root)
            self.assertNotIn(str(d), seen, f"{d} 被两个站共用（{seen.get(str(d))} 与 {meta['key']}）")
            seen[str(d)] = meta["key"]
            self.assertEqual(d.name, meta["site_key"])
        self.assertEqual(len(seen), len(cp.SITE_PROFILES))

    def test_profile_lives_under_data_root_profiles(self):
        """配置档必须在**运行期数据根**的 `profiles/` 下 —— 那是仓库外的私有位置"""
        from kiana_vnext_plus import cookie_profile as cp
        from kiana_vnext_plus.config import data_root
        d = cp.profile_dir("bilibili")
        self.assertEqual(d.parent.parent, Path(data_root()))
        self.assertEqual(d.parent.name, "profiles")
        self.assertTrue(d.is_absolute())

    def test_never_touches_daily_browser_profile(self):
        """**红线**：绝不落在用户日常 Chrome 的 User Data 里

        Chrome 136 起 `--remote-debugging-*` 配默认用户数据目录会被忽略（防信息窃取），
        而且去连日常配置档本身就是在动用户的私人数据。
        """
        from kiana_vnext_plus import cookie_profile as cp
        low = str(cp.profile_dir("bilibili")).lower().replace("/", "\\")
        self.assertNotIn("google\\chrome\\user data", low)
        for bad in ("\\chrome\\user data", "\\microsoft\\edge\\user data",
                    "\\brave-browser\\user data"):
            self.assertNotIn(bad, low, f"配置档落到了日常浏览器目录：{low}")

    def test_create_makes_directory_and_is_idempotent(self):
        from kiana_vnext_plus import cookie_profile as cp
        root = _tmp_root()
        d1 = cp.profile_dir("bilibili", create=True, root=root)
        self.assertTrue(d1.is_dir())
        d2 = cp.profile_dir("bilibili", create=True, root=root)   # 再来一次不许炸
        self.assertEqual(d1, d2)

    def test_paths_are_derived_from_resolved_site(self):
        """三种写法算出的**必须是同一个文件**，否则 GUI/CLI 会各看各的"""
        from kiana_vnext_plus import cookie_profile as cp
        root = _tmp_root()
        a = cp.storage_state_path("bilibili", root=root)
        b = cp.storage_state_path("www.bilibili.com", root=root)
        c = cp.storage_state_path("bilibili.com", root=root)
        self.assertEqual(a, b)
        self.assertEqual(b, c)
        self.assertEqual(a.name, "storage_state.json")
        # 路径计算**不许建目录**（只算路径的函数不该有副作用）
        self.assertFalse(a.parent.exists())


class TestCookieConversion(unittest.TestCase):
    """`ctx.cookies()` → Netscape：**格式错了才是会静默出事的地方**。"""

    def test_httponly_rename_is_the_whole_point(self):
        """**决定性**：驼峰 `httpOnly` 必须变成 `http_only`，且写出 `#HttpOnly_` 前缀

        漏了改名 → B站的 SESSDATA 写成普通行 → 严格解析器按注释丢掉 →
        "文件看着是好的、服务端说未登录"。工程为此吃过 `code=-101`。
        """
        from kiana_vnext_plus import cookie_profile as cp
        rows = cp.cookies_for_netscape([SESSDATA, BILI_JCT])
        by_name = {r["name"]: r for r in rows}
        self.assertTrue(by_name["SESSDATA"]["http_only"], "httpOnly 没被改名成 http_only")
        self.assertFalse(by_name["bili_jct"]["http_only"])

    def test_ytdlp_reads_back_including_httponly(self):
        """导出物交给 **yt-dlp 真解析器**读回 —— 含 HttpOnly 的 SESSDATA 一条不少"""
        from kiana_vnext_plus import cookie_profile as cp
        root = _tmp_root()
        out = root / "cookies.txt"
        got_path = cp.export_cookies_from_state(_fake_state(), "bilibili", out=out)
        self.assertEqual(got_path, out)
        try:
            got = _ytdlp_read(out)
        except ImportError:
            self.skipTest("环境里没有 yt-dlp")
        self.assertEqual(got.get("SESSDATA"), "AAA%2CBBB", "HttpOnly 的 SESSDATA 丢了")
        self.assertEqual(got.get("bili_jct"), "deadbeef")

    def test_httponly_prefix_present_in_bytes(self):
        """字节级：`#HttpOnly_` 前缀必须在（curl 等严格解析器靠它）"""
        from kiana_vnext_plus import cookie_profile as cp
        root = _tmp_root()
        out = root / "cookies.txt"
        cp.export_cookies_from_state(_fake_state(), "bilibili", out=out)
        raw = out.read_text(encoding="utf-8")
        self.assertIn("#HttpOnly_.bilibili.com", raw)
        data = [ln for ln in raw.splitlines() if ln and not ln.startswith("#")]
        for ln in data:
            self.assertEqual(len(ln.split("\t")), 7, f"列数不是 7：{ln!r}")

    def test_partitioned_cookie_is_dropped(self):
        """分区(CHIPS) cookie **必须丢**：7 列格式表达不了分区语义，
        写出去等于把分区 cookie 降级泄漏给所有顶层请求。"""
        from kiana_vnext_plus import cookie_profile as cp
        chips = dict(BILI_JCT, name="chips_ck",
                     partitionKey={"topLevelSite": "https://example.com"})
        rows = cp.cookies_for_netscape([BILI_JCT, chips])
        self.assertEqual([r["name"] for r in rows], ["bili_jct"])

    def test_session_cookie_expires_passthrough(self):
        """会话 cookie 的 `-1` 在**本层**原样透传

        ⚠️ **这只覆盖中间层，不是最终文件。**
        写盘层（`cookie_utils.write_netscape_cookies`）会把它**归一成 `0`** ——
        因为 yt-dlp 只认 `[0-9]+`，`-1` 会让它
        `skipping cookie file entry due to invalid expires at -1` **整行跳过**。
        端到端那条在 `test_session_cookie_survives_to_file`。
        """
        from kiana_vnext_plus import cookie_profile as cp
        rows = cp.cookies_for_netscape([SESSION_CK])
        self.assertEqual(rows[0]["expires"], -1)
        self.assertEqual(rows[0]["path"], "/")
        self.assertTrue(rows[0]["http_only"])

    def test_session_cookie_survives_to_file(self):
        """**端到端**：会话 cookie 一路走到文件，且 yt-dlp 能读回

        这是补上"只测中间层"的缺口 —— 中间层保真（`-1`）+ 写盘层归一（`0`），
        两层合起来才对。只断言中间层的话，写盘层被改坏也发现不了。
        """
        from kiana_vnext_plus import cookie_profile as cp
        from kiana_vnext_plus.cookie_utils import write_netscape_cookies
        import tempfile, os
        rows = cp.cookies_for_netscape([SESSION_CK])
        d = tempfile.mkdtemp()
        p = os.path.join(d, "ck.txt")
        write_netscape_cookies(rows, p)
        raw = open(p, encoding="utf-8").read()
        # 写进文件的 expires 必须是非负整数
        for ln in raw.splitlines():
            if ln and not ln.startswith("#"):
                self.assertRegex(ln.split("\t")[4], r"^[0-9]+$",
                                 f"expires 不是非负整数，yt-dlp 会跳过该行: {ln!r}")
        try:
            from yt_dlp.cookies import YoutubeDLCookieJar
            jar = YoutubeDLCookieJar(p)
            jar.load(ignore_discard=True, ignore_expires=True)
            names = {c.name for c in jar}
        except ImportError:
            self.skipTest("环境里没有 yt-dlp")
        self.assertIn(SESSION_CK["name"], names,
                      "会话 cookie 在写盘后丢了 —— 多半是 expires=-1 没归一")

    def test_state_cookies_uses_same_conversion(self):
        """`storage_state()` 的 cookies 与 `ctx.cookies()` **同形** ⇒ 必须共用一份转换"""
        from kiana_vnext_plus import cookie_profile as cp
        self.assertEqual(cp.state_cookies(_fake_state()),
                         cp.cookies_for_netscape(_fake_state()["cookies"]))
        self.assertEqual(cp.state_cookies(None), [])

    def test_empty_state_writes_no_file(self):
        """一条 cookie 都没有时**不写空文件** —— 空壳文件会被引擎当成"配好了但啥都没有"，
        比"文件不存在"更难查（`universal_downloader.py:167-179` 记过同类误导）。"""
        from kiana_vnext_plus import cookie_profile as cp
        root = _tmp_root()
        out = root / "cookies.txt"
        self.assertIsNone(cp.export_cookies_from_state({"cookies": [], "origins": []},
                                                       "bilibili", out=out))
        self.assertFalse(out.exists())

    def test_export_generates_text_without_path(self):
        """`out=None` 只生成文本不落盘（给"看看里面有什么"这类用法留口子）"""
        from kiana_vnext_plus import cookie_profile as cp
        from kiana_vnext_plus.cookie_utils import write_netscape_cookies
        self.assertIsNone(cp.export_cookies_from_state(_fake_state(), "bilibili"))
        text = write_netscape_cookies(cp.state_cookies(_fake_state()))
        self.assertIn("SESSDATA", text)


class TestStorageState(unittest.TestCase):
    """`storage_state.json` 的读写与结构 —— **localStorage 是重点**。"""

    def test_roundtrip_preserves_local_storage(self):
        """**核心**：B站的 `ac_time_value` 必须能存下来、读回来

        它只存在于 localStorage —— 手动导出的 cookies.txt 里**根本没有它**。
        将来接续期接口就靠这个值，所以现在必须先留住。
        """
        from kiana_vnext_plus import cookie_profile as cp
        root = _tmp_root()
        state = _fake_state(ac_time_value="ac-time-token-123")
        p = cp.save_storage_state(state, "bilibili", root=root)
        self.assertIsNotNone(p)
        assert p is not None
        self.assertTrue(p.exists())
        back = cp.load_storage_state("bilibili", root=root)
        self.assertIsNotNone(back)
        ls = cp.local_storage_of(back, "https://www.bilibili.com")
        self.assertEqual(ls.get("ac_time_value"), "ac-time-token-123")
        self.assertEqual(ls.get("buvid_fp_plain"), "abcdef")
        # cookie 部分也要完整（含 HttpOnly 标记）
        names = {c["name"] for c in cp.state_cookies(back)}
        self.assertEqual(names, {"SESSDATA", "bili_jct"})
        self.assertTrue([c for c in back["cookies"] if c["name"] == "SESSDATA"][0]["httpOnly"])

    def test_file_is_private_and_valid_json(self):
        """凭据文件：POSIX 上权限必须收紧到 0600；内容必须是合法 JSON"""
        from kiana_vnext_plus import cookie_profile as cp
        root = _tmp_root()
        p = cp.save_storage_state(_fake_state(), "bilibili", root=root)
        assert p is not None
        json.loads(p.read_text(encoding="utf-8"))          # 坏 JSON 会在这里炸
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600,
                             "配置档里的凭据文件权限没收紧")

    def test_unicode_local_storage_survives(self):
        """localStorage 里出现中文/需要转义的值，读写不许乱码"""
        from kiana_vnext_plus import cookie_profile as cp
        root = _tmp_root()
        cp.save_storage_state(_fake_state(ac_time_value="中文值/换行\\n 与\"引号\""),
                              "bilibili", root=root)
        back = cp.load_storage_state("bilibili", root=root)
        self.assertEqual(cp.local_storage_of(back, "https://www.bilibili.com")["ac_time_value"],
                         "中文值/换行\\n 与\"引号\"")

    def test_atomic_write_leaves_no_tmp(self):
        """原子替换：不能留下 `.tmp` 残骸（否则下次读到半截 JSON）"""
        from kiana_vnext_plus import cookie_profile as cp
        root = _tmp_root()
        p = cp.save_storage_state(_fake_state(), "bilibili", root=root)
        assert p is not None
        leftovers = list(p.parent.glob("*.tmp"))
        self.assertEqual(leftovers, [])
        self.assertEqual(list(p.parent.glob("storage_state.json")), [p])

    def test_missing_state_returns_none(self):
        from kiana_vnext_plus import cookie_profile as cp
        self.assertIsNone(cp.load_storage_state("bilibili", root=_tmp_root()))

    def test_corrupt_state_returns_none_not_raise(self):
        """坏文件 → `None` + 告警，**不抛异常**（调用方两种局面都得重新登录）"""
        from kiana_vnext_plus import cookie_profile as cp
        root = _tmp_root()
        p = cp.storage_state_path("bilibili", root=root)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{ 这不是 JSON", encoding="utf-8")
        self.assertIsNone(cp.load_storage_state("bilibili", root=root))
        p.write_text('["顶层不是对象"]', encoding="utf-8")
        self.assertIsNone(cp.load_storage_state("bilibili", root=root))

    def test_empty_state_not_persisted(self):
        """空 state 不落盘 —— 写空壳会让 `--status` 把"从没登录过"报成"有配置档" """
        from kiana_vnext_plus import cookie_profile as cp
        root = _tmp_root()
        self.assertIsNone(cp.save_storage_state({}, "bilibili", root=root))
        self.assertIsNone(
            cp.save_storage_state({"cookies": [], "origins": []}, "bilibili", root=root))
        self.assertFalse(cp.storage_state_path("bilibili", root=root).exists())

    def test_export_profile_cookies_from_saved_state(self):
        """`--export-only` 走的那条：**不开浏览器**，从已存配置档重导 cookies.txt"""
        from kiana_vnext_plus import cookie_profile as cp
        root = _tmp_root()
        self.assertIsNone(cp.export_profile_cookies("bilibili", root=root))   # 还没登录过
        cp.save_storage_state(_fake_state(), "bilibili", root=root)
        got = cp.export_profile_cookies("bilibili", root=root)
        self.assertIsNotNone(got)
        assert got is not None
        self.assertTrue(got.exists())
        self.assertEqual(got.name, "cookies.txt")
        self.assertEqual(got.parent, cp.profile_dir("bilibili", root=root))


class TestMergeCookieFiles(unittest.TestCase):
    """合并：yt-dlp / `_ensure_cookie_file()` 最终只吃**一个**文件。"""

    def _write(self, root, name, cookies):
        from kiana_vnext_plus.cookie_utils import write_netscape_cookies
        p = root / name
        write_netscape_cookies(cookies, str(p))
        return p

    def test_merge_dedupes_by_domain_and_name_last_wins(self):
        """去重键 `(domain, name)`，**后面的覆盖前面的**（与 `_ensure_cookie_file` 同约定）"""
        from kiana_vnext_plus import cookie_profile as cp
        root = _tmp_root()
        a = self._write(root, "a.txt", [
            {"name": "SESSDATA", "value": "old", "domain": ".bilibili.com",
             "path": "/", "secure": True, "http_only": True, "expires": 0}])
        b = self._write(root, "b.txt", [
            {"name": "SESSDATA", "value": "new", "domain": ".bilibili.com",
             "path": "/", "secure": True, "http_only": True, "expires": 0},
            {"name": "sessionid", "value": "dy", "domain": ".douyin.com",
             "path": "/", "secure": True, "http_only": True, "expires": 0}])
        text, n = cp.merge_cookie_files([a, b])
        self.assertEqual(n, 2, "同 (domain,name) 应当只留一条")
        self.assertIn("new", text)
        self.assertNotIn("old", text)

    def test_merge_preserves_httponly_and_is_readable_by_ytdlp(self):
        """合并**不许把 HttpOnly 前缀摘掉** —— 那是每一步都容易掉的登录态"""
        from kiana_vnext_plus import cookie_profile as cp
        root = _tmp_root()
        a = self._write(root, "a.txt", [
            {"name": "SESSDATA", "value": "v1", "domain": ".bilibili.com",
             "path": "/", "secure": True, "http_only": True, "expires": 0}])
        b = self._write(root, "b.txt", [
            {"name": "BDUSS", "value": "v2", "domain": ".tieba.baidu.com",
             "path": "/", "secure": True, "http_only": True, "expires": 0}])
        out = root / "all.txt"
        text, n = cp.merge_cookie_files([a, b], out=out)
        self.assertIn("#HttpOnly_.bilibili.com", text)
        self.assertIn("#HttpOnly_.tieba.baidu.com", text)
        try:
            got = _ytdlp_read(out)
        except ImportError:
            self.skipTest("环境里没有 yt-dlp")
        self.assertEqual(got, {"SESSDATA": "v1", "BDUSS": "v2"})

    def test_merge_skips_unreadable_file(self):
        """读不到的文件跳过并告警，**不炸**（少一份不该毁掉整次合并）"""
        from kiana_vnext_plus import cookie_profile as cp
        root = _tmp_root()
        good = self._write(root, "a.txt", [
            {"name": "X", "value": "1", "domain": ".a.com", "path": "/",
             "secure": False, "http_only": False, "expires": 0}])
        text, n = cp.merge_cookie_files([root / "missing.txt", good])
        self.assertEqual(n, 1)
        self.assertIn("X", text)

    def test_merge_empty_input(self):
        from kiana_vnext_plus import cookie_profile as cp
        text, n = cp.merge_cookie_files([])
        self.assertEqual(n, 0)
        self.assertIn("# Netscape HTTP Cookie File", text)


class TestSourceLevelInvariants(unittest.TestCase):
    """**源码级**不变量：这几条坏掉之后测试全绿、真机静默失效。

    ⚠️ 教训（`test_cookie_domain_match_safety.py:84-89`）：断言源码时**只看真正的代码**
    —— 用 `ast.unparse`（天然不含注释），否则注释里出现的字样会把测试骗红。
    """

    @staticmethod
    def _tree():
        return ast.parse(MODULE_SRC.read_text(encoding="utf-8"))

    def test_headless_is_false_and_hardcoded(self):
        """登录必须**有头**：`headless=False` 写死，且不许出现任何 `headless=True`"""
        found = []
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "launch_persistent_context":
                found.append(node)
        self.assertTrue(found, "没找到 launch_persistent_context —— 函数被改名/重构了？")
        for call in found:
            kw = {k.arg: k.value for k in call.keywords if k.arg}
            self.assertIn("headless", kw, "launch_persistent_context 没显式给 headless")
            self.assertIsInstance(kw["headless"], ast.Constant)
            self.assertIs(kw["headless"].value, False, "登录窗口被改成无头了")
            # 只允许这三个参数：多出来的都是"自定义指纹"的开端
            self.assertEqual(set(kw), {"channel", "headless"},
                             f"出现了多余参数（自定义 UA/headers 会与 TLS 指纹失配）：{set(kw)}")

    def test_no_custom_user_agent_or_headers(self):
        """反检测场景**不许自定义 UA / headers**（会与 TLS 指纹对不上，反而暴露）"""
        src = ast.unparse(self._tree())
        for bad in ("user_agent", "extra_http_headers", "userAgent"):
            self.assertNotIn(bad, src, f"源码里出现了 {bad} —— 自定义指纹是反向优化")

    def test_uses_shared_netscape_writer(self):
        """cookies.txt **只许**由 `cookie_utils.write_netscape_cookies` 写出。

        自己拼 `"\\t".join(...)` 就会丢掉 7 列规格 / CRLF / `#HttpOnly_` 前缀
        —— 那正是本工程吃过 `code=-101` 的那个坑。
        """
        called = set()
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                called.add(node.func.id)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                called.add(node.func.attr)
        self.assertIn("write_netscape_cookies", called, "没有走共享的 netscape 写出函数")
        # 用**正则**而不是 `assertNotIn('"\\t".join', ...)`：后者只认双引号写法，
        # 换成单引号 `'\t'.join` 就漏过去了（"拿文本当结构"的又一变体）。
        self.assertNotRegex(ast.unparse(self._tree()), r"""["']\\t["']\s*\.\s*join""",
                            "自己拼 tab 分隔行了 —— 格式会漂，必须走共享写出函数")

    def test_no_bilibili_refresh_api(self):
        """**明令不做**：不许出现 B站 cookie 续期接口（法律风险未决）。

        钉住它是为了防"下一轮顺手接上"——那条线上的接口调用会改动用户账号状态。

        ⚠️ **判据是"代码里的字符串字面量"，明确排除 docstring**：
        docstring 在 AST 里**也是 `ast.Constant`**（我把取字面量当成了"排除注释"，
        第一版这里就是这么误判的）。而模块 docstring 正好在解释
        "为什么现在不接续期、但要先留住 `refresh_token`" ——
        不排除它，**正确状态会判红**。这与本工程
        `test_cookie_domain_match_safety.py:84-89` 记下的坑是同一个，
        我在这里又踩了一次。
        """
        tree = self._tree()
        doc_nodes = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                body = getattr(node, "body", None) or []
                if body and isinstance(body[0], ast.Expr) \
                        and isinstance(body[0].value, ast.Constant) \
                        and isinstance(body[0].value.value, str):
                    doc_nodes.add(id(body[0].value))
        lits = [n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and id(n) not in doc_nodes]
        self.assertTrue(lits, "没取到任何字面量 —— 判据失效了（先修测试）")
        blob = "\n".join(lits)
        for bad in ("refresh_token", "CookieRefresh", "cookie/refresh",
                    "SESSDATA_refresh", "x/web-interface/coin"):
            self.assertNotIn(bad, blob,
                             f"代码里出现了续期相关字面量 {bad} —— 本模块明确不接续期接口")

    def test_launch_kwargs_exist_in_real_signature(self):
        """传给 `launch_persistent_context` 的关键字**必须真实存在于该 API 签名里**。

        `headless` 拼成 `headles` 这类错，AST 守卫看不出来（它只查字面量），
        而**真机上会直接 TypeError** —— 那正是"没启动过浏览器就交付"最可能翻的车。
        这里用**真实签名**（`inspect.signature`）当裁判，把参数名背错挡在离线测试里。

        patchright 不在环境里就跳过（纯逻辑测试不该硬依赖浏览器引擎）。
        """
        try:
            from patchright.async_api import BrowserType
        except ImportError:
            self.skipTest("环境里没有 patchright")
        params = set(inspect.signature(BrowserType.launch_persistent_context).parameters)
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "launch_persistent_context":
                for k in node.keywords:
                    if k.arg is None:
                        continue      # **kwargs 展开，无从静态检查
                    self.assertIn(k.arg, params,
                                  f"{k.arg!r} 不是 launch_persistent_context 的参数 "
                                  f"（真机会 TypeError）；可用：{sorted(params)}")

    def test_importable_without_browser_engine(self):
        """模块**必须能在没装浏览器引擎时 import**（CI 就是这种环境）"""
        code = ("import sys;"
                "sys.modules['patchright']=None;sys.modules['playwright']=None;"
                f"sys.path.insert(0,{str(ROOT)!r});"
                "import kiana_vnext_plus.cookie_profile as cp;"
                "print(cp.HAS_PATCHRIGHT, cp.HAS_PLAYWRIGHT, len(cp.SITE_PROFILES))")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, timeout=120)
        self.assertEqual(r.returncode, 0, f"模块在无浏览器引擎时 import 失败：{r.stderr}")


class TestCliEntryPoint(unittest.TestCase):
    """CLI **必须能独立跑通**（不开浏览器的路径）—— 否则"能自测"是空话。"""

    CLI = ROOT / "tools" / "cookie_login.py"

    def _run(self, *args, env=None):
        e = dict(os.environ)
        if env:
            e.update(env)
        return subprocess.run([sys.executable, str(self.CLI), *args],
                              capture_output=True, text=True, timeout=180,
                              cwd=str(ROOT), env=e, encoding="utf-8", errors="replace")

    def test_list_sites_lists_all_six(self):
        r = self._run("--list-sites")
        self.assertEqual(r.returncode, 0, r.stderr)
        for want in ("bilibili", "douyin", "tieba", "kuaishou", "xiaohongshu", "wechat_mp"):
            self.assertIn(want, r.stdout)

    def test_dry_run_prints_paths_without_browser(self):
        """`--dry-run` 是"能不能自测"的关键：走完整解析、**只不开窗**"""
        root = _tmp_root()
        r = self._run("--site", "bilibili", "--dry-run",
                      env={"LOCALAPPDATA": str(root)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("passport.bilibili.com", r.stdout)
        self.assertIn("storage_state.json", r.stdout)
        self.assertIn("cookies.txt", r.stdout)
        self.assertIn("--dry-run：不启动浏览器", r.stdout)
        self.assertIn(str(root), r.stdout, "dry-run 报的路径没跟着数据根走")
        # dry-run **不许建目录**（只解析路径，不该有副作用）
        self.assertFalse((root / "KianaVnextPlus" / "profiles").exists(),
                         "dry-run 居然建了配置档目录")

    def test_unknown_site_exits_2(self):
        r = self._run("--site", "bilibli", "--dry-run")
        self.assertEqual(r.returncode, 2, f"未知站名应当退出 2：{r.stdout}{r.stderr}")
        self.assertIn("bilibili", r.stderr)

    def test_export_only_without_profile_exits_1(self):
        """没有配置档时 `--export-only` 退出 1，并提示先去登录"""
        root = _tmp_root()
        r = self._run("--site", "bilibili", "--export-only",
                      env={"LOCALAPPDATA": str(root)})
        self.assertEqual(r.returncode, 1, f"{r.stdout}{r.stderr}")
        self.assertIn("先跑一次", r.stderr)

    def test_export_only_end_to_end_offline(self):
        """**端到端（离线）**：预置一份 storage_state → CLI 真的导出 cookies.txt

        这条是"CLI 独立跑得通"的最强证据：不碰浏览器，但走完了
        站点解析 → 配置档定位 → 读 state → 写 cookies.txt 的**全链路**，
        并且用的是**子进程 + 隔离的 LOCALAPPDATA**（不污染用户的真实数据根，
        也不给仓库留 KianaData/）。
        """
        from kiana_vnext_plus import cookie_profile as cp
        root = _tmp_root()
        # 先在"隔离数据根"里放一份假配置档：LOCALAPPDATA 决定 data_root()，
        # 而 data_root() 不在便携模式下就是 %LOCALAPPDATA%\KianaVnextPlus。
        real_la = os.environ.get("LOCALAPPDATA")
        os.environ["LOCALAPPDATA"] = str(root)
        try:
            cp.save_storage_state(_fake_state(), "bilibili")
            expect_dir = root / "KianaVnextPlus" / "profiles" / "bilibili"
            self.assertTrue(cp.storage_state_path("bilibili").parent == expect_dir,
                            "隔离数据根没生效 —— 这条测试会去碰真实数据根")
        finally:
            if real_la is None:
                os.environ.pop("LOCALAPPDATA", None)
            else:
                os.environ["LOCALAPPDATA"] = real_la

        r = self._run("--site", "bilibili", "--export-only",
                      env={"LOCALAPPDATA": str(root)})
        self.assertEqual(r.returncode, 0, f"{r.stdout}{r.stderr}")
        out = expect_dir / "cookies.txt"
        self.assertTrue(out.exists(), "CLI 说成功了但文件不在")
        try:
            got = _ytdlp_read(out)
        except ImportError:
            self.skipTest("环境里没有 yt-dlp")
        self.assertEqual(got.get("SESSDATA"), "AAA%2CBBB")
        self.assertIn("#HttpOnly_.bilibili.com", out.read_text(encoding="utf-8"))

    def test_help_works(self):
        r = self._run("--help")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--site", r.stdout)
        self.assertIn("--export-only", r.stdout)



class TestWindowCloseStillExports(unittest.TestCase):
    """**真机踩到的致命 bug 的回归测试**：用户关窗口后必须仍能导出。

    ## 现象（用户 2026-10-03 实测）
    扫码登录成功、主页也进去了、按提示关了窗口 →
    **界面说"没有正常获取到"**，而配置档目录里
    **`cookies.txt` 与 `storage_state.json` 都不存在**。

    ## 根因
    原实现的轮询循环**唯一的跳出条件就是"用户关掉了窗口"**，
    而跳出**之后**才去读一次 `ctx.cookies()` ——
    那一刻 context **已经死了**，抛异常被 `except` 吞成 `cookies = []`
    ⇒ `if cookies else None` 让导出整段跳过。

    ## 为什么旧测试没抓住
    假的 context **不会在"关窗"时把自己变死** ——
    所以"关窗后读 cookie"在那个假环境下**恰好能读到**。
    **测试模型比现实宽松，就测不出真机的失败。**
    本类补一个"关窗即死"的假 context。
    """

    def test_snapshot_taken_while_alive_not_after_close(self):
        """**核心断言**：源码必须在**判活之前**读快照

        这是**结构性**断言（AST），因为"读的时机"在运行时不表现为返回值差异 ——
        旧实现与新实现都会返回一个 result，**只有真机会不一样**。
        所以钉住结构：`ctx.cookies()` 的调用必须出现在 `_context_alive` 的
        **同一次循环迭代里、且在它之前**。
        """
        import ast
        from pathlib import Path as _P
        src = _P(cp_file()).read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = None
        for n in ast.walk(tree):
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "open_profile_for_login":
                fn = n
        self.assertIsNotNone(fn, "找不到 open_profile_for_login")
        # ⚠️ 我第一版这里写的是"读 cookies 的位置必须早于判活的位置"——
        # **那是错的**：正确结构本来就是
        #     `if _context_alive(ctx): last_cookies = await ctx.cookies()`
        # 即**判活在前、读取在它的 if 体内**（活着才读）。
        # 真正的不变量是：**读快照必须发生在轮询循环【内部】，
        # 而不是像出 bug 那版一样在循环【之后】才读一次。**
        def _walks(node):
            return {id(x) for x in ast.walk(node)}

        loops = [n for n in ast.walk(fn) if isinstance(n, ast.While)]
        self.assertTrue(loops, "找不到轮询循环 —— 结构变了？")
        in_loop = set()
        for lp in loops:
            in_loop |= _walks(lp)
        # 找函数里所有的 `ctx.cookies()` 调用节点
        reads = [n for n in ast.walk(fn)
                 if isinstance(n, ast.Call)
                 and ast.unparse(n.func).endswith("ctx.cookies")]
        self.assertTrue(reads, "找不到读 cookies 的调用")
        inside = [r for r in reads if id(r) in in_loop]
        self.assertTrue(
            inside,
            "**没有任何一次 `ctx.cookies()` 在轮询循环里** —— "
            "那正是真机 bug：等到跳出循环（=用户关窗、context 已死）才去读，"
            "读出来是空，导出整段被跳过")
        body = ast.unparse(fn)
        self.assertIn("last_cookies", body, "没有轮询期快照变量 —— bug 会复发")
        self.assertIn("last_state", body, "没有轮询期快照变量 —— bug 会复发")
        # 导出必须用快照，不许又去读一次活的 context 当主路径
        # ⚠️ 不要写精确字符串 —— `ast.unparse` 会给元组加括号
        # （实测它输出的是 `cookies, state = (last_cookies, last_state)`），
        # 精确匹配会因为一个括号而假红。这里只要求"赋值右值里出现快照变量"。
        import re as _re
        self.assertTrue(
            _re.search(r"cookies,\s*state\s*=\s*\(?\s*last_cookies", body),
            "导出没用轮询期快照 —— 关窗后又是空的")


def cp_file():
    from pathlib import Path as _P
    return str(_P(__file__).resolve().parent.parent
               / "kiana_vnext_plus" / "cookie_profile.py")

if __name__ == "__main__":
    unittest.main()