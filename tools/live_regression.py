# -*- coding: utf-8 -*-
"""真机回归汇总器 —— 跑一批真实 URL，把"该看的指标"从日志里挖出来

**为什么需要它**：这个工程长期缺少"真机回归"这一层（覆盖率线早就点名过：
`tools/stealth_bench.py` 等探针存在但不进 CI）。而实际排障时，我每次都在**手工**
从 `crawl.log` 里 grep 这几个指标——既慢又容易漏。本工具把它固化下来。

**它回答的问题**（一次跑完给出对照表）：
  1. 多少页成功 / 失败？（失败率是"修复有没有用"最直接的判据）
  2. 引擎的**隐身链自证**报了几次？（`隐身链自证疑点（plugins=0）` —— 反检测是否真的生效）
  3. 有没有**拼错主机**的链接？（短链种子的经典症状：`b23.tv/video/BV...`）
  4. 碰到几次**验证码挑战**？（`geetest` 等）
  5. 有没有 SSRF 拦截、限流、崩溃？

**它不做的事**：不做断言、不进 CI（真机结果受网络/风控影响，本来就不该当门禁）。
它是**给人看的诊断工具**，判据由使用者结合上下文决定。

用法：
    python tools/live_regression.py --urls 链接文件.txt
    python tools/live_regression.py --url "https://b23.tv/xxx" -m 5
    python tools/live_regression.py --log <已有的 crawl.log>   # 只分析，不跑
"""
import argparse
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 每项指标： (显示名, 正则, 说明)
#
# ⚠️ **正则必须锚定日志级别**。首版我写成裸关键词（`隐身链自证疑点`），结果把
# **解释性文字里提到这个词**的行也数进去了——修复后新增的那条 ERROR
# （"…已自动改用 playwright…（真机表现：「隐身链自证疑点（plugins=0）」）"）
# 里就含这个词，于是**修好了反而报 1 次**。这正是本工程反复踩的
# "拿文本当结构"：要数的是**事件**，不是**词的出现**。
_METRICS = (
    ("隐身链自证疑点", re.compile(r"\[WARNING\][^\n]*隐身链自证疑点"), "反检测未生效的告警（期望 0）"),
    ("B站风控壳页", re.compile(r"\[WARNING\][^\n]*B站风控壳页"), "页面无视频数据（IP 限流）"),
    ("验证码挑战", re.compile(r"\[INFO\][^\n]*检测到挑战"), "被弹验证码"),
    ("GeeTest 失败", re.compile(r"\[WARNING\][^\n]*(?:GeeTest slider not found|求解失败)"), "滑块没找到"),
    ("SSRF 拦截", re.compile(r"\[WARNING\][^\n]*SSRF 拦截"), "安全闸生效（正常，非错误）"),
    ("限流命中", re.compile(r"\[WARNING\][^\n]*(?:429|Too Many Requests|THROTTLED)"), "被限流"),
    ("重试降级", re.compile(r"\[WARNING\][^\n]*后降级重试"), "走了格式降级链"),
    ("下载失败", re.compile(r"\[(?:WARNING|ERROR)\][^\n]*(?:Video download failed|下载失败)"), "媒体下载失败"),
)


def _find_log(root: Path) -> Path:
    """在产出目录里找引擎自己的 crawl.log（它比 stdout 全）"""
    hits = sorted(root.rglob("crawl.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return hits[0] if hits else None


def _summarize(log_path: Path) -> dict:
    text = log_path.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()
    out = {"log": str(log_path), "lines": len(lines)}

    # ① 成功页数：`Saved: ...json`
    out["已保存页面"] = len(re.findall(r"page_processor: Saved:", text))

    # ② 各指标计数
    for label, pat, _desc in _METRICS:
        out[label] = len(pat.findall(text))

    # ③ 拼错主机的链接：短链主机 + 路径（经典症状）
    #    b23.tv 是个短链服务，它下面不该有 /video/ 这种路径
    bad = set(re.findall(r"https?://b23\.tv/(?:video|space|game)/[^\s\"'<>)]*", text))
    out["拼错主机链接(去重)"] = len(bad)
    out["_bad_examples"] = sorted(bad)[:3]

    # ④ 正常主机出现次数（对照）
    out["www.bilibili.com 出现"] = len(re.findall(r"www\.bilibili\.com", text))
    return out


def _run_crawl(urls, outdir: Path, max_pages: int, cookie_file: str, extra):
    cmd = [sys.executable, str(ROOT / "run_crawler.py"), *urls,
           "-m", str(max_pages), "-o", str(outdir), "--no-video", "--no-image"]
    if cookie_file:
        cmd += ["--cookie-file", cookie_file]
    cmd += list(extra or [])
    print(f"[跑] {' '.join(cmd[:6])} …（{len(urls)} 个种子）")
    t0 = time.monotonic()
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                          encoding="utf-8", errors="ignore")
    dt = time.monotonic() - t0
    print(f"[完] {dt:.0f} 秒，退出码 {proc.returncode}")
    tail = (proc.stdout or "").strip().splitlines()[-6:]
    for line in tail:
        print("     " + line.strip())
    return proc


def main():
    ap = argparse.ArgumentParser(description="真机回归汇总（跑一批 URL 并把指标挖出来）")
    ap.add_argument("--url", action="append", default=[], help="单个 URL，可重复")
    ap.add_argument("--urls", help="URL 文件（一行一个，忽略空行与 # 注释）")
    ap.add_argument("--log", help="只分析已有的 crawl.log，不跑爬取")
    ap.add_argument("-m", "--max-pages", type=int, default=5)
    ap.add_argument("--cookie-file", default="", help="cookies.txt（留在仓库外）")
    ap.add_argument("--outdir", default="", help="产出目录（默认在系统临时目录下随机建一个）")
    ap.add_argument("--extra", nargs=argparse.REMAINDER, help="透传给 run_crawler 的其它参数")
    args = ap.parse_args()

    if args.log:
        log = Path(args.log)
        if not log.exists():
            print(f"找不到日志: {log}")
            return 2
    else:
        urls = list(args.url)
        if args.urls:
            for line in Path(args.urls).read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    urls.append(line)
        if not urls:
            print("没给 URL（--url / --urls），也没给 --log")
            return 2
        outdir = Path(args.outdir) if args.outdir else Path(
            tempfile.mkdtemp(prefix="kiana-live-"))
        outdir.mkdir(parents=True, exist_ok=True)
        _run_crawl(urls, outdir, args.max_pages, args.cookie_file, args.extra)
        log = _find_log(outdir)
        if not log:
            print("跑完了但找不到 crawl.log——无法汇总")
            return 1

    s = _summarize(log)
    print("\n" + "═" * 62)
    print(f"  真机回归汇总   日志: {s['log']}")
    print("═" * 62)
    print(f"  已保存页面               {s['已保存页面']}")
    for label, _pat, desc in _METRICS:
        flag = "  ← 期望 0" if label == "隐身链自证疑点" else ""
        print(f"  {label:<22} {s[label]:<4} {desc}{flag}")
    print(f"  {'拼错主机链接(去重)':<22} {s['拼错主机链接(去重)']:<4} 短链主机下不该有 /video/ 这类路径")
    for ex in s["_bad_examples"]:
        print(f"      · {ex[:88]}")
    print(f"  {'www.bilibili.com 出现':<22} {s['www.bilibili.com 出现']:<4} 正常主机的对照量")
    print("═" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
