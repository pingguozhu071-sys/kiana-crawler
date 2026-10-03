# -*- coding: utf-8 -*-
"""「登录并取 cookies」按钮的**纯逻辑 + 接线**测试 —— 一个浏览器都不启动、一个窗口都不开。

## 为什么这么切

这条链最容易出的事故**不是"没弹出浏览器"，而是"没登录成功却报了成功"**：

`cookie_profile.open_profile_for_login` 的 `ok` 只表示"写出了 cookies.txt"
（它自己的 docstring 写明：`login_cookies` 只用于提前收工，**不是导出闸门**），
而**未登录也会有匿名 cookie**（buvid 之类）⇒ 文件非空、ok=True。
**只看 ok 就会谎报** —— 这正是本工程"用无关检查冒充成功"的老毛病。
所以本文件把火力对准"结论怎么算"：

1. 站点选项**来自登记表**（不许自己编一套中文名）；
2. 登录凭据核验的**三态**（有 / 明确没有 / 不猜）；
3. 结果措辞：**"文件写出来了但里面没有登录凭据"必须判失败**（核心断言）；
4. Cookies 框回填的顺序（引擎合并是**先到先得**，新值必须排最前才盖得住旧值）；
5. 后台桥：真起线程、真起独立 asyncio 事件循环、真投递；**异常也必须有结论**
   （否则界面会永远停在"等待登录中"，比报错更糟）。

## 判据上的两条纪律

- **只注入假协程**，绝不调用 `open_profile_for_login`（它会弹有头浏览器等用户）。
- 另有一组 **AST 断言**（照 `tests/test_identity_gui_wiring.py` 的样式）钉住接线：
  **没有接线 = 功能不存在**，而"界面能开、点了没反应"在本工程是反复出现过的事故。

## [v7] 这一轮 GUI 重排后本文件改了什么

登录那一整套（下拉 / 按钮 / 状态行 / 自定义登录页）**从首页搬到了设置页的
「Cookies 管理」卡**，于是：
- `TestLoginButtonWiring` 里"控件建在首页"的那条改成断言**建在设置页**，
  并**加一条反向断言**：首页**不许**再有 `login_btn` ——
  否则就是"按钮搬走了、旧的还在"，那比不搬更乱；
- 其余判定/措辞/桥的断言**一条都没动**（这是硬要求：诚实性机制不许为了界面好看放松）。
"""
import ast
import asyncio
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Qt/依赖缺失的环境里跳过逻辑测试（CI 装了 PySide6，本机也有）；AST 断言那组不依赖导入。
try:
    import launcher_v9 as gui
    _IMPORT_ERR = ""
except Exception as _e:                                     # pragma: no cover - 取决于环境
    gui = None
    _IMPORT_ERR = f"{type(_e).__name__}: {_e}"

_NEEDS_GUI = unittest.skipIf(gui is None, f"launcher_v9 不可导入（{_IMPORT_ERR}）")


# ══════════════════════════════════════════════════════════════════════
# 构造假 cookie 文件用的最小形状（键名照 `write_netscape_cookies` 认的那套）
# ══════════════════════════════════════════════════════════════════════
def _ck(name, domain=".bilibili.com", value="v", http_only=False):
    return {"name": name, "value": value, "domain": domain, "path": "/",
            "secure": True, "http_only": http_only, "expires": 0}


def _write_cookies(rows) -> Path:
    """用**唯一的写出实现**造一份 cookies.txt（不自己拼列，免得测的是拼串而不是解析）。

    `http_only=True` 的行会带 `#HttpOnly_` 前缀 —— B站的 `SESSDATA` 正是这种，
    而严格解析器**会把没有该前缀的注释行整行丢掉**（工程为此吃过 code=-101），
    所以这里顺带把那条老坑也覆盖上了。
    """
    from kiana_vnext_plus.cookie_utils import write_netscape_cookies
    d = Path(tempfile.mkdtemp(prefix="kiana_login_test_"))
    p = d / "cookies.txt"
    write_netscape_cookies(list(rows), path=str(p))
    return p


@_NEEDS_GUI
class TestSiteChoices(unittest.TestCase):
    """站点下拉：名字必须**来自登记表**，且至少要能选 B站。"""

    def test_choices_match_registry(self):
        from kiana_vnext_plus import cookie_profile as cp
        opts = cp.site_options()
        got = gui.login_site_choices()
        self.assertEqual([o["key"] for o in got], [o["key"] for o in opts],
                         "下拉项与站点登记表不同源/不同序")
        self.assertEqual([o["label"] for o in got], [o["label"] for o in opts],
                         "下拉里的中文名不是登记表里的那份（自己编了一套？）")

    def test_bilibili_is_selectable(self):
        keys = {o["site_key"] for o in gui.login_site_choices()}
        self.assertIn("bilibili", keys, "至少要能选 B站")

    def test_index_to_site_key_mapping_and_bounds(self):
        """下拉索引 → 站点键：6 项全对，越界一律空串（空串 = 界面上什么都不做）。"""
        choices = gui.login_site_choices()
        for i, o in enumerate(choices):
            self.assertEqual(gui.login_site_key_at(choices, i), o["key"])
        for bad in (-1, len(choices), 999, None, "abc", 1.9):
            self.assertEqual(gui.login_site_key_at(choices, bad), "",
                             f"{bad!r} 越界/非法时必须返回空串（拿错站去建目录写 cookies 更糟）")
        self.assertEqual(gui.login_site_key_at([], 0), "")
        self.assertEqual(gui.login_site_key_at(None, 0), "")

    def test_display_name_never_invents_a_name(self):
        from kiana_vnext_plus import cookie_profile as cp
        want = cp.SITE_PROFILES["bilibili.com"]["label"]
        for raw in ("bilibili", "bilibili.com", "www.bilibili.com"):
            self.assertEqual(gui.site_display_name(raw), want)
        # 不认识的站点 → 原样返回输入，**不编名字**（编了就会拿错站去建目录写 cookies）
        self.assertEqual(gui.site_display_name("不存在.example"), "不存在.example")
        self.assertEqual(gui.site_display_name(""), "")


@_NEEDS_GUI
class TestVerifyLoginCookie(unittest.TestCase):
    """产物核验的三态 —— 本功能的"诚实性"就落在这一层。"""

    def test_true_when_login_cookie_present(self):
        p = _write_cookies([_ck("SESSDATA", value="secret", http_only=True), _ck("bili_jct")])
        self.assertIs(gui.verify_login_cookie("bilibili.com", p), True)

    def test_false_when_only_anonymous_cookies(self):
        """**核心**：文件非空（匿名 cookie 也在）但登录凭据不在 → 明确判否，不是"不猜"。"""
        p = _write_cookies([_ck("buvid3"), _ck("b_nut"), _ck("_uuid")])
        self.assertIs(gui.verify_login_cookie("bilibili.com", p), False)

    def test_false_when_name_matches_but_domain_does_not(self):
        """同名 cookie 出现在无关域上不算登录成功（域必须沾边）。"""
        p = _write_cookies([_ck("SESSDATA", domain=".evil.example")])
        self.assertIs(gui.verify_login_cookie("bilibili.com", p), False)

    def test_false_for_header_only_file(self):
        from kiana_vnext_plus.cookie_utils import write_netscape_cookies
        d = Path(tempfile.mkdtemp(prefix="kiana_login_test_"))
        p = d / "cookies.txt"
        write_netscape_cookies([], path=str(p))          # 只有注释头
        self.assertIs(gui.verify_login_cookie("bilibili.com", p), False)

    def test_false_when_login_cookie_value_is_a_logout_placeholder(self):
        """登出后服务端留的是 `SESSDATA=deleted`（或空值）：**不算登录态**。

        只看"名字+域"会把"已登出"报成"✅ 已就位" —— 这是本层唯一一处能把失败说成
        成功的出口（对抗性审查点名过），故显式钉死。
        """
        for bad in ("deleted", "DELETED", "", "   "):
            p = _write_cookies([_ck("SESSDATA", value=bad, http_only=True)])
            self.assertIs(gui.verify_login_cookie("bilibili.com", p), False,
                          f"SESSDATA={bad!r} 不该被当成有效登录凭据")

    def test_none_when_site_has_no_registered_login_cookie(self):
        """登记表没登记登录凭据名的站点（快手/公众号）→ **不猜**（None），不冒充 False。"""
        from kiana_vnext_plus import cookie_profile as cp
        unknown = [k for k, m in cp.SITE_PROFILES.items() if not m.get("login_cookies")]
        if not unknown:
            self.skipTest("登记表里每个站都登记了登录凭据名")
        p = _write_cookies([_ck("whatever")])
        self.assertIsNone(gui.verify_login_cookie(unknown[0], p))

    def test_none_when_file_missing_or_no_path(self):
        self.assertIsNone(gui.verify_login_cookie("bilibili.com", None))
        self.assertIsNone(gui.verify_login_cookie("bilibili.com", ""))
        self.assertIsNone(gui.verify_login_cookie("bilibili.com", "/no/such/file.txt"))


@_NEEDS_GUI
class TestSummarizeLoginResult(unittest.TestCase):
    """结果 → 界面措辞。**没有任何一条分支允许把"没登录"说成"成功"。**"""

    OK = {"ok": True, "status": "ok", "site": "bilibili.com", "cookie_count": 12,
          "cookies_path": r"C:\Users\x\AppData\Local\KianaVnextPlus\profiles\bilibili\cookies.txt"}

    def test_verified_success_reports_path_and_count(self):
        s = gui.summarize_login_result(self.OK, True)
        self.assertTrue(s["ok"])
        self.assertEqual(s["level"], "success")
        self.assertIn("12", s["headline"])
        self.assertIn(self.OK["cookies_path"], s["headline"], "成功时必须说清 cookies 存到哪了")
        self.assertFalse(s["retry"])

    def test_written_file_without_login_cookie_is_NOT_success(self):
        """**本文件最重要的一条**：文件写出来了但没登录凭据 —— 必须判失败并建议重试。"""
        from kiana_vnext_plus import cookie_profile as cp
        s = gui.summarize_login_result(self.OK, False)
        self.assertFalse(s["ok"], "没有登录凭据却回填给了引擎 = 把 480P 伪装成已登录")
        self.assertEqual(s["level"], "warning")
        self.assertTrue(s["retry"])
        self.assertIn("是否重试", s["headline"])
        names = "、".join(cp.SITE_PROFILES["bilibili.com"]["login_cookies"])
        self.assertIn(names, s["headline"], "要说清缺的是哪个凭据名")
        self.assertIn(self.OK["cookies_path"], s["detail"], "排查要能看到文件在哪")

    def test_unverifiable_site_is_not_treated_as_usable(self):
        """核验给不出结论（该站没登记凭据名）→ **不回填、不报成功**，并说清怎么办。

        对抗性审查指出过：这条曾经 `ok=True`（= 自动回填 + 日志打 ✅），
        那会把一份可能只是匿名 cookie 的文件接给引擎、还伪装成成功。
        现在改为 `ok=False, level=warning`：状态行说"未自动接给引擎"并给出可行做法。
        """
        s = gui.summarize_login_result(self.OK, None)
        self.assertFalse(s["ok"], "说不清就不许当成'可以用'（会覆盖用户已有的可用配置）")
        self.assertEqual(s["level"], "warning")
        self.assertIn("无法确认", s["headline"])
        self.assertIn("未自动接给引擎", s["headline"])
        self.assertIn("清空首页 Cookies 框", s["headline"], "要给出可行做法，不能只说不知道")
        self.assertNotIn("✅", s["headline"], "不许把'不知道'装饰成'成功'")

    def test_verify_error_is_reported_as_such_not_as_unregistered_site(self):
        """核验**出错**（如文件被占用）与"该站没登记凭据名"是两回事，不能念同一句。"""
        s = gui.summarize_login_result(
            {**self.OK, "verify_note": "核验 cookies 文件时出错（按「无法判定」处理）：PermissionError: x"},
            None)
        self.assertFalse(s["ok"])
        self.assertIn("核验没能完成", s["headline"])
        self.assertNotIn("没登记登录凭据名", s["headline"])
        self.assertIn("PermissionError", s["detail"])

    def test_no_login_is_reported_and_offers_retry(self):
        s = gui.summarize_login_result(
            {"status": "no_login", "site": "bilibili.com", "cookie_count": 0,
             "cookies_path": None, "note": "没有任何 cookie"}, None)
        self.assertFalse(s["ok"])
        self.assertTrue(s["retry"])
        self.assertIn("没检测到有效登录态", s["headline"])

    def test_no_browser_and_bad_url_are_distinct(self):
        a = gui.summarize_login_result({"status": "no_browser", "note": "没装引擎"}, None)
        b = gui.summarize_login_result({"status": "bad_url", "note": "非 https"}, None)
        self.assertEqual((a["level"], b["level"]), ("error", "error"))
        self.assertIn("浏览器引擎", a["headline"])
        self.assertIn("登录页", b["headline"])

    def test_cancelled_does_not_claim_anything_was_written(self):
        s = gui.summarize_login_result(
            {"status": "cancelled", "site": "bilibili.com", "cookies_path": None}, None)
        self.assertFalse(s["ok"])
        self.assertNotIn("✅", s["headline"])
        self.assertIn("没把结果取回来", s["headline"])

    def test_exception_and_unknown_status_are_errors_not_successes(self):
        e = gui.summarize_login_result({"status": "exception", "note": "RuntimeError: x"}, None)
        u = gui.summarize_login_result({"status": "未来才有的状态"}, None)
        for s in (e, u):
            self.assertFalse(s["ok"])
            self.assertEqual(s["level"], "error")
            self.assertIn("❌", s["headline"])

    def test_verify_note_surfaces_in_detail(self):
        """核验本身出错（如文件被占用）时，原因必须能查到，不许静默。"""
        s = gui.summarize_login_result(
            {**self.OK, "verify_note": "核验 cookies 文件时出错：PermissionError"}, True)
        self.assertIn("PermissionError", s["detail"])

    def test_empty_result_never_claims_success(self):
        for res in ({}, None, {"status": ""}):
            self.assertFalse(gui.summarize_login_result(res, None)["ok"])


@_NEEDS_GUI
class TestMergeCookiePathValue(unittest.TestCase):
    """Cookies 框回填：新路径必须排**最前**（顺序错了新登录就静默不生效）。"""

    def test_empty_box_gets_the_new_path(self):
        self.assertEqual(gui.merge_cookie_path_value("", r"C:\new.txt"), r"C:\new.txt")
        self.assertEqual(gui.merge_cookie_path_value("   ", r"C:\new.txt"), r"C:\new.txt")

    def test_new_path_goes_first_because_engine_merge_is_first_wins(self):
        """判据来自引擎实现：`universal_downloader._ensure_cookie_file` 里
        `if key in seen: continue` ⇒ **先出现的文件赢**。新 cookies 排后面就盖不住旧 SESSDATA。"""
        v = gui.merge_cookie_path_value(r"C:\old.txt", r"C:\new.txt")
        self.assertEqual([x for x in v.split(";") if x], [r"C:\new.txt", r"C:\old.txt"])

    def test_existing_multi_file_value_is_kept(self):
        v = gui.merge_cookie_path_value(r"C:\a.txt;C:\b.txt", r"C:\c.txt")
        for p in (r"C:\a.txt", r"C:\b.txt", r"C:\c.txt"):
            self.assertIn(p, v)
        self.assertTrue(v.startswith(r"C:\c.txt"))

    def test_idempotent(self):
        """已在**最前**时逐字返回原值（连分隔符都不碰）。

        为什么要这么严：返回值会被 `setText()` 写回输入框，而**换行分隔**也是引擎
        认的写法。无脑重建会把用户手写的多行清单规范成一行 —— 内容没丢，
        但他会以为程序动了自己的配置。所以"已满足"时**一个字节都不改**。

        [v7 修复] 原来这条写的是"只要在列表里就返回原值"，那是**错的**：
        它让「设为最优先」对已在列表里的项完全无效（详见
        `tests/test_cookie_sources_manager.py::TestAddFront`）。
        幂等只对"已经在最前"成立。
        """
        cur = r"C:\new.txt;C:\old.txt"
        self.assertEqual(gui.merge_cookie_path_value(cur, r"C:\new.txt"), cur)
        # 换行分隔（引擎也认）同样逐字保留
        cur2 = "C:\\new.txt\nC:\\old.txt"
        self.assertEqual(gui.merge_cookie_path_value(cur2, r"C:\new.txt"), cur2)

    def test_existing_path_is_moved_to_the_front(self):
        """[v7] 已在列表**中间/末尾**时必须移到最前 —— 那是「设为最优先」的语义。"""
        v = gui.merge_cookie_path_value(r"C:\a.txt;C:\b.txt", r"C:\b.txt")
        self.assertEqual([x for x in v.split(";") if x], [r"C:\b.txt", r"C:\a.txt"])
        # 不产生重复项
        self.assertEqual(v.count(r"C:\b.txt"), 1)

    def test_no_new_path_keeps_current(self):
        self.assertEqual(gui.merge_cookie_path_value(r"C:\old.txt", ""), r"C:\old.txt")


@_NEEDS_GUI
class TestCookieLoginBridge(unittest.TestCase):
    """后台桥：真线程、真事件循环、真投递 —— 但**协程是假的**（绝不弹浏览器）。

    ## 桩的签名纪律（CLAUDE.md 第九节）

    桥对协程工厂的调用是 `self._opener(site, url)`（[v7] 起多一个 `url`）。
    桩必须**按真实签名**写 —— 窄签名桩（只收 `site`）会让被测代码走异常分支，
    制造**假绿**：测试过了，真机上自定义登录页一填就炸。
    所以本文件里的桩统一是 `lambda s, u: ...`，并且 `_kick` 会把 `url` 也记下来。
    """

    def _kick(self, opener, verify=None, site="bilibili.com", url=None, timeout=15.0):
        box = {}
        done = threading.Event()

        def deliver(kind, payload):
            box["kind"] = kind
            box["payload"] = payload
            box["thread"] = threading.current_thread().name
            done.set()

        bridge = gui.CookieLoginBridge(deliver, opener=opener, verify=verify)
        started = bridge.start(site, url=url)
        got = done.wait(timeout)
        return bridge, box, started, got

    def test_result_is_delivered_from_a_worker_thread(self):
        async def fake(site, url):
            return {"ok": True, "status": "ok", "site": "bilibili.com",
                    "cookie_count": 3, "cookies_path": "X:/c.txt"}

        bridge, box, started, got = self._kick(fake, verify=lambda s, p: True)
        self.assertTrue(started)
        self.assertTrue(got, "后台线程没在超时内投递结果（界面会永远停在'等待登录中'）")
        self.assertEqual(box["kind"], "login")
        self.assertNotEqual(box["thread"], threading.current_thread().name,
                            "结果是在主线程算出来的？那界面早冻住了")
        self.assertEqual(box["payload"]["status"], "ok")
        self.assertIs(box["payload"]["login_cookie_verified"], True)
        self.assertFalse(bridge.running, "跑完了还 running —— 按钮会被永久禁用")

    def test_custom_url_reaches_the_coroutine(self):
        """[v7] 自定义登录页必须**原样**递到协程；不传时是 `None`（= 用默认登录页）。

        这条是"能填不生效"的专门防线：界面上加个输入框很容易，
        忘记把它接进那条已经在跑的链上就完全白做。
        """
        seen = {}

        async def fake(site, url):
            seen["site"], seen["url"] = site, url
            return {"status": "ok", "cookies_path": None, "cookie_count": 0}

        _, _, _, got = self._kick(fake, site="bilibili.com", url="https://space.bilibili.com/1")
        self.assertTrue(got)
        self.assertEqual(seen, {"site": "bilibili.com", "url": "https://space.bilibili.com/1"})
        # 不填 → None（而不是空串：默认登录页由 cookie_profile 那边决定）
        _, _, _, got2 = self._kick(fake, site="bilibili.com", url="")
        self.assertTrue(got2)
        self.assertIsNone(seen["url"], "空串应当归一成 None，别把空串塞给 cookie_profile")

    def test_verify_receives_the_written_path(self):
        seen = {}

        async def fake(site, url):
            return {"status": "ok", "cookies_path": "X:/c.txt", "cookie_count": 2}

        def verify(site, path):
            seen["site"], seen["path"] = site, path
            return False

        _, box, _, got = self._kick(fake, verify=verify)
        self.assertTrue(got)
        self.assertEqual(seen, {"site": "bilibili.com", "path": "X:/c.txt"})
        self.assertIs(box["payload"]["login_cookie_verified"], False)

    def test_coroutine_exception_becomes_a_verdict_not_a_hang(self):
        async def boom(site, url):
            raise RuntimeError("事件循环炸了")

        _, box, _, got = self._kick(boom)
        self.assertTrue(got, "协程抛异常时也必须投递结论")
        self.assertEqual(box["payload"]["status"], "exception")
        self.assertIn("RuntimeError", box["payload"]["note"])
        s = gui.summarize_login_result(box["payload"], box["payload"]["login_cookie_verified"])
        self.assertFalse(s["ok"])
        self.assertEqual(s["level"], "error")

    def test_opener_creation_failure_is_reported(self):
        def bad_opener(site, url):
            raise ImportError("没有 cookie_profile")

        _, box, _, got = self._kick(bad_opener)
        self.assertTrue(got)
        self.assertEqual(box["payload"]["status"], "exception")
        self.assertIn("ImportError", box["payload"]["note"])

    def test_verify_failure_is_not_silent(self):
        async def fake(site, url):
            return {"status": "ok", "cookies_path": "X:/c.txt", "cookie_count": 1}

        def verify(site, path):
            raise PermissionError("文件被占用")

        _, box, _, got = self._kick(fake, verify=verify)
        self.assertTrue(got)
        self.assertIsNone(box["payload"]["login_cookie_verified"], "核验出错 → 只能算'无法判定'")
        self.assertIn("PermissionError", box["payload"]["verify_note"],
                      "核验失败的原因必须带出来（不许静默）")

    def test_second_start_while_running_is_refused(self):
        """防"点两次开两个浏览器"：同一配置档目录开两个 Chrome 会撞 singleton 锁。"""
        started = threading.Event()

        async def slow(site, url):
            started.set()
            await asyncio.sleep(0.25)
            return {"status": "ok", "cookies_path": None, "cookie_count": 0}

        done = threading.Event()
        bridge = gui.CookieLoginBridge(lambda k, p: done.set(), opener=slow)
        self.assertTrue(bridge.start("bilibili.com"))
        self.assertTrue(started.wait(5), "协程没跑起来")
        self.assertFalse(bridge.start("bilibili.com"), "已在跑却又起了第二个线程")
        self.assertTrue(done.wait(10), "第一个任务没结束")

    def test_stop_cancels_the_coroutine_so_the_browser_gets_closed(self):
        """关程序时的软取消：协程必须收到取消并走完自己的 finally
        —— `open_profile_for_login` 正是在 finally 里 `await ctx.close()` 关浏览器的。"""
        seen = {}
        done = threading.Event()

        async def long_wait(site, url):
            try:
                while True:
                    await asyncio.sleep(0.02)
            except asyncio.CancelledError:
                seen["cancelled"] = True
                raise
            finally:
                seen["finally"] = True

        bridge = gui.CookieLoginBridge(lambda k, p: done.set(), opener=long_wait)
        self.assertTrue(bridge.start("bilibili.com"))
        # 白盒：等事件循环与任务就位（`_loop`/`_task` 是 stop() 的抓手）
        for _ in range(400):
            if bridge._loop is not None and bridge._task is not None:
                break
            time.sleep(0.01)
        self.assertIsNotNone(bridge._task, "任务没建起来，stop() 会是个空操作")
        self.assertTrue(bridge.stop(), "没有发出取消请求")
        self.assertTrue(done.wait(10), "取消后没投递结论")
        self.assertTrue(seen.get("cancelled"), "协程没收到取消")
        self.assertTrue(seen.get("finally"), "协程没走完 finally —— 浏览器不会被关掉")


# ══════════════════════════════════════════════════════════════════════
# 接线断言（**不需要导入 launcher_v9**，纯读源码）
# ══════════════════════════════════════════════════════════════════════
class _StripProse(ast.NodeTransformer):
    """删掉所有"裸字符串语句"（docstring/伪注释）——它们不是可执行代码。"""

    def generic_visit(self, node):
        super().generic_visit(node)
        for field in ("body", "orelse", "finalbody"):
            lst = getattr(node, field, None)
            if isinstance(lst, list):
                kept = [s for s in lst if not (
                    isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)
                    and isinstance(s.value.value, str))]
                if field == "body" and not kept:
                    kept = [ast.Pass()]
                setattr(node, field, kept)
        return node


def _src() -> str:
    return (ROOT / "launcher_v9.py").read_text(encoding="utf-8")


def _code_only() -> str:
    """源码 → **只含可执行代码**的文本。

    断言"代码里不再出现 X"时，说明性注释本身就会引用 X（本文件里遍地都是），
    直接搜原文会被自己的注释骗过（假红/假绿）。同 `tests/test_v2197_secrets.py`。
    """
    return ast.unparse(_StripProse().visit(ast.parse(_src())))


def _func(tree, name, cls=None):
    if cls is not None:
        for n in ast.walk(tree):
            if isinstance(n, ast.ClassDef) and n.name == cls:
                for m in n.body:
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name == name:
                        return m
        raise AssertionError(f"找不到 {cls}.{name}")
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    raise AssertionError(f"找不到函数 {name}")


class TestLoginButtonWiring(unittest.TestCase):
    """没有接线 = 功能不存在。本工程的老事故正是"界面能开、点了没反应"。"""

    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(_src())

    def test_widgets_created_on_settings_not_home(self):
        """[v7 重排] 登录控件建在**设置页**，且**首页不许再有** —— 两半都要断言。

        只断言"搬到了设置页"是不够的：留一份旧的在首页 = "按钮搬走了、旧的还在"，
        用户点那个旧的会走到哪条链上谁也说不准。所以要有一条**反向断言**钉死。
        """
        st = _func(self.tree, "build", cls="SettingsPage")
        attrs = {n.attr for n in ast.walk(st) if isinstance(n, ast.Attribute)}
        for a in ("login_btn", "login_site_combo", "login_status", "login_sites",
                  "login_url_edit", "cookie_sources_view", "cookie_manager_status"):
            self.assertIn(a, attrs, f"设置页没有创建 {a}")
        home = _func(self.tree, "build", cls="HomePage")
        home_src = ast.unparse(home)
        self.assertNotIn("login_btn", home_src, "首页还留着登录按钮（搬走了就该撤干净）")
        self.assertNotIn("login_site_combo", home_src, "首页还留着站点下拉")

    def test_cookie_input_stays_on_home(self):
        """[v7 决策] Cookies **输入框留在首页**（每次抓取都要看一眼），登录/管理在设置页。

        这条不是"顺手记一笔"：它是本轮唯一一处**机主留给我的判断题**，
        而它一旦被后来者改掉（把框也挪走），`_start()` 就会读到空值 ⇒
        引擎按无 cookies 跑、界面一片正常 —— 正是本工程最忌讳的那类静默失效。
        """
        home = _func(self.tree, "build", cls="HomePage")
        self.assertIn("cookie_edit", {n.attr for n in ast.walk(home)
                                      if isinstance(n, ast.Attribute)},
                      "首页 Cookies 输入框没了？它与设置页镜像之间的同步链会整条断掉")
        start = _func(self.tree, "_start", cls="KianaV9")
        self.assertIn("cookie_edit", ast.unparse(start),
                      "_start() 没把首页 Cookies 框传给引擎 = 用户配的 cookies 根本不生效")

    def test_button_is_wired(self):
        wire = _func(self.tree, "_wire", cls="KianaV9")
        s = ast.unparse(wire)
        self.assertIn("login_btn", s, "_wire 没接线按钮")
        self.assertIn("clicked", s)
        self.assertIn("_on_login_cookies", s)

    def test_bridge_is_created_once_in_init(self):
        init = _func(self.tree, "__init__", cls="KianaV9")
        s = ast.unparse(init)
        self.assertIn("CookieLoginBridge", s)
        self.assertIn("_deliver_async", s)

    def test_no_asyncio_run_on_the_gui_thread(self):
        """**绝不能** `asyncio.run()`：它会把一个新事件循环塞进 Qt 主线程 → 界面冻死
        （登录要等用户几分钟）。必须 `new_event_loop()` 在后台线程里跑。"""
        code = _code_only()
        self.assertNotIn("asyncio.run(", code)
        self.assertIn("asyncio.new_event_loop()", code)

    def test_login_runs_in_a_daemon_thread(self):
        start = _func(self.tree, "start", cls="CookieLoginBridge")
        calls = [n for n in ast.walk(start) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and n.func.attr == "Thread"]
        self.assertTrue(calls, "CookieLoginBridge.start 没有起线程")
        kws = {k.arg: k.value for k in calls[0].keywords}
        self.assertIn("daemon", kws, "必须是 daemon 线程（否则退出程序时会被挂住）")
        self.assertIs(kws["daemon"].value, True)

    def test_worker_never_touches_qt_widgets(self):
        """worker 线程只能吐结果，不许碰控件：它在跑的时候主线程也在用那些控件。

        判据看**真实的调用/属性节点**，不看 `unparse` 出来的文本 ——
        文本里连 docstring 都算，会被自己的说明文字骗到（假红）。
        """
        work = _func(self.tree, "_work", cls="CookieLoginBridge")
        calls = {n.func.attr for n in ast.walk(work)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        names = {n.id for n in ast.walk(work) if isinstance(n, ast.Name)}
        for bad in ("setText", "postEvent"):
            self.assertNotIn(bad, calls, f"worker 线程里调用了 {bad}")
        for bad in ("QApplication",):
            self.assertNotIn(bad, names, f"worker 线程里用了 {bad}")

    def test_result_delivery_goes_through_post_event(self):
        """CLAUDE.md 陷阱表：worker 线程 emit 信号偶发崩 → 统一走 postEvent。"""
        d = _func(self.tree, "_deliver_async", cls="KianaV9")
        s = ast.unparse(d)
        self.assertIn("postEvent", s)
        self.assertIn("_LoginEvent", s)

    def test_event_dispatches_login_and_privacy(self):
        """[v2.19.9] 后台登录态探测的结果**必须仍有人接**。

        接的人从"首页那行已删的小字"换成了日志页（`_on_privacy_status`）。
        这条是"异步了、结果被丢掉"的防线：算出来却没人显示，用户等于没被告知
        —— 那比同步假死更隐蔽（假死至少看得见）。
        """
        ev = _func(self.tree, "event", cls="KianaV9")
        s = ast.unparse(ev)
        self.assertIn("_LoginEvent", s)
        self.assertIn("_on_login_done", s)
        self.assertIn("_on_privacy_status", s, "登录态探测结果没人接 = 结果被丢掉")
        self.assertNotIn(".privacy_status", s, "首页那行已删，这里不许再引用它")

    def test_success_path_refreshes_the_privacy_line(self):
        """硬要求：取完 cookies 后那次登录态刷新**不许被删**（成功要看到变 ✅，失败也要看到它没变）。"""
        done = _func(self.tree, "_on_login_done", cls="KianaV9")
        s = ast.unparse(done)
        self.assertIn("_refresh_privacy_status_async", s, "取完 cookies 没刷登录态")
        self.assertIn("_use_fresh_cookies", s, "拿到的 cookies 没接给引擎用")

    def test_privacy_text_builder_does_not_touch_widgets(self):
        """算那串文本的函数要能在**后台线程**里跑（里面有 10s 超时的联网探测）。"""
        f = _func(self.tree, "_privacy_status_text", cls="KianaV9")
        calls = {n.func.attr for n in ast.walk(f)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        self.assertNotIn("setText", calls, "算文本的函数里出现了 setText —— 它就不能在后台跑了")
        self.assertNotIn("home", {n.attr for n in ast.walk(f) if isinstance(n, ast.Attribute)},
                         "算文本的函数碰了 self.home.*（控件）")

    def test_start_never_computes_the_login_state_on_the_gui_thread(self):
        """[v2.19.9 假死修复] `_start()` **绝不许**同步算登录态。

        这条是本轮的核心防线：`_privacy_status_text()` 里有一次
        `requests.get("https://api.bilibili.com/x/web-interface/nav", timeout=10)`
        —— 在 GUI 线程算它，点一下「开始爬取」界面就冻最多 10 秒，
        而且爬取要等探测跑完才真正启动。
        原来那个"算 + 显示"的薄壳 `_check_cookies()` 因此一并删除，这里连它一起钉死。
        """
        start = ast.unparse(_func(self.tree, "_start", cls="KianaV9"))
        self.assertNotIn("_privacy_status_text", start,
                         "_start() 又同步算登录态了 —— 主线程联网 = 假死十秒")
        self.assertNotIn("_check_cookies", start, "已删的同步薄壳又被调回来了")
        self.assertIn("_refresh_privacy_status_async", start,
                      "_start() 不再探测登录态了？结果不许因为'异步'就被丢掉")

    def test_start_probe_runs_after_the_engine_is_up(self):
        """顺序是刻意的，要钉住：**先 `engine.start()`，再后台探测**。

        两个理由（缺任一条这个顺序就不能动）：
          1. 不阻塞启动 —— 登录态只是提示、不是前置条件（`_start()` 里没有任何分支
             读它的结果；引擎自己按 cookies 跑，没登录就 480P，但**能跑**）；
          2. 顺手关掉一条老竞态 —— 探测要临时改进程级 `KIANA_COOKIE_FILES`，
             而 `EngineBridge.start()` 也写它。放在它**返回之后**发起，
             传进去的又是 `cfg["cookie_file"]`（= 它写进环境变量的**同一个字符串**）
             ⇒ 快照 == 写进去的值，回滚回去还是同一个值，抹不掉本次爬取要用的 cookies。
        """
        start = _func(self.tree, "_start", cls="KianaV9")
        src = ast.unparse(start)
        i_engine = src.find("engine.start")
        i_probe = src.find("_refresh_privacy_status_async")
        self.assertNotEqual(i_engine, -1, "_start() 没启动引擎？")
        self.assertNotEqual(i_probe, -1)
        self.assertLess(i_engine, i_probe,
                        "探测跑在 engine.start() 之前了 —— 那样又会和它并发写 "
                        "KIANA_COOKIE_FILES，回滚那一下可能抹掉本次爬取要用的 cookies")
        self.assertIn("cookie_file", src, "没把引擎用的那份 cookies 值交给探测（就不是同一份了）")

    def test_login_state_probe_uses_the_projects_thread_pattern(self):
        """后台探测必须照本工程既有模式：daemon 线程 + `postEvent` 回主线程。

        - **不许 `asyncio.run()`**：它会把事件循环塞进 Qt 主线程（另有测试全局钉死）；
        - **不许跨线程 Signal**（CLAUDE.md 陷阱表：worker 线程 emit 信号偶发崩）；
        - 控件值**在主线程取**，线程里只算文本。
        """
        f = _func(self.tree, "_refresh_privacy_status_async", cls="KianaV9")
        src = ast.unparse(f)
        self.assertIn("Thread", src, "登录态探测没起后台线程（会假死十秒）")
        self.assertIn("daemon", src, "必须是 daemon 线程（否则关窗口时会被挂住）")
        self.assertIn("_deliver_async", src, "结果没经 postEvent 投递回主线程")
        self.assertIn("cookie_edit", src, "Cookies 框的值要在主线程取")
        # 断言"事件循环没被塞进主线程"这一条在整套测试里已有全局判据
        # （`test_no_asyncio_run_on_the_gui_thread`），这里不重复第二份。

    def test_the_deleted_privacy_line_and_env_selfcheck_leave_nothing_behind(self):
        """删干净：代码里（不含注释/docstring）不许再有这三样。

        - `privacy_status`：首页那行小字（只写不读的装饰，已删）；
        - `_env_selfcheck`：**死代码**（第一句 `self.cfg` 全文件从未赋值 ⇒
          立刻 AttributeError 被 `except Exception: pass` 吞掉，自 v2.16 起
          一行都没真正执行过），连同它的 1200ms `QTimer` 一起删；
        - `_env_status`：上面那个函数喂的只写不读的字典（全仓 0 个读取点），
          `_save_captcha_keys` 里那两行记账一并删。

        用 `_code_only()`（AST 去 docstring + 去注释）判，免得被本文件里
        遍地都是的说明性注释骗成假红。

        ⚠️ 判据要**精确到控件属性**：`_on_privacy_status` / `_privacy_status_text`
        这两个**保留的**名字里也含 `privacy_status` 子串，直接搜裸词是假红
        （我第一版就是这么写的，当场被自己的测试抓住）。被删的是
        `self.home.privacy_status` 这种**属性访问**（前面带点）。
        """
        code = _code_only()
        self.assertNotIn(".privacy_status", code, "代码里还在访问被删的控件 privacy_status")
        self.assertNotIn("privacy_status =", code, "代码里还在创建/赋值 privacy_status")
        for gone in ("_env_selfcheck", "_env_status"):
            self.assertNotIn(gone, code, f"代码里还留着 {gone}")

    def test_privacy_refresh_is_skipped_while_the_engine_runs(self):
        """探针要临时改进程级环境变量 `KIANA_COOKIE_FILES`，而 `EngineBridge.start()`
        也在写它 —— 两条线并发写同一个全局变量，回滚时可能把引擎刚设好的值盖回去
        （取完 cookies 反而把本次爬取用的 cookies 抹了）。所以爬取期间必须跳过探测。"""
        f = _func(self.tree, "_refresh_privacy_status_async", cls="KianaV9")
        self.assertIn("engine.running", ast.unparse(f))

    def test_gui_never_writes_cookies_txt_itself(self):
        """GUI 不许自己拼 cookies.txt —— 写出只走 cookie_utils（7 列 / CRLF /
        `#HttpOnly_` 前缀的唯一实现，拼错就是静默丢登录态）。"""
        self.assertNotIn("write_netscape_cookies", _code_only())

    def test_close_event_soft_cancels_the_login(self):
        """退出前要软取消登录，并**等它真的收工**（协程的 finally 才会关掉浏览器进程）。

        对抗性审查抓到的真问题：只投一个 `task.cancel()` 就返回是不够的 ——
        登录跑在 daemon 线程里，解释器退出**不 join daemon 线程**，
        `await ctx.close()` 几乎必然跑不完，留下占着配置档目录的 chrome.exe。
        所以必须 `ignore()` 这次关闭 + 定时器等线程收工，再真关。
        """
        cls = None
        for n in ast.walk(self.tree):
            if isinstance(n, ast.ClassDef) and n.name == "KianaV9":
                cls = n
        self.assertIsNotNone(cls)
        names = {m.name for m in cls.body if isinstance(m, ast.FunctionDef)}
        self.assertIn("closeEvent", names)
        self.assertIn("_close_when_login_done", names, "缺「等它收工」的收尾函数")
        close_src = ast.unparse(_func(self.tree, "closeEvent", cls="KianaV9"))
        self.assertIn("stop()", close_src)
        self.assertIn("ignore", close_src, "没拦住这次关闭 → daemon 线程会被直接掐掉")
        self.assertIn("super()", close_src, "必须回掉父类实现（否则窗口关不掉的语义就变了）")
        wait_src = ast.unparse(_func(self.tree, "_close_when_login_done", cls="KianaV9"))
        self.assertIn("self.close()", wait_src, "等完了要真关窗")
        self.assertIn("_LOGIN_CLOSE_MAX_TICKS", wait_src, "必须有超时上限（否则窗口永远关不掉）")

    def test_log_line_does_not_double_the_status_emoji(self):
        """结论文字自带 ✅/⚠️/❌，`_on_login_done` 不该再补一个前缀（否则日志出现"✅ ✅"）。"""
        s = ast.unparse(_func(self.tree, "_on_login_done", cls="KianaV9"))
        for emoji in ("✅ ", "⚠️ "):
            self.assertNotIn(emoji, s, f"日志里又补了一个 {emoji!r} 前缀")

    def test_privacy_status_text_does_not_revert_the_env_while_engine_runs(self):
        """回滚 `KIANA_COOKIE_FILES` 前要再判一次引擎在不在跑：

        引擎启动（`EngineBridge.start`）也写这两个变量，而探测最长 10 秒 ——
        期间用户点了「开始爬取」的话，回滚会把引擎刚设好的值盖回旧快照
        （"取完 cookies 反而把本次爬取要用的 cookies 抹了"）。
        """
        f = _func(self.tree, "_privacy_status_text", cls="KianaV9")
        self.assertIn("_engine_running", ast.unparse(f))

    def test_login_waits_for_the_user_to_close_the_window(self):
        """承诺是"**你关窗口 = 完成**"：不许传 `wait_for_login=True`
        （那会在检测到凭据时抢走用户的窗口），也不设 timeout（等多久由用户决定）。"""
        code = _code_only()
        self.assertNotIn("wait_for_login", code)
        self.assertNotIn("timeout_s", code)
        self.assertIn("open_profile_for_login", code, "登录入口本身没了？")

    def test_custom_login_url_is_passed_through(self):
        """[v7 新增] 自定义登录页要**真的递到** `open_profile_for_login(url=...)`。

        只在界面上多一个输入框、却不把它交给登录协程 = "能填不生效"，
        本工程第七节把这种接线缺口点过名（六个跳点缺任何一处就是白做）。
        """
        opener = _func(self.tree, "_default_login_opener")
        self.assertIn("url", ast.unparse(opener), "默认 opener 没接 url 参数")
        # 桥要把 url 一路带到 `self._opener(site, url)`，否则输入框的值会静默丢掉
        lv = _func(self.tree, "_login_and_verify", cls="CookieLoginBridge")
        self.assertIn("url", ast.unparse(lv), "_login_and_verify 没把 url 传给协程工厂")
        work = _func(self.tree, "_work", cls="CookieLoginBridge")
        self.assertIn("url", ast.unparse(work), "_work 把 url 丢了")
        start = _func(self.tree, "start", cls="CookieLoginBridge")
        self.assertIn("url", ast.unparse(start), "start() 不接受 url")

    def test_login_url_gate_is_checked_before_opening_a_browser(self):
        """[v7] 网址闸**先判一次**：那道闸在后台线程里，拒绝时界面只会看到 bad_url，
        用户不知道自己敲错了什么。判据必须与引擎那道闸**同源**（`login_url_usable`），
        不许在这儿自己写第二个"看起来差不多"的 http 判定。"""
        on = _func(self.tree, "_on_login_cookies", cls="KianaV9")
        s = ast.unparse(on)
        self.assertIn("login_url_usable", s, "没在开浏览器之前判网址")
        self.assertIn("site_key_from_login_url", ast.unparse(
            _func(self.tree, "_selected_login_site", cls="KianaV9")),
            "自定义网址没有参与站点键推导")
        # 判定函数本身只认 https + 回环 http（与 cookie_profile 里那道闸一致）
        self.assertTrue(gui.login_url_usable("https://space.bilibili.com/")[0])
        self.assertTrue(gui.login_url_usable("")[0], "留空 = 用默认登录页，不算错")
        self.assertTrue(gui.login_url_usable("http://127.0.0.1:8080/")[0])
        self.assertTrue(gui.login_url_usable("http://localhost/x")[0])
        self.assertFalse(gui.login_url_usable("http://example.com/")[0], "明文 http 必须拒绝")
        self.assertFalse(gui.login_url_usable("ftp://example.com/")[0])

    def test_site_key_derivation_never_guesses(self):
        """站点键决定"cookies 落到哪个配置档目录"，**认不出来就必须给空串**。

        拿一个猜的站名去建目录、写 cookies，比不写糟得多
        （`cookie_profile.resolve_site` 宁可报错也不猜，同一个道理）。
        """
        self.assertEqual(gui.site_key_from_login_url("https://space.bilibili.com/123"), "bilibili.com")
        self.assertEqual(gui.site_key_from_login_url("https://www.douyin.com/"), "douyin.com")
        self.assertEqual(gui.site_key_from_login_url(""), "")
        # 完全陌生的站点：**不猜**（本轮已评估：给任意站点建配置档目录要动
        # cookie_profile 的键映射，宁可少做）
        self.assertEqual(gui.site_key_from_login_url("https://totally-unknown.example/x"), "")

    def test_cookie_manager_wiring_is_present(self):
        """[v7] 「Cookies 管理」的六个跳点：控件 → 接线 → 后台核验 → postEvent 回填。

        "界面改了、引擎没变"是本工程反复吃过的亏，所以这里逐跳钉住。
        """
        wire = ast.unparse(_func(self.tree, "_wire", cls="KianaV9"))
        for need in ("_refresh_cookie_sources", "cookie_edit"):
            self.assertIn(need, wire, f"_wire 少了 {need}")
        ev = ast.unparse(_func(self.tree, "event", cls="KianaV9"))
        self.assertIn("cookies_check", ev, "后台核验结果没接回主线程")
        self.assertIn("_on_cookie_check_done", ev)
        chk = ast.unparse(_func(self.tree, "_check_cookie_sources_async", cls="KianaV9"))
        self.assertIn("Thread", chk, "有效性检查没起后台线程（会假死十几秒）")
        self.assertIn("daemon", chk)
        self.assertIn("_engine_running", chk,
                      "爬取期间必须跳过：核验要改进程级 KIANA_COOKIE_FILES，"
                      "与 EngineBridge.start() 并发写同一全局变量会互相盖")
        # 列表/说明文本是**纯函数**（脱离 Qt 可测），且不许在 GUI 线程里联网
        for fn in ("_cookies_manager_text", "_cookies_manager_note", "_cookies_health_text"):
            node = _func(self.tree, fn)
            calls = {n.func.attr for n in ast.walk(node)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
            self.assertNotIn("setText", calls, f"{fn} 应当是纯函数（不碰控件）")

    def test_gui_shows_all_sources_not_just_one(self):
        """[v7 核心] "多个 cookies"在界面上必须**逐个列出来**（含来源），

        并且**来源解析走引擎包那一份实现** —— 界面自己再算一遍优先级链，
        早晚与引擎分叉（"界面显示的"和"引擎读的"不是一回事）。
        """
        rows_fn = _func(self.tree, "cookie_source_rows")
        s = ast.unparse(rows_fn)
        self.assertIn("resolve_cookie_sources", s,
                      "来源列表没走 cookie_utils 的唯一实现")
        tree_src = _code_only()
        self.assertIn("cookie_paths_add_front", tree_src, "加一项没走引擎包实现")
        self.assertIn("cookie_paths_drop", tree_src, "删一项没走引擎包实现")

    def test_removal_never_deletes_files_on_disk(self):
        """[v7] 「移除选中」只改配置，**绝不删用户的文件**。

        删文件是不可逆的破坏动作，而用户点"移除"时想的是"从这份配置里去掉"。
        这条断言钉死代码里不出现任何 unlink/remove/rmtree。
        """
        for fn in ("_remove_selected_cookie_source", "_clear_cookie_box"):
            calls = {n.func.attr for n in ast.walk(_func(self.tree, fn, cls="KianaV9"))
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
            for bad in ("unlink", "rmtree", "remove", "rmdir"):
                self.assertNotIn(bad, calls, f"{fn} 里出现了删文件动作 {bad}")


# ══════════════════════════════════════════════════════════════════════
# [v2.19.9] 离屏构造：删掉的东西**真的不在了**、且点「开始爬取」真的不卡
# ══════════════════════════════════════════════════════════════════════
@pytest.mark.skipif(gui is None, reason="launcher_v9 不可导入")
class TestOffscreenNoPrivacyLine:
    """AST 只能证明"代码里没写"，证明不了"控件真的不存在 / 真的不卡"。

    这里离屏构造真窗口（`QT_QPA_PLATFORM=offscreen`，**不 show()**，
    一个像素都不出现在机主屏幕上），把这两件事点验一遍。
    """

    @pytest.fixture
    def win(self, tmp_path, monkeypatch):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        # 数据根挪到临时目录：离屏构造会读 launcher_config / cookies 来源，
        # 不许碰到机主真实的 %LOCALAPPDATA%\KianaVnextPlus
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        monkeypatch.delenv("KIANA_COOKIE_FILES", raising=False)
        monkeypatch.delenv("KIANA_COOKIE_FILE", raising=False)
        try:
            from PySide6.QtWidgets import QApplication
        except Exception as e:                              # pragma: no cover
            pytest.skip(f"PySide6 不可用：{e}")
        # ⚠️ **必须在构造之前**就把 `save_config` 换成空操作。
        # 为什么：`launcher_v8.CONFIG_FILE` 是**导入期**算好的（`_launcher_config_path()`
        # 在模块顶部就调用了），所以上面那句 `setenv("LOCALAPPDATA", …)` 对
        # 启动器配置**不生效** —— 任何一次 `save_config` 都会写到机主真实的
        # `%LOCALAPPDATA%\KianaVnextPlus\launcher_config.json`。
        # 而这个 fixture 会改 Cookies 框（→ `_on_cookie_home` → `save_config`），
        # 不屏蔽就等于"跑一次测试改一次机主的配置"。
        monkeypatch.setattr(gui, "save_config", lambda *a, **k: None)
        # [v2.19.9] 再把**路径本身**也钉到 tmp。比"把 save_config 打成空操作"更彻底：
        # 空操作只挡写入，`load_config()` 照样读机主的真配置 → `win.config` 里带着
        # 机主的 `cookie_file`，测试结果就随机主本机状态漂。钉住路径后读写都在 tmp 里。
        # 两个模块都要打：`launcher_v9.py:86` 是 `from launcher_v8 import CONFIG_FILE`
        # —— import 搬的是值，`gui.CONFIG_FILE` 是另一个名字绑定。
        fake_cfg = tmp_path / "launcher_config.json"
        for _mod in (sys.modules.get("launcher_v8"), gui):
            if _mod is not None:
                monkeypatch.setattr(_mod, "CONFIG_FILE", fake_cfg, raising=False)
        assert gui.CONFIG_FILE == fake_cfg, f"launcher_v9.CONFIG_FILE 没被打桩：{gui.CONFIG_FILE}"
        app = QApplication.instance() or QApplication([])
        w = gui.KianaV9()
        monkeypatch.setattr(w, "_toast", lambda *a, **k: None)   # 别弹 InfoBar
        yield w
        w.close()
        app.processEvents()

    def test_home_page_has_no_privacy_line(self, win):
        """首页那行小字真的没了（不是"藏起来"，是**没有这个属性**）。"""
        assert not hasattr(win.home, "privacy_status"), \
            "首页还留着 privacy_status —— 删控件要连着布局行一起撤干净"

    def test_env_selfcheck_is_gone_whole(self, win):
        """死函数连它的定时器一起清掉了：属性、方法都不该在。"""
        assert not hasattr(win, "_env_selfcheck"), "死函数还在"
        assert not hasattr(win, "_env_status"), "只写不读的 _env_status 还在"

    def test_the_text_builder_survives_for_the_settings_page(self, win):
        """**必须保住的那一处**：`_privacy_status_text()` 还在（设置页「检查有效性」复用）。"""
        assert hasattr(win, "_privacy_status_text")
        text = win._privacy_status_text("")
        assert text.startswith("隐私状态: "), f"文本格式变了：{text!r}"
        assert "Cookies:" in text

    def test_start_returns_immediately_even_when_the_probe_hangs(self, win):
        """**本轮的核心回归**：探测再慢，点「开始爬取」也必须秒回，
        而且**结果照样要落到看得见的地方**（日志页）。

        做法：把 B站探针换成"一直挂着直到测试放行"的桩（真实代码的 timeout 是 10 秒，
        桩用事件等待而不是 sleep，测试更快也更确定）——
        然后直接调 `_start()`，量**墙钟时间**，再放行探测线程、轮询日志页。

        两半判据缺一不可：
          · 时间：同步版在这里会一直等到放行（≥ 桩的等待时间）→ 必红；
          · 结果：只把探测丢进线程却没人显示 = "异步了，结论丢了"，
            那比同步假死更隐蔽。所以这里连 postEvent 回填那一段一起端到端验。
        """
        from PySide6.QtWidgets import QApplication
        from kiana_vnext_plus import cookie_health

        started = {}
        release = threading.Event()

        class _FakeEngine:
            """假引擎：只记下"被启动了"，不起真线程、不爬任何东西。"""

            running = False

            def start(self, urls, cfg):
                started["urls"] = list(urls)
                started["cfg"] = dict(cfg)

            def stop(self):
                pass

        def _hang(*a, **k):
            release.wait(10.0)          # 模拟"探测挂住"（真实 timeout 就是 10 秒）
            return {"ok": True, "status": "ok", "msg": "桩", "fields": []}

        win.engine = _FakeEngine()
        win.home.url_edit.setPlainText("https://example.com")
        win.home.cookie_edit.setText(str(_write_cookies([_ck("SESSDATA")])))
        orig = cookie_health.check_bilibili
        cookie_health.check_bilibili = _hang
        try:
            t0 = time.perf_counter()
            win._start()
            elapsed = time.perf_counter() - t0
        finally:
            cookie_health.check_bilibili = orig

        engine_started = started.get("urls")
        release.set()                   # 放探测线程收工
        # 等后台线程把结论 postEvent 回主线程、再落进日志页
        app = QApplication.instance()
        deadline = time.time() + 10
        text = ""
        while time.time() < deadline:
            app.processEvents()
            text = win.logp.log_view.toPlainText()
            if "隐私状态:" in text:
                break
            time.sleep(0.02)

        assert engine_started == ["https://example.com"], \
            "引擎没被启动（探测把启动挡住了？）"
        assert elapsed < 1.0, (
            f"_start() 花了 {elapsed:.2f} 秒 —— 它又在主线程等探测了。"
            "登录态只是提示、不是爬取的前置条件，不许阻塞启动")
        assert "隐私状态:" in text, \
            "探测结果没落进日志页 —— 异步了但结论被丢掉了"

    def test_the_probe_result_lands_in_the_log_page(self, win):
        """异步了也**不许把结果丢掉**：结论必须落到看得见的地方（日志页）。"""
        win._on_privacy_status("隐私状态: 桩")
        assert "隐私状态: 桩" in win.logp.log_view.toPlainText(), \
            "登录态结论没落进日志页 —— 那就是算了个寂寞"


if __name__ == "__main__":
    unittest.main()
