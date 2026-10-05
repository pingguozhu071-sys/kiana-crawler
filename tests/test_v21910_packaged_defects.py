"""[v2.19.10] 打包版真机测试暴露的缺陷 —— 回归测试。

来源：2026-10-04 用**打包版**（`KianaVnextPlus-Setup-2.19.9.0.exe`）跑了一次 B站番剧抓取，
暴露了「源码正常、打包产物失效」的问题。每个测试对应一条，注释里写清**当时的真机症状**。

本文件刻意分成两类判据：
  · **构建面**（spec 收没收某个包）→ 只能查文本，这是构建配置，不是运行时行为；
  · **运行面**（错误分类、URL 归属、日志去重）→ 一律**调真函数**，不查源码子串。
"""

import asyncio
import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ───────────────────────── P0-1：浏览器驱动必须随包 ─────────────────────────
class TestBrowserDriverBundled:
    """真机症状：浏览器层**整体降级 protocol-only**，日志逐字为
    `FileNotFoundError: [WinError 2] 系统找不到指定的文件。`，抛点链
    `playwright._impl._transport.connect()` → `subprocess._execute_child`
    —— 即**要启动的 driver 可执行文件不存在**。

    安装目录实测对比（这是根因铁证）：
        _internal\\patchright\\driver\\   ✅ 在（110 个文件）
        _internal\\playwright\\          ❌ **整个包都不在**

    而引擎策略是「优先 patchright，其 add_init_script 自证不通过则回退 playwright」
    ⇒ **回退目标不在包里 ⇒ 整层失效**：JS 渲染兜底与 55 维隐身链一行都不执行。
    """

    @pytest.mark.parametrize("spec_name", ["KianaCrawler.spec", "KianaLauncher.spec"])
    @pytest.mark.parametrize("pkg", ["patchright", "playwright"])
    def test_spec_collects_browser_package(self, spec_name, pkg):
        """两个包、两个 spec，**四个组合缺一不可**。"""
        text = (ROOT / spec_name).read_text(encoding="utf-8")
        assert f'"{pkg}"' in text or f"'{pkg}'" in text, (
            f"{spec_name} 没有收集 {pkg} —— 真机上缺失的那个包正是这样漏掉的")
        # 光有包名不够：driver 是**非 .py 数据文件**，必须靠 collect_all / collect_data_files 才会进包
        assert "collect_all" in text, (
            f"{spec_name} 未使用 collect_all —— 纯 Python 模块进 PYZ 不解决 driver 缺失")

    def test_collect_all_really_yields_driver(self):
        """真正证明"修法有效"：跑一次 collect_all，断言结果里**确实有 driver 可执行文件**。

        这条是上游判据（不是查文本）：如果哪天 PyInstaller 改了行为、或包结构变了，
        这里会红，而不是等到打完包真机上才发现浏览器起不来。
        """
        hooks = pytest.importorskip("PyInstaller.utils.hooks")
        datas, binaries, _hidden = hooks.collect_all("playwright")
        names = {str(p).replace("\\", "/") for p in list(datas) + list(binaries)}
        # collect_all 返回 (src, dest) 二元组；dest 里应能看到 driver 路径
        assert any("driver" in n for n in names), (
            f"collect_all('playwright') 没收到 driver —— 实际收到 {len(names)} 项")
        assert any("node" in n.lower() for n in names), (
            "collect_all('playwright') 收到 driver 但没有 node 可执行文件")


# ───────────────────── P0-2：justext 停用词表必须随包 ─────────────────────
class TestJutextStoplistsBundled:
    """真机症状（日志里刷了**数十次**，另有 `recall retry failed` 同因）：

        justext candidate failed: [WinError 3] 系统找不到指定的路径。:
          'C:\\...\\_internal\\justext\\stoplists'

    ⇒ 基于 justext 的正文提取通道在打包版里**整条不可用**，每次都失败后走兜底。
    """

    @pytest.mark.parametrize("spec_name", ["KianaCrawler.spec", "KianaLauncher.spec"])
    def test_spec_collects_justext_data(self, spec_name):
        text = (ROOT / spec_name).read_text(encoding="utf-8")
        assert "justext" in text, f"{spec_name} 没有收集 justext 的包内数据文件"

    def test_jutext_actually_has_stoplists_data(self):
        """上游判据：确认 justext 真的带数据目录（否则 spec 里那句是空转）。"""
        pytest.importorskip("justext")
        import justext

        stoplists = Path(justext.__file__).parent / "stoplists"
        assert stoplists.is_dir(), f"justext 没有 stoplists 目录：{stoplists}"
        assert any(stoplists.iterdir()), "justext/stoplists 是空的"


# ─────────────── P1-1：连不通的域名不该走满 6 次重试 ───────────────
class TestFetchErrorClassification:
    """真机症状：一次 150 页抓取耗时 **1463 秒**，`fail=46` 里十几个是**根本连不通**的
    外国域名，每个走满 **6 次重试 × 每次约 21 秒超时**。

    下面每个样例**逐字取自那次的 `crawl.log`**。
    """

    @staticmethod
    def _cls():
        from kiana_vnext_plus.protocol_engine import ProtocolEngine

        return ProtocolEngine

    @pytest.mark.parametrize("msg,expected_kind", [
        ("Failed to perform, curl: (6) Could not resolve host: management.core.windows.net", "dns"),
        ("Failed to perform, curl: (28) Failed to connect to support.google.com:443 after 22064 ms", "timeout"),
        ("Failed to perform, curl: (35) Recv failure: Connection was reset", "reset"),
        ("Failed to perform, curl: (35) BoringSSL SSL_connect: Connection closed abruptly", "reset"),
        ("Failed to perform, curl: (7) Failed to connect to x:443", "unreachable"),
        ("ValueError: something else entirely", "other"),
    ])
    def test_kind_matches_real_log_lines(self, msg, expected_kind):
        kind, _cap, _why = self._cls().classify_fetch_error(RuntimeError(msg))
        assert kind == expected_kind, f"{msg!r} 被判成 {kind}"

    @pytest.mark.parametrize("msg", [
        "Failed to perform, curl: (6) Could not resolve host: x",
        "Failed to perform, curl: (28) Failed to connect to x:443 after 22064 ms",
        "Failed to perform, curl: (7) Failed to connect to x:443",
    ])
    def test_pointless_errors_are_capped(self, msg):
        """「再试也没用」的三类必须有**明确更低的**上限，且小于引擎默认。"""
        _kind, cap, why = self._cls().classify_fetch_error(RuntimeError(msg))
        assert cap is not None and cap < 6, f"{msg!r} 没有被限流（cap={cap}）"
        assert why, "限流必须带人话原因 —— 静默少试等于静默改变行为"

    def test_transient_errors_keep_full_retries(self):
        """⚠️ **反向判据**（防"一刀切"）：连接被重置这类**可能只是抖动**的必须保留完整重试。

        没有这条，后人图省事把重试次数统一降到 1 也能"通过"上面的测试 ——
        那等于用可靠性换时间，是真机抖动场景下的回归。
        """
        _kind, cap, _why = self._cls().classify_fetch_error(
            RuntimeError("Failed to perform, curl: (35) Recv failure: Connection was reset"))
        assert cap is None, "连接被重置被限流了 —— 瞬时抖动会变成真失败"


# ─────────── P2-1：B站 结论不许串台到别的域名 ───────────
class TestBilibiliUrlOwnership:
    """真机症状：下载 Apple / Microsoft 官网宣传片时，日志照样打印
    `B站 cookies **登录有效**（大会员） → 最高画质` —— 与本次下载毫无关系。
    """

    @staticmethod
    def _fn():
        from kiana_vnext_plus.universal_downloader import is_bilibili_url

        return is_bilibili_url

    @pytest.mark.parametrize("url", [
        "https://www.bilibili.com/bangumi/play/ep259635",
        "https://bilibili.com/video/BV1xx",
        "https://b23.tv/abcdef",
        "https://live.bilibili.com/123",
    ])
    def test_real_bilibili_urls(self, url):
        assert self._fn()(url) is True, f"{url} 应判为 B站"

    @pytest.mark.parametrize("url", [
        "https://edgestatic.azureedge.net/x.mp4",       # 真机日志里被串台的那个
        "https://cdsassets.apple.com/x.mp4",            # 同上
        "https://www.firefox.com/zh-CN/",
        # ⚠️ 下面两条是**关键**：子串匹配会误判，后缀匹配不会
        "https://evilbilibili.com/x",
        "https://bilibili.com.evil.example/x",
        "https://notb23.tv/x",
    ])
    def test_non_bilibili_urls(self, url):
        assert self._fn()(url) is False, f"{url} 不该判为 B站（子串冒充域名归属）"


# ─────────── P2-2：同域身份缺失只提示一次 ───────────
class TestIdentityAbsenceLogDedup:
    """真机症状：一次 150 页抓取里刷了 **60+ 行**
    `[身份] 域 xxx 不可用（not_provided）：该站未入库任何身份` ——
    身份池只有 1 个身份（B站），其余 26 个域名每个每页都刷一行。
    """

    def _make(self):
        import kiana_vnext_plus.page_processor as pp

        cls = next(v for v in vars(pp).values()
                   if isinstance(v, type) and hasattr(v, "_fetch_with_identity"))
        obj = object.__new__(cls)
        calls = []

        class _Router:
            async def fetch(self, url, domain, job):
                calls.append((url, domain))
                return "RESP"

        from kiana_vnext_plus.cookie_armory import Unavailable, UnavailableReason

        class _Armory:
            def acquire_identity(self, domain):
                return Unavailable(domain, UnavailableReason.NOT_PROVIDED,
                                   "该站未入库任何身份（请先提供 cookies）")

        obj.router = _Router()
        obj.cookie_armory = _Armory()
        return obj, calls

    def test_same_domain_logged_once(self, caplog):
        obj, calls = self._make()
        logger = logging.getLogger("kiana_vnext_plus.page_processor")
        with caplog.at_level(logging.INFO, logger=logger.name):
            for i in range(5):
                asyncio.run(obj._fetch_with_identity(
                    f"https://www.firefox.com/p{i}", "www.firefox.com", {}))
        hits = [r for r in caplog.records
                if "不可用（not_provided）" in r.getMessage() and r.levelno == logging.INFO]
        assert len(hits) == 1, f"同一域名打了 {len(hits)} 次 INFO，应为 1 次"
        # 降级行为本身**一点没变**：5 次都照常去抓了
        assert len(calls) == 5, "去重不应影响抓取行为"

    def test_different_domains_each_logged(self, caplog):
        """⚠️ **反向判据**：不许为了"降噪"把不同域名的提示也吃掉 ——
        那样"为什么这些站是匿名抓的"就查不出来了。"""
        obj, _calls = self._make()
        logger = logging.getLogger("kiana_vnext_plus.page_processor")
        with caplog.at_level(logging.INFO, logger=logger.name):
            for d in ("a.com", "b.com", "c.com"):
                asyncio.run(obj._fetch_with_identity(f"https://{d}/x", d, {}))
        hits = [r for r in caplog.records
                if "不可用（not_provided）" in r.getMessage() and r.levelno == logging.INFO]
        assert len(hits) == 3, f"三个不同域名只打了 {len(hits)} 次，信息被吃掉了"


# ─────────── P1-4：心跳的 pend 死键 / pct 恒 100% ───────────
class TestHeartbeatProgressNumbers:
    """真机症状（比它要修的 bug 更值钱的一条）：

        [1030s] ====== 100.0%  done=150 fail=46 skip=587 pend=0  0.1p/s

    这行让人以为"待处理为空、活干完了" —— 而那一刻前沿**还有 587 行**。
    根因：`_progress['pending']` **全仓没有任何地方自增**（死键），
    且 `total` 只在 `done` 增长时被抬 ⇒ `pct` **恒 100.0%**。
    那次排查里的**两次误判**（"在等连不通的外国域名"、"在等在途页面请求"）**都源于这一行**。
    """

    @staticmethod
    def _fn():
        from run_crawler import _progress_display

        return _progress_display

    def test_dead_keys_no_longer_report_all_done(self):
        """**核心回归**：复刻那次的确切数字。

        `_progress` 说 done=150 / pending=0 / total=150（= 旧口径下的"全干完了"），
        而 frontier 的权威计数说还有 500 pending + 87 retry。
        修复后必须**如实显示还有 587 行没落定**，且 pct **不再是 100%**。
        """
        progress = {"done": 150, "failed": 46, "skipped": 0, "pending": 0, "total": 150}
        counts = {"done": 150, "failed": 46, "pending": 500, "retry": 87}
        d, _f, _sk, pn, tot, pct = self._fn()(progress, counts)
        assert d == 150
        assert pn == 587, f"pending+retry 应为 587，实得 {pn} —— 死键没修好"
        # total = **全部状态之和**（150 done + 46 failed + 500 pending + 87 retry = 783）
        assert tot == 783, f"total 应为全部状态之和 783，实得 {tot}"
        assert pct < 100.0, f"还有 587 行没落定，pct 却报 {pct}"

    def test_pct_reaches_100_when_frontier_drained(self):
        """跑完必须能到 100% —— 否则修掉假 100% 会换来"永远不到 100%"的另一种坏。"""
        counts = {"done": 150, "failed": 46, "skipped": 587}
        _d, _f, _sk, pn, _tot, pct = self._fn()({}, counts)
        assert pn == 0
        assert pct == pytest.approx(100.0), f"前沿已排空，pct 应为 100，实得 {pct}"

    def test_leased_counts_as_unsettled(self):
        """`leased`（正在处理）也算"没落定" —— 否则并发跑着的时候会假报 100%。"""
        counts = {"done": 10, "pending": 0, "retry": 0, "leased": 5}
        _d, _f, _sk, _pn, _tot, pct = self._fn()({}, counts)
        assert pct < 100.0, f"还有 5 个 leased，pct 却报 {pct}"

    def test_falls_back_when_counts_unavailable(self):
        """**反向判据**：取不到权威计数时回退原口径，**不许抛异常** ——
        心跳挂了比数字不准更糟（Redis 后端 / `_read_conn` 未就绪都会走到这里）。"""
        progress = {"done": 7, "failed": 1, "skipped": 2, "pending": 0, "total": 10}
        d, f, sk, pn, tot, pct = self._fn()(progress, {})
        assert (d, f, sk, pn, tot) == (7, 1, 2, 0, 10)
        assert pct == pytest.approx(70.0)


# ─────── P0-3：队列库刷批**泄漏连接**（真机 `database is locked` 659 次） ───────
class TestFlusherDoesNotLeakConnections:
    """真机症状（2026-10-05 那次 18 集抓取）：

        OperationalError: database is locked      ← **659 次，连续 34 分钟**
        [ERROR] page_processor: Job failed: ...   ← 抓下来的页面全部"结果未落盘"

    整个爬虫因此从 13:55 空转到 14:29（一个产物都没出），被迫人工杀掉。

    根因：`_batch_flusher` 里写的是 `async with aiosqlite.connect(...) as db:`
    —— **`async with` 对连接只管事务提交/回滚，*不关连接***（与 `sqlite3` 的 `with` 同款语义）。
    ⇒ **每刷一批就泄漏一个连接 + 一个 aiosqlite 后台线程**；数千批之后 WAL 被撑到 26.9 MB、
    写锁再也拿不到。

    判据用**线程数**：aiosqlite 每个连接起一个线程 ⇒ 泄漏 40 批就多约 40 个线程。
    这是**结构化判据**（数线程），不是查源码文本。
    """

    def test_flushing_many_batches_does_not_grow_threads(self, tmp_path):
        import threading

        from kiana_vnext_plus.frontier import FrontierDB

        async def _run():
            db = FrontierDB(str(tmp_path / "frontier.db"))
            await db.init_async()
            await asyncio.sleep(0.3)
            before = threading.active_count()
            # 一条一个批次 ⇒ 逼 flusher 走 40 次 connect（正是泄漏点）
            for i in range(40):
                await db._write_queue.put(
                    ("DELETE FROM errors WHERE timestamp < ?", (float(i),)))
                await asyncio.sleep(0.01)
            for _ in range(100):
                if db._write_queue.empty():
                    break
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.6)
            after = threading.active_count()
            await db.close()
            return before, after

        before, after = asyncio.run(_run())
        grew = after - before
        assert grew <= 5, (
            f"刷 40 批后线程数涨了 {grew}（{before} → {after}）—— "
            f"每批泄漏一个 aiosqlite 连接/线程；这正是真机上 "
            f"`database is locked` 刷 34 分钟的根因")
