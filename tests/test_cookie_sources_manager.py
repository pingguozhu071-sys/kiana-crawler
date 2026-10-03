# -*- coding: utf-8 -*-
"""「Cookies 管理（多个来源）」的纯逻辑测试 —— **不弹窗口、不弹浏览器、不联网**。

## 这个功能真正要防的是什么

机主的原话是"你现在无法同时管理多个 cookies"。但**引擎一直就支持多份**
（`KIANA_COOKIE_FILES` 分号/换行分隔，`_ensure_cookie_file` 合并时**先到先得**）。
缺的从来不是引擎能力，而是**看得见**：

    用户在界面上"配好了" → 引擎其实按无 cookies 跑 → 界面一片正常

（真机前科：界面上填的是 `www.bilibili.com_cookies.txt`，而浏览器下载时改名成了
`... (1).txt` ⇒ 路径失效 ⇒ 全套 480P，用户毫不知情。`universal_downloader`
里那段 `logger.error` 就是为这件事加的，但**界面上一声不吭**。）

所以本文件的火力对准三件事：

1. **来源解析与引擎同源** —— 界面显示的清单必须就是引擎真会读的那几个文件。
   这里直接断言 `resolve_cookie_sources()` 与 `cookie_source_files()`
   在同样的环境/参数下给出**同一份路径列表**（同一实现，不可能分叉）。
2. **增/删的语义** —— 加进来的排**最前**（先到先得，排后面就盖不住旧 SESSDATA）、
   删只删配置**不删文件**、删除不动别的项。
3. **不撒谎的措辞** —— "文件不存在""空文件""自动发现 vs 显式指定"
   必须分开说；聚合判断**不许归因到某一行**（引擎本来就是合并读的）。

## 为什么不测"真去读一份 cookie"

那种测试属于 `tests/test_cookie_netscape_spec.py` / `test_cookie_profile.py`
的范围（格式解析）。这里只关心**"哪几个文件、按什么顺序、以什么身份参与"**。
"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kiana_vnext_plus.cookie_utils import (  # noqa: E402
    cookie_paths_add_front, cookie_paths_drop, cookie_source_files,
    default_cookie_file, profiles_cookie_files, resolve_cookie_sources,
)

try:
    import launcher_v9 as gui
    _IMPORT_ERR = ""
except Exception as _e:                                     # pragma: no cover
    gui = None
    _IMPORT_ERR = f"{type(_e).__name__}: {_e}"

_needs_gui = pytest.mark.skipif(gui is None, reason=f"launcher_v9 不可导入（{_IMPORT_ERR}）")


# ══════════════════════════════════════════════════════════════════════
# 环境隔离：**每个测试都必须有一个假的 LOCALAPPDATA**
# ══════════════════════════════════════════════════════════════════════
@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """把 `LOCALAPPDATA` 指到临时目录，并清掉两个 cookies 环境变量。

    为什么必须 autouse：默认位置与 `profiles/*/cookies.txt` 都挂在
    `%LOCALAPPDATA%\\KianaVnextPlus\\` 下。不隔离的话，**机主本机的真实 cookies**
    会跑进断言里 —— 那既会让测试随本机状态飘，也可能把真实路径写进失败输出。
    """
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("KIANA_COOKIE_FILES", raising=False)
    monkeypatch.delenv("KIANA_COOKIE_FILE", raising=False)
    return tmp_path


def _mkfile(p: Path, text: str = "x") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _select_source(win, index: int):
    """在「Cookies 管理」列表里选中**第 index 个来源**（0 基）。

    布局契约：列表文本第 0 行是表头说明（"按顺序生效…"），第 1 行才是第 1 个来源。
    所以 `QTextCursor` 的 `blockNumber() == index + 1` 对应 `rows[index]`
    —— `_selected_cookie_source()` 里那条 `- 1` 就是补这个偏移。

    ⚠️ 这条偏移**真的错过一次**：忘了减 1 时第 1 项永远选不中、最后一行越界，
    表现为"移除/设最优先点了没反应"。所以这里把契约写死一处，
    并且**第 1 项也要测**（`test_first_row_is_selectable` 专门钉它）。
    """
    from PySide6.QtGui import QTextCursor
    cur = win.settings.cookie_sources_view.textCursor()
    cur.movePosition(QTextCursor.MoveOperation.Start)
    for _ in range(index + 1):
        cur.movePosition(QTextCursor.MoveOperation.Down)
    win.settings.cookie_sources_view.setTextCursor(cur)


# ══════════════════════════════════════════════════════════════════════
# 1. 来源解析：优先级链与"从哪来"
# ══════════════════════════════════════════════════════════════════════
class TestResolveCookieSources:
    def test_nothing_configured_and_no_profiles_is_empty(self):
        assert resolve_cookie_sources("") == []

    def test_explicit_multi_file_keeps_order_and_marks_origin(self, tmp_path):
        a, b = _mkfile(tmp_path / "a.txt"), _mkfile(tmp_path / "b.txt")
        rows = resolve_cookie_sources(f"{a};{b}")
        assert [r["path"] for r in rows] == [str(a), str(b)]
        assert {r["origin"] for r in rows} == {"explicit"}
        assert all(r["exists"] for r in rows)

    def test_explicit_wins_over_default_and_profiles(self, tmp_path):
        """显式指定之后**不再追加**默认位置/配置档 —— 尊重用户选择，语义不许改。"""
        explicit = _mkfile(tmp_path / "mine.txt")
        _mkfile(tmp_path / "KianaVnextPlus" / "cookies.txt")
        _mkfile(tmp_path / "KianaVnextPlus" / "profiles" / "bilibili" / "cookies.txt")
        rows = resolve_cookie_sources(str(explicit))
        assert [r["path"] for r in rows] == [str(explicit)]
        assert rows[0]["origin"] == "explicit"

    def test_env_single_file_is_second_priority(self, tmp_path, monkeypatch):
        one = _mkfile(tmp_path / "one.txt")
        monkeypatch.setenv("KIANA_COOKIE_FILE", str(one))
        rows = resolve_cookie_sources("")
        assert [r["path"] for r in rows] == [str(one)]
        assert rows[0]["origin"] == "file_env"

    def test_default_location_third_priority(self, tmp_path):
        d = _mkfile(Path(default_cookie_file()))
        rows = resolve_cookie_sources("")
        assert [r["path"] for r in rows] == [str(d)]
        assert rows[0]["origin"] == "default"

    def test_profiles_are_the_last_resort(self, tmp_path):
        """用户什么都没配 → 配置档兜底（`cookie_profile` 登录过一次就该生效）。"""
        p = _mkfile(tmp_path / "KianaVnextPlus" / "profiles" / "bilibili" / "cookies.txt")
        rows = resolve_cookie_sources("")
        assert [r["path"] for r in rows] == [str(p)]
        assert rows[0]["origin"] == "profile"

    def test_profiles_and_default_are_MERGED_profiles_first(self, tmp_path):
        """**回归：真机踩到的"影子文件"**（本轮真机发现，值得单独钉死）。

        `%LOCALAPPDATA%\\KianaVnextPlus\\cookies.txt` 里躺着一个**9 天前的失效文件**
        时，原实现会"默认位置存在就直接返回"，于是**刚刚用 GUI 登录好的
        `profiles/bilibili/cookies.txt` 被整个挡住** ——
        表现正是机主报的"登录明明成功了，抓取还是 480P"。

        为什么单测抓不到它：所有单测都用**空的临时目录**，
        没有"默认位置恰好躺着一个旧文件"这种状态。
        ⇒ 这条测试**专门构造那个状态**，把两档的关系钉死：
        **配置档在前、默认位置在后**（引擎合并是先到先得 ⇒ 配置档的同名 cookie 会赢）。
        """
        old = _mkfile(tmp_path / "KianaVnextPlus" / "cookies.txt")          # 影子文件
        new = _mkfile(tmp_path / "KianaVnextPlus" / "profiles" / "bilibili" / "cookies.txt")
        rows = resolve_cookie_sources("")
        paths = [r["path"] for r in rows]
        assert str(old) in paths, "默认位置仍要被认（历史遗留不该被丢掉）"
        assert str(new) in paths, "配置档必须也在 —— 就是它被挡住了才出 480P"
        assert paths.index(str(new)) < paths.index(str(old)), \
            "配置档必须排在默认位置**前面**（先到先得 ⇒ 它才压得住那份旧的失效文件）"

    def test_explicit_still_shadows_everything_auto_discovered(self, tmp_path):
        """但**用户显式指定**时，仍然不再追加自动发现的那些（那条语义不许改）。"""
        mine = _mkfile(tmp_path / "mine.txt")
        _mkfile(tmp_path / "KianaVnextPlus" / "cookies.txt")
        _mkfile(tmp_path / "KianaVnextPlus" / "profiles" / "bilibili" / "cookies.txt")
        rows = resolve_cookie_sources(str(mine))
        assert [r["path"] for r in rows] == [str(mine)]

    def test_missing_file_is_reported_as_missing_not_dropped(self, tmp_path):
        """**核心诚实性**：配了但文件不存在，必须**列出来并标明不存在**。

        静默丢掉它会表现成"我明明配了，界面却说没配" —— 而真正的原因是
        浏览器下载重名文件时改名成了 'xxx (1).txt'（真机踩过，全套 480P）。
        """
        ghost = str(tmp_path / "nope.txt")
        rows = resolve_cookie_sources(ghost)
        assert len(rows) == 1, "配了但不存在的路径不许被静默丢掉"
        assert rows[0]["path"] == ghost
        assert rows[0]["exists"] is False

    def test_empty_file_counts_as_existing_but_zero_bytes(self, tmp_path):
        p = _mkfile(tmp_path / "empty.txt", "")
        rows = resolve_cookie_sources(str(p))
        assert rows[0]["exists"] is True
        assert rows[0]["size"] == 0

    def test_newline_separated_explicit_value(self, tmp_path):
        """引擎认换行分隔（`parse_cookie_file_list`），界面也必须认。"""
        a, b = _mkfile(tmp_path / "a.txt"), _mkfile(tmp_path / "b.txt")
        rows = resolve_cookie_sources(f"{a}\n{b}")
        assert [r["path"] for r in rows] == [str(a), str(b)]


class TestResolveMatchesEngineEntryPoint:
    """**同一实现**的硬断言：界面读的清单 == 引擎读的清单。

    这是本轮最重要的结构保证 —— 本工程最稳定的缺陷模式就是"同一能力两份实现"
    （`cookie_health._cookie_files()` 与 `universal_downloader._cookie_sources()`
    曾经逐字重复过一遍，改一处漏一处就静默分叉）。

    ⚠️ 比法要**同输入**：`cookie_source_files()` 从**环境变量**取显式值，
    而界面把**输入框的文本**当参数传进来（框里的值还没落进 env）。
    所以这里先把同样的值写进 env，再比两边给出的路径列表 ——
    否则比的是"两个不同的输入"，不是"两份实现是否一致"。
    """

    def _assert_same(self, monkeypatch, explicit=None):
        if explicit is not None:
            monkeypatch.setenv("KIANA_COOKIE_FILES", explicit)
        assert ([r["path"] for r in resolve_cookie_sources(explicit)]
                == cookie_source_files())

    def test_same_when_nothing_configured(self, tmp_path, monkeypatch):
        _mkfile(tmp_path / "KianaVnextPlus" / "profiles" / "tieba" / "cookies.txt")
        self._assert_same(monkeypatch)

    def test_same_with_explicit(self, tmp_path, monkeypatch):
        a, b = _mkfile(tmp_path / "a.txt"), _mkfile(tmp_path / "b.txt")
        self._assert_same(monkeypatch, f"{a};{b}")

    def test_same_with_default_location(self, tmp_path, monkeypatch):
        _mkfile(Path(default_cookie_file()))
        self._assert_same(monkeypatch)

    def test_same_with_missing_file(self, tmp_path, monkeypatch):
        self._assert_same(monkeypatch, str(tmp_path / "ghost.txt"))


# ══════════════════════════════════════════════════════════════════════
# 2. 增 / 删 的语义
# ══════════════════════════════════════════════════════════════════════
class TestAddFront:
    """新加进来（或被「设为最优先」）的一定排**最前** ——
    引擎合并是先到先得，排后面就盖不住旧凭据。"""

    def test_empty_box(self):
        assert cookie_paths_add_front("", r"C:\new.txt") == r"C:\new.txt"

    def test_new_goes_first(self):
        got = cookie_paths_add_front(r"C:\old.txt", r"C:\new.txt")
        assert got.split(";") == [r"C:\new.txt", r"C:\old.txt"]

    def test_already_present_is_MOVED_to_front_not_ignored(self):
        """**回归（离屏测试抓到的真 bug）**：原来"已在列表里"就原样返回，
        于是「设为最优先」按钮对已在列表里的项**完全无效**，
        而界面还显示"已经在最前面了"（假话）—— 典型的"按钮没用"。
        """
        got = cookie_paths_add_front(r"C:\a.txt;C:\b.txt", r"C:\b.txt")
        assert got.split(";") == [r"C:\b.txt", r"C:\a.txt"], "没有把它移到最前"

    def test_move_keeps_everything_else_and_does_not_duplicate(self):
        got = cookie_paths_add_front(r"C:\a.txt;C:\b.txt;C:\c.txt", r"C:\b.txt")
        assert got.split(";") == [r"C:\b.txt", r"C:\a.txt", r"C:\c.txt"]

    def test_idempotent_when_already_first(self):
        """**这一种**才是真幂等：已在最前 → 原样返回，绝不产生重复项。"""
        cur = r"C:\new.txt;C:\old.txt"
        assert cookie_paths_add_front(cur, r"C:\new.txt") == cur
        # 换行分隔（引擎也认）同样算"已包含"：位置不变、也不重复
        cur2 = "C:\\new.txt\nC:\\old.txt"
        assert cookie_paths_add_front(cur2, "C:\\new.txt") == cur2

    def test_blank_new_keeps_current(self):
        assert cookie_paths_add_front(r"C:\a.txt", "") == r"C:\a.txt"

    def test_three_files_stay_in_order(self):
        got = cookie_paths_add_front(r"C:\a.txt;C:\b.txt", r"C:\c.txt")
        assert got.split(";") == [r"C:\c.txt", r"C:\a.txt", r"C:\b.txt"]


class TestDrop:
    def test_drop_first(self):
        assert cookie_paths_drop(r"C:\a.txt;C:\b.txt", r"C:\a.txt") == r"C:\b.txt"

    def test_drop_middle_keeps_others(self):
        got = cookie_paths_drop(r"C:\a.txt;C:\b.txt;C:\c.txt", r"C:\b.txt")
        assert got.split(";") == [r"C:\a.txt", r"C:\c.txt"]

    def test_drop_last_leaves_empty_string(self):
        """删到一个不剩 = 回到"什么都不配" → 引擎恢复自动兜底（默认位置 → 配置档）。"""
        assert cookie_paths_drop(r"C:\only.txt", r"C:\only.txt") == ""

    def test_drop_unknown_path_is_a_no_op(self):
        """**不猜、不做模糊匹配**：只有原样的路径才算命中。

        "看着像就删"会删掉用户另一份文件，而他不会立刻发现
        （引擎会静默降级到更少 cookies —— 又一种"看起来正常"）。
        """
        cur = r"C:\a.txt"
        assert cookie_paths_drop(cur, r"C:\a (1).txt") == cur
        assert cookie_paths_drop(cur, "a.txt") == cur
        assert cookie_paths_drop(cur, "") == cur

    def test_newline_separated_value(self):
        got = cookie_paths_drop("C:\\a.txt\nC:\\b.txt", "C:\\a.txt")
        assert got == "C:\\b.txt"

    def test_add_then_drop_round_trips(self):
        cur = r"C:\a.txt;C:\b.txt"
        back = cookie_paths_drop(cookie_paths_add_front(cur, r"C:\c.txt"), r"C:\c.txt")
        assert back == cur


# ══════════════════════════════════════════════════════════════════════
# 3. 界面文案（纯函数）：不许撒谎、不许静默
# ══════════════════════════════════════════════════════════════════════
@_needs_gui
class TestManagerText:
    def _rows(self, tmp_path, **kw):
        return gui.cookie_source_rows(kw.get("box", ""))

    def test_empty_list_says_what_will_actually_happen(self):
        """空列表**不是**"什么都没有"就完了 —— 要说清引擎会按无 cookies 跑，
        并给出**默认位置在哪**（否则用户不知道该把文件放哪儿才会被自动发现）。"""
        text = gui._cookies_manager_text([])
        assert "先出现的赢" in text or "先到先得" in text
        assert "没有解析到任何 cookies 来源" in text

    def test_missing_file_is_flagged_in_the_list(self, tmp_path):
        ghost = str(tmp_path / "ghost.txt")
        note = gui._cookies_manager_note(gui.cookie_source_rows(ghost))
        assert "文件不存在" in note
        assert ghost in note, "点名的路径必须能看见（用户要照着去改）"

    def test_empty_file_is_flagged_separately_from_missing(self, tmp_path):
        empty = _mkfile(tmp_path / "empty.txt", "")
        note = gui._cookies_manager_note(gui.cookie_source_rows(str(empty)))
        assert "空文件" in note
        assert "文件不存在" not in note, "空文件与不存在是两回事，不许念成同一句"

    def test_auto_discovered_sources_are_labelled_as_such(self, tmp_path):
        _mkfile(tmp_path / "KianaVnextPlus" / "profiles" / "bilibili" / "cookies.txt")
        rows = gui.cookie_source_rows("")
        note = gui._cookies_manager_note(rows)
        assert "自动发现" in note
        assert "profiles" in gui._cookies_manager_text(rows)

    def test_list_shows_origin_and_order_number(self, tmp_path):
        a = _mkfile(tmp_path / "a.txt")
        b = _mkfile(tmp_path / "b.txt")
        text = gui._cookies_manager_text(gui.cookie_source_rows(f"{a};{b}"))
        assert "先出现的赢" in text, "顺序语义要写在列表头上（原先任何界面都看不到）"
        assert text.count("显式指定") == 2
        assert "1." in text and "2." in text

    def test_list_never_shows_cookie_values_only_paths(self, tmp_path):
        """列表里**只许出现路径**，绝不出现任何 cookie 值（路径不是凭据，值才是）。"""
        p = _mkfile(tmp_path / "c.txt", "# Netscape HTTP Cookie File\n")
        text = gui._cookies_manager_text(gui.cookie_source_rows(str(p)))
        assert str(p) in text.replace(" … ", "") or Path(p).name in text
        assert "SESSDATA" not in text


@_needs_gui
class TestHealthTextHonesty:
    """有效性检查的措辞 —— 三态与"不许归因到某一行"是这一层的全部意义。"""

    def test_per_source_lines_only_state_file_level_facts(self, tmp_path):
        a = _mkfile(tmp_path / "a.txt")
        ghost = str(tmp_path / "ghost.txt")
        rows = gui.cookie_source_rows(f"{a};{ghost}")
        text = gui._cookies_health_text(rows)
        assert "文件不存在" in text
        assert "1 ✅" not in text, "别把'文件在'写成登录态结论"

    def test_aggregate_section_admits_it_cannot_be_attributed(self, tmp_path):
        """**核心**：引擎是**合并读**多份文件的，所以"B站的 SESSDATA 在哪一份里"
        单个文件回答不了。逐项写"✅ 有登录凭据"就是谎报 ⇒ 必须显式标成聚合判断。"""
        p = _mkfile(tmp_path / "a.txt")
        text = gui._cookies_health_text(gui.cookie_source_rows(str(p)))
        assert "聚合" in text
        assert "无法归因到某一行" in text

    def test_unregistered_site_says_unable_to_determine(self, tmp_path):
        """登记表没登记登录凭据名的站点（快手/公众号）→ 说"无法判定"，**不猜**。"""
        p = _mkfile(tmp_path / "a.txt")
        text = gui._cookies_health_text(gui.cookie_source_rows(str(p)))
        assert "无法判定" in text
        assert "不猜" in text

    def test_privacy_line_is_appended_verbatim(self, tmp_path):
        p = _mkfile(tmp_path / "a.txt")
        text = gui._cookies_health_text(gui.cookie_source_rows(str(p)),
                                        "隐私状态: 🔐 密钥DPAPI加密 · Cookies: B站✅")
        assert "隐私状态: 🔐 密钥DPAPI加密" in text

    def test_no_sources_is_not_reported_as_all_good(self):
        text = gui._cookies_health_text([], "")
        assert "没有来源可查" in text
        assert "✅" not in text.split("聚合")[0].replace("没有来源可查", "")


# ══════════════════════════════════════════════════════════════════════
# 4. 合并产物过滤：界面不许把"引擎读不到的东西"列成生效来源
# ══════════════════════════════════════════════════════════════════════
@_needs_gui
class TestMergeArtifactIsNotListedAsActive:
    """`tools/cookie_login.py --merge` 写出的 `profiles/cookies.all.txt` 是**派生物**。

    `profiles_cookie_files()` 只扫 `profiles/*/cookies.txt`（一级子目录）
    ⇒ 引擎**事实上读不到**它。把它列进"生效来源"就是谎报。
    """

    def test_merge_artifact_under_profiles_is_excluded_when_auto_discovering(self, tmp_path):
        base = tmp_path / "KianaVnextPlus" / "profiles"
        _mkfile(base / "cookies.all.txt")
        _mkfile(base / "bilibili" / "cookies.txt")
        rows = gui.cookie_source_rows("")
        paths = [r["path"] for r in rows]
        assert all("cookies.all.txt" not in p for p in paths), \
            "聚合派生物被列成了生效来源（引擎读不到它）"
        assert any("bilibili" in p for p in paths), "真正的配置档来源不该被连带滤掉"

    def test_explicitly_chosen_merge_artifact_is_still_honoured(self, tmp_path):
        """但用户**显式**把它填进框里时，引擎是真会读的 ⇒ 必须照常列出。

        过滤只针对"自动发现"那条路（那才是引擎读不到的情形）。
        显式路径下把它滤掉，就会变成"我明明配了、界面说没有" —— 反向撒谎。
        """
        p = _mkfile(tmp_path / "KianaVnextPlus" / "profiles" / "cookies.all.txt")
        rows = gui.cookie_source_rows(str(p))
        assert [r["path"] for r in rows] == [str(p)]

    def test_profiles_cookie_files_ignores_derived_file(self, tmp_path):
        """把上面那条判断的**根据**钉在引擎实现上（不是我在界面里猜的规则）。"""
        base = tmp_path / "KianaVnextPlus" / "profiles"
        _mkfile(base / "cookies.all.txt")
        assert profiles_cookie_files() == []


# ══════════════════════════════════════════════════════════════════════
# 5. "先到先得"的引擎事实：合并顺序真的决定谁赢
# ══════════════════════════════════════════════════════════════════════
class TestFirstWinsIsReal:
    """界面反复告诉用户"排前面的压得住后面的"——那条话必须有引擎实现兜着。"""

    def test_first_file_wins_on_same_domain_and_name(self, tmp_path, monkeypatch):
        from kiana_vnext_plus import universal_downloader as ud
        from kiana_vnext_plus.cookie_utils import write_netscape_cookies

        old = tmp_path / "old.txt"
        new = tmp_path / "new.txt"
        write_netscape_cookies(
            [{"name": "SESSDATA", "value": "OLD", "domain": ".bilibili.com",
              "path": "/", "secure": True, "http_only": True}], path=str(old))
        write_netscape_cookies(
            [{"name": "SESSDATA", "value": "NEW", "domain": ".bilibili.com",
              "path": "/", "secure": True, "http_only": True}], path=str(new))

        monkeypatch.setenv("TEMP", str(tmp_path))
        monkeypatch.setattr(ud, "_COOKIE_FILE", None, raising=False)
        monkeypatch.setattr(ud, "_COOKIE_SRC_DIR", None, raising=False)

        monkeypatch.setenv("KIANA_COOKIE_FILES", f"{new};{old}")
        first_new = ud._ensure_cookie_file()
        assert first_new is not None
        assert "NEW" in first_new.read_text(encoding="utf-8", errors="ignore")

        # 反过来放 → 赢家也反过来（证明真的是"先到先得"，不是"后到覆盖"）
        monkeypatch.setattr(ud, "_COOKIE_FILE", None, raising=False)
        monkeypatch.setattr(ud, "_COOKIE_SRC_DIR", None, raising=False)
        monkeypatch.setenv("KIANA_COOKIE_FILES", f"{old};{new}")
        first_old = ud._ensure_cookie_file()
        assert first_old is not None
        assert "OLD" in first_old.read_text(encoding="utf-8", errors="ignore")

    def test_httponly_prefix_survives_the_merge(self, tmp_path, monkeypatch):
        """合并这一步**必须**把 `#HttpOnly_` 带回去：B站的 SESSDATA 正是 HttpOnly，
        丢了前缀 = 严格解析器整行丢掉 = 登录态静默消失（工程为此吃过 code=-101）。"""
        from kiana_vnext_plus import universal_downloader as ud
        from kiana_vnext_plus.cookie_utils import write_netscape_cookies

        p = tmp_path / "bili.txt"
        write_netscape_cookies(
            [{"name": "SESSDATA", "value": "V", "domain": ".bilibili.com",
              "path": "/", "secure": True, "http_only": True}], path=str(p))
        monkeypatch.setenv("TEMP", str(tmp_path))
        monkeypatch.setattr(ud, "_COOKIE_FILE", None, raising=False)
        monkeypatch.setattr(ud, "_COOKIE_SRC_DIR", None, raising=False)
        monkeypatch.setenv("KIANA_COOKIE_FILES", str(p))
        merged = ud._ensure_cookie_file()
        assert merged is not None
        assert "#HttpOnly_" in merged.read_text(encoding="utf-8", errors="ignore")


# ══════════════════════════════════════════════════════════════════════
# 6. 纯逻辑：网址 → 站点键 / 网址闸（与引擎那道闸同源）
# ══════════════════════════════════════════════════════════════════════
@_needs_gui
class TestLoginTargetHelpers:
    def test_url_gate_matches_the_engine_gate(self):
        """判据必须与 `cookie_profile.open_profile_for_login` 里那道闸一致
        （只认 https + 本机回环 http）。这里逐条对齐，防止界面闸与引擎闸分叉。"""
        assert gui.login_url_usable("")[0] is True          # 留空 = 用默认登录页
        assert gui.login_url_usable("https://passport.bilibili.com/login")[0] is True
        assert gui.login_url_usable("http://127.0.0.1/x")[0] is True
        assert gui.login_url_usable("http://localhost:8080/x")[0] is True
        assert gui.login_url_usable("http://example.com/")[0] is False
        assert gui.login_url_usable("ftp://example.com/")[0] is False
        assert gui.login_url_usable("javascript:alert(1)")[0] is False
        # 拒绝时要给出**可读原因**（引擎那道闸只会回 bad_url，用户看不懂）
        assert "https" in gui.login_url_usable("http://example.com/")[1]

    def test_site_key_from_url_never_guesses(self):
        assert gui.site_key_from_login_url("https://space.bilibili.com/123") == "bilibili.com"
        assert gui.site_key_from_login_url("https://v.douyin.com/abc") == "douyin.com"
        assert gui.site_key_from_login_url("") == ""
        assert gui.site_key_from_login_url("https://unknown-host.example/") == ""

    def test_origin_tag_table_comes_from_the_engine_package(self):
        """来源标注的文字只有一份（引擎包里那张表），界面不许自己再编一套。"""
        from kiana_vnext_plus.cookie_utils import SOURCE_ORIGIN_LABEL
        for k, v in SOURCE_ORIGIN_LABEL.items():
            assert gui._ORIGIN_TAG[k] == v


# ══════════════════════════════════════════════════════════════════════
# 7. 离屏构造：接线真的接上了（**不 show()、不弹窗**）
# ══════════════════════════════════════════════════════════════════════
@pytest.mark.skipif(gui is None, reason="launcher_v9 不可导入")
class TestOffscreenWiring:
    """构造真窗口（offscreen），把六跳接线**逐跳点验**一遍。

    为什么值得真构造一次：AST 断言只能证明"代码里写了"，证明不了
    "属性真的存在、点下去真的不炸"。离屏构造（`QT_QPA_PLATFORM=offscreen`，
    不 `show()`）两者都能覆盖，而且一个像素都不会出现在机主屏幕上。
    """

    @pytest.fixture
    def win(self, tmp_path, monkeypatch):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        monkeypatch.delenv("KIANA_COOKIE_FILES", raising=False)
        monkeypatch.delenv("KIANA_COOKIE_FILE", raising=False)
        # ⚠️ **[v2.19.9 修复的真实事故]** 必须在 `KianaV9()` **之前**把启动器配置
        # 路径钉到 tmp —— 上面那句 `setenv("LOCALAPPDATA", tmp_path)` 对它**无效**。
        #
        # 为什么：`launcher_v8.py:45  CONFIG_FILE = _launcher_config_path()` 是
        # **导入期**求值的模块常量，import 那一刻就把机主真实的
        # `%LOCALAPPDATA%\KianaVnextPlus\launcher_config.json` 定死了。
        # 本 fixture 后面每一次 `win.home.cookie_edit.setText(...)`（→ `_on_cookie_home`
        # → `save_config`）都会写那份真配置 —— 本文件确实这么干过：机主的
        # `cookie_file` 被抹成 `""`，刚登录好的 B站 cookies 路径没了。
        #
        # 为什么**两个模块都要打**：`launcher_v9.py:86` 是
        # `from launcher_v8 import CONFIG_FILE, ...` —— import 搬的是值，
        # `gui.CONFIG_FILE` 是**另一个**名字绑定，只打 launcher_v8 那个，
        # v9 的 `save_config()` 照旧写真实路径（实测确认）。
        fake_cfg = tmp_path / "launcher_config.json"
        for _mod in (sys.modules.get("launcher_v8"), gui):
            if _mod is not None:
                monkeypatch.setattr(_mod, "CONFIG_FILE", fake_cfg, raising=False)
        try:
            from PySide6.QtWidgets import QApplication
        except Exception as e:                              # pragma: no cover
            pytest.skip(f"PySide6 不可用：{e}")
        # 跑之前先证明隔离**真的**生效：两个模块都必须指向 tmp，
        # 否则"以为隔离了"就是下次事故的起点。
        assert gui.CONFIG_FILE == fake_cfg, f"launcher_v9.CONFIG_FILE 没被打桩：{gui.CONFIG_FILE}"
        _v8 = sys.modules.get("launcher_v8")
        if _v8 is not None:
            assert _v8.CONFIG_FILE == fake_cfg, f"launcher_v8.CONFIG_FILE 没被打桩：{_v8.CONFIG_FILE}"
        assert not fake_cfg.exists() or fake_cfg.parent == tmp_path
        app = QApplication.instance() or QApplication([])
        w = gui.KianaV9()
        yield w
        w.close()
        app.processEvents()

    def test_home_no_longer_has_login_widgets(self, win):
        for gone in ("login_btn", "login_site_combo", "login_status", "login_sites"):
            assert not hasattr(win.home, gone), f"首页还留着 {gone} —— 搬走了就该撤干净"

    def test_settings_has_the_manager(self, win):
        st = win.settings
        for need in ("login_btn", "login_site_combo", "login_status", "login_sites",
                     "login_url_edit", "cookie_sources_view", "cookie_manager_status"):
            assert hasattr(st, need), f"设置页缺控件 {need}"

    def test_mirror_is_read_only_and_follows_the_box(self, win, tmp_path):
        """设置页那格是**只读镜像**：权威值只有首页 Cookies 框一个。

        [v7 决策] 同一份配置两个**可写**入口，早晚"这边改了那边没改"。
        这里**两个方向都测**：
          · 首页框变 → 镜像跟着变（同步有效）；
          · 程序性往镜像里写 → **不许**反向改配置（那是原来的反向连线，
            只读控件被 setText 时 Qt 照样发 textChanged，所以必须断掉）。
        """
        assert win.settings.cookie_edit_d.isReadOnly()
        f = _mkfile(tmp_path / "x.txt")
        win.home.cookie_edit.setText(str(f))
        assert win.settings.cookie_edit_d.text() == str(f)
        assert win.config.get("cookie_file") == str(f)
        # 反向：往镜像里写**不该**改权威值
        win.settings.cookie_edit_d.setText("SHOULD_NOT_LEAK")
        assert win.config.get("cookie_file") == str(f), \
            "只读镜像反向改了配置 —— 一份配置两个方向同时写"
        assert win.home.cookie_edit.text() == str(f)

    def test_manager_lists_every_source_with_its_origin(self, win, tmp_path):
        a, b = _mkfile(tmp_path / "a.txt"), _mkfile(tmp_path / "b.txt")
        win.home.cookie_edit.setText(f"{a};{b}")
        text = win.settings.cookie_sources_view.toPlainText()
        assert str(a) in text.replace(" … ", "") or "a.txt" in text
        assert "b.txt" in text
        assert "显式指定" in text
        assert win._cookie_box_value() == f"{a};{b}"

    def test_first_row_is_selectable(self, win, tmp_path):
        """**回归**：列表第 1 项曾经永远选不中（忘了减表头那一行）。

        症状是"选中了、点「移除选中」却没反应"，且**最后一行越界**。
        用单项列表测最灵：偏移错 1 时它直接落空。
        """
        a = _mkfile(tmp_path / "only.txt")
        win.home.cookie_edit.setText(str(a))
        _select_source(win, 0)
        assert str(win._selected_cookie_source().get("path") or "") == str(a), \
            "第 1 项选不中（表头那一行的偏移没减）"

    def test_selection_out_of_range_returns_nothing(self, win, tmp_path):
        """光标落在表头行/文末时**必须取不到任何项** —— 调用方据此什么都不做。"""
        a = _mkfile(tmp_path / "only.txt")
        win.home.cookie_edit.setText(str(a))
        from PySide6.QtGui import QTextCursor
        cur = win.settings.cookie_sources_view.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.Start)      # 表头行
        win.settings.cookie_sources_view.setTextCursor(cur)
        assert win._selected_cookie_source() == {}
        win._remove_selected_cookie_source()
        assert win._cookie_box_value() == str(a), "没选中却动了配置"

    def test_add_file_prepends_and_removal_keeps_the_file(self, win, tmp_path, monkeypatch):
        a = _mkfile(tmp_path / "a.txt")
        b = _mkfile(tmp_path / "b.txt")
        win.home.cookie_edit.setText(str(a))
        monkeypatch.setattr(win.settings, "_pick_cookie", lambda: str(b))
        win._add_cookie_file()
        # 「添加文件…」进来的排**最前**（先到先得：排后面就盖不住同名旧凭据）
        assert win._cookie_box_value().split(";") == [str(b), str(a)]

        # 选中第 2 项（= a，索引 1）后移除 → 只动配置，**磁盘文件必须还在**
        _select_source(win, 1)
        assert Path(str(win._selected_cookie_source()["path"])) == a, "选中的不是第 2 项？"
        win._remove_selected_cookie_source()
        assert win._cookie_box_value() == str(b), "移除的应当只是选中的那一项"
        assert a.exists(), "「移除选中」把用户的文件删了 —— 这是不可逆的破坏"
        assert b.exists()

    def test_promote_moves_the_selected_source_to_the_front(self, win, tmp_path):
        a = _mkfile(tmp_path / "a.txt")
        b = _mkfile(tmp_path / "b.txt")
        win.home.cookie_edit.setText(f"{a};{b}")     # 顺序 a → b
        _select_source(win, 1)                        # 第 2 项 = b
        assert Path(str(win._selected_cookie_source()["path"])) == b
        win._promote_selected_cookie_source()
        assert win._cookie_box_value().split(";") == [str(b), str(a)], "没提到最前"
        # 已经在最前 → 幂等，不许把顺序搅乱
        win._promote_selected_cookie_source()
        assert win._cookie_box_value().split(";") == [str(b), str(a)]

    def test_removing_an_auto_discovered_source_is_refused_honestly(self, win, tmp_path):
        """[v7] 自动发现的来源（默认位置 / 配置档兜底）**不在输入框里**，删不掉。

        这时必须**如实说明并给出可行做法**，而不是装作删掉了
        （装作删掉 = 用户以为不再读它了，而引擎下次照样读）。
        """
        _mkfile(tmp_path / "KianaVnextPlus" / "profiles" / "bilibili" / "cookies.txt")
        win.home.cookie_edit.setText("")
        _select_source(win, 0)
        assert win._selected_cookie_source()["origin"] == "profile"
        win._remove_selected_cookie_source()
        msg = win.settings.cookie_manager_status.text()
        assert "自动发现" in msg and "无法单独移除" in msg
        assert "不会" in msg, "要说清本程序不会替你删文件"

    def test_clear_box_falls_back_to_auto_discovery(self, win, tmp_path):
        _mkfile(tmp_path / "KianaVnextPlus" / "profiles" / "bilibili" / "cookies.txt")
        win.home.cookie_edit.setText(str(tmp_path / "whatever.txt"))
        win._clear_cookie_box()
        assert win._cookie_box_value() == ""
        rows = win._cookie_source_rows()
        assert rows and rows[0]["origin"] == "profile", \
            "清空之后应当回落到配置档兜底（那正是'用户什么都没配'时的语义）"

    def test_health_check_is_skipped_while_the_engine_runs(self, win, monkeypatch):
        """爬取期间必须跳过核验：它要临时改进程级 `KIANA_COOKIE_FILES`，
        而 `EngineBridge.start()` 也在写它 —— 并发写同一全局变量会互相盖
        （"检查完 cookies 反而把本次爬取要用的抹了"）。"""
        monkeypatch.setattr(win, "_engine_running", lambda: True)
        win._check_cookie_sources_async()
        assert "跳过" in win.settings.cookie_manager_status.text()
        assert not getattr(win, "_cookie_check_busy", False), "跳过了就不该置忙标志"

    def test_no_window_is_shown(self, win):
        """这条测试本身**不该**让窗口出现在屏幕上（离屏 + 不 show）。"""
        assert not win.isVisible()
