# -*- coding: utf-8 -*-
"""[v2.19.7 安全·扫描发现] 密钥落盘与脱敏装配的回归测试。

覆盖三处修复：
  1. CLI 日志脱敏结构性缺口（`cli.py` 从不 import config → Filter 永不装配）
  2. CLI 主密码公开弱兜底 `"kiana-fallback"` 已删除
  3. GUI 打码密钥：DPAPI 密文落盘（原为明文 + 谎称加密），且真正传导进引擎
"""
import ast
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class _StripProse(ast.NodeTransformer):
    """删掉所有"裸字符串语句"（docstring/伪注释）——它们不是可执行代码"""

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


def _code_only(rel_path: str) -> str:
    """源码 → **只含可执行代码**的文本（剥掉全部 docstring 与裸字符串注释）。

    为什么必须这样：这些测试断言的是"代码里不再出现 X"，而修复说明本身就会引用 X
    （例如解释"原先兜底是 kiana-fallback"），直接搜原文会被自己的注释骗过（假红/假绿）。
    """
    src = (ROOT / rel_path).read_text(encoding="utf-8")
    return ast.unparse(_StripProse().visit(ast.parse(src)))



# ════════════════════════════════════════════════════════════
# 1. CLI 脱敏装配 + 弱兜底删除
# ════════════════════════════════════════════════════════════
class TestCliSanitizeWiring:
    def test_cli_imports_config(self):
        """main() 必须在 basicConfig 前 import config（否则脱敏 Filter 不装配）"""
        code = _code_only("kiana_vnext_plus/cli.py")
        main_body = code.split("def main():", 1)[1]
        assert "import config" in main_body, "main() 未装配日志脱敏"
        # 顺序：先 import config，后 basicConfig（Handler 构造时才挂得上 Filter）
        assert main_body.index("import config") < main_body.index("logging.basicConfig")

    def test_no_public_fallback_password(self):
        """公开字面量兜底密码必须消失（否则弹药库加密形同虚设）"""
        assert "kiana-fallback" not in _code_only("kiana_vnext_plus/cli.py")

    def test_password_helper_uses_config_data_root(self):
        code = _code_only("kiana_vnext_plus/cli.py")
        body = code.split("def _read_master_password", 1)[1].split("\ndef ", 1)[0]
        assert "GlobalConfig" in body, "未复用 config 体系（便携模式会找错目录）"
        assert "LOCALAPPDATA" not in body, "仍硬编码 %LOCALAPPDATA%"

    def test_handler_autosanitize_installs_filter(self):
        """装配后新建的 handler 必须自动带脱敏 Filter（结构性保证）"""
        from kiana_vnext_plus import config as cfg_mod
        import logging
        cfg_mod._install_handler_autosanitize()
        h = logging.StreamHandler()
        assert any(isinstance(f, cfg_mod._SanitizeLogFilter) for f in h.filters)


# ════════════════════════════════════════════════════════════
# 2. 打码密钥 DPAPI 存储（privacy_store 原语）
# ════════════════════════════════════════════════════════════
@pytest.fixture()
def isolated_data_root(tmp_path, monkeypatch):
    """把 data_root() 指到 tmp（密钥文件绝不落用户真实目录）"""
    from kiana_vnext_plus import config as cfg_mod
    monkeypatch.setattr(cfg_mod, "data_root", lambda: str(tmp_path))
    return tmp_path


class TestSecretJson:
    def test_roundtrip_encrypted(self, isolated_data_root, monkeypatch):
        from kiana_vnext_plus import privacy_store as ps
        # 非 Windows 无 DPAPI：只断言"不可用时不静默写明文"
        if sys.platform != "win32":
            assert ps.save_secret_json("captcha_keys", {"a": "b"}) is False
            return
        assert ps.save_secret_json("captcha_keys", {"twocaptcha": "KEY-123"}) is True
        blob = ps.secret_path("captcha_keys")
        assert blob.exists()
        # 落盘内容不得含明文密钥
        raw = blob.read_bytes()
        assert b"KEY-123" not in raw
        assert ps.load_secret_json("captcha_keys") == {"twocaptcha": "KEY-123"}

    def test_load_missing_returns_none(self, isolated_data_root):
        from kiana_vnext_plus import privacy_store as ps
        assert ps.load_secret_json("nope_not_here") is None

    def test_delete_secret(self, isolated_data_root):
        from kiana_vnext_plus import privacy_store as ps
        if sys.platform != "win32":
            pytest.skip("DPAPI 仅 Windows")
        ps.save_secret_json("captcha_keys", {"capsolver": "X"})
        assert ps.secret_path("captcha_keys").exists()
        assert ps.delete_secret("captcha_keys") is True
        assert not ps.secret_path("captcha_keys").exists()

    def test_name_sanitized(self, isolated_data_root):
        """name 里的路径分隔符不得逃出 secrets/ 目录"""
        from kiana_vnext_plus import privacy_store as ps
        p = ps.secret_path("../../evil")
        assert ".." not in p.parts
        assert p.parent.name == "secrets"


# ════════════════════════════════════════════════════════════
# 3. launcher 侧：明文迁移 + 引擎传导 + 不谎报
# ════════════════════════════════════════════════════════════
class TestCaptchaKeysLauncher:
    def _l8(self):
        import importlib
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        try:
            return importlib.import_module("launcher_v8")
        except Exception as e:                      # GUI 依赖缺失（无 Qt）→ 跳过
            pytest.skip(f"launcher_v8 不可导入: {e}")

    def test_migration_drops_plaintext(self, isolated_data_root, monkeypatch):
        l8 = self._l8()
        if sys.platform != "win32":
            pytest.skip("DPAPI 仅 Windows")
        cfg = {"captcha_api_keys": {"twocaptcha": "PLAIN-1", "capsolver": "", "anticaptcha": ""}}
        got = l8.load_captcha_keys(cfg)
        assert got.get("twocaptcha") == "PLAIN-1"
        # 明文键已被抹掉 + 密文文件已建立 + 密文里读得回来
        assert "captcha_api_keys" not in cfg
        from kiana_vnext_plus import privacy_store as ps
        assert ps.secret_path("captcha_keys").exists()
        assert ps.load_secret_json("captcha_keys").get("twocaptcha") == "PLAIN-1"

    def test_encrypted_preferred_over_plaintext(self, isolated_data_root):
        l8 = self._l8()
        if sys.platform != "win32":
            pytest.skip("DPAPI 仅 Windows")
        assert l8.save_captcha_keys({"twocaptcha": "ENC-2"}) is True
        cfg = {"captcha_api_keys": {"twocaptcha": "PLAIN-OLD"}}
        assert l8.load_captcha_keys(cfg).get("twocaptcha") == "ENC-2"

    def test_save_empty_deletes_ciphertext(self, isolated_data_root):
        l8 = self._l8()
        if sys.platform != "win32":
            pytest.skip("DPAPI 仅 Windows")
        l8.save_captcha_keys({"capsolver": "Z"})
        from kiana_vnext_plus import privacy_store as ps
        assert ps.secret_path("captcha_keys").exists()
        assert l8.save_captcha_keys({"twocaptcha": "", "capsolver": "", "anticaptcha": ""}) is True
        assert not ps.secret_path("captcha_keys").exists()

    def test_engine_cfg_carries_keys(self):
        """EngineBridge 翻译表必须含 captcha_api_keys（原缺口：GUI 能填不生效）"""
        code = _code_only("launcher_v8.py")
        body = code.split("engine_cfg = {", 1)[1].split("}", 1)[0]
        assert "captcha_api_keys" in body

    def test_run_crawler_forwards_keys(self):
        """run_crawler 必须把 cr 密钥塞进 GlobalConfig（否则 crawler 读到默认空 dict）"""
        code = _code_only("run_crawler.py")
        body = code.split("gcfg = GlobalConfig(OmegaConf.create({", 1)[1].split("}))", 1)[0]
        assert "captcha_api_keys" in body

    def test_toast_does_not_lie(self):
        """保存提示不得无条件宣称 DPAPI 加密（DPAPI 失败分支必须如实说明）"""
        code = _code_only("launcher_v9.py")
        body = code.split("def _save_captcha_keys", 1)[1].split("\ndef ", 1)[0]
        assert "enc_ok" in body, "未根据加密结果分支提示"
        assert "明文落盘" in body, "DPAPI 不可用时未如实告知"


# ════════════════════════════════════════════════════════════
# 4. 引擎侧消费者：cfg 里真的有密钥才构造打码后端
# ════════════════════════════════════════════════════════════
class TestSolverReceivesKeys:
    def test_crawler_reads_captcha_keys(self):
        src = (ROOT / "kiana_vnext_plus" / "crawler.py").read_text(encoding="utf-8")
        assert 'self.cfg.get("captcha_api_keys"' in src
        # 三个平台键都要接线（任一漏掉 = 该平台打码静默失效）
        for k in ("twocaptcha", "capsolver", "anticaptcha"):
            assert k in src
