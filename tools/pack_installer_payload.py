"""把两个 PyInstaller onedir 合成**一份**安装载荷（路径并集，同路径取后者）。

为什么要有这一步
================
`dist\\KianaLauncher` 与 `dist\\KianaCrawler` 各自是一整套 PyInstaller onedir。
2026-10-23 实测（`tools/pack_installer_payload.py --check`）：

    launcher 12,859 文件 / 1.804 GiB
    crawler  15,612 文件 / 2.095 GiB
    naive    3.898 GiB      ← 两棵树各自压进安装包
    并集     2.125 GiB      ← 实际需要

其中 12,847 个同名文件逐字节相同，白压 1.774 GiB（45.5%）。

而 `kiana_setup.nsi` 本来就是**依次铺进同一个 `$INSTDIR`**（后者覆盖前者，
见该文件 `[v2.18 P2-11]` 段注释）⇒ 最终落盘状态**就是并集**。于是安装包里
那 1.774 GiB 纯属浪费，并且正好把 NSIS 顶到 2 GB 硬上限：

    Internal compiler error #12345: error mmapping file (1950187200, 33554432)
    is out of range.

2026-10-23 实测：32 位 makensis 3.12 与 64 位 makensis（PE machine=0x8664）
**在同一字节数报同一句错**（后者 161.4 秒后照旧失败，峰值内存 3 MB）⇒
撞的是 NSIS 自身的格式上限，不是编译器的地址空间。**换 64 位编译器无用。**

本工具把"并集"在编译前做好，用**硬链接**落地（同卷零额外占用），
安装包只压一份；落盘的文件集合与内容与改造前**逐字节相同**。

语义对齐（关键）
----------------
NSIS 的 `File /r` 对同路径文件是**后者覆盖前者**。所以这里也按"路径"归并、
**后出现的目录优先**，不做内容比较——同名同内容的文件因为共享同一个相对路径
而自然合并，不需要哈希。（只有在 `--check` 里才统计"同名不同大小"用于告警。）

用法
----
    python tools/pack_installer_payload.py              # 生成 dist\\_pack
    python tools/pack_installer_payload.py --check      # 只统计，不写盘
    python tools/pack_installer_payload.py --copy       # 硬链接不可用时改用复制

退出码：0 成功；2 参数/前置条件不满足（目录不存在、输出目录越界）。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path
from typing import Iterable, Sequence

# 两个 onedir 的目录名（顺序即 NSIS 里的铺设顺序：后者覆盖前者）
BASE_DIR_NAMES: tuple[str, ...] = ("KianaLauncher", "KianaCrawler")

# 合成后的载荷目录名（相对 dist/）
PACK_DIR_NAME = "_pack"


def plan_union(bases: Sequence[Path], exclude: Sequence[str] = ()) -> dict[str, Path]:
    """把若干目录树按**相对路径**归并成一份计划：``{相对路径: 源文件}``。

    后出现的目录覆盖先出现的（与 NSIS ``File /r`` 的覆盖语义一致）。
    键一律用 posix 风格（``/`` 分隔），这样在测试里可以逐字比较。

    ``exclude`` 是**相对路径前缀**（posix 风格）：命中的条目整支跳过。
    用途见 ``--exclude``：把 ``_internal/browsers/`` 排除后载荷从 2.125 GiB
    降到 1.47 GiB —— 这是唯一能落进 NSIS ``2^31`` 输入上限的档位。
    """
    exclude = tuple(e.strip("/") for e in exclude if e and e.strip("/"))
    plan: dict[str, Path] = {}
    for base in bases:
        base = Path(base)
        for dirpath, _dirnames, filenames in os.walk(base):
            for filename in filenames:
                src = Path(dirpath) / filename
                rel = src.relative_to(base).as_posix()
                if exclude and any(
                    rel == e or rel.startswith(e + "/") for e in exclude
                ):
                    continue
                plan[rel] = src
    return plan


def total_bytes(plan: dict[str, Path]) -> int:
    """计划里所有源文件的大小之和（stat 失败的文件按 0 计，不抛）。"""
    total = 0
    for src in plan.values():
        try:
            total += src.stat().st_size
        except OSError:
            pass
    return total


def naive_bytes(roots: Iterable[Path]) -> int:
    """不做归并时（= 现在安装包要压的量）的字节数。"""
    total = 0
    for root in roots:
        for dirpath, _dirnames, filenames in os.walk(root):
            for filename in filenames:
                try:
                    total += (Path(dirpath) / filename).stat().st_size
                except OSError:
                    pass
    return total


def size_conflicts(bases: Sequence[Path]) -> list[tuple[str, int, int]]:
    """同名但**大小不同**的路径（真正需要人眼看的冲突）。

    只看大小：同名同大小视为可互换（内容差异不影响并集语义，覆盖规则已定）。
    """
    seen: dict[str, dict[Path, int]] = {}
    for base in bases:
        base = Path(base)
        for dirpath, _dirnames, filenames in os.walk(base):
            for filename in filenames:
                src = Path(dirpath) / filename
                rel = src.relative_to(base).as_posix()
                try:
                    size = src.stat().st_size
                except OSError:
                    continue
                seen.setdefault(rel, {})[base] = size
    out: list[tuple[str, int, int]] = []
    for rel, per_base in seen.items():
        sizes = list(per_base.values())
        if len(sizes) > 1 and len(set(sizes)) > 1:
            out.append((rel, sizes[0], sizes[-1]))
    out.sort()
    return out


def materialize(plan: dict[str, Path], out_dir: Path, *, use_hardlink: bool = True) -> int:
    """把计划落成 ``out_dir`` 下的真实目录树，返回写出的文件数。

    ``use_hardlink=True`` 时用硬链接（同卷不占额外空间）；任何一次硬链接失败
    （跨卷、文件系统不支持）就退回 ``copy2``。目标目录**先清空**——但它必须是
    一个专门的载荷目录，调用方负责校验边界（见 :func:`_guard_out_dir`）。
    """
    out_dir = Path(out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    for rel, src in plan.items():
        dst = out_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        linked = False
        if use_hardlink:
            try:
                os.link(src, dst)
                linked = True
            except OSError:
                linked = False
        if not linked:
            shutil.copy2(src, dst)
        written += 1
    return written


def _guard_out_dir(dist_root: Path, out_dir: Path, bases: Sequence[Path]) -> None:
    """输出目录必须落在 dist 之内、且不能是任何一棵源树的根。"""
    dist_res = dist_root.resolve()
    out_res = out_dir.resolve()
    if out_res == dist_res or dist_res not in out_res.parents:
        raise ValueError(f"输出目录必须位于 {dist_res} 之内：{out_res}")
    for base in bases:
        if out_res == Path(base).resolve():
            raise ValueError(f"输出目录不能是源树本身：{out_res}")


def _human(n: int) -> str:
    return f"{n / 2**30:.3f} GiB"


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="把两个 PyInstaller onedir 合成一份安装载荷（路径并集，同路径取后者）",
    )
    ap.add_argument("--dist", default="dist", help="dist 根目录（默认 dist）")
    ap.add_argument(
        "--out",
        default=None,
        help=f"载荷输出目录（默认 <dist>/{PACK_DIR_NAME}）",
    )
    ap.add_argument("--check", action="store_true", help="只打印统计，不写盘")
    ap.add_argument(
        "--copy",
        action="store_true",
        help="不建硬链接、直接复制（硬链接正常情况下零额外占用，不给这个开关也会自动回退）",
    )
    ap.add_argument(
        "--bases",
        default=",".join(BASE_DIR_NAMES),
        help="要归并的 onedir 名字，逗号分隔，按 NSIS 铺设顺序给（默认 launcher,crawler）",
    )
    ap.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="REL",
        help=(
            "整支跳过的相对路径前缀（可重复）；例："
            "--exclude _internal/browsers/ 把随包 Chromium 排除在外"
        ),
    )
    ap.add_argument(
        "--allow-oversize",
        action="store_true",
        help="载荷超过 NSIS 2^31 输入上限时仍然写盘（默认直接失败：编不出来的东西不写）",
    )
    args = ap.parse_args(argv)

    dist_root = Path(args.dist)
    names = [n for n in args.bases.split(",") if n]
    bases = [dist_root / n for n in names]
    missing = [str(b) for b in bases if not b.is_dir()]
    if missing:
        print(f"X 缺少 onedir 目录：{', '.join(missing)}", file=sys.stderr)
        print("  先跑 PyInstaller 打包（[5/7]），或检查 --dist/--bases。", file=sys.stderr)
        return 2

    out_dir = Path(args.out) if args.out else dist_root / PACK_DIR_NAME
    try:
        _guard_out_dir(dist_root, out_dir, bases)
    except ValueError as exc:
        print(f"X {exc}", file=sys.stderr)
        return 2

    plan = plan_union(bases, exclude=args.exclude)
    union = total_bytes(plan)
    naive = naive_bytes(bases)
    conflicts = size_conflicts(bases)

    print(f"归并 {len(bases)} 棵 onedir → {out_dir}")
    for base in bases:
        n = sum(1 for _ in os.walk(base))
        print(f"  {base.name:<16} dirs={n}")
    if args.exclude:
        print(f"  排除前缀：{', '.join(args.exclude)}")
    print(f"  并集 {len(plan)} 文件 / {_human(union)}"
          f"（NSIS 单次输入上限 2.000 GiB = {2**31:,} 字节）")
    if naive > union:
        print(f"  不归并 {_human(naive)} → 省下 {_human(naive - union)}"
              f"（{(naive - union) / naive:.1%}）")
    else:
        print(f"  不归并 {_human(naive)}")
    if union >= 2**31:
        print(f"  !! 载荷 {union:,} 字节 仍超 2^31（{2**31:,}）—— NSIS 会在 "
              f"`error mmapping datablock` 处崩掉；请用 --exclude 再减", file=sys.stderr)
    else:
        print(f"  余量 {_human(2**31 - union)}（{2**31 - union:,} 字节）")
    if conflicts:
        print(f"  ! 同名不同大小 {len(conflicts)} 处（后者覆盖前者）：")
        for rel, s1, s2 in conflicts[:10]:
            print(f"      {rel}  {s1} → {s2}")
    else:
        print("  ! 同名不同大小 0 处")

    if args.check:
        print("--check：未写盘。")
        return 0

    if union >= 2**31 and not args.allow_oversize:
        print(
            f"X 载荷 {union:,} 字节 ≥ NSIS 上限 2^31（{2**31:,}）—— "
            f"makensis 一定会在 `error mmapping datablock` 处崩掉，不写这个包。\n"
            f"  超了 {union - 2**31:,} 字节。请用 --exclude 去掉不需要随包的大件"
            f"（例：--exclude _internal/browsers/），或加 --allow-oversize 强行写出。",
            file=sys.stderr,
        )
        return 1

    written = materialize(plan, out_dir, use_hardlink=not args.copy)
    if written != len(plan):
        print(f"X 写出文件数不符：计划 {len(plan)}，实际 {written}", file=sys.stderr)
        return 1
    landed = naive_bytes([out_dir])
    if landed != union:
        print(f"X 载荷字节数不符：计划 {union}，实际 {landed}", file=sys.stderr)
        return 1
    print(f"  OK 载荷就绪：{written} 文件 / {_human(landed)}（"
          f"{'硬链接' if not args.copy else '复制'}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
