"""v2.19.11 回归：安装包载荷必须是**路径并集**，不许把两棵 onedir 各压一份。

**为什么盯这条**（2026-10-23 实测证据）：

    dist\\KianaLauncher   12,859 文件 / 1.804 GiB
    dist\\KianaCrawler    15,612 文件 / 2.095 GiB
    不归并（旧装法）       3.898 GiB
    并集                 2.125 GiB   ← 12,847 个同名文件逐字节相同，白压 1.774 GiB

`kiana_setup.nsi` 本来就是"两棵树依次铺进同一个 `$INSTDIR`、后者覆盖前者"，
所以落盘状态**本来就是并集**；双份压进安装包只有一个后果——把 NSIS 顶到 2 GB
上限，安装包直接产不出来：

    Internal compiler error #12345: error mmapping file (1950187200, 33554432)
    is out of range.

同日实测：32 位 makensis 3.12 与 64 位 makensis（PE machine=0x8664）**在同一
字节数报同一句错**（后者 161.4 秒、峰值内存 3 MB）——所以"换 64 位编译器"
不是解法，**只有把重复载荷去掉**才是。

判据一律查**结构**：并集语义、硬链接落盘、边界守卫、以及 `nsi`/`bat` 里的
接线是否真的指到载荷目录。**零处**断言日志文案。
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_tool():
    """从 tools/ 载入被测模块（tools 不是包）。"""
    path = ROOT / "tools" / "pack_installer_payload.py"
    spec = importlib.util.spec_from_file_location("pack_installer_payload", path)
    assert spec and spec.loader, f"载入失败：{path}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pip = _load_tool()


# ══════════════════════════════════════════════════════════════════════
# 夹具：两棵最小 onedir（结构模拟真实 dist，体积极小）
# ══════════════════════════════════════════════════════════════════════
@pytest.fixture()
def dist(tmp_path: Path) -> Path:
    """模拟 dist/：launcher 与 crawler 有大量同名同内容 + 少量各自独有。

    对齐真实数据：`_internal/base_library.zip` 同名但内容不同（真机唯一冲突）。
    """
    launcher = tmp_path / "dist" / "KianaLauncher"
    crawler = tmp_path / "dist" / "KianaCrawler"

    # 两边都有、内容相同（真实世界里占 45.5% 的那批）
    for base in (launcher, crawler):
        (base / "_internal" / "browsers").mkdir(parents=True)
        (base / "_internal" / "browsers" / "chrome.dll").write_bytes(b"X" * 64)
        exe = "KianaLauncher.exe" if base is launcher else "KianaCrawler.exe"
        (base / exe).write_bytes(b"E" * 8)

    # 只有 launcher 有
    (launcher / "launcher_only.txt").write_bytes(b"L" * 4)
    # 只有 crawler 有
    (crawler / "_internal" / "crawler_only.pyd").write_bytes(b"C" * 16)
    # 同名不同内容（真机唯一冲突：base_library.zip）
    (launcher / "_internal" / "base_library.zip").write_bytes(b"AAAA")
    (crawler / "_internal" / "base_library.zip").write_bytes(b"BBBBBB")
    return tmp_path / "dist"


def _bases(dist: Path):
    return [dist / "KianaLauncher", dist / "KianaCrawler"]


# ══════════════════════════════════════════════════════════════════════
# 1. 并集语义：与 NSIS 的"后者覆盖前者"逐字对齐
# ══════════════════════════════════════════════════════════════════════
class TestPlanUnion:
    def test_same_path_takes_later_dir(self, dist: Path):
        """同名不同内容 ⇒ 取**后**铺设的那份（= Crawler），与 NSIS 覆盖顺序一致。"""
        plan = pip.plan_union(_bases(dist))
        src = plan["_internal/base_library.zip"]
        assert src.parent.parent.name == "KianaCrawler"
        assert src.read_bytes() == b"BBBBBB"

    def test_keeps_unique_files_from_both(self, dist: Path):
        plan = pip.plan_union(_bases(dist))
        assert "launcher_only.txt" in plan
        assert "_internal/crawler_only.pyd" in plan
        assert plan["launcher_only.txt"].read_bytes() == b"L" * 4

    def test_identical_bytes_collapse_to_one_entry(self, dist: Path):
        """同名同内容只留**一条**——这是 1.774 GiB 被省掉的机制本身。"""
        plan = pip.plan_union(_bases(dist))
        assert sum(1 for k in plan if k.endswith("chrome.dll")) == 1
        assert len(plan) == 6

    def test_union_smaller_than_naive(self, dist: Path):
        plan = pip.plan_union(_bases(dist))
        union = pip.total_bytes(plan)
        naive = pip.naive_bytes(_bases(dist))
        assert naive == 64 + 8 + 4 + 4 + 64 + 8 + 16 + 6
        assert union < naive
        # 两个 exe 名字不同 ⇒ 都在；共同的那份 chrome.dll 只算一次
        assert union == 64 + 8 + 8 + 4 + 16 + 6

    def test_conflicts_report_only_size_mismatch(self, dist: Path):
        """同名不同**大小**才报冲突；同大小（内容不同）不算——覆盖规则已定。"""
        conflicts = pip.size_conflicts(_bases(dist))
        assert [c[0] for c in conflicts] == ["_internal/base_library.zip"]


# ══════════════════════════════════════════════════════════════════════
# 2. 落盘：硬链接（零额外占用）与回退
# ══════════════════════════════════════════════════════════════════════
class TestMaterialize:
    def test_writes_union_and_uses_hardlink(self, dist: Path, tmp_path: Path):
        out = dist / "_pack"
        plan = pip.plan_union(_bases(dist))
        written = pip.materialize(plan, out)
        assert written == len(plan)
        assert pip.naive_bytes([out]) == pip.total_bytes(plan)
        # 硬链接：同一份数据块（st_ino 相同）
        dst = out / "launcher_only.txt"
        assert dst.exists()
        if hasattr(os.stat_result, "st_ino"):
            assert os.stat(dst).st_ino == os.stat(plan["launcher_only.txt"]).st_ino

    def test_falls_back_to_copy_when_link_unavailable(self, dist: Path, monkeypatch):
        """跨卷等场景硬链接会失败——必须退回复制，不许半途而废。"""
        def boom(*_a, **_kw):
            raise OSError("cross-device link")

        monkeypatch.setattr(pip.os, "link", boom)
        out = dist / "_pack"
        plan = pip.plan_union(_bases(dist))
        assert pip.materialize(plan, out) == len(plan)
        assert (out / "launcher_only.txt").read_bytes() == b"L" * 4

    def test_stale_pack_is_cleared(self, dist: Path):
        out = dist / "_pack"
        out.mkdir(parents=True)
        (out / "stale_from_last_build.bin").write_bytes(b"old")
        pip.materialize(pip.plan_union(_bases(dist)), out)
        assert not (out / "stale_from_last_build.bin").exists()


# ══════════════════════════════════════════════════════════════════════
# 3. 边界：只许动 dist/_pack
# ══════════════════════════════════════════════════════════════════════
class TestGuard:
    def test_rejects_outside_dist(self, dist: Path, tmp_path: Path):
        with pytest.raises(ValueError):
            pip._guard_out_dir(dist, tmp_path / "elsewhere", _bases(dist))

    def test_rejects_dist_root_itself(self, dist: Path):
        with pytest.raises(ValueError):
            pip._guard_out_dir(dist, dist, _bases(dist))

    def test_rejects_source_tree(self, dist: Path):
        with pytest.raises(ValueError):
            pip._guard_out_dir(dist, dist / "KianaCrawler", _bases(dist))


# ══════════════════════════════════════════════════════════════════════
# 4. CLI
# ══════════════════════════════════════════════════════════════════════
class TestCli:
    def test_check_does_not_write(self, dist: Path, capsys):
        assert pip.main(["--dist", str(dist), "--check"]) == 0
        assert not (dist / "_pack").exists()

    def test_missing_onedir_is_exit_2(self, tmp_path: Path, capsys):
        (tmp_path / "dist" / "KianaLauncher").mkdir(parents=True)
        assert pip.main(["--dist", str(tmp_path / "dist")]) == 2

    def test_build_ok(self, dist: Path, capsys):
        assert pip.main(["--dist", str(dist)]) == 0
        out = dist / "_pack"
        assert (out / "KianaLauncher.exe").exists()
        assert (out / "KianaCrawler.exe").exists()
        assert (out / "_internal" / "browsers" / "chrome.dll").exists()


# ══════════════════════════════════════════════════════════════════════
# 5. 接线：nsi 与构建脚本必须真的指到载荷目录（防回退）
# ══════════════════════════════════════════════════════════════════════
class TestWiring:
    def test_nsi_packs_single_union_payload(self):
        nsi = (ROOT / "kiana_setup.nsi").read_text(encoding="utf-8", errors="ignore")
        assert 'File /r "${DIST_DIR}\\_pack\\*.*"' in nsi
        # 回退判据：两棵树各自入包的写法必须已经不存在
        assert 'File /r "${DIST_DIR}\\KianaLauncher\\*.*"' not in nsi
        assert 'File /r "${DIST_DIR}\\KianaCrawler\\*.*"' not in nsi

    def test_nsi_keeps_upgrade_guard(self):
        """载荷换成并集后，升级前清 `_internal` 的防护不能顺手丢掉。"""
        nsi = (ROOT / "kiana_setup.nsi").read_text(encoding="utf-8", errors="ignore")
        assert 'RMDir /r "$INSTDIR\\_internal"' in nsi

    def test_build_script_runs_payload_tool_before_makensis(self):
        bat = (ROOT / "一键构建.bat").read_text(encoding="utf-8", errors="ignore")
        tool_pos = bat.find("pack_installer_payload.py")
        makensis_pos = bat.find("kiana_setup.nsi")
        assert tool_pos != -1, "构建脚本没有调用载荷合成工具"
        assert makensis_pos != -1
        assert tool_pos < makensis_pos, "载荷必须在 makensis 之前合成"

    def test_build_script_still_has_utf8_env(self):
        """[4b/7] 假 FAIL 的老坑：UTF-8 环境变量与脚本编码必须成对出现。"""
        bat = (ROOT / "一键构建.bat").read_text(encoding="utf-8", errors="ignore")
        assert "PYTHONIOENCODING" in bat
        assert "chcp 65001" in bat
        raw = (ROOT / "一键构建.bat").read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf"), "一键构建.bat 必须 UTF-8 BOM（否则 cmd 读坏中文）"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
