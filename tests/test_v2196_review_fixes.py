"""v2.19.6 回归：pr-review-toolkit 审查发现的问题（修复后防回退）

审查（code-reviewer + silent-failure-hunter）发现本轮 v2.19 修复存在多处真问题，
其中两处是**修复本身制造的**（比不修更隐蔽）：

  R1 SSRF 闸用 403 作拦截态 → 403 是本工程回退链"升级到浏览器"的信号
     → 被拦 URL 交给无闸的 Playwright `page.goto()` → 浏览器跟随 302 进内网
     （实测：拦截后又以 200 返回内网正文）。**修复**：拦截态改 400 + 浏览器层单点闸
  R2 重定向上限 5 且超限静默 break 返回 3xx → 302 不可重试 → **永久 dead**，
     且 status>=400 不成立 → errors 零记录、日志零输出
     （curl_cffi 默认跟随 30 跳 → 6~30 跳的合法链"从能抓变静默判死"）。
     **修复**：上限 10 + 超限返回 508（可重试可诊断）+ 缺 Location 返回 502
  R3 `download_video` 的闸被 fallback 绕过（crawler 用同一 URL 走 media_downloader.download_direct）
     → **修复**：闸下沉到共享原语 probe_url / stream_to_file / download_direct
  R4 m3u8 闸用 dns_check=False → 域名型私网可穿过（localtest.me 类）
     → **修复**：改 dns_check=True（HOST_CACHE 已按 host 缓存，成本可控）
  R5 `mark_failed` 无状态守卫 → 看门狗（page_timeout 本轮默认开启）可把已 done 的页打回 retry
     → **修复**：三条 UPDATE 加 `AND status != 'done'`
  R6 `mark_done_checked` 把 DB 异常当"租约易主" → 成功页被丢弃 + 误导排查方向
     → **修复**：三态返回（True/False/None）

全部离线。
"""
import asyncio
import inspect
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiana_vnext_plus.frontier import FrontierDB


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class TestBlockedStatusNotUpgradeSignal(unittest.TestCase):
    """R1：拦截态必须是 400，不能是回退链会消费的 403/429/503"""

    def test_block_status_is_400(self):
        from kiana_vnext_plus.protocol_engine import ProtocolEngine
        src = inspect.getsource(ProtocolEngine.fetch)
        self.assertIn("ResponseAdapter(400", src, "拦截态应为 400")
        self.assertNotIn("ResponseAdapter(403", src, "拦截态不得用 403（会触发浏览器升级）")

    def test_400_not_consumed_by_fallback_chain(self):
        """确认 400 不会被任何回退判定消费（403/429/503 才会）

        [v6 修复·**这条测试自己踩过"拿文本当结构"的坑**]
        原来它把 `_needs_solver` 的**源码文本**拿来断言：
            self.assertIn("(403, 429, 503)", src)
            self.assertNotIn("400", src)          # ← 只要注释里出现"400"就炸
        我 v6 改这个函数时，**在 docstring 里写了一句"不得含 400"**
        （正常的中文说明），它当场变红 —— 而**行为完全正确**。
        **它考的是"文本里有没有这几个字符"，不是"400 会不会触发回退"。**

        改成**查行为**：直接喂不同状态码的响应，看回退判定怎么答。
        这样既更强（真的验证语义），也不会被注释里的字绊倒。
        """
        from kiana_vnext_plus.engine_router import EngineRouter
        from kiana_vnext_plus.response_adapter import ResponseAdapter

        class _R(EngineRouter):
            def __init__(self):   # 只测这一个纯函数，不建依赖
                pass

        r = _R()
        # 干净正文：排除关键词干扰，单独看状态码的作用
        clean = "<html><body><h1>ok</h1></body></html>"
        for code in (403, 429, 503):
            self.assertTrue(
                r._needs_solver(ResponseAdapter(code, "https://x/", {}, raw_text=clean)),
                f"{code} 必须触发回退（既有语义）")
        self.assertFalse(
            r._needs_solver(ResponseAdapter(400, "https://x/", {}, raw_text=clean)),
            "400 不得触发回退（400 是**拦截态**的主动信号，不是挑战信号）")
        self.assertFalse(
            r._needs_solver(ResponseAdapter(200, "https://x/", {}, raw_text=clean)),
            "干净的 200 不该触发回退")

    def test_solver_layer_has_ssrf_guard(self):
        """R1 第二部分：浏览器层（solver）必须自建闸（纵深防御）"""
        from kiana_vnext_plus.solver_engine import SolverEngine
        for meth in (SolverEngine.render_simple, SolverEngine.solve):
            src = inspect.getsource(meth)
            self.assertIn("is_private_url", src,
                          f"{meth.__name__} 必须含 SSRF 闸（浏览器会跟随重定向）")


class TestRedirectLimitDiagnosable(unittest.TestCase):
    """R2：超限必须可诊断、可重试"""

    def test_hop_limit_raised_and_reported(self):
        from kiana_vnext_plus.protocol_engine import ProtocolEngine
        src = inspect.getsource(ProtocolEngine.fetch)
        self.assertIn("_MAX_HOPS = 10", src, "跳数上限应为 10（原 5 过紧）")
        self.assertIn("508", src, "超限应返回 508（可重试 + 可诊断）")
        self.assertIn("502", src, "缺 Location 应显式报错")
        self.assertIn("too many redirects", src)


class TestDownloadPrimitivesGuarded(unittest.TestCase):
    """R3：闸必须下沉到共享原语（否则 fallback 绕过）"""

    def test_media_downloader_direct_guarded(self):
        from kiana_vnext_plus.media_downloader import MediaDownloader
        src = inspect.getsource(MediaDownloader.download_direct)
        self.assertIn("is_private_url", src, "download_direct 是 fallback 终点，必须自建闸")

    def test_universal_downloader_primitives_guarded(self):
        import kiana_vnext_plus.universal_downloader as ud
        for fn in (ud.probe_url, ud.stream_to_file):
            src = inspect.getsource(fn)
            self.assertIn("is_private_url", src, f"{fn.__name__} 应含闸")


class TestM3U8GuardUsesDns(unittest.TestCase):
    """R4：m3u8 闸必须做 DNS 校验（字面判定可被域名型私网穿过）"""

    def test_blocked_uses_dns_check(self):
        """源码断言：有效代码里必须调用带 dns_check=True 的判定。

        注：反面（不再是 dns_check=False）由本类的行为测试
        `test_localhost_domain_blocked` 覆盖——源码字符串断言在含多行 docstring
        时容易误判，故此处只做正向断言。"""
        from kiana_vnext_plus.m3u8_downloader import M3U8Downloader
        src = inspect.getsource(M3U8Downloader._blocked)
        self.assertIn("is_private_url(str(url), dns_check=True)", src,
                      "应改用 DNS 校验（dns_check=True）")

    def test_localhost_domain_blocked(self):
        """localhost 类域名（需 DNS 才能识别）应被拦"""
        from kiana_vnext_plus.m3u8_downloader import M3U8Downloader
        self.assertTrue(M3U8Downloader._blocked("http://localhost/seg.ts"))
        self.assertTrue(M3U8Downloader._blocked("http://127.0.0.1/seg.ts"))
        self.assertFalse(M3U8Downloader._blocked("https://cdn.example.com/seg.ts"))


class TestMarkFailedDoesNotClobberDone(unittest.TestCase):
    """R5：mark_failed 不得把已 done 的页打回 retry（看门狗场景）"""

    def test_mark_failed_guard_in_sql(self):
        import kiana_vnext_plus.frontier as fm
        src = inspect.getsource(fm.FrontierDB.mark_failed)
        # 只看有效代码行（注释里也提到该守卫，会干扰计数）
        code = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
        self.assertGreaterEqual(code.count("status != 'done'"), 3,
                                "retry / throttled-retry / throttled-dead 三条都应带守卫")

    def test_done_not_reverted_by_mark_failed(self):
        """行为验证：状态为 done 时调用 mark_failed 不改变状态"""
        import shutil, tempfile
        tmp = tempfile.mkdtemp(prefix="kiana_r5_")
        try:
            async def go():
                db = FrontierDB(str(Path(tmp) / "f.db"))
                await db.init_async()
                try:
                    await db.push("https://example.com/r5")
                    await db.flush()
                    b = await db.pop_batch(limit=1)
                    uh = b[0]["url_hash"]
                    await db.mark_done(uh)                 # 置 done
                    await db.flush()
                    await db.mark_failed(uh, retry=True)   # 模拟看门狗误标
                    await db.flush()
                    cur = await db._read_conn.execute(
                        "SELECT status FROM frontier WHERE url_hash=?", (uh,))
                    self.assertEqual((await cur.fetchone())[0], "done",
                                     "已完成的页不得被打回 retry（否则重复导出）")
                finally:
                    await db.close()
            _run(go())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestMarkDoneCheckedTriState(unittest.TestCase):
    """R6：mark_done_checked 三态（True 赢 / False 易主 / None 落库失败）"""

    def test_returns_three_states(self):
        import kiana_vnext_plus.frontier as fm
        src = inspect.getsource(fm.FrontierDB.mark_done_checked)
        self.assertIn("return None", src, "落库失败应返回 None（区别于租约易主的 False）")

    def test_win_returns_true_lose_returns_false(self):
        import shutil, tempfile
        tmp = tempfile.mkdtemp(prefix="kiana_r6_")
        try:
            async def go():
                db = FrontierDB(str(Path(tmp) / "f.db"))
                await db.init_async()
                try:
                    await db.push("https://example.com/r6")
                    await db.flush()
                    b = await db.pop_batch(limit=1)
                    uh, leased = b[0]["url_hash"], b[0]["leased_at"]
                    self.assertIs(await db.mark_done_checked(uh, leased), True, "首次应赢")
                    # 已 done → 再打卡（陈旧租约）应 False（易主语义）
                    self.assertIs(await db.mark_done_checked(uh, leased), False)
                finally:
                    await db.close()
            _run(go())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestRedisBackendSignatureAligned(unittest.TestCase):
    """R7：Redis 后端签名须与 FrontierDB 对齐（否则 304 分支 TypeError）"""

    def test_redis_frontier_signatures(self):
        from kiana_vnext_plus.redis_frontier import RedisFrontier
        for name, params in (("mark_done", ("leased_at",)),
                             ("mark_failed", ("throttled",)),
                             ("mark_done_checked", ()),
                             ("heartbeat_lease", ())):
            self.assertTrue(hasattr(RedisFrontier, name), f"RedisFrontier 应实现 {name}")
            sig = inspect.signature(getattr(RedisFrontier, name))
            for p in params:
                self.assertIn(p, sig.parameters, f"{name} 应含形参 {p}")


if __name__ == "__main__":
    unittest.main()
