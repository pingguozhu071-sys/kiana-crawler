# -*- coding: utf-8 -*-
"""低速卡死看门狗 —— "什么时候该放弃一条传输"的**唯一判据**（回归 + 反向断言）

## 真机病象（用户 2026-10-03 那趟抓取的时间线）

```
[  71s] 100.0% done=20 fail=1 skip=74     ← 到这里其实已经基本干完了
...                                        ← 又空转 217 秒
[ 288s] 100.0%
最后: ERROR: [download] Got error: 8606 bytes read, 2693189 more expected.
                     Giving up after 10 retries
```

那是一个约 2.6MB 的分片，速度掉到 **7~15 KiB/s**。

## 定位结论（先查清，再改）

那句话是 **yt-dlp 自己的**，而句中那个 "10" 就是**本工程写下的**配置：

| 配置 | 位置 | 作用 |
|---|---|---|
| `'retries': 10` | `universal_downloader.download_video` 的 `opts_base` | yt-dlp 的"重试 10 次" |
| `'fragment_retries': 10` | 同上 | 分片重试 10 次 |
| `'socket_timeout': 30` | 同上 | 单次**读空闲**超时 30s |

**工程侧此前没有任何"速度/时长"上限** —— 只有"重试多少次"，而重试次数对"慢"
这件事完全不敏感：它只会让慢的东西慢 10 遍。直连那条路（`stream_to_file`）也只有
curl 的 `timeout=180`（整段上限）与 `retries`，同样与"速度"无关。

## 判据（这次新增）

`StallWatchdog`：**观察窗内的平均速率**低于阈值才叫停 ——
不是"瞬时慢"，因为**用户可能在慢网络上跑**。三条防误判写在类 docstring 里。

## 本文件守的两件事

| 断言 | 为什么 |
|---|---|
| 卡死的传输**被及时放弃**（且不重试） | 那是 217 秒的浪费 |
| **正常慢速不被打断** | 任务书明确：不许因为想提速就砍掉慢网下载 |
"""
import ast
import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PKG = ROOT / "kiana_vnext_plus"

from kiana_vnext_plus import universal_downloader as ud          # noqa: E402
from kiana_vnext_plus.universal_downloader import (              # noqa: E402
    LowSpeedAbort, StallWatchdog, stream_to_file, ytdlp_progress_hooks,
)

KIB = 1024


class _FakeClock:
    """假钟：`advance()` 推进时间，测试不真等 90 秒。"""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def _fn_body(mod_name: str, fn_name: str) -> str:
    src = (PKG / mod_name).read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == fn_name), None)
    if fn is None:
        raise AssertionError(f"找不到函数 {fn_name}（{mod_name}）")
    return ast.unparse(fn)


# ══════════════════════════════════════════════════════════════════════════
# 判据本身
# ══════════════════════════════════════════════════════════════════════════
class TestWatchdogAbortsStuck(unittest.TestCase):
    def test_real_machine_rate_is_aborted(self):
        """**真机那档**：7~15 KiB/s 的滴流，一个观察窗后必须叫停。"""
        for rate in (7 * KIB, 10 * KIB, 15 * KIB):
            clk = _FakeClock()
            wd = StallWatchdog(clock=clk)
            total = 0
            wd.note_bytes(0)                        # 定基线
            with self.assertRaises(LowSpeedAbort, msg=f"{rate / KIB:.0f} KiB/s 没被叫停"):
                for _ in range(60):                 # 每 2 秒喂一次，跑满 120 秒
                    clk.advance(2.0)
                    total += int(rate * 2.0)
                    wd.note_bytes(total)
            self.assertTrue(wd.aborted, "抛了异常但 aborted 标志没置上")

    def test_zero_progress_is_aborted(self):
        """完全不动的传输同样叫停（喂进来的累计值一直不变）。"""
        clk = _FakeClock()
        wd = StallWatchdog(clock=clk)
        wd.note_bytes(200 * KIB)                    # 已经拿到 200KiB
        with self.assertRaises(LowSpeedAbort):
            for _ in range(20):
                clk.advance(10.0)
                wd.note_bytes(200 * KIB)            # 一个字节都不再动

    def test_abort_message_carries_the_evidence(self):
        """报错必须**带数字** —— 否则排查时看不出"到底是多慢"。"""
        clk = _FakeClock()
        wd = StallWatchdog(clock=clk)
        wd.note_bytes(100 * KIB)
        with self.assertRaises(LowSpeedAbort) as ctx:
            for _ in range(20):
                clk.advance(10.0)
                wd.note_bytes(100 * KIB)
        msg = str(ctx.exception)
        self.assertIn("KiB/s", msg)
        self.assertIn("阈值", msg)


class TestWatchdogDoesNotKillHealthySlow(unittest.TestCase):
    """⚠️ 反向断言组：**误杀正常慢速下载比多等 90 秒坏得多**。"""

    def test_slow_but_moving_128kib_survives(self):
        """128 KiB/s —— 已经是"很慢的网络"了，但它是**在动**的，不许打断。"""
        clk = _FakeClock()
        wd = StallWatchdog(clock=clk)
        total = 0
        wd.note_bytes(0)
        for _ in range(300):                        # 跑 600 秒
            clk.advance(2.0)
            total += int(128 * KIB * 2.0)
            wd.note_bytes(total)                    # 不抛 = 通过
        self.assertFalse(wd.aborted)

    def test_bursty_then_slow_average_saves_it(self):
        """窗口内来一发快传 → 均值被拉高 → 不叫停（这正是用"窗内均速"的理由）。"""
        clk = _FakeClock()
        wd = StallWatchdog(clock=clk)
        wd.note_bytes(0)
        total = 0
        for i in range(20):
            clk.advance(5.0)
            # 每 5 秒里前一段慢、最后一发补上 4MiB —— 窗内均值仍远高于阈值
            total += 512 * KIB
            wd.note_bytes(total)
            if i % 3 == 2:
                clk.advance(5.0)
                total += 8 * 1024 * KIB
                wd.note_bytes(total)
        self.assertFalse(wd.aborted)

    def test_below_min_bytes_never_judged(self):
        """还没拿到起步字节数之前不判"慢" —— 那是"连不上"，归超时管，不归本条。"""
        clk = _FakeClock()
        wd = StallWatchdog(clock=clk)
        wd.note_bytes(0)
        for _ in range(50):
            clk.advance(30.0)
            wd.note_bytes(1024)                     # 一直只有 1KiB（< 64KiB 起步线）
        self.assertFalse(wd.aborted)

    def test_new_attempt_resets_baseline(self):
        """换档/换源/新一轮尝试会**重新从 0 计数** —— 不许把它算成"速度为负"。"""
        clk = _FakeClock()
        wd = StallWatchdog(clock=clk)
        wd.note_bytes(5 * 1024 * KIB)               # 上一档已经下了 5MiB
        clk.advance(600.0)                          # 中间隔了很久（失败+退避）
        wd.note_bytes(0)                            # 新一档从 0 开始
        self.assertFalse(wd.aborted, "计数被重置时误判成了卡死")
        total = 0
        for _ in range(30):                         # 新一档跑得正常
            clk.advance(5.0)
            total += 1024 * KIB
            wd.note_bytes(total)
        self.assertFalse(wd.aborted)

    def test_single_shot_note_never_aborts(self):
        """只喂一次就再也没消息（yt-dlp 一个字节都没读到）→ 本条不叫停。"""
        clk = _FakeClock()
        wd = StallWatchdog(clock=clk)
        wd.note_bytes(0)
        clk.advance(10_000.0)
        self.assertFalse(wd.aborted)


# ══════════════════════════════════════════════════════════════════════════
# 接到两个下载原语上
# ══════════════════════════════════════════════════════════════════════════
class _StubSession:
    """桩会话：按给定的 (字节块, 每块耗时秒) 吐数据，并记录被调用次数。"""

    def __init__(self, chunks, seconds_per_chunk):
        self.chunks = list(chunks)
        self.seconds_per_chunk = seconds_per_chunk
        self.calls = 0
        self._clk = None

    def bind_clock(self, clk):
        self._clk = clk

    async def get(self, url, headers=None, proxy=None, timeout=180, stream=True, **k):
        self.calls += 1
        sess = self

        class _Resp:
            status_code = 200
            headers = {}

            async def aiter_content(self):
                for c in sess.chunks:
                    if sess._clk is not None:
                        sess._clk.advance(sess.seconds_per_chunk)
                    yield c

            def close(self):
                pass

        return _Resp()


def _run_stream(tmp_path, chunks, secs_per_chunk, name, retries=3):
    """用假钟跑一遍 `stream_to_file`，返回 (结果, 桩会话)。"""
    clk = _FakeClock()
    sess = _StubSession(chunks, secs_per_chunk)
    sess.bind_clock(clk)

    _real = ud.StallWatchdog
    ud.StallWatchdog = lambda *a, **k: _real(clock=clk)
    try:
        async def flow():
            return await stream_to_file(sess, "http://x/f.bin", tmp_path / name,
                                        {}, retries=retries)
        return asyncio.run(flow()), sess
    finally:
        ud.StallWatchdog = _real


class TestDirectDownloadPath(unittest.TestCase):
    def test_stuck_transfer_is_abandoned_without_retrying(self):
        """卡死的直连：**放弃**，而且**不重试**（重试一条卡死的传输正是要修的那件事）。"""
        import tempfile
        td = Path(tempfile.mkdtemp(prefix="kiana_stall_"))
        # 每 5 秒只给 32KiB（≈6.5 KiB/s）——真机那档
        chunks = [b"x" * (32 * KIB)] * 100
        out, sess = _run_stream(td, chunks, 5.0, "stuck.bin", retries=3)
        self.assertIsNone(out, "卡死的传输居然下完了")
        self.assertEqual(sess.calls, 1,
                         f"卡死后还重试了（会话被调用 {sess.calls} 次）——那正是 217 秒的由来")

    def test_healthy_slow_transfer_still_completes(self):
        """**反向断言**：慢但在动的直连必须照常下完（不许被看门狗打断）。"""
        import tempfile
        td = Path(tempfile.mkdtemp(prefix="kiana_slow_"))
        # 每 5 秒 640KiB（=128 KiB/s）：很慢，但在动
        chunks = [b"y" * (640 * KIB)] * 8
        out, sess = _run_stream(td, chunks, 5.0, "slow.bin", retries=1)
        self.assertIsNotNone(out, "正常慢速下载被打断了（误杀）")
        self.assertEqual(out.stat().st_size, 640 * KIB * 8)


class TestWatchdogIsWired(unittest.TestCase):
    def test_ytdlp_hook_only_feeds_on_downloading(self):
        clk = _FakeClock()
        wd = StallWatchdog(clock=clk)
        hook = ytdlp_progress_hooks(wd)[0]
        hook({"status": "finished", "downloaded_bytes": 10 ** 9})
        hook({"status": "error"})
        hook(None)
        self.assertFalse(wd._started, "非 downloading 状态不该喂进看门狗")

    def test_ytdlp_hook_does_not_swallow_the_abort(self):
        """看门狗的异常**必须从 hook 抛出去** —— yt-dlp 的 `_hook_progress` 外面
        没有 try/except（已读源码确认），抛出去才能中断这次传输。"""
        clk = _FakeClock()
        wd = StallWatchdog(clock=clk)
        hook = ytdlp_progress_hooks(wd)[0]
        hook({"status": "downloading", "downloaded_bytes": 200 * KIB})
        with self.assertRaises(LowSpeedAbort):
            for _ in range(20):
                clk.advance(10.0)
                hook({"status": "downloading", "downloaded_bytes": 200 * KIB})

    def test_both_ytdlp_paths_install_the_hook(self):
        """**两条 yt-dlp 路都要装**（视频 / 音频）——只装一条就是"修了一半"，
        而"同一能力多份实现只改一份"正是本工程连续栽过的那个坑。"""
        for fn in ("download_video", "download_audio"):
            self.assertIn("ytdlp_progress_hooks", _fn_body("universal_downloader.py", fn),
                          f"{fn} 没装看门狗")

    def test_direct_path_installs_the_watchdog(self):
        self.assertIn("StallWatchdog", _fn_body("universal_downloader.py", "_stream_one"),
                      "_stream_one（直连/断点续传）没装看门狗")

    def test_abort_short_circuits_format_chain(self):
        """看门狗已叫停时必须**直接收工**，不许继续换档 ——
        否则"慢"会被乘以格式档数（慢 3 倍）。"""
        body = _fn_body("universal_downloader.py", "download_video")
        self.assertGreaterEqual(body.count("_wd.aborted"), 2,
                                "download_video 里没有在异常/返回两处都判 aborted")


if __name__ == "__main__":
    unittest.main()
