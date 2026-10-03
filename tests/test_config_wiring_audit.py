# -*- coding: utf-8 -*-
"""配置键「接线完整性」守卫 —— 防"引擎会读、但没有任何入口"的死键

**本轮发现的真问题**：`cookie_armory_enabled` 这条链**缺了一整跳**——

| 环节 | 状态（修复前） |
|---|---|
| `DEFAULT_GLOBAL` | ✅ 有 |
| 引擎 `crawler._init_cookie_armory` 读取 | ✅ 有 |
| `run_crawler` 的 `gcfg` 传导 | ❌ **缺** |
| GUI 开关 / CLI 参数 | ❌ 都没有 |

后果：**`CookieArmory`（按站身份池：健康分/额度/冷却/复活，约 30 个测试）
从未被任何用户路径启用过——它一直是死代码。**

而这不是孤例：`run_crawler.py` 里作者自己留过一条注释
"**副产物开关 GUI 化（原 config 死键，GUI 一直无入口）**" —— 同一个坑踩过两次。

**本文件的作用**：把"引擎会读的键"与"有入口的键"**对账**。
凡对不上又不在**已知清单**里的，就是新出现的死键，直接红。

> 已知清单**不是"免检名单"**，而是**已评估过、决定暂不提供入口**的登记
> （多数是内部调优参数：GC 阈值 / CPU 水位 / 自适应并发…）。
> 它必须**逐条有结论**——这正是 `03-终检与闭环方案` 说的"登记表不允许有空白格"。
"""
import os
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kiana_vnext_plus.config import DEFAULT_GLOBAL      # noqa: E402

PKG = ROOT / "kiana_vnext_plus"

# 入口文件：GUI 壳 + 命令行入口
ENTRY_FILES = ("launcher_v9.py", "launcher_v8.py", "run_crawler.py")

# ── 「引擎会读、但入口文件里没有」的键：**逐条给结论** ──────────────
#    ⚠️ 这不是"免检名单"，而是**已评估台账**。首版我按"组"写了六条概括性理由——
#    那等于**没逐条评估**，正是本工程一直强调的"登记表不允许有空白格"的反面。
#    现改为**每个键一条类别**，四种类别：
#      TUNING  内部调优：改错会伤稳定性，刻意不给界面
#      SAFE    安全默认：刻意常开、不给开关（给开关=邀请用户把自己暴露出去）
#      INTERNAL 由外部流程/文件负责（如环境变量、上游注入）
#      GAP     **真缺口**：看起来是用户会想控的功能，但当前无入口 → 已登记待办
CATEGORY = {
    "TUNING": "内部调优（改错会伤稳定性，刻意不给界面）",
    "SAFE": "安全默认（刻意常开，给开关=邀请用户把自己暴露出去）",
    "INTERNAL": "由外部流程/文件负责（环境变量、上游注入）",
    "GAP": "**真缺口**：用户可能想控但当前无入口（已登记在 docs/后续待开功能.md）",
}

KNOWN_INTERNAL = {
    # ── TUNING：阈值与调度（10+37 里的绝大多数）
    "cpu_threshold_green": "TUNING", "cpu_threshold_yellow": "TUNING",
    "cpu_threshold_critical": "TUNING", "mem_threshold_green": "TUNING",
    "mem_threshold_yellow": "TUNING", "mem_threshold_orange": "TUNING",
    "mem_threshold_critical": "TUNING", "gc_threshold_generation0": "TUNING",
    "gc_threshold_generation1": "TUNING", "gc_threshold_generation2": "TUNING",
    "memory_recycle_interval": "TUNING", "meltdown_threshold": "TUNING",
    "single_machine_optimized": "TUNING", "quality_min_score": "TUNING",
    "adaptive_adjust_interval": "TUNING", "adaptive_max_concurrency": "TUNING",
    "adaptive_min_concurrency": "TUNING", "adaptive_target_success_rate": "TUNING",
    "smart_adaptive_tuning": "TUNING", "global_concurrency": "TUNING",
    "max_concurrency": "TUNING", "min_concurrency": "TUNING",
    "max_requests_per_proxy": "TUNING", "max_bandwidth_mb": "TUNING",
    "rotation_interval": "TUNING", "proxy_fetch_interval": "TUNING",
    "http_cache_ttl": "TUNING", "cooldown_seconds": "TUNING",
    "cooldown_threshold": "TUNING", "max_retries": "TUNING",
    "browser_render_max": "TUNING", "llm_link_budget": "TUNING",
    "evasion_dimensions": "TUNING", "defense_mode": "TUNING",
    "pressure_enabled": "TUNING", "gpu_acceleration": "TUNING",
    "private_dns_resolve_check": "TUNING",
    # ── SAFE：反检测相关**刻意常开**
    "stealth_injection_enabled": "SAFE", "ultimate_evasion_enabled": "SAFE",
    # ── INTERNAL：由外部负责
    "master_password": "INTERNAL", "bili_danmaku_ass": "INTERNAL",
    "llm_link_scoring": "INTERNAL",
    # ── [v6 修复·审计盲区] 下面 9 个键**此前完全不在台账里** ──────────────
    #  原因：`_engine_read_keys()` **只认字典式** `cfg.get("K")`，
    #  而这 9 个全是**属性式** `self.cfg.K`（OmegaConf 最常用的写法）
    #  ⇒ 它们既没被"引擎读的键必须有入口"抓到，也没被登记成 GAP。
    #  **"建好了没入口"能长期藏着，根因就是这个。**（Redis 只是症状之一。）
    #  补上属性式检测后，这 9 个当场冒出来 —— 逐个给结论：
    "browser_max_pages_per_context": "TUNING",   # 浏览器单上下文页数上限（防内存爆）
    "browser_memory_limit_mb": "TUNING",         # 浏览器内存上限（超了回收上下文）
    "browser_pool_size": "TUNING",               # 浏览器池大小
    "challenge_max_wait": "TUNING",              # 验证码求解最长等待
    "m3u8_concurrency": "TUNING",                # m3u8 分片并发
    "protocol_engine_impersonate": "TUNING",     # curl_cffi TLS 指纹档
    "forbid_direct": "SAFE",                     # 禁止直连（安全默认，刻意不给开关）
    # Redis 后端：**用户 2026-10-03 明确决定废弃**。
    # 键保留只为兼容旧配置；引擎默认 `sqlite`，读到别的值也不会自己启用 Redis。
    # **刻意不给入口** —— 那条路已废弃，给了开关反而误导。
    # ⚠️ 若将来要复活，**必须先补入口**（否则用户永远切不过去，见 docs 待办 P-D）。
    "frontier_backend": "INTERNAL",
    "redis_url": "INTERNAL",
    # ── GAP：曾经逐条核对后认定为真缺口 —— **v6 P2 已全部补上入口** ──────────
    #  这几个键原来标 GAP，本轮（v6 P2）在界面上给了入口（六跳齐）：
    #    headless                  → 首页开关「显示浏览器窗口」（**反向**）
    #    export_markdown           → 首页开关「导出 Markdown」
    #    proxy_fetcher_enabled     → 设置页「自动抓取代理」
    #    proxy_source              → 设置页「代理源」输入框（与上者配套）
    #    fingerprint_update_enabled→ 设置页「指纹库自动更新」
    #  ⚠️ 入口补上后**必须从这里删掉**：下面的
    #  `test_gap_keys_are_actually_unreachable` 要求"标 GAP 的必须真的没入口"，
    #  留着会红 —— **这是设计如此**（台账不许过期）。
    #  GAP 目前为空：即"已评估的键里，没有'该有入口却没有'的了"。
}


def _engine_read_keys():
    """引擎里读到的 DEFAULT_GLOBAL 键 —— **字典式与属性式都要认**。

    [v6 修复·审计盲区] 原来**只认字典式** `cfg.get("KEY")`。
    而 OmegaConf **最常用的写法是属性式** `self.cfg.frontier_backend` ——
    它**完全没被覆盖**。

    真机后果：`frontier_backend`（选 SQLite 还是 Redis）**一直逃过审计** ——
    既没被"引擎读的键必须有入口"抓到，也没被登记成 GAP。
    于是"**建好了没入口**"（用户只能手改 config.yaml）能长期藏着。
    **Redis 只是症状，这才是病。**
    """
    keys = sorted(DEFAULT_GLOBAL.keys())
    read = set()
    for f in os.listdir(PKG):
        if not f.endswith(".py"):
            continue
        t = (PKG / f).read_text(encoding="utf-8", errors="ignore")
        for k in keys:
            if not k:
                continue
            # ① 字典式：`cfg.get("KEY")` / `self.config.get('KEY')`
            if re.search(r'(?:self\.)?(?:cfg|config)\.get\(\s*["\']'
                         + re.escape(k) + r'["\']', t):
                read.add(k)
                continue
            # ② **属性式**：`self.cfg.KEY` / `cfg.KEY`（OmegaConf 常用写法）
            if re.search(r'(?:\.self\.)?(?:cfg|config)\.' + re.escape(k) + r'\b', t):
                read.add(k)
    return read




# ── 已知「定义了但引擎（不含 config.py）不读」的键 ───────────────────
#    **这不是免检名单**，而是逐条结论。反证时把 `config.py` 从扫描里排除后，
#    这条对账立刻暴露出 6 个此前被"定义处自己算引用"掩盖的键。
KNOWN_ORPHAN = {
    "log_level": "**假阳性**：由 config.py 自身的日志初始化消费；扫描排除定义处故在此登记",
    "task_retention_days": "只在 run_crawler 里透传，引擎侧无消费者 —— **疑似死键**",
    "task_retention_max_gb": "同上",
    "proxy_warmup_requests": "**无人使用** —— 疑似死键",
    "temp_dir": "**无人使用** —— 疑似死键",
    "tls_consistency_with_proxy": "**无人使用** —— 疑似死键（注意：UA↔TLS 一致性已由 "
                                  "fingerprint_consistency 强制，此开关已无意义）",
}

def _engine_ast_referenced_keys():
    """引擎里被**真正引用**的 DEFAULT_GLOBAL 键（字符串常量 or `cfg.KEY` 属性访问）。

    ⚠️ 用 **AST** 而不是"文本里出现过"：注释与 docstring 里提到键名不算被读取——
    本工程已经踩过多次"拿文本当结构"的坑。
    """
    import ast
    keys = set(DEFAULT_GLOBAL)
    hit = set()
    for f in os.listdir(PKG):
        if not f.endswith(".py"):
            continue
        # ⚠️ **必须跳过 config.py**：键的**定义本身**就在那里，不排除的话
        # `"某键"` 这个字面量会被当成"被引用"，从而**任何**孤儿键都能蒙混过关。
        # （首版没排除 → 反证注入一个孤儿键，测试照样绿——假阴性。）
        if f == "config.py":
            continue
        try:
            tree = ast.parse((PKG / f).read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in keys:
                hit.add(n.value)
            elif isinstance(n, ast.Attribute) and n.attr in keys:
                hit.add(n.attr)
    return hit


def _entry_text():
    return "\n".join((ROOT / f).read_text(encoding="utf-8", errors="ignore")
                     for f in ENTRY_FILES if (ROOT / f).exists())


class TestNoDeadConfigKey(unittest.TestCase):
    def test_engine_read_keys_have_an_entry_or_are_registered(self):
        """**本文件的核心**：引擎会读的键，要么有入口，要么在已知清单里"""
        read = _engine_read_keys()
        entry = _entry_text()
        unaccounted = sorted(k for k in read
                             if k not in KNOWN_INTERNAL and k not in entry)
        self.assertEqual(
            unaccounted, [],
            "发现**引擎会读、但既无入口也未登记**的配置键（死键）："
            f"{unaccounted}——请接上入口（GUI/CLI/gcfg），或加进 KNOWN_INTERNAL 并写明理由")

    def test_fixture_is_non_trivial(self):
        """夹具自证：扫不到键的话上面那条会**假绿**"""
        read = _engine_read_keys()
        self.assertGreaterEqual(len(read), 30, f"只扫到 {len(read)} 个引擎读取键，扫描可能失效")

    def test_registry_entries_still_exist(self):
        """已知清单不许腐烂：键被删了就把它从清单里去掉"""
        stale = sorted(k for k in KNOWN_INTERNAL if k not in DEFAULT_GLOBAL)
        self.assertEqual(stale, [], f"KNOWN_INTERNAL 里有已不存在的键: {stale}")



    def test_every_registry_entry_has_a_valid_category(self):
        """**无空白格**：台账里每个键都必须有类别，且类别必须合法"""
        bad = {k: v for k, v in KNOWN_INTERNAL.items() if v not in CATEGORY}
        self.assertEqual(bad, {}, f"台账里有类别非法的键: {bad}")

    def test_gap_keys_are_actually_unreachable(self):
        """标成 GAP 的必须是**真的没入口**——否则类别是错的"""
        entry = _entry_text()
        wrong = sorted(k for k, v in KNOWN_INTERNAL.items()
                       if v == "GAP" and k in entry)
        self.assertEqual(wrong, [],
                         f"这些键标成 GAP 但入口文件里已出现，类别应改: {wrong}")

    def test_gaps_are_documented_in_handover(self):
        """GAP 必须写进交接文档——**登记在测试里不算登记给人**"""
        doc = (ROOT / "docs" / "后续待开功能.md").read_text(encoding="utf-8")
        missing = sorted(k for k, v in KNOWN_INTERNAL.items()
                         if v == "GAP" and k not in doc)
        self.assertEqual(missing, [],
                         f"这些真缺口没写进 docs/后续待开功能.md: {missing}")


class TestReadDetectionCoversAttributeStyle(unittest.TestCase):
    """**审计盲区的钉子**：`cfg.KEY` 属性式读取也必须被认出来。

    原来 `_engine_read_keys()` **只认字典式** `cfg.get("KEY")`，
    而 OmegaConf **最常用的写法是属性式** `self.cfg.frontier_backend`。
    后果：9 个键（含 `frontier_backend` / `redis_url` / `browser_pool_size` …）
    **完全不在台账里** —— 既没被"引擎读的键必须有入口"抓到，也没登记成 GAP。
    **Redis"建好了没入口"能长期藏着，根因就是这个。**
    """

    def test_attribute_style_reads_are_detected(self):
        """属性式读取的键要出现在"引擎读到的键"里"""
        read = _engine_read_keys()
        # 这几个都是**属性式**读的（`self.cfg.K`），修复前一个都检测不到
        for k in ("frontier_backend", "browser_pool_size", "forbid_direct",
                  "m3u8_concurrency", "protocol_engine_impersonate"):
            self.assertIn(k, read,
                          f"{k} 是属性式读取，没被检测到 —— 盲区又回来了")

    def test_detection_source_mentions_attribute_form(self):
        """检测实现里必须**两种写法都覆盖**（源码级断言，防止被简化回去）"""
        import inspect
        src = inspect.getsource(_engine_read_keys)
        self.assertIn("cfg|config", src, "字典式那半没了")
        # 属性式那条正则
        self.assertIn(r"\.", src)
        self.assertIn("continue", src,
                      "两种写法应各自独立判定（否则字典式命中后会漏掉属性式）")

    def test_exactly_the_documented_blind_spot_keys(self):
        """把"补检测后冒出来的 9 个键"钉住 —— 它们必须都在台账里"""
        read = _engine_read_keys()
        registered = set(KNOWN_INTERNAL)
        # 这 9 个就是修复后当场冒出来的（见 KNOWN_INTERNAL 里的说明）
        for k in ("browser_max_pages_per_context", "browser_memory_limit_mb",
                  "browser_pool_size", "challenge_max_wait", "forbid_direct",
                  "frontier_backend", "m3u8_concurrency",
                  "protocol_engine_impersonate", "redis_url"):
            self.assertIn(k, read, f"{k} 不再被检测到")
            self.assertIn(k, registered, f"{k} 没登记进台账 —— 那条对账会红")


class TestNoOrphanConfigKey(unittest.TestCase):
    """**反方向的对账**：定义了却没人读的配置键 —— 纯死配置。

    第 36 轮查的是"引擎会读、但没入口"；这里查"定义了、但引擎不读"。
    两个方向都堵上，配置表才可信。
    """

    def test_every_default_key_is_referenced_in_engine(self):
        referenced = _engine_ast_referenced_keys()
        orphans = sorted(k for k in (set(DEFAULT_GLOBAL) - referenced)
                         if k not in KNOWN_ORPHAN)
        self.assertEqual(
            orphans, [],
            f"DEFAULT_GLOBAL 里这些键**引擎从不读取**（纯死配置）: {orphans}——"
            f"要么接线，要么删掉")

    def test_orphan_registry_has_no_stale_entries(self):
        """台账不许腐烂：某个键被接上消费后，应把它从 KNOWN_ORPHAN 里去掉"""
        referenced = _engine_ast_referenced_keys()
        stale = sorted(k for k in KNOWN_ORPHAN if k in referenced)
        self.assertEqual(stale, [],
                         f"这些键现在**已经被读取**了，应从 KNOWN_ORPHAN 移除: {stale}")

    def test_fixture_is_non_trivial(self):
        """夹具自证：AST 扫描一旦失效，上面那条会**假绿**"""
        n = len(_engine_ast_referenced_keys())
        self.assertGreaterEqual(n, 60, f"只解析到 {n} 个被引用的键，扫描可能失效")


class TestCookieArmoryIsReachable(unittest.TestCase):
    """本轮修复的回归钉：身份弹药库必须有**至少一个**可达入口"""

    def test_cli_flag_exists(self):
        src = (ROOT / "run_crawler.py").read_text(encoding="utf-8")
        self.assertIn("--cookie-armory", src, "缺 CLI 入口")
        self.assertIn('cfg["cookie_armory_enabled"] = True', src, "CLI 参数未映射进 cfg")

    def test_gcfg_propagates_it(self):
        src = (ROOT / "run_crawler.py").read_text(encoding="utf-8")
        self.assertIn('"cookie_armory_enabled"', src,
                      "gcfg 未传导 —— 引擎永远读到 DEFAULT_GLOBAL 的 False（修复前的状态）")

    def test_default_stays_off(self):
        """风险敏感特性：默认必须是关的（开启需显式动作）"""
        self.assertIs(DEFAULT_GLOBAL.get("cookie_armory_enabled"), False)


if __name__ == "__main__":
    unittest.main()
