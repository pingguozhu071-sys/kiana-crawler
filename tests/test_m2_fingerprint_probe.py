# -*- coding: utf-8 -*-
"""结构指纹与变化告警回归（M2-a）

要锁死的核心区别：
  · **结构没变、文本变了 → 指纹不变**（这才使它区别于全文哈希——全文哈希改个日期
    就报警，噪声大到没法用）；
  · **结构变了 → 指纹变**；
  · **探针失败（取不到/解析不了/被拦）不计入"结构变了"**，也不累计连续次数——
    站点临时抽风不能把规则判死。这是本模块最容易写错的一条。
"""
import asyncio
import os
import shutil
import tempfile
import unittest

from kiana_vnext_plus import fingerprint_probe as fp


class _Resp:
    """最小响应替身：探针只读 `.text`"""

    def __init__(self, text):
        self.text = text

SAME_STRUCT_A = """
<html><body><div class="wrap"><ul class="list"><li>alpha</li><li>beta</li></ul></div></body></html>
"""
SAME_STRUCT_B = """
<html><body><div class="wrap"><ul class="list"><li>完全不同的文字</li><li>2026-10-01</li></ul></div></body></html>
"""
DIFF_STRUCT = """
<html><body><section class="hero"><article><h1>t</h1><p>b</p></article></section></body></html>
"""


class TestStructureTokens(unittest.TestCase):
    def test_tokens_are_structural_only(self):
        toks = fp.structure_tokens(SAME_STRUCT_A)
        self.assertIn("div", toks)
        self.assertIn("div.wrap", toks)
        self.assertIn("ul.list", toks)
        self.assertNotIn("alpha", toks, "文本内容不得进 token（否则等同全文哈希）")

    def test_script_style_skipped(self):
        html = "<html><body><script>var x=1;</script><style>.a{}</style><p>hi</p></body></html>"
        toks = fp.structure_tokens(html)
        self.assertNotIn("script", toks)
        self.assertNotIn("style", toks)
        self.assertIn("p", toks)

    def test_garbage_returns_empty(self):
        self.assertEqual(fp.structure_tokens(""), [])
        self.assertEqual(fp.structure_tokens(None), [])


class TestFingerprint(unittest.TestCase):
    def test_text_change_does_not_move_fingerprint(self):
        """**本文件最重要的一条**：文案/日期变了，结构指纹必须不动。"""
        a = fp.structure_fingerprint(SAME_STRUCT_A)
        b = fp.structure_fingerprint(SAME_STRUCT_B)
        self.assertNotEqual(a, 0)
        self.assertEqual(a, b, "文本变化不得影响结构指纹（否则就是全文哈希）")
        self.assertEqual(fp.similarity(a, b), 1.0)

    def test_structure_change_moves_fingerprint(self):
        a = fp.structure_fingerprint(SAME_STRUCT_A)
        c = fp.structure_fingerprint(DIFF_STRUCT)
        self.assertNotEqual(a, c)
        self.assertLess(fp.similarity(a, c), 0.90, "结构大改后相似度应跌到阈值以下")

    def test_unparsable_or_empty_is_zero(self):
        self.assertEqual(fp.structure_fingerprint(""), 0)
        self.assertEqual(fp.similarity(0, 123), 0.0)


class TestDecide(unittest.TestCase):
    def test_ok_when_similar(self):
        a = fp.structure_fingerprint(SAME_STRUCT_A)
        v, sim, n = fp.decide(a, a, strikes=2)
        self.assertEqual(v, fp.VERDICT_OK)
        self.assertEqual(n, 0, "恢复正常必须把连续计数清零")

    def test_escalates_warn_then_changed(self):
        a = fp.structure_fingerprint(SAME_STRUCT_A)
        c = fp.structure_fingerprint(DIFF_STRUCT)
        v1, _, n1 = fp.decide(a, c, strikes=0)
        v2, _, n2 = fp.decide(a, c, strikes=n1)
        v3, _, n3 = fp.decide(a, c, strikes=n2)
        self.assertEqual((v1, n1), (fp.VERDICT_WARN, 1), "单次噪声只告警")
        self.assertEqual((v2, n2), (fp.VERDICT_WARN, 2))
        self.assertEqual(v3, fp.VERDICT_CHANGED, "连续 3 次才判结构变了")
        self.assertEqual(n3, 3)

    def test_probe_failure_does_not_count_a_strike(self):
        """**探针失败 ≠ 结构变化**：不累计、不判死（最容易写错的一条）"""
        a = fp.structure_fingerprint(SAME_STRUCT_A)
        v, sim, n = fp.decide(a, 0, strikes=2)          # 页面取不到
        self.assertEqual(v, fp.PROBE_FAILED)
        self.assertEqual(n, 2, "探针失败不得累计连续次数")
        v2, _, n2 = fp.decide(0, a, strikes=2)          # 基准侧缺失
        self.assertEqual(v2, fp.PROBE_FAILED)
        self.assertEqual(n2, 2)

    def test_probe_failure_never_reaches_changed(self):
        a = fp.structure_fingerprint(SAME_STRUCT_A)
        n = 0
        for _ in range(10):
            v, _, n = fp.decide(a, 0, strikes=n)
            self.assertNotEqual(v, fp.VERDICT_CHANGED, "无论失败多少次都不许判结构变了")
        self.assertEqual(n, 0)


class TestFingerprintStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kiana_fp_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_roundtrip_and_missing_file(self):
        st = fp.FingerprintStore(self.tmp)
        self.assertEqual(st.load(), {}, "首次运行没有库 → 空库，不该报错")
        st.put("rule:a", 12345, url="https://a/x")
        got = st.get("rule:a")
        self.assertEqual(got["fingerprint"], 12345)
        self.assertEqual(got["strikes"], 0)
        self.assertTrue(os.path.exists(st.path))
        self.assertFalse(os.path.exists(st.path + ".tmp"), "原子替换后不留半截文件")

    def test_write_failure_is_reported_not_swallowed(self):
        st = fp.FingerprintStore(self.tmp)
        st.dir = os.path.join(self.tmp, "nope", "\0bad")     # 非法路径
        self.assertFalse(st.save({"a": 1}), "写失败必须返回 False，不许静默")

    def test_default_store_is_outside_repo(self):
        """指纹是观测数据，**不得进版本库**——默认路径不能落在仓库里"""
        st = fp.FingerprintStore()
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.assertFalse(os.path.abspath(st.path).startswith(repo),
                         f"指纹库默认路径落在了仓库内: {st.path}")


class TestStructureProbe(unittest.TestCase):
    """探针执行器：旁路、独立限流、过闸、失败不判死"""

    STRUCT_A = SAME_STRUCT_A
    STRUCT_B = DIFF_STRUCT

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kiana_probe_")
        self.store = fp.FingerprintStore(self.tmp)
        self._orig_safe_get = fp.safe_get
        self.calls = []

    def tearDown(self):
        fp.safe_get = self._orig_safe_get
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _patch_fetch(self, html):
        async def _f(session, url, headers=None, **kw):
            self.calls.append(url)
            return _Resp(html) if html is not None else None
        fp.safe_get = _f

    def _probe(self):
        # 注入快速限流器：默认桶是给线上低频探针用的，测试里会把 0.2/s 等成几秒
        fast = fp.RateLimiter(global_rate=1000, global_burst=1000,
                              domain_rate=1000, domain_burst=1000)
        p = fp.StructureProbe(store=self.store, limiter=fast)
        return p

    def _run(self, p, key="rule:x", url="https://example.com/a"):
        return asyncio.run(p.probe(key, url, session=object()))

    # ── 旁路与过闸 ──
    def test_default_limiter_is_per_instance_not_shared(self):
        """探针必须有自己的限流桶——共享会让它挤占/被挤占正常抓取的额度"""
        p1, p2 = fp.StructureProbe(), fp.StructureProbe()
        self.assertIsInstance(p1.limiter, fp.RateLimiter)
        self.assertIsNot(p1.limiter, p2.limiter, "限流桶必须是独立实例，不能全局共享")

    def test_fetches_through_ssrf_gate(self):
        """探针不是闸的例外：取页面必须走 safe_get"""
        self._patch_fetch(self.STRUCT_A)
        self._run(self._probe())
        self.assertEqual(self.calls, ["https://example.com/a"])

    # ── 基线与判定 ──
    def test_first_probe_builds_baseline_without_judging(self):
        self._patch_fetch(self.STRUCT_A)
        r = self._run(self._probe())
        self.assertEqual(r["verdict"], fp.VERDICT_BASELINE)
        self.assertNotEqual(r["fingerprint"], 0)

    def test_escalates_to_changed_over_probes(self):
        self._patch_fetch(self.STRUCT_A)
        p = self._probe()
        self._run(p)                                     # 建基线
        self._patch_fetch(self.STRUCT_B)                 # 结构换了
        v1 = self._run(p)["verdict"]
        v2 = self._run(p)["verdict"]
        v3 = self._run(p)["verdict"]
        self.assertEqual((v1, v2), (fp.VERDICT_WARN, fp.VERDICT_WARN), "单次/两次只告警")
        self.assertEqual(v3, fp.VERDICT_CHANGED)

    def test_changed_does_not_auto_accept_new_structure(self):
        """结构变了要**人确认**，探针不许悄悄把新结构当成正常"""
        self._patch_fetch(self.STRUCT_A)
        p = self._probe()
        base = self._run(p)["fingerprint"]
        self._patch_fetch(self.STRUCT_B)
        for _ in range(3):
            self._run(p)
        self.assertEqual(self.store.get("rule:x")["fingerprint"], base,
                         "changed 后基准指纹不得被自动改写")

    def test_accept_rebuilds_baseline(self):
        self._patch_fetch(self.STRUCT_A)
        p = self._probe()
        self._run(p)
        self._patch_fetch(self.STRUCT_B)
        for _ in range(3):
            self._run(p)
        self.assertTrue(p.accept("rule:x", "https://example.com/a"))
        rec = self.store.get("rule:x")
        self.assertEqual(rec["strikes"], 0, "确认后告警计数清零")
        v = self._run(p)
        self.assertEqual(v["verdict"], fp.VERDICT_OK, "重建基线后应判正常")

    # ── 失败不判死（端到端） ──
    def test_fetch_failure_does_not_advance_strikes(self):
        """**本文件最重要的一条**：探针失败穿插在告警之间，不得推进连续计数。"""
        self._patch_fetch(self.STRUCT_A)
        p = self._probe()
        self._run(p)
        self._patch_fetch(self.STRUCT_B)
        self.assertEqual(self._run(p)["strikes"], 1)
        self._patch_fetch(None)                          # 站点抽风（safe_get 返回 None）
        r = self._run(p)
        self.assertEqual(r["verdict"], fp.PROBE_FAILED)
        self.assertEqual(r["strikes"], 1, "失败不得推进连续计数（要如实回显存储值）")
        self._patch_fetch(self.STRUCT_B)                 # 站点恢复
        self.assertEqual(self._run(p)["strikes"], 2, "恢复后应接着上次的计数")

    def test_blocked_by_gate_is_probe_failed_not_changed(self):
        """被 SSRF 闸拦下 → 探针失败，绝不是"结构变了" """
        self._patch_fetch(None)
        r = self._run(self._probe())
        self.assertEqual(r["verdict"], fp.PROBE_FAILED)
        self.assertIn("闸", r["detail"])

    def test_many_failures_never_report_changed(self):
        self._patch_fetch(self.STRUCT_A)
        p = self._probe()
        self._run(p)
        self._patch_fetch(None)
        for _ in range(10):
            self.assertNotEqual(self._run(p)["verdict"], fp.VERDICT_CHANGED)

    def test_failure_does_not_touch_stored_record(self):
        self._patch_fetch(self.STRUCT_A)
        p = self._probe()
        self._run(p)
        before = dict(self.store.get("rule:x"))
        self._patch_fetch(None)
        for _ in range(3):
            self._run(p)
        self.assertEqual(self.store.get("rule:x"), before,
                         "探针失败不得改动既有基线记录")

    def test_missing_key_or_url_is_probe_failed(self):
        self._patch_fetch(self.STRUCT_A)
        p = self._probe()
        self.assertEqual(self._run(p, key="", url="https://x/a")["verdict"], fp.PROBE_FAILED)
        self.assertEqual(self._run(p, key="k", url="")["verdict"], fp.PROBE_FAILED)


if __name__ == "__main__":
    unittest.main()
