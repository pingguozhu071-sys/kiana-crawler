# -*- coding: utf-8 -*-
"""plugins / mimeTypes 注入回归（v6 修复：守卫把"存在但为 0"判成了"不存在"）

**背景（真机证据）**：引擎跑真实爬取时，自己的隐身链自证告警 5 次
「隐身链自证疑点（plugins=0）」（`solver_engine._prewarm_page`），而同一份审计
**没有报 webdriver** —— 说明 `navigator.webdriver` 被成功隐藏，唯独 plugins 漏了。
playwright 实测（headless Chromium）：

| 模式 | navigator.plugins.length | navigator.mimeTypes.length |
|---|---|---|
| headless=True（引擎用的） | 0 | 0 |
| headless=False（真人浏览器） | 5 | 2 |

**两条真根因（本次都修了）**：

1. `injection_scripts` §2/§22 的守卫写成 `typeof x.length === 'undefined'`——
   headless 下 `navigator.plugins` **存在**、`length` **也已定义**，只是值等于 0，
   于是守卫恒为假，整段伪造从来没有真正执行过；
2. `ultimate_evasion` 的 WebRTC SDP 分支把换行写成了**真实 CR/LF**（f-string 模板未转义），
   生成的 JS 直接语法错误 —— 而它与 §1-§3 层是拼进**同一个** `add_init_script` 的，
   一处语法错误让**整条 55 维链一行都不执行**。（这才是真机 plugins=0 的主因：
   链整体没跑，而 webdriver 是被另一条独立脚本 `evasion_v2` 遮住的。）

**本文件三层判据**：

- 静态层（永远跑）：守卫判据必须覆盖 `length === 0`；假对象接口必须齐全；脚本里不得有裸控制字符；
- 语法层（有 node 才跑，否则 skip）：生成的脚本（尤其**拼接后的整条链**）必须能被解析；
- 浏览器层（有 playwright + 浏览器才跑，否则 skip）：headless 下 0 → 注入后必须 > 0，
  且"本来就报正常数量"时**不得覆盖**。
"""
import asyncio
import json
import logging
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 最小指纹（各 build_* 均按 .get 容错取值）
FP = {
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "platform": "Win32",
    "language": "zh-CN",
    "languages": ["zh-CN", "zh", "en"],
    "timezone": "Asia/Shanghai",
    "geo_code": "CN",
    "vendor": "Google Inc.",
    "hardware_concurrency": 16,
    "device_memory": 8,
    "screen_width": 1920,
    "screen_height": 1080,
    "color_depth": 24,
    "webgl_vendor": "Google Inc. (NVIDIA)",
    "webgl_renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0)",
    "canvas_noise": 0.01,
}

_FAKE_PLUGIN_NAMES = [
    "PDF Viewer", "Chrome PDF Viewer", "Chromium PDF Viewer",
    "Microsoft Edge PDF Viewer", "WebKit built-in PDF",
]


def _stealth_js() -> str:
    from kiana_vnext_plus.injection_scripts import build_stealth_scripts
    return build_stealth_scripts(dict(FP))


def _strip_line_comments(js: str) -> str:
    """丢掉整行 `//` 注释，只留代码（注释里会引用旧的坏写法做说明）"""
    return "\n".join(l for l in js.splitlines() if not l.lstrip().startswith("//"))


def _section(js: str, number: int) -> str:
    """按 `// ═══ N.` 标记切出某一节（标记缺失直接失败，避免"断言悄悄失效"）"""
    start = js.find(f"// ═══ {number}.")
    assert start >= 0, f"注入脚本里找不到第 {number} 节标记 —— 断言无法定位，测试失效"
    nxt = js.find("// ═══ ", start + 5)
    return js[start:nxt if nxt > 0 else len(js)]


def _all_injected_scripts() -> dict:
    """引擎真实注入的全部脚本（逐条 + 拼接后的整条链）"""
    from kiana_vnext_plus.injection_scripts import build_stealth_scripts
    from kiana_vnext_plus.evasion_engine import build_evasion_scripts
    from kiana_vnext_plus.behavioral_biometrics import build_biometrics_script
    from kiana_vnext_plus.ultimate_evasion import build_ultimate_evasion_scripts
    from kiana_vnext_plus.evasion_v2 import ALL_EVASION_SCRIPTS
    from kiana_vnext_plus.stealth_v3 import GOOGLEBOT_CDP_EVASION, HUMAN_BEHAVIOR_SCRIPT

    layers = [
        ("injection_scripts", build_stealth_scripts(dict(FP))),
        ("evasion_engine", build_evasion_scripts(dict(FP))),
        ("behavioral_biometrics", build_biometrics_script(dict(FP))),
        ("ultimate_evasion", build_ultimate_evasion_scripts(dict(FP))),
    ]
    out = dict(layers)
    for i, s in enumerate(ALL_EVASION_SCRIPTS):
        out[f"evasion_v2_{i}"] = s
    out["stealth_v3_googlebot"] = GOOGLEBOT_CDP_EVASION
    out["stealth_v3_human"] = HUMAN_BEHAVIOR_SCRIPT
    # 关键：solver_engine 把前四层用 "\n" 拼成**一个** add_init_script ——
    # 所以"单条合法"不等于"整条合法"，必须单独钉住拼接结果。
    out["JOINED_L1_L4"] = "\n".join(s for _n, s in layers)
    return out


# ════════════════════════════════════════════════════════════════════
# 第 1 层：静态断言（不依赖浏览器/node，永远跑）
# ════════════════════════════════════════════════════════════════════
class TestGuardCondition(unittest.TestCase):
    """**本 bug 的核心**：守卫必须覆盖 `length === 0` 这种"存在但为空"的情况"""

    def test_section22_guard_covers_zero_length(self):
        sec = _section(_stealth_js(), 22)
        self.assertIn("length === 0", sec,
                      "§22 的守卫没覆盖 length === 0 —— 这正是原 bug：headless 下 "
                      "navigator.plugins 存在、length 已定义且为 0，"
                      "`typeof x.length === 'undefined'` 恒为假，伪造从不执行")
        self.assertIn("__kianaInstallPluginFakes", sec,
                      "§22 应在【报 0/不存在】时整体替换假对象"
                      "（host 对象 length 不可配置，只能换掉整个对象）")

    def test_old_broken_guard_pattern_is_gone(self):
        # 只看**代码**：注释里为说明原因会引用旧写法，不能算违规
        js = _strip_line_comments(_stealth_js())
        for bad in ("typeof pluginsObj.length", "typeof mimeObj.length"):
            self.assertNotIn(bad, js,
                             f"脚本里仍有旧的坏判据 `{bad} === 'undefined'`："
                             f"它把【存在但为 0】误判成【不存在】，守卫恒为假")

    def test_installer_replaces_only_when_zero_or_missing(self):
        js = _stealth_js()
        # 两个量各自独立判断：一个为 0 不应连累另一个被覆盖
        self.assertIn("!curPlugins || curPlugins.length === 0", js)
        self.assertIn("!curMimes || curMimes.length === 0", js)


class TestFakeInterfaceShape(unittest.TestCase):
    """假对象必须"像真的"：length / 数字索引 / item / namedItem / refresh / 原型链"""

    def setUp(self):
        self.js = _stealth_js()

    def test_plugin_and_mime_type_surface_present(self):
        for token in ("PluginArray", "MimeTypeArray", "namedItem", "refresh",
                      "Symbol.toStringTag", "Object.create"):
            self.assertIn(token, self.js, f"假对象缺少 {token}（接口不像真的，一眼可识破）")
        for name in _FAKE_PLUGIN_NAMES:
            self.assertIn(name, self.js, f"缺少真实 Chrome 的 plugin 名 {name!r}")
        self.assertIn("application/pdf", self.js)
        self.assertIn("text/pdf", self.js)

    def test_prototype_chain_is_wired_not_plain_array(self):
        # 关键差异：真 PluginArray 的 length/item/namedItem 在**原型**上，实例只有数字索引。
        # 原型链必须挂到真接口上（instanceof 才成立），不能再是普通数组字面量。
        self.assertIn("Object.create(base)", self.js)
        self.assertNotIn("const fakePlugins = [", self.js,
                         "又退回【普通数组冒充 PluginArray】的老写法了")


class TestNoEscapeCorruption(unittest.TestCase):
    """生成的 JS 不得含**裸控制字符** —— 那是 f-string 模板转义写错的直接指纹"""

    def test_no_raw_control_chars(self):
        for name, src in _all_injected_scripts().items():
            for ch, label in (("\r", "CR"), ("\x0b", "VT"), ("\x0c", "FF")):
                self.assertNotIn(
                    ch, src,
                    f"{name} 生成物里有裸 {label}（通常是把 CR/LF 直接写进模板、"
                    f"没写成转义序列导致的）—— 拼进同一个 add_init_script 后"
                    f"会让整条链语法错误、一行都不执行")


# ════════════════════════════════════════════════════════════════════
# 第 2 层：真语法检查（有 node 才跑）
# ════════════════════════════════════════════════════════════════════
@unittest.skipIf(shutil.which("node") is None, "本机没有 node，跳过 JS 语法检查")
class TestGeneratedScriptsParse(unittest.TestCase):
    """拼接后的脚本必须真的能被 JS 引擎解析（"一处坏、整条链不执行"的回归闸门）"""

    def test_every_injected_script_parses(self):
        tmp = Path(tempfile.mkdtemp(prefix="kiana_js_"))
        bad = []
        try:
            for name, src in _all_injected_scripts().items():
                f = tmp / f"{name}.js"
                f.write_text(src, encoding="utf-8")
                r = subprocess.run(["node", "--check", str(f)],
                                   capture_output=True, text=True,
                                   encoding="utf-8", errors="replace")
                if r.returncode != 0:
                    detail = " ".join((r.stderr or "").strip().splitlines()[:3])
                    bad.append(f"{name}: {detail}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(bad, [], "生成物里有语法错误的 JS（拼接后整条注入链都会不执行）：\n"
                                  + "\n".join(bad))


# ════════════════════════════════════════════════════════════════════
# 第 3 层：真浏览器断言（有 playwright + 浏览器才跑）
# ════════════════════════════════════════════════════════════════════
_PROBE_JS = """() => {
  const p = navigator.plugins, m = navigator.mimeTypes;
  const toStringTag = p ? Object.prototype.toString.call(p) : '';
  const first = p && p.length ? p[0] : null;
  const firstMime = m && m.length ? m[0] : null;
  return {
    plugins_len: p ? p.length : -1,
    mime_len: m ? m.length : -1,
    plugins_tag: toStringTag,
    plugins_is_plugin_array: (typeof PluginArray !== 'undefined' && p)
        ? (p instanceof PluginArray) : null,
    mimes_is_mime_array: (typeof MimeTypeArray !== 'undefined' && m)
        ? (m instanceof MimeTypeArray) : null,
    has_item: p ? typeof p.item : 'missing',
    has_named_item: p ? typeof p.namedItem : 'missing',
    has_refresh: p ? typeof p.refresh : 'missing',
    names: p ? Array.from({length: Math.min(p.length, 8)}, (_, i) => (p[i] && p[i].name) || null) : [],
    named_item_hit: p && typeof p.namedItem === 'function'
        ? !!(p.namedItem('Chrome PDF Viewer')) : false,
    item_zero_name: first ? String(first.name) : null,
    plugin_desc: first ? String(first.description) : null,
    plugin_filename: first ? String(first.filename) : null,
    plugin_mime_len: first ? first.length : -1,
    plugin_mime_0_type: (first && first.length) ? String(first[0].type) : null,
    mime_0_type: firstMime ? String(firstMime.type) : null,
    mime_named_hit: m && typeof m.namedItem === 'function'
        ? !!(m.namedItem('application/pdf')) : false,
    mime_enabled_plugin_matches: (m && m.length && m[0].enabledPlugin && p && p.length)
        ? (m[0].enabledPlugin === p[0]) : null,
  };
}"""

# 模拟"浏览器报 0"：显式装上**空的** PluginArray/MimeTypeArray（headless 的天然状态）
# 注意两件事：
#   1) `add_init_script` 只**求值**传入的字符串，不像 `page.evaluate` 会调用函数 ——
#      这里必须写成 IIFE `(() => {...})()`，否则整个预置脚本是个"没被调用的箭头函数"= 静默失效；
#   2) 本文件不是 f-string，花括号**不要**写成 `{{`/`}}`（那样是 JS 语法错误，同样静默失效）。
_EMPTY_FAKE_JS = """(() => {
  const mk = (tag, ctor) => {
    const base = (typeof ctor !== 'undefined' && ctor.prototype) ? ctor.prototype : Object.prototype;
    const proto = Object.create(base);
    Object.defineProperty(proto, 'length', { get: () => 0, configurable: true });
    Object.defineProperty(proto, Symbol.toStringTag, { value: tag, configurable: true });
    return Object.create(proto);
  };
  Object.defineProperty(Navigator.prototype, 'plugins', {
    get: () => mk('PluginArray', typeof PluginArray !== 'undefined' ? PluginArray : undefined),
    configurable: true,
  });
  Object.defineProperty(Navigator.prototype, 'mimeTypes', {
    get: () => mk('MimeTypeArray', typeof MimeTypeArray !== 'undefined' ? MimeTypeArray : undefined),
    configurable: true,
  });
})()"""

# 模拟"浏览器本来就正常报数"：装上 3 项哨兵（名字带标记，便于断言"没被动过"）
_SENTINEL_JS = """(() => {
  const sentinelPlugins = [
    {name: 'SENTINEL-PLUGIN-A'}, {name: 'SENTINEL-PLUGIN-B'}, {name: 'SENTINEL-PLUGIN-C'},
  ];
  const sentinelMimes = [{type: 'application/x-sentinel'}];
  Object.defineProperty(Navigator.prototype, 'plugins', {
    get: () => sentinelPlugins, configurable: true,
  });
  Object.defineProperty(Navigator.prototype, 'mimeTypes', {
    get: () => sentinelMimes, configurable: true,
  });
})()"""

_SENTINEL_PROBE_JS = """() => {
  const p = navigator.plugins, m = navigator.mimeTypes;
  return {
    plugins_len: p ? p.length : -1,
    mime_len: m ? m.length : -1,
    first_name: (p && p.length) ? String(p[0].name) : null,
    first_mime: (m && m.length) ? String(m[0].type) : null,
  };
}"""


def _production_chain() -> str:
    """复刻 solver_engine._build_full_evasion_chain：前四层拼成一个 init script"""
    from kiana_vnext_plus.injection_scripts import build_stealth_scripts
    from kiana_vnext_plus.evasion_engine import build_evasion_scripts
    from kiana_vnext_plus.behavioral_biometrics import build_biometrics_script
    from kiana_vnext_plus.ultimate_evasion import build_ultimate_evasion_scripts

    return "\n".join([
        build_stealth_scripts(dict(FP)),
        build_evasion_scripts(dict(FP)),
        build_biometrics_script(dict(FP)),
        build_ultimate_evasion_scripts(dict(FP)),
    ])


async def _measure(init_scripts, probe_js, pre_scripts=()):
    """起一个 headless 上下文，按顺序注入 init script，返回探针结果。

    超时给得宽（浏览器冷启动可能十几秒）：用例失败应当是**断言**失败，不是超时。
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, timeout=60000)
        try:
            ctx = await browser.new_context()
            for s in pre_scripts:
                await ctx.add_init_script(s)
            for s in init_scripts:
                await ctx.add_init_script(s)
            page = await ctx.new_page()
            page.set_default_timeout(30000)
            await page.goto("about:blank", wait_until="domcontentloaded", timeout=30000)
            out = await page.evaluate(probe_js)
            await ctx.close()
            return out
        finally:
            await browser.close()


async def _measure_child_frame(init_scripts, probe_js=_PROBE_JS):
    """在**子框架**里测同一件事：反爬脚本常在 iframe 里复核 navigator（§17 就是为此存在的）"""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, timeout=60000)
        try:
            ctx = await browser.new_context()
            for s in init_scripts:
                await ctx.add_init_script(s)
            page = await ctx.new_page()
            page.set_default_timeout(30000)
            await page.set_content(
                "<html><body><iframe id='f' srcdoc='<p>probe</p>'></iframe></body></html>",
                timeout=30000)
            await page.wait_for_timeout(300)
            frames = [f for f in page.frames if f != page.main_frame]
            if not frames:  # pragma: no cover - 环境异常
                raise AssertionError("页面里没有子框架，无法验证 iframe 场景")
            out = await frames[0].evaluate(probe_js)
            await ctx.close()
            return out
        finally:
            await browser.close()


async def _can_launch() -> bool:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True, timeout=60000)
        await b.close()
    return True


_BROWSER_OK = None


def _browser_available() -> bool:
    """能否真起浏览器（只在类级 setUpClass 里判一次）。

    注意：**只在"起不来"时 skip**，不吞测试内部的异常 —— 否则探测脚本自己写错
    （例如 API 用错）会被误当成"环境不可用"而静默跳过，等于假绿。
    """
    global _BROWSER_OK
    if _BROWSER_OK is None:
        try:
            _BROWSER_OK = asyncio.run(_can_launch())
        except Exception:  # noqa: BLE001 - 环境问题（没装浏览器/缺依赖）一律判为不可用
            _BROWSER_OK = False
    return _BROWSER_OK


def _browser_measure(init_scripts, probe_js=_PROBE_JS, pre_scripts=()):
    return asyncio.run(_measure(init_scripts, probe_js, pre_scripts))


class TestRealBrowserPlugins(unittest.TestCase):
    """真机断言：headless 的 0 必须被修成"像真的"5 项，且不覆盖正常报数"""

    @classmethod
    def setUpClass(cls):
        if not _browser_available():
            raise unittest.SkipTest("本机 playwright 起不了 chromium，跳过真机断言")

    def test_zero_length_plugins_are_replaced_with_complete_fake(self):
        """**0 也会触发替换**（本 bug 的核心：原守卫只认"不存在"）"""
        natural = _browser_measure([], _PROBE_JS)
        got = _browser_measure([_stealth_js()], _PROBE_JS, pre_scripts=[_EMPTY_FAKE_JS])

        self.assertEqual(got["plugins_len"], 5,
                         f"注入后 navigator.plugins.length 仍不是 5：{got['plugins_len']}"
                         f"（headless 天然值={natural['plugins_len']}）")
        self.assertEqual(got["mime_len"], 2, f"mimeTypes 未被修好：{json.dumps(got)}")
        self.assertEqual(got["names"], _FAKE_PLUGIN_NAMES)
        for key in ("has_item", "has_named_item", "has_refresh"):
            self.assertEqual(got[key], "function", f"{key} 不是函数：{json.dumps(got)}")
        self.assertTrue(got["named_item_hit"], "plugins.namedItem('Chrome PDF Viewer') 未命中")
        self.assertEqual(got["plugins_tag"], "[object PluginArray]",
                         f"Object.prototype.toString 结果不对：{got['plugins_tag']}")
        if got["plugins_is_plugin_array"] is not None:
            self.assertTrue(got["plugins_is_plugin_array"],
                            "navigator.plugins 不是 PluginArray 实例（普通数组一眼假）")
        if got["mimes_is_mime_array"] is not None:
            self.assertTrue(got["mimes_is_mime_array"], "navigator.mimeTypes 不是 MimeTypeArray 实例")
        # plugin 对象自身也要像真的：name/filename/description + 数字索引的 mimeType
        self.assertEqual(got["item_zero_name"], "PDF Viewer")
        self.assertEqual(got["plugin_desc"], "Portable Document Format")
        self.assertEqual(got["plugin_filename"], "internal-pdf-viewer")
        self.assertEqual(got["plugin_mime_len"], 2, "plugin.length（mimeType 个数）不对")
        self.assertEqual(got["plugin_mime_0_type"], "application/pdf")
        self.assertEqual(got["mime_0_type"], "application/pdf")
        self.assertTrue(got["mime_named_hit"], "mimeTypes.namedItem('application/pdf') 未命中")
        self.assertTrue(got["mime_enabled_plugin_matches"],
                        "mimeType.enabledPlugin 未回指 plugin（真 Chrome 是互相引用的）")

    def test_full_production_chain_still_reports_plugins(self):
        """**整条生产链**（55 维 + evasion_v2 + stealth_v3）之后仍必须是 5 ——
        这里同时也是"拼接后语法坏掉 / 被后面的层覆盖回 0/3"的回归闸门。"""
        from kiana_vnext_plus.evasion_v2 import ALL_EVASION_SCRIPTS
        from kiana_vnext_plus.stealth_v3 import GOOGLEBOT_CDP_EVASION, HUMAN_BEHAVIOR_SCRIPT

        scripts = [_production_chain(), *ALL_EVASION_SCRIPTS,
                   GOOGLEBOT_CDP_EVASION, HUMAN_BEHAVIOR_SCRIPT]
        got = _browser_measure(scripts, _PROBE_JS, pre_scripts=[_EMPTY_FAKE_JS])

        self.assertEqual(got["plugins_len"], 5,
                         f"整条链跑完后 plugins={got['plugins_len']}（应为 5）"
                         f"—— 注意 evasion_v2 的 CDP 脚本排在链之后，会覆盖 plugins")
        self.assertEqual(got["mime_len"], 2, f"整条链跑完后 mimeTypes={got['mime_len']}（应为 2）")
        self.assertEqual(got["names"], _FAKE_PLUGIN_NAMES)
        if got["plugins_is_plugin_array"] is not None:
            self.assertTrue(got["plugins_is_plugin_array"],
                            "整条链跑完后 navigator.plugins 退化成了普通数组")

    def test_existing_plugins_are_not_overwritten(self):
        """浏览器本来就报正常数量时**不得覆盖**（避免画蛇添足）"""
        got = _browser_measure([_stealth_js()], _SENTINEL_PROBE_JS,
                               pre_scripts=[_SENTINEL_JS])
        self.assertEqual(got["plugins_len"], 3, f"哨兵 plugins 被覆盖了：{json.dumps(got)}")
        self.assertEqual(got["mime_len"], 1, f"哨兵 mimeTypes 被覆盖了：{json.dumps(got)}")
        self.assertEqual(got["first_name"], "SENTINEL-PLUGIN-A")
        self.assertEqual(got["first_mime"], "application/x-sentinel")

    def test_child_frame_also_reports_plugins(self):
        """**iframe 里也要一致**：反爬常在子框架复核 navigator（§17 就是为跨 iframe 检测做的）"""
        got = asyncio.run(_measure_child_frame([_stealth_js()]))
        self.assertEqual(got["plugins_len"], 5,
                         f"子框架里 plugins={got['plugins_len']}（应为 5）—— "
                         f"主框架修好而 iframe 仍为 0，等于把检测面让出去")
        self.assertEqual(got["mime_len"], 2, f"子框架里 mimeTypes={got['mime_len']}（应为 2）")


# ════════════════════════════════════════════════════════════════════
# 第 4 层：**引擎真实启动路径**（上一轮漏掉的正是这一层）
# ════════════════════════════════════════════════════════════════════
# 上一轮的教训：验证用的是 **playwright 直连**，生产走的是 `SolverEngine._launch_browser`
# 的 **patchright 分支**。实测（本机 patchright 1.61.2 / driver playwright-core 1.61.1）：
# patchright 改写了 Chromium "页面会话初始化"里的 init script 注册路径 —— 上游是
# `for (const initScript of page.allInitScripts()) _evaluateOnNewDocument(
# initScript, "main", true /* runImmediately */)`，patchright 换成"按 context.initScripts /
# page.initScripts 各循环一次"且**丢掉了 runImmediately**（两个 driver bundle 已比对）
# → `add_init_script` **全程静默失效**：marker 在第 1 次导航 / 第 2 次导航 / reload 后
# 都读到 0，任何 plugins 注入都不生效。
# 于是"链修好了"却在生产里一行都没跑（真机自证 5 次 plugins=0）。
#
# 因此引擎必须**先探通道、再定引擎**，且每个上下文都要能自证"链真的跑了"。
_ENGINE_MARK = "__KIANA_INJECT_CHANNEL_OK__"
_CHANNEL_MARK_JS = f"() => window.{_ENGINE_MARK} || 0"

_ENGINE_STEALTH_CFG = {
    "stealth_injection_enabled": True,
    "evasion_dimensions": 55,
    "ultimate_evasion_enabled": True,
}


class _StubBrowser:
    def __init__(self, name):
        self.name = name


class _StubChromium:
    def __init__(self, name, rec):
        self._name, self._rec = name, rec

    async def launch(self, **kwargs):
        self._rec.append(self._name)
        return _StubBrowser(self._name)


class _StubPW:
    def __init__(self, name, rec):
        self.chromium = _StubChromium(name, rec)


class _StubStarter:
    """冒充 `se.pr_async` / `se.pw_async`（**不起真浏览器**）"""

    def __init__(self, name, rec):
        self._name, self._rec = name, rec

    async def start(self):
        return _StubPW(self._name, self._rec)


class TestEngineSelectsVerifiedChannel(unittest.TestCase):
    """**根因回归（确定性、无浏览器）**：装了 patchright ≠ patchright 能注入。

    引擎必须实测注入通道，探不通就换 playwright —— 否则"链修好了"在生产里照样一行不跑。
    """

    def setUp(self):
        import kiana_vnext_plus.solver_engine as se
        self.se = se
        self._orig = (se.pr_async, se.pw_async, se.HAS_PATCHRIGHT, se.HAS_PLAYWRIGHT,
                      se._probe_init_script_channel, se._PATCHRIGHT_CHANNEL_CACHE)

    def tearDown(self):
        (self.se.pr_async, self.se.pw_async, self.se.HAS_PATCHRIGHT, self.se.HAS_PLAYWRIGHT,
         self.se._probe_init_script_channel, self.se._PATCHRIGHT_CHANNEL_CACHE) = self._orig

    def _arm(self, probe_ok, reason: str = "桩：add_init_script 未执行"):
        """把两个引擎都换成桩，并钉死探针结论（probe_ok 支持 True/False/None 三态）"""
        rec = []
        se = self.se
        se.pr_async = lambda: _StubStarter("patchright", rec)
        se.pw_async = lambda: _StubStarter("playwright", rec)
        se.HAS_PATCHRIGHT = True
        se.HAS_PLAYWRIGHT = True
        se._PATCHRIGHT_CHANNEL_CACHE = None

        async def _probe(factory, args, headless=True):
            return probe_ok, reason

        se._probe_init_script_channel = _probe
        return rec

    def test_broken_patchright_channel_falls_back_to_playwright(self):
        rec = self._arm(probe_ok=False)
        eng = self.se.SolverEngine(pool_size=1, headless=True)
        with self.assertLogs("kiana_vnext_plus.solver_engine", level="ERROR") as cm:
            asyncio.run(eng._launch_browser())
        self.assertEqual(rec, ["playwright"],
                         f"patchright 注入通道实测不通时必须回退，实际起的引擎={rec}")
        self.assertEqual(eng.injection_engine, "playwright")
        self.assertFalse(eng.patchright_channel_ok)
        self.assertIn("桩：add_init_script 未执行", eng.patchright_channel_reason)
        joined = "\n".join(cm.output)
        self.assertIn("注入通道", joined,
                      "回退必须在日志里留下可读原因（否则用户只看到 plugins=0，无从定位）")

    def test_working_patchright_channel_keeps_patchright(self):
        """patchright 真能注入时就该用它（回退不是"一律不用 patchright"）"""
        rec = self._arm(probe_ok=True, reason="桩：通道可用")
        eng = self.se.SolverEngine(pool_size=1, headless=True)
        asyncio.run(eng._launch_browser())
        self.assertEqual(rec, ["patchright"])
        self.assertEqual(eng.injection_engine, "patchright")
        self.assertTrue(eng.patchright_channel_ok)

    def test_unverifiable_channel_does_not_change_behaviour(self):
        """**"没测出来" ≠ "测出来是坏的"**：探针自身失败（起不来/超时/工厂不可用）时
        保持原行为用 patchright，但必须留 WARNING 说明原因；且**不得写进缓存**
        （一次瞬时故障不能把整个进程钉在"无法判定"上）。"""
        rec = self._arm(probe_ok=None, reason="桩：探针无法判定")
        eng = self.se.SolverEngine(pool_size=1, headless=True)
        with self.assertLogs("kiana_vnext_plus.solver_engine", level="WARNING") as cm:
            asyncio.run(eng._launch_browser())
        self.assertEqual(rec, ["patchright"], "无法判定时不应擅自换引擎")
        self.assertTrue(eng.patchright_channel_ok, "无法判定不得等同于『实测不可用』")
        self.assertIn("无法判定", "\n".join(cm.output))
        self.assertIsNone(self.se._PATCHRIGHT_CHANNEL_CACHE,
                          "『无法判定』被写进进程级缓存了 —— 会把后续引擎选择一起带偏")


class TestEngineRealLaunchPath(unittest.TestCase):
    """**走引擎真实启动路径**：SolverEngine.init → _launch_browser → _create_solver_context
    → _prewarm_page（引擎自证审计）。

    skip 只判"浏览器起不起得来"，其余一律断言失败（不许把异常吞成 skip = 假绿）。
    注意 `_PATCHRIGHT_CHANNEL_CACHE` 是**进程级**的：进本类前先清一次，避免别的用例
    （如 CDP 接管用桩启动器）留下的探针结论影响这里的引擎选择。
    """

    @classmethod
    def setUpClass(cls):
        if not _browser_available():
            raise unittest.SkipTest("本机 playwright 起不了 chromium，跳过引擎真实启动路径断言")

    def _run_engine(self, probe_script_override=None):
        """真起一次引擎（headless），返回 (页面探针结果, 自证日志记录, 引擎)"""
        import logging
        from kiana_vnext_plus.solver_engine import SolverEngine

        records = []

        class _Collect(logging.Handler):
            def emit(self, record):
                records.append(record)

        logger = logging.getLogger("kiana_vnext_plus.solver_engine")
        handler = _Collect(level=logging.DEBUG)
        old_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        orig_probe_script = self.se._INJECT_PROBE_SCRIPT
        if probe_script_override is not None:
            self.se._INJECT_PROBE_SCRIPT = probe_script_override

        async def _run():
            eng = SolverEngine(pool_size=1, max_pages_per_context=2, memory_limit_mb=4096,
                               stealth_config=dict(_ENGINE_STEALTH_CFG), headless=True)
            await eng.init()
            try:
                entry = await eng._contexts.get()
                page = entry.page
                page.set_default_timeout(30000)
                # 真页面（新文档）上核验：生产里被检测的正是真实导航后的 document
                await page.goto("data:text/html,<html><body>engine-probe</body></html>",
                                wait_until="domcontentloaded", timeout=30000)
                got = await page.evaluate(_PROBE_JS)
                got["channel_mark"] = await page.evaluate(_CHANNEL_MARK_JS)
                await eng._contexts.put(entry)
            finally:
                await eng.close()
            return got, eng

        try:
            got, eng = asyncio.run(asyncio.wait_for(_run(), timeout=300))
        finally:
            self.se._INJECT_PROBE_SCRIPT = orig_probe_script
            logger.removeHandler(handler)
            logger.setLevel(old_level)
        return got, records, eng

    def setUp(self):
        import kiana_vnext_plus.solver_engine as se
        self.se = se
        # 进程级探针缓存在本类里**必须清空**：别的用例可能已经用桩启动器探过一次，
        # 那份结论不代表本机真实 patchright 的情况（否则本类会"继承"错误的引擎选择）。
        se._PATCHRIGHT_CHANNEL_CACHE = None
        self._orig_cache = None

    def tearDown(self):
        self.se._PATCHRIGHT_CHANNEL_CACHE = self._orig_cache

    def test_engine_real_path_activates_plugins_chain(self):
        """**最终判据**：引擎自己的启动方式下，注入后 plugins>0 且 instanceof PluginArray"""
        got, records, eng = self._run_engine()

        self.assertGreaterEqual(
            got["channel_mark"], 1,
            f"注入通道自证 marker 没出现（引擎={eng.injection_engine}）："
            f"说明这台机器上被选中的引擎 **一行 init script 都没执行**，"
            f"整条 55 维链（含 plugins/mimeTypes）全部失效")
        self.assertGreater(
            got["plugins_len"], 0,
            f"引擎真实路径下 navigator.plugins.length={got['plugins_len']}（应为 5）"
            f"—— 这正是真机「隐身链自证疑点（plugins=0）」的复现")
        self.assertEqual(got["names"], _FAKE_PLUGIN_NAMES)
        self.assertEqual(got["mime_len"], 2)
        self.assertTrue(got["plugins_is_plugin_array"],
                        "navigator.plugins 不是 PluginArray 实例（普通数组一眼假）")

        warn_msgs = [r.getMessage() for r in records
                     if r.levelno == logging.WARNING and "隐身链自证疑点" in r.getMessage()]
        self.assertEqual(
            warn_msgs, [],
            f"引擎仍报隐身链自证疑点（引擎={eng.injection_engine}）：\n" + "\n".join(warn_msgs))
        # 正面证据：断言**结构 + 稳定标记**，不许断言日志文案。
        # 这里真踩过一次（commit e794712）：首次成功的日志从 DEBUG 改成 INFO 且句子重写，
        # 而断言写的是旧文案 ⇒ 判据默默失效。当时引擎 pool_size=1 ⇒ 永远只有一个上下文
        # ⇒ 只会走 INFO 那支 ⇒ 旧文案**永不可能出现**，红出来却像"反检测回归"。
        self.assertTrue(
            eng.stealth_selfcheck_ok,
            "引擎自己的自证结论没置位（引擎=%s、通道探针=%s）："
            "说明 `_prewarm_page` 的通道自证没通过，或压根没跑"
            % (eng.injection_engine, eng.patchright_channel_reason or "未记录"))
        self.assertTrue(
            any(self.se.STEALTH_SELFCHECK_OK_MARK in r.getMessage() for r in records),
            "引擎没有留下任何『自证通过』的正面记录 —— 不能只靠『没告警』判定注入生效"
            "（引擎=%s、标记=%s）" % (eng.injection_engine, self.se.STEALTH_SELFCHECK_OK_MARK))
        # 引擎最终用的那一套必须自洽：用 patchright ⟺ 探针通过；否则就是回退到 playwright
        if eng.injection_engine == "patchright":
            self.assertTrue(eng.patchright_channel_ok,
                            f"选了 patchright 却没探通：{eng.patchright_channel_reason}")
        else:
            self.assertEqual(eng.injection_engine, "playwright")
            self.assertFalse(eng.patchright_channel_ok,
                             "回退到 playwright 的前提是 patchright 探针不通过")

    def test_prewarm_selfcheck_catches_dead_channel(self):
        """自证审计**不能是摆设**：通道真断时必须报 ERROR（否则下次又静默失效）"""
        got, records, eng = self._run_engine(probe_script_override="void 0; /* 通道静默失效 */")
        self.assertEqual(got["channel_mark"], 0, "探针 mark 本应被停掉（测试自身失效）")
        errors = [r.getMessage() for r in records if r.levelno >= 40]
        self.assertTrue(any("注入通道自证失败" in m for m in errors),
                        f"通道断了但引擎只字未提，日志里没有 ERROR：{errors}")


if __name__ == "__main__":
    unittest.main()
