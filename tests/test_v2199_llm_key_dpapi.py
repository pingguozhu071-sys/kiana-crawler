# -*- coding: utf-8 -*-
"""[v2.19.9 安全] `llm_key` 不再明文落 `launcher_config.json` —— 与打码密钥**同一套** DPAPI。

## 为什么该加密（结论，不是"顺手统一"）

`llm_key` 原先是明文存在用户真实 `%LOCALAPPDATA%\\KianaVnextPlus\\launcher_config.json`
里的 API Key。逐条核过之后判定**应当加密**，依据是三条代码事实：

1. 它**唯一**的活消费者是**进程内**的 `run_crawler._maybe_llm_enhancer`
   （`EngineBridge` 是 daemon 线程里进程内调 `crawl()`，**不是子进程**）
   ⇒ "在入口 cfg 那一层解出来"就够了，没有第二个进程需要这份明文；
2. 全仓**没有** env / argv / 外部工具 / 导出件需要它
   （`DEEPSEEK_API_KEY` 只被搬进 `os.environ`，**从来没有任何代码读它**）；
3. 也没有任何注释说"故意留明文"——相反 内部施工方案 的 v2.19.8 附注
   写明"若要统一，按 v2.19.7 的 privacy_store 原语改造即可（含旧明文读到即迁移）"。

**代价（如实记下）**：DPAPI 是"当前用户 + 本机"作用域，密文换机器/换用户解不开；
便携模式（`KIANA_PORTABLE=1`）的数据根在 exe 旁边，那份目录从此不可搬移。
打码密钥早已接受同一代价，且 DPAPI 不可用时**降级为明文并如实告知**——
本文件把这条纪律也一起钉住。

## 纪律

⚠️ **本文件绝不读用户的真实配置**，也绝不把它写进任何断言消息：所有数据根都被
`data_root()` 打桩到 `tmp_path`（`secrets/` 也随之落在 tmp 里）。
"""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 用一个明显的哨兵串代替真密钥：它一旦出现在任何落盘文件里，就是"明文泄漏"的证据。
SENTINEL = "sk-TEST-ONLY-NOT-A-REAL-KEY-0000"


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """把 `data_root()` 指到 tmp —— `secrets/` 绝不落用户真实目录。"""
    from kiana_vnext_plus import config as cfg_mod
    monkeypatch.setattr(cfg_mod, "data_root", lambda: str(tmp_path))
    return tmp_path


def _v8():
    try:
        import launcher_v8
        return launcher_v8
    except Exception as e:                      # 无 Qt/qfluentwidgets 的环境
        pytest.skip(f"launcher_v8 不可导入：{e}")


# ══════════════════════════════════════════════════════════════════════
# 1. 落盘形态：密文文件里不许有明文；配置字典里不许留明文键
# ══════════════════════════════════════════════════════════════════════
class TestLlmKeyAtRest:
    def test_same_primitive_as_captcha_keys(self, isolated):
        """**必须是同一套机制**：走 `privacy_store.save_secret_json` →
        `data_root()/secrets/<name>.bin`。不新造第二种加密。"""
        from kiana_vnext_plus import privacy_store as ps
        l8 = _v8()
        if sys.platform != "win32":
            pytest.skip("DPAPI 仅 Windows")
        assert l8.save_llm_key(SENTINEL) is True
        p = ps.secret_path(l8._LLM_KEY_SECRET)
        assert p.exists(), "密文文件没落在 secrets/ 下（是不是自己造了第二套存储？）"
        assert p.parent.name == "secrets"
        assert p.suffix == ".bin"

    def test_ciphertext_never_contains_the_plaintext(self, isolated):
        """落盘字节里不许出现明文 —— 这是本轮的**核心安全判据**。"""
        from kiana_vnext_plus import privacy_store as ps
        l8 = _v8()
        if sys.platform != "win32":
            pytest.skip("DPAPI 仅 Windows")
        l8.save_llm_key(SENTINEL)
        raw = ps.secret_path(l8._LLM_KEY_SECRET).read_bytes()
        assert SENTINEL.encode() not in raw, "密文里能直接搜到明文 API Key"
        assert b"sk-" not in raw, "密文里出现了 API Key 前缀（DPAPI 没真的生效？）"

    def test_roundtrip(self, isolated):
        l8 = _v8()
        if sys.platform != "win32":
            pytest.skip("DPAPI 仅 Windows")
        assert l8.save_llm_key(SENTINEL) is True
        assert l8.load_llm_key({}) == SENTINEL

    def test_empty_deletes_the_ciphertext(self, isolated):
        """清空 Key → 密文文件也要删掉（不然"清空"只是界面上的假动作）。"""
        from kiana_vnext_plus import privacy_store as ps
        l8 = _v8()
        if sys.platform != "win32":
            pytest.skip("DPAPI 仅 Windows")
        l8.save_llm_key(SENTINEL)
        assert ps.secret_path(l8._LLM_KEY_SECRET).exists()
        assert l8.save_llm_key("") is True
        assert not ps.secret_path(l8._LLM_KEY_SECRET).exists()
        assert l8.load_llm_key({}) == ""

    def test_no_key_at_all_is_not_an_error(self, isolated):
        l8 = _v8()
        assert l8.load_llm_key({}) == ""


# ══════════════════════════════════════════════════════════════════════
# 2. 旧明文自动迁移（照 load_captcha_keys 的先例）
# ══════════════════════════════════════════════════════════════════════
class TestLegacyPlaintextMigration:
    def test_migration_drops_plaintext_and_keeps_value(self, isolated):
        """读到旧明文 → 写密文 → **把明文键从配置字典里抹掉** → 值不丢。"""
        from kiana_vnext_plus import privacy_store as ps
        l8 = _v8()
        if sys.platform != "win32":
            pytest.skip("DPAPI 仅 Windows")
        cfg = {"llm_key": SENTINEL, "llm_api_base": "https://api.deepseek.com/v1"}
        got = l8.load_llm_key(cfg)
        assert got == SENTINEL, "迁移把值搞丢了"
        assert "llm_key" not in cfg, "明文键没被抹掉 —— 配置里留着第二份明文副本"
        assert ps.secret_path(l8._LLM_KEY_SECRET).exists()
        assert ps.load_secret_json(l8._LLM_KEY_SECRET).get("key") == SENTINEL
        # 其余 LLM 设置不许被这次迁移牵连
        assert cfg["llm_api_base"] == "https://api.deepseek.com/v1"

    def test_ciphertext_wins_over_a_stale_plaintext_copy(self, isolated):
        """密文优先：配置里还留着旧明文时，以密文为准（否则"迁移过了又变回去"）。"""
        l8 = _v8()
        if sys.platform != "win32":
            pytest.skip("DPAPI 仅 Windows")
        assert l8.save_llm_key("sk-NEW-0000") is True
        cfg = {"llm_key": "sk-OLD-STALE-0000"}
        assert l8.load_llm_key(cfg) == "sk-NEW-0000"

    def test_the_gui_migrates_at_startup_not_at_next_save(self, isolated):
        """**启动钩子是承重的**：迁移只在内存字典里抹明文，真正落盘要靠调用方再
        `save_config()`。少了这一步，用户的明文 Key 会一直在磁盘上躺着
        （打码密钥那条也是靠同一个启动钩子）。"""
        import ast
        src = (ROOT / "launcher_v9.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        init = None
        for n in ast.walk(tree):
            if isinstance(n, ast.ClassDef) and n.name == "KianaV9":
                init = next(m for m in n.body
                            if isinstance(m, ast.FunctionDef) and m.name == "__init__")
        assert init is not None
        code = ast.unparse(init)
        assert "load_llm_key" in code, "启动时没有做 LLM Key 迁移"
        assert "save_config" in code, "启动钩子没有落盘 —— 明文会一直留在磁盘上"

    def test_widget_is_populated_from_the_ciphertext(self):
        """设置页那个 Key 框必须从**密文解出来的值**填，不是从 `win.config`。

        从 config 填的话：迁移一跑，config 里就没这个键了 → 用户重开程序看到空白框，
        会以为"Key 丢了"（打码密钥那三个框也是这么填的：`load_captcha_keys(win.config)`）。
        """
        import ast
        src = (ROOT / "launcher_v9.py").read_text(encoding="utf-8")
        build = None
        for n in ast.walk(ast.parse(src)):
            if isinstance(n, ast.ClassDef) and n.name == "SettingsPage":
                build = next(m for m in n.body
                             if isinstance(m, ast.FunctionDef) and m.name == "build")
        code = ast.unparse(build)
        assert "self.llm_key = QLineEdit(load_llm_key(" in code, \
            "Key 框不是从密文解出来的值填的（迁移后重开会显示空白）"


# ══════════════════════════════════════════════════════════════════════
# 3. 不许谎称加密（照 _save_captcha_keys 的诚实性纪律）
# ══════════════════════════════════════════════════════════════════════
class TestHonestyWhenDpapiUnavailable:
    def test_save_reports_failure_instead_of_writing_plaintext(self, isolated, monkeypatch):
        """DPAPI 不可用时 `save_llm_key` 返回 False —— 它自己**绝不**偷偷写明文
        （`privacy_store.save_secret_json` 的契约就是这样，这里钉住 llm 这层没破坏它）。"""
        from kiana_vnext_plus import privacy_store as ps
        l8 = _v8()
        monkeypatch.setattr(ps, "save_secret_json", lambda *a, **k: False)
        assert l8.save_llm_key(SENTINEL) is False
        assert not ps.secret_path(l8._LLM_KEY_SECRET).exists(), \
            "保存失败了却还是落了盘（那就是绕过 DPAPI 写了明文）"

    def test_toast_does_not_lie(self):
        """提示语必须按 `enc_ok` 分支，并在失败分支里**明说没加密**。"""
        import ast

        class _Strip(ast.NodeTransformer):
            def generic_visit(self, node):
                super().generic_visit(node)
                for f in ("body", "orelse", "finalbody"):
                    lst = getattr(node, f, None)
                    if isinstance(lst, list):
                        kept = [s for s in lst if not (
                            isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)
                            and isinstance(s.value.value, str))]
                        if f == "body" and not kept:
                            kept = [ast.Pass()]
                        setattr(node, f, kept)
                return node

        tree = ast.parse((ROOT / "launcher_v9.py").read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_save_llm_settings")
        body = ast.unparse(_Strip().visit(fn))
        assert "enc_ok" in body, "没根据加密结果分支提示"
        assert "明文落盘" in body, "DPAPI 不可用时未如实告知"

    def test_gui_keeps_plaintext_only_as_an_honest_fallback(self):
        """降级路径必须存在且唯一：**只有** DPAPI 失败才把明文写回配置，
        成功时一定要 `pop` 掉（否则"加密了"和"没加密"两份同时存在）。"""
        import ast

        class _Strip(ast.NodeTransformer):
            def generic_visit(self, node):
                super().generic_visit(node)
                for f in ("body", "orelse", "finalbody"):
                    lst = getattr(node, f, None)
                    if isinstance(lst, list):
                        kept = [s for s in lst if not (
                            isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)
                            and isinstance(s.value.value, str))]
                        if f == "body" and not kept:
                            kept = [ast.Pass()]
                        setattr(node, f, kept)
                return node

        tree = ast.parse((ROOT / "launcher_v9.py").read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_save_llm_settings")
        body = ast.unparse(_Strip().visit(fn))
        # 注意：`ast.unparse` 会把字符串字面量**规范化成单引号**，所以这里必须按
        # unparse 之后的形态断言（写成双引号会假红——我第一版就是这么翻车的）。
        assert "self.config.pop('llm_key', None)" in body, "成功加密后没抹掉明文副本"
        assert "self.config['llm_key'] = key" in body, "DPAPI 失败时没有保留可用的降级路径"

    def test_toast_does_not_claim_llm_is_on_without_a_key(self):
        """[v2.19.9 补] 开着开关却没填 Key/地址时**不许报"已启用"**。

        `run_crawler._maybe_llm_enhancer` 要求 key 与 base **同时**非空才构建
        `LLMClient`，否则整趟爬完什么都不会发生 —— 而界面说"LLM 已启用"，
        正是本工程反复吃亏的"用无关检查冒充成功"。这条把诚实性钉在源码上。
        """
        import ast

        class _Strip(ast.NodeTransformer):
            def generic_visit(self, node):
                super().generic_visit(node)
                for f in ("body", "orelse", "finalbody"):
                    lst = getattr(node, f, None)
                    if isinstance(lst, list):
                        kept = [s for s in lst if not (
                            isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)
                            and isinstance(s.value.value, str))]
                        if f == "body" and not kept:
                            kept = [ast.Pass()]
                        setattr(node, f, kept)
                return node

        tree = ast.parse((ROOT / "launcher_v9.py").read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_save_llm_settings")
        body = ast.unparse(_Strip().visit(fn))
        # 判据：在说"已启用"的那条分支**之前**，必须先有一条"缺 Key/地址"的告警分支。
        i_missing = body.find("不会真的调用 LLM")
        i_enabled = body.find("LLM 已启用")
        assert i_missing != -1, "缺 Key/地址时没有如实告知（会谎报『已启用』）"
        assert i_enabled != -1, "原来的『已启用』提示被删了"
        assert i_missing < i_enabled, \
            "告警分支排在了『已启用』后面 —— 那么缺 Key 时仍然会先报『已启用』"


# ══════════════════════════════════════════════════════════════════════
# 4. 引擎仍拿得到 Key（加密不许变成"能存不生效"）
# ══════════════════════════════════════════════════════════════════════
class TestEngineStillGetsTheKey:
    def test_engine_bridge_translation_table_resolves_from_ciphertext(self):
        import ast
        src = (ROOT / "launcher_v8.py").read_text(encoding="utf-8")
        code = ast.unparse(ast.parse(src))
        # `ast.unparse` 规范化成单引号（见上一条测试的说明）
        assert "'llm_key': load_llm_key(cfg)" in code, \
            "EngineBridge 翻译表没接密文读取 → 引擎恒读空 Key（能填不生效）"

    def test_run_crawler_still_consumes_the_key(self):
        """下游消费者一个字都不该动：入口 cfg 解出来的明文照旧喂给 LLMClient。"""
        src = (ROOT / "run_crawler.py").read_text(encoding="utf-8")
        assert 'key = str(cfg.get("llm_key") or "").strip()' in src
        assert '"api_key": key' in src

    def test_ciphertext_is_not_wired_into_engine_global_config(self):
        """**反面断言**：解密后的 Key 不许被塞进 `GlobalConfig` 的 gcfg 键表。

        引擎里还有一条从 `self.cfg` 读 `llm_key` 的潜在路径（`crawler.py`）——
        那条路今天是死的。一旦有人"顺手"把 llm_key 加进 gcfg 键表，解出来的明文就会
        进入引擎配置、被更多代码看见（也更容易被记进日志）。解密应当停在入口边界。
        """
        src = (ROOT / "run_crawler.py").read_text(encoding="utf-8")
        gcfg = src.split("gcfg = GlobalConfig(OmegaConf.create({", 1)[1].split("}))", 1)[0]
        assert "llm_key" not in gcfg, "解密后的 LLM Key 被塞进了引擎 GlobalConfig"
