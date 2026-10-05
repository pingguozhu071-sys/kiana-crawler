"""v2.19.10 回归：撞页数上限后的收尾，不许再干等"注定被跳过"的在途/排队任务。

**真机判据**（2026-10-04 打包版，`max_pages=150`；证据 = 该次运行的 crawl.log 与
`cli_a169d579/stats.jsonl` 逐批计数）：

    t=1027.3s  done=150（上限达成）pending=587   ← 最后一个真批次在这里就返回了
    t=1027.7→1035.8s  batch=50 ×11 轮            ← `_batch_cap` 设计内的"快速排空"
    t=1035.8→1069.6s  只剩 6 条未到重试排期的行，1 秒一轮**空转 33.8 秒** ← 本文件盯的就是它
    t=1069.6s  pending=0 → break → 收尾；t=1069.9s Shutdown complete.（只花 0.3s）
    t=1069.9→1462.2s  LLM 增强 392.3 秒（另一件事，不在本文件范围）

本文件的两条主线：
  ① **上限达成后不再等"排期未到"的重试行**（它们到期也只会被判超限跳过）——
     取批带 `ignore_schedule=True`，计数口径**不变**（仍记 skipped，不是 failed）；
  ② **上限达成后才取到的那批任务**（唯一去向是 skip）只给收尾窗口，超时即取消 +
     如实告警 + 按"撞上限跳过"记账，且**不留下** pending/retry 半写状态。

判据一律查**结构**（进度计数器 / DB 终态 / 取批实参 / 是否真的取消），不查日志文案；
唯一的文本断言是"取消必须如实说出来（含数量）"——因为静默正是这条缺陷最坏的部分。
"""
import asyncio
import logging
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from kiana_vnext_plus import crawler as crawler_mod  # noqa: E402
from kiana_vnext_plus.crawler import Crawler, _batch_cap, _cap_reached  # noqa: E402

MAXP = 150          # 与真机那趟同值：done 花完时 room<=0 ⇒ 走"排空"分支
GRACE = 0.5         # 测试里的收尾窗口（真机默认 15s，见 crawler._CAP_TAIL_GRACE）


# ══════════════════════════════════════════════════════════════════════
# 夹具：真 Crawler（真 FrontierDB、真 run 循环）+ win32 侧零副作用桩
# （与 tests/test_v2162_engine_integration.py 的 _make_proj_crawler 同源）
# ══════════════════════════════════════════════════════════════════════

def _make_crawler(tmp_path, monkeypatch, max_pages=MAXP):
    import kiana_vnext_plus.win32_native as w32
    monkeypatch.setattr(w32, "apply_all_optimizations", lambda *a, **k: None)
    monkeypatch.setattr(w32, "ensure_high_performance_power_plan", lambda *a, **k: None)
    monkeypatch.setattr(w32, "restore_power_plan", lambda *a, **k: None)
    monkeypatch.setattr(w32, "memory_recycle_loop", lambda *a, **k: asyncio.sleep(3600))
    from omegaconf import OmegaConf
    from kiana_vnext_plus.config import GlobalConfig
    from kiana_vnext_plus.identity import ProjectIdentity
    from kiana_vnext_plus.p1_enhancements import HttpCache
    proj = ProjectIdentity(str(tmp_path / "proj"))
    proj.dir.mkdir(parents=True, exist_ok=True)
    proj.config.limits.max_pages = max_pages
    gcfg = GlobalConfig(OmegaConf.create({
        "log_level": "WARNING", "download_path": str(tmp_path),
        "privacy_sanitize": True,
        "video_download_enabled": False, "image_download_enabled": False,
        "audio_download_enabled": False,
    }))
    crawler = Crawler(proj, gcfg)
    # http_cache 默认落全局 data_root()（会污染真实数据根，conftest 有护栏）
    crawler.http_cache = HttpCache(cache_dir=tmp_path / "hc", ttl=3600)
    return proj, crawler


def _seed_rows(proj, n, status, ahead=0.0):
    """直接落库 n 行：status='done' 模拟"上限已花完"，'retry' + 未来排期模拟
    "退避还没到期的重试行"（真机那 6 条就是这个形状）。"""
    conn = sqlite3.connect(proj.get_db_path(), timeout=30)
    try:
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO frontier "
                "(url_hash, normalized_url, domain, depth, priority, status, scheduled_at, retry_count) "
                "VALUES (?,?,?,?,?,?,?,?)",
                [(f"h_{status}_{i}", f"https://seed.test/{status}/{i}", "seed.test", 0, 5,
                  status, time.time() + ahead, 1 if status == "retry" else 0)
                 for i in range(n)])
    finally:
        conn.close()


def _statuses(proj):
    conn = sqlite3.connect(proj.get_db_path(), timeout=30)
    try:
        return dict(conn.execute(
            "SELECT status, COUNT(*) FROM frontier GROUP BY status").fetchall())
    finally:
        conn.close()


def _hanging_processor(release=None):
    """在途请求"永不返回"的假处理器；给 release 就能在断言之后放它走。"""
    async def _job(job):
        if release is None:
            await asyncio.Event().wait()
        else:
            await release.wait()
        return None
    return _job


# ══════════════════════════════════════════════════════════════════════
# ① 上限达成 + 在途/排队任务永不返回 ⇒ 收尾必须短窗口内结束，且口径不变
# ══════════════════════════════════════════════════════════════════════

def test_cap_tail_is_bounded_and_recorded_as_skipped(tmp_path, monkeypatch, caplog):
    """撞上限后取到的批次：窗口内取消 + 如实告警 + 记 `skipped`（**不是** failed）。"""
    monkeypatch.setattr(crawler_mod, "_CAP_TAIL_GRACE", GRACE)
    proj, crawler = _make_crawler(tmp_path, monkeypatch)
    _seed_rows(proj, MAXP, "done")            # 150 页额度已花完 ⇒ 走排空分支
    _seed_rows(proj, 3, "retry", ahead=600)   # 3 条"退避未到期"的重试行（真机 6 条同形）
    _pops = []

    async def flow():
        await crawler.setup()
        crawler.processor.process_job = _hanging_processor()   # 在途请求永不返回
        _orig_pop = crawler.frontier.pop_batch

        async def _spy_pop(limit=10, worker_id="worker1", strategy="bfs",
                           ignore_schedule=False):
            _pops.append(ignore_schedule)
            return await _orig_pop(limit, worker_id=worker_id, strategy=strategy,
                                   ignore_schedule=ignore_schedule)

        crawler.frontier.pop_batch = _spy_pop
        t0 = time.monotonic()
        await crawler.run([])
        return time.monotonic() - t0

    with caplog.at_level(logging.INFO):
        elapsed = asyncio.run(flow())

    # 结构判据 1：收尾有界（改动前这里会永远等下去 —— 见下一个测试的红样本）
    assert elapsed < 4.0, (
        f"撞上限后的收尾用了 {elapsed:.1f}s（窗口 {GRACE}s）—— 又在为不会产出任何东西的"
        f"在途/排队任务干等")
    # 结构判据 2：撞上限后的取批必须无视重试排期（否则就是那 33.8 秒空转的来源）
    assert _pops and _pops[0] is True, (
        f"上限达成后取批没有带 ignore_schedule=True（实参序列 {_pops}）—— "
        f"未到排期的重试行取不到 ⇒ 1 秒一轮空转")
    # 结构判据 3：口径 —— 记 skipped，绝不记 failed（在途请求不是失败）
    assert crawler._progress["skipped"] == 3, crawler._progress
    assert crawler._progress["failed"] == 0, (
        f"在途请求被记成了 failed：{crawler._progress}（撞上限是跳过的口径）")
    # 结构判据 4：不留下半写状态 —— 被取消的 3 行必须落到终端态（超限跳过=dead，
    # 与 page_processor 的超限分支同一口径），队列里不许再剩 pending/retry
    st = _statuses(proj)
    assert st.get("dead") == 3, f"被取消的行没落终端态: {st}"
    assert st.get("pending", 0) == 0 and st.get("retry", 0) == 0, f"队列没排空: {st}"
    # 结构判据 5（唯一的文本断言）：取消必须**如实说出来**并带上数量 —— 静默取消
    # 比空转更坏（用户会以为那些页被正常处理过）
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("取消" in m and "3" in m for m in msgs), f"取消在途请求没有如实告警: {msgs}"


def test_future_scheduled_rows_are_drained_as_skips_without_waiting(tmp_path, monkeypatch):
    """**真机那 6 条的形状**：上限已达成 + 退避还差几百秒 ⇒ 立刻按超限跳过排空。

    这一条盯的是"计数口径不变"：改动前后这 6 行都该记 `skipped`、都该离开队列 ——
    改的只是**不再为它们的退避干等**（真机 33.8 秒）。
    """
    monkeypatch.setattr(crawler_mod, "_CAP_TAIL_GRACE", GRACE)
    proj, crawler = _make_crawler(tmp_path, monkeypatch)
    _seed_rows(proj, MAXP, "done")
    _seed_rows(proj, 6, "retry", ahead=600)     # 6 条，退避还差几百秒（= 真机那 6 条）

    async def _cap_skip_only(job):
        # 等价于真 process_job 开头的超限分支：行移出队列 + 记 skipped（不是 failed）
        await crawler.frontier.mark_failed(job["url_hash"], retry=False)
        crawler.processor._update_progress("skipped")

    async def flow():
        await crawler.setup()
        crawler.processor.process_job = _cap_skip_only
        t0 = time.monotonic()
        await crawler.run([])
        return time.monotonic() - t0

    elapsed = asyncio.run(flow())
    assert elapsed < 4.0, (
        f"未到期的重试行仍被 1 秒一轮地等：{elapsed:.1f}s（真机同形状空转 33.8 秒）")
    assert crawler._progress["skipped"] == 6, (
        f"这 6 行的记账口径变了：{crawler._progress}（改动前后都该是 skipped=6）")
    assert crawler._progress["failed"] == 0, crawler._progress
    st = _statuses(proj)
    assert st.get("dead") == 6 and st.get("retry", 0) == 0, f"队列没排空: {st}"


def test_cap_raised_mid_flight_keeps_waiting_for_real_products(tmp_path, monkeypatch):
    """**防丢产物的例外**：窗口到期时若 `max_pages` 已被调高（GUI 可热改），
    这批任务就可能真的产出页面 ⇒ 窗口作废、继续等，**不许**按超限跳过砍掉。"""
    monkeypatch.setattr(crawler_mod, "_CAP_TAIL_GRACE", GRACE)
    proj, crawler = _make_crawler(tmp_path, monkeypatch)
    _seed_rows(proj, MAXP, "done")
    _seed_rows(proj, 2, "retry", ahead=0)
    release = asyncio.Event()

    async def _job_then_raise(job):
        # 任务在跑的过程中把上限调高（GUI 设置页热改的等价物）
        proj.config.limits.max_pages = MAXP + 500
        await release.wait()
        return None

    async def flow():
        await crawler.setup()
        crawler.processor.process_job = _job_then_raise
        task = asyncio.create_task(crawler.run([]))
        with pytest.raises(asyncio.TimeoutError):
            # 窗口早就到点了，但上限已调高 ⇒ 必须继续等（不许取消）
            await asyncio.wait_for(asyncio.shield(task), 2.0)
        release.set()
        await task
        return True

    asyncio.run(flow())
    assert crawler._progress["skipped"] == 0 and crawler._progress["failed"] == 0, (
        f"上限被调高后仍按超限跳过砍了在途任务: {crawler._progress}")
    st = _statuses(proj)
    assert st.get("dead", 0) == 0, f"上限被调高后仍把行标死: {st}"


def test_prefix_shape_would_wait_forever_proof_that_bound_is_not_vacuous(
        tmp_path, monkeypatch):
    """**反空断言样本**：把行为改回改动前（无窗口 + 取批不无视排期），同一个场景
    就不再收尾 —— 证明上面那条"收尾有界"确实是这次改动带来的，不是环境凑出来的。

    改动前形状 = ① `_await_capped_batch` 退回裸 `asyncio.gather`
                ② `pop_batch` 没有 `ignore_schedule`（消费方也传不进去）
    """
    monkeypatch.setattr(crawler_mod, "_CAP_TAIL_GRACE", GRACE)
    proj, crawler = _make_crawler(tmp_path, monkeypatch)
    _seed_rows(proj, MAXP, "done")     # 上限同样已花完
    _seed_rows(proj, 2, "retry", ahead=0)   # 这两条是"取得到"的（排期已到）
    release = asyncio.Event()

    async def _prefix_await_batch(self, tasks, batch):
        # 改动前：没有窗口，直接等（在途请求永不返回 ⇒ 这里也永远不返回）
        return await asyncio.gather(*tasks, return_exceptions=True)

    monkeypatch.setattr(Crawler, "_await_capped_batch", _prefix_await_batch)

    async def flow():
        await crawler.setup()
        crawler.processor.process_job = _hanging_processor(release)
        _orig_pop = crawler.frontier.pop_batch

        async def _prefix_pop(limit=10, worker_id="worker1", strategy="bfs",
                              ignore_schedule=False):
            # 改动前：签名里根本没有 ignore_schedule（传了也不生效）
            return await _orig_pop(limit, worker_id=worker_id, strategy=strategy)

        crawler.frontier.pop_batch = _prefix_pop
        task = asyncio.create_task(crawler.run([]))
        t0 = time.monotonic()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), 2.0)
        _waited = time.monotonic() - t0
        release.set()          # 放走在途请求，收尾流程正常结束
        await task
        return _waited

    _waited = asyncio.run(flow())
    assert _waited >= 2.0, "改动前形状竟也提前收尾了——本测试的前提不成立"
    assert crawler._progress["skipped"] == 0 and crawler._progress["failed"] == 0, (
        f"改动前形状竟然还记了账: {crawler._progress}")


def test_no_cap_means_legit_inflight_work_is_never_cancelled(tmp_path, monkeypatch):
    """**上限没达成时，在途任务一秒都不许提前砍**（那是真产物，不是"注定被跳过"）。"""
    monkeypatch.setattr(crawler_mod, "_CAP_TAIL_GRACE", GRACE)
    proj, crawler = _make_crawler(tmp_path, monkeypatch, max_pages=5000)
    _seed_rows(proj, 2, "pending", ahead=0)
    release = asyncio.Event()

    async def flow():
        await crawler.setup()
        crawler.processor.process_job = _hanging_processor(release)
        task = asyncio.create_task(crawler.run([]))
        # 窗口到点也不许取消：上限没达成 ⇒ 它的结果仍然算数
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), 2.0)
        release.set()
        await task
        return True

    asyncio.run(flow())
    assert crawler._progress["skipped"] == 0 and crawler._progress["failed"] == 0, (
        f"未达上限却动了在途任务的记账: {crawler._progress}")
    st = _statuses(proj)
    assert st.get("dead", 0) == 0 and st.get("retry", 0) == 0, f"未达上限却改了行状态: {st}"


def test_backend_ignoring_ignore_schedule_still_stops_spinning(tmp_path, monkeypatch,
                                                              caplog):
    """**降级兜底**：若后端不认 `ignore_schedule`（例如 Redis 后端的降级路径），
    上限达成后**也不许**回到"1 秒一轮等退避"——如实报出还剩多少条再收尾。"""
    monkeypatch.setattr(crawler_mod, "_CAP_TAIL_GRACE", GRACE)
    proj, crawler = _make_crawler(tmp_path, monkeypatch)
    _seed_rows(proj, MAXP, "done")
    _seed_rows(proj, 2, "retry", ahead=600)   # 未来排期 + 后端不肯无视它

    async def flow():
        await crawler.setup()
        crawler.processor.process_job = _hanging_processor()
        _orig_pop = crawler.frontier.pop_batch

        async def _stubborn_pop(limit=10, worker_id="worker1", strategy="bfs",
                                ignore_schedule=False):
            return await _orig_pop(limit, worker_id=worker_id, strategy=strategy)

        crawler.frontier.pop_batch = _stubborn_pop
        t0 = time.monotonic()
        await crawler.run([])
        return time.monotonic() - t0

    with caplog.at_level(logging.INFO):
        elapsed = asyncio.run(flow())
    assert elapsed < 4.0, (
        f"后端没照做 ignore_schedule 时又空转了 {elapsed:.1f}s —— 兜底分支没生效")
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("未到重试排期" in m and "2" in m for m in msgs), (
        f"提前收尾时没有如实报出还剩多少条任务: {msgs}")
    # 这些行没被消费 ⇒ 老老实实留在 retry（不是假装处理过、也不是记成 failed）
    st = _statuses(proj)
    assert st.get("retry") == 2, f"未到期的行被改写了状态: {st}"


# ══════════════════════════════════════════════════════════════════════
# ③ 后端语义：ignore_schedule 只绕过"排期"，不绕过任何正确性判据
# ══════════════════════════════════════════════════════════════════════

def test_pop_batch_ignore_schedule_only_bypasses_backoff(tmp_path):
    from kiana_vnext_plus.frontier import FrontierDB

    db = FrontierDB(str(tmp_path / "frontier.db"))

    async def flow():
        await db.init_async()
        try:
            for i in range(3):
                await db.push(f"https://bo.test/{i}")
            await db.flush()
            _leased = await db.pop_batch(limit=10)          # 先租出来才拿得到 url_hash
            assert len(_leased) == 3
            for _j in _leased:
                # delay=600 ⇒ 退避 = 600×1.8^n×U(0.7,1.3)（封顶 600）⇒ 排期在几百秒之后
                await db.mark_failed(_j["url_hash"], retry=True, delay=600)
            await db.flush()

            assert await db.pop_batch(limit=10) == [], (
                "未到排期的重试行被普通取批取走了——那正是真机上 33.8 秒空转的来源")
            drained = await db.pop_batch(limit=10, ignore_schedule=True)
            assert len(drained) == 3, f"撞上限排空必须能取到未到期的重试行: {len(drained)}"

            # 不绕过正确性：重试次数用尽的行仍由 pop_batch 自己判死，不许被排空放出来
            await db.flush()
            _c = sqlite3.connect(str(tmp_path / "frontier.db"), timeout=30)
            try:
                with _c:
                    _c.execute("UPDATE frontier SET retry_count = max_retries")
            finally:
                _c.close()
            assert await db.pop_batch(limit=10, ignore_schedule=True) == [], (
                "ignore_schedule 把'重试次数用尽'的判据也绕过了——排空不是绕过正确性")
        finally:
            await db.close()

    asyncio.run(flow())


# ══════════════════════════════════════════════════════════════════════
# ④ 判据单一来源：取批与收尾不许各判一套
# ══════════════════════════════════════════════════════════════════════

def test_cap_judgement_has_a_single_source():
    assert _cap_reached(150, 150) is True
    assert _cap_reached(150, 149) is False
    assert _cap_reached(3, 10) is True            # 已经超了（历史遗留/上限被调小）
    assert _cap_reached(1, 1) is True
    # 取批侧必须与同一判据一致：额度用完 ⇒ 取满（快速排空，而不是收窄到 0 变成空转）
    assert _cap_reached(3, 3) and _batch_cap(3, 3) == 50
    assert _cap_reached(3, 999) and _batch_cap(3, 999) == 50
    # 额度没用完 ⇒ 批量必须 ≤ 剩余额度（v2.19.9 的"上限拦不住一个批"教训）
    assert not _cap_reached(3, 1) and _batch_cap(3, 1) == 2
    assert not _cap_reached(100, 90) and _batch_cap(100, 90) == 10
    assert not _cap_reached(5000, 0) and _batch_cap(5000, 0) == 50
    # 窗口是个"短"窗口（不是把 150 秒排空窗口换汤不换药）
    assert 0 < float(crawler_mod._CAP_TAIL_GRACE) <= 30.0
