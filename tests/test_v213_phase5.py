"""Kiana Vnext Plus — v2.13 阶段 5：Cookie 弹药库回归（加密/轮换/冷却/反馈闭环）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from kiana_vnext_plus.cookie_armory import CookieArmory, parse_cookie_file


class TestCookieArmory:
    def test_add_and_acquire(self, tmp_path):
        arm = CookieArmory(str(tmp_path / "f.db"), "pw-test")
        assert arm.add_account("bilibili", "acc1", "SESSDATA=abc; buvid=xyz")
        got = arm.acquire("bilibili")
        assert got == "SESSDATA=abc; buvid=xyz"

    def test_encrypted_at_rest(self, tmp_path):
        """明文 cookies 绝不入库（读原始 db 文件验证）"""
        arm = CookieArmory(str(tmp_path / "f.db"), "pw-test")
        secret = "SESSDATA=super-secret-value"
        arm.add_account("bilibili", "acc1", secret)
        raw = (tmp_path / "f.db").read_bytes()
        assert b"super-secret-value" not in raw   # 密文落库
        assert arm.acquire("bilibili") == secret  # 解密可取

    def test_health_rotation(self, tmp_path):
        """健康分轮换：acc1 连续失败坐冷板凳 → acquire 切到 acc2"""
        arm = CookieArmory(str(tmp_path / "f.db"), "pw-test")
        arm.add_account("bili", "acc1", "a=1")
        arm.add_account("bili", "acc2", "a=2")
        # 初始按 updated_at 取最新（acc2）
        assert arm.acquire("bili") == "a=2"
        for _ in range(4):  # acc2 连败 4 次 → 0 分坐冷板凳
            arm.report_failure("bili", "acc2")
        assert arm.acquire("bili") == "a=1"       # 轮到 acc1
        rows = {r["name"]: r for r in arm.list_accounts("bili")}
        assert rows["acc2"]["health"] == 0.0
        assert rows["acc2"]["cooldown_left"] > 0

    def test_success_restores(self, tmp_path):
        arm = CookieArmory(str(tmp_path / "f.db"), "pw-test")
        arm.add_account("bili", "acc1", "a=1")
        arm.report_failure("bili", "acc1")
        arm.report_failure("bili", "acc1")
        arm.report_success("bili", "acc1")   # +0.1 回血
        rows = arm.list_accounts("bili")
        assert rows[0]["health"] > 0.3

    def test_all_cooldown_returns_none(self, tmp_path):
        arm = CookieArmory(str(tmp_path / "f.db"), "pw-test")
        arm.add_account("bili", "only", "a=1")
        for _ in range(4):
            arm.report_failure("bili", "only")
        assert arm.acquire("bili") is None

    def test_parse_cookie_file(self, tmp_path):
        p = tmp_path / "c.txt"
        p.write_text("# Netscape HTTP Cookie File\n"
                     ".bilibili.com\tTRUE\t/\tFALSE\t0\tSESSDATA\tabc\n"
                     ".bilibili.com\tTRUE\t/\tFALSE\t0\tbuvid3\td\nef\n", encoding="utf-8")
        s = parse_cookie_file(str(p))
        assert "SESSDATA=abc" in s and "buvid3=d" in s
