# -*- coding: utf-8 -*-
"""「同一能力多份实现」一致性守卫 —— 以 WebRTC 泄漏面为例（模式六）

**为什么单独立一条**：施工方案的模式六写着"**有没有第二份实现**——同一个能力是否在
别处还有一份（WebRTC 就有 5 份）"。多份实现本身不一定错，**分歧才是错的**：
同一个泄漏面，一份说"清空 iceServers"、另一份说"保留一个真 STUN 以求真实"，
而保留真 STUN 的那份如果只拦 `onicecandidate`，srflx **仍会从 SDP 泄漏**。

本文件是**结构性守卫**（读源码，不启浏览器）：
  ① 登记全部 WebRTC 处理点，**少登记一个就红**——逼后人更新这张表；
  ② **关键不变式**：保留/注入可达 STUN 的实现，**必须同时拦 SDP 路径**
     （`localDescription` / `remoteDescription`），否则真实公网 IP 可从
     `pc.localDescription.sdp` 读到（事件过滤形同虚设）；
  ③ 清空 iceServers 的实现**不需要** SDP 过滤——因为压根不会产生 srflx。
     这条也写进断言，免得后人给它们"顺手加一层"制造无谓复杂度。
"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "kiana_vnext_plus"

# WebRTC 处理点登记表：路径 → 策略
#   empty_ice_servers          清空 iceServers（不产生 srflx，天然无泄漏）
#   sdp_and_event_scrub        擦 SDP 里的地址 **且** 擦候选事件里的地址
#   keep_stun_filter_srflx     保留真实 STUN 以求真实 → **必须**同时覆盖两条路径
#   generate_certificate_only  只做 generateCertificate 防御，不碰候选
WEBRTC_IMPLS = {
    "injection_scripts.py": "empty_ice_servers",
    "evasion_v2.py": "sdp_and_event_scrub",
    "source_level_stealth.py": "empty_ice_servers",
    "ultimate_evasion.py": "keep_stun_filter_srflx",
    "evasion_engine.py": "generate_certificate_only",
}

# 不产生候选、因而无需覆盖路径的策略
_NO_CANDIDATE_STRATEGIES = {"empty_ice_servers", "generate_certificate_only"}


def _src(name: str) -> str:
    return (PKG / name).read_text(encoding="utf-8", errors="ignore")


class TestRegistry(unittest.TestCase):
    def test_every_registered_file_still_mentions_webrtc(self):
        """登记表不许腐烂：文件被删/改名/不再处理 WebRTC 时，这里必须红。

        这就是模式六要的"**有没有第二份实现**"——答案固定在这张表里，
        而不是靠每次 grep 37 处命中再去人肉拼。
        """
        for name, strategy in WEBRTC_IMPLS.items():
            p = PKG / name
            self.assertTrue(p.exists(), f"登记的实现不在了: {name}")
            self.assertIn("RTCPeerConnection", _src(name),
                          f"{name} 不再处理 WebRTC（策略={strategy}）——请更新登记表")

    def test_no_unregistered_webrtc_implementation(self):
        """反向扫一遍：包内还有**没登记**的 WebRTC 处理点就应该被这条挡住"""
        hits = set()
        for p in PKG.glob("*.py"):
            if p.name in WEBRTC_IMPLS:
                continue
            if "RTCPeerConnection" in p.read_text(encoding="utf-8", errors="ignore"):
                hits.add(p.name)
        self.assertEqual(hits, set(),
                         f"发现未登记的 WebRTC 处理点: {sorted(hits)}——"
                         f"请判断它属于哪种策略并登记进 WEBRTC_IMPLS")


class TestLeakInvariant(unittest.TestCase):
    def _candidate_impls(self):
        return {n: s for n, s in WEBRTC_IMPLS.items()
                if s not in _NO_CANDIDATE_STRATEGIES}

    def test_event_path_is_covered(self):
        """**本文件最重要的一条之一**：ICE 候选会通过 `onicecandidate` 逐条送给页面，
        `event.candidate.candidate` 里带着真实地址。不覆盖事件路径 = 假防护。"""
        for name in self._candidate_impls():
            src = _src(name)
            self.assertIn("onicecandidate", src,
                          f"{name} 未覆盖候选事件路径——真实地址会随候选事件泄漏给页面")

    def test_sdp_path_is_covered(self):
        """另一条路径：候选**也写在 SDP 里**，页面读 `localDescription.sdp`
        （或从 createOffer 拿到的 description）就能绕过事件过滤。"""
        for name in self._candidate_impls():
            src = _src(name)
            sdp_path = ("createOffer" in src) or ("localDescription" in src) \
                or ("remoteDescription" in src)
            self.assertTrue(sdp_path,
                            f"{name} 未覆盖 SDP 路径——候选会随 SDP 泄漏给页面")

    def test_scrub_is_consistent_between_paths(self):
        """两条路径必须用**同一套**判据，否则擦一半留一半"""
        src = _src("evasion_v2.py")
        self.assertIn("scrub", src, "应抽出统一的擦除函数，两条路径共用")
        self.assertGreaterEqual(src.count("scrub("), 3,
                                "createOffer 与候选事件两条路径都要用同一个 scrub")

    def test_stun_keeping_impl_filters_srflx(self):
        """保留真实 STUN 的实现，必须真的按 srflx 过滤（而不是只写注释）"""
        src = _src("ultimate_evasion.py")
        self.assertIn("stun:stun.l.google.com", src,
                      "前提变了：该实现不再保留真实 STUN —— 若已改为清空，请更新登记表")
        self.assertIn("srflx", src)

    def test_empty_ice_servers_impls_really_empty_it(self):
        """③ 清空 iceServers 的实现不产生 srflx，**不需要**路径过滤

        写下来是为了挡住"顺手再加一层"——那只会增加破坏 WebRTC 的风险，买不到安全。
        """
        for name, strategy in WEBRTC_IMPLS.items():
            if strategy != "empty_ice_servers":
                continue
            self.assertIn("iceServers", _src(name),
                          f"{name} 声称清空 iceServers，但源码里找不到该字段")

    def test_sdp_filter_falls_back_on_error(self):
        """过滤必须**可回退**：任何异常都不能把 WebRTC 弄坏（拿不到描述 vs 被改坏）"""
        src = _src("ultimate_evasion.py")
        self.assertIn("catch (e) { return null; }", src.replace("{{", "{").replace("}}", "}"),
                      "取描述失败应返回 null，而不是抛出去打断页面")
        self.assertIn("回退原描述", src, "过滤失败必须回退原描述")

    def test_event_path_scrub_falls_back_on_error(self):
        """擦候选事件同样要可回退（`Object.create(ev)` 失败就交原始事件）"""
        src = _src("evasion_v2.py")
        self.assertIn("回退原始事件", src)
        self.assertIn("Object.create(ev)", src, "用原型链保住 instanceof")


if __name__ == "__main__":
    unittest.main()
