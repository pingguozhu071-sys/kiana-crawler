"""Kiana Vnext Plus — 阶段 3 隐私拉满回归测试（v2.11）

覆盖：DPAPI 加密往返（Windows）、master.key.bin 迁移与明文删除、Fernet 链删除、
产物保鲜 prune_task_dirs（过期清理/容量兜底/新任务保护）、日志过滤器代理脱敏。
"""
import sys
import os
import time
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault('KIANA_CRYPTO_KEY', 'kv_test')  # 兼容旧测试引导；identity 已不要求

PROJECT = Path(__file__).parent.parent


class TestDpapi:
    """privacy_store：CryptProtectData/CryptUnprotectData 往返"""

    def test_roundtrip(self):
        import pytest
        from kiana_vnext_plus.privacy_store import dpapi_protect, dpapi_unprotect
        if os.name != "nt":
            pytest.skip("DPAPI 仅 Windows")
        secret = "master-password-测试-123"
        blob = dpapi_protect(secret.encode("utf-8"))
        assert blob != secret.encode()          # 密文 ≠ 明文
        assert secret.encode() not in blob       # 明文不出现在密文中
        assert dpapi_unprotect(blob).decode("utf-8") == secret

    def test_save_load_protected(self, tmp_path):
        import pytest
        from kiana_vnext_plus.privacy_store import save_protected, load_protected
        if os.name != "nt":
            pytest.skip("DPAPI 仅 Windows")
        f = tmp_path / "sub" / "master.key.bin"
        save_protected(f, "pw-secret-9876")
        assert f.read_bytes()[:4] != b"pw-s"     # 落盘非明文
        assert load_protected(f) == "pw-secret-9876"


class TestMasterKeyMigration:
    """GlobalConfig master_password：明文 master.key → DPAPI .bin 迁移后明文即删"""

    def test_migration(self, tmp_path, monkeypatch):
        import pytest
        if os.name != "nt":
            pytest.skip("DPAPI 仅 Windows")
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        appdir = tmp_path / "KianaVnextPlus"
        appdir.mkdir(parents=True, exist_ok=True)
        (appdir / "master.key").write_text("old-plain-pw", encoding="utf-8")
        # 清 sys.modules 缓存强制重新实例化
        import importlib
        from omegaconf import OmegaConf
        import kiana_vnext_plus.config as cfg_mod
        importlib.reload(cfg_mod)
        g = cfg_mod.GlobalConfig(OmegaConf.create({}))
        assert g.cfg.master_password == "old-plain-pw"   # 迁移保值
        assert (appdir / "master.key.bin").exists()       # 加密产物存在
        assert not (appdir / "master.key").exists()       # 明文已删
        # 二次实例化走 .bin 读取
        g2 = cfg_mod.GlobalConfig(OmegaConf.create({}))
        assert g2.cfg.master_password == "old-plain-pw"
        importlib.reload(cfg_mod)  # 还原模块状态


class TestFernetRemoved:
    """identity.py Fernet 链删除（硬编码默认密钥类风险根除）"""

    def test_no_fernet(self):
        import kiana_vnext_plus.identity as ident
        assert not hasattr(ident, "Fernet")
        assert not hasattr(ident, "derive_fernet_key")
        from kiana_vnext_plus.identity import ProjectIdentity
        p = ProjectIdentity("t_fernet", base_dir=PROJECT / ".tmp_test_projects")
        assert not hasattr(p, "fernet")
        assert not hasattr(p, "crypto_key")
        shutil.rmtree(PROJECT / ".tmp_test_projects" / "t_fernet", ignore_errors=True)

    def test_identity_works_without_env(self, monkeypatch):
        # 原 KIANA_CRYPTO_KEY 缺失即 raise 的启动失败模式已消除
        import subprocess
        import sys
        code = ("import sys; sys.path.insert(0, r'%s');"
                "from kiana_vnext_plus.identity import ProjectIdentity;"
                "p = ProjectIdentity('t_env', base_dir=r'%s'); print('OK')" % (
                    str(PROJECT), str(PROJECT / ".tmp_test_projects")))
        env = {k: v for k, v in os.environ.items() if k != "KIANA_CRYPTO_KEY"}
        env["PYTHONIOENCODING"] = "utf-8"
        r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, env=env, timeout=30)
        assert "OK" in r.stdout, r.stderr
        shutil.rmtree(PROJECT / ".tmp_test_projects" / "t_env", ignore_errors=True)


class TestPruneTaskDirs:
    """产物保鲜：过期清理 + 容量兜底 + 新任务保护"""

    def _mk(self, base, name, age_h, size_mb=1):
        d = base / name
        d.mkdir(parents=True, exist_ok=True)
        f = d / "data.bin"
        f.write_bytes(b"x" * (size_mb * 1024 * 1024))
        old = time.time() - age_h * 3600
        os.utime(d, (old, old))
        os.utime(f, (old, old))
        return d

    def test_expired_pruned_fresh_kept(self, tmp_path):
        from kiana_vnext_plus.enhancements import prune_task_dirs
        old = self._mk(tmp_path, "cli_oldtask", age_h=200)     # 8 天前
        fresh = self._mk(tmp_path, "cli_newtask", age_h=1)     # 1h 前（保护）
        removed = prune_task_dirs(tmp_path, keep_days=7, max_gb=0)
        assert removed == 1
        assert not old.exists()
        assert fresh.exists()

    def test_capacity_cap(self, tmp_path):
        from kiana_vnext_plus.enhancements import prune_task_dirs
        self._mk(tmp_path, "cli_a", age_h=10, size_mb=6)
        self._mk(tmp_path, "cli_b", age_h=20, size_mb=6)
        # 上限 0.01GB → 只留最旧先删直到达标
        prune_task_dirs(tmp_path, keep_days=0, max_gb=0.01)
        remaining = list(tmp_path.glob("cli_*"))
        assert len(remaining) <= 1

    def test_no_dirs_safe(self, tmp_path):
        from kiana_vnext_plus.enhancements import prune_task_dirs
        assert prune_task_dirs(tmp_path, 7, 50) == 0
        assert prune_task_dirs(tmp_path / "missing", 7, 50) == 0


class TestLogFilterProxySafe:
    """根日志过滤器：URL token 脱敏仍生效（代理凭据由 sanitize_proxy 三处定点覆盖）"""

    def test_sanitize_url_in_filter(self):
        import logging
        from kiana_vnext_plus.config import _SanitizeLogFilter
        rec = logging.LogRecord("t", logging.INFO, "f", 1,
                                "visit https://x.com/a?token=supersecret&page=1",
                                None, None)
        _SanitizeLogFilter().filter(rec)
        assert "supersecret" not in rec.getMessage()
        assert "[REDACTED]" in rec.getMessage()
