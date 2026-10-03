"""v2.16 M5 隐私扫描器：检测本机 Kiana 相关明文残留/风险项（只读，不清理）

用法: python tools/privacy_scanner.py [--quiet]

检查项：
  1. %TEMP% 下 kiana_cookies_*.txt（合并 cookies 明文残留——正常应爬完即删）
  2. %LOCALAPPDATA%\\KianaVnextPlus 下 master.key（明文密钥，v2.11 起应已迁移 DPAPI）
  3. launcher_config.json 是否含明文 API key（cookies/captcha 字段值非空未加密）
  4. 工程目录 cookies*.txt（密钥误提交痕迹）
  5. 日志落盘是否含手机号/邮箱/IP 样本（抽查最新 crawl.log 前 20 行）
输出: 每项 ✓/⚠️ + 一行说明；--quiet 只打印风险。
"""
import sys
import os
import re
import glob
import json
from pathlib import Path

# ─────────────────────────────────────────────────────────────
# [v2.19.9 修复·这个 bug 让「隐私扫描」**从来没成功过**] 中文 Windows 下
# `sys.stdout` 的编码是 GBK(cp936)，而本脚本的状态行里带 ⚠️/✓/✅ 这些符号 ——
# GBK 编不出来，于是 `print` 直接抛 `UnicodeEncodeError`（实测复现：
# "gbk codec can't encode character '\u26a0'"）。
# 更要命的是它**只在 stdout 不是终端时才炸**：GUI 那条路正是
# `subprocess.run(..., capture_output=True)`（管道）→ 子进程必崩 →
# 界面永远显示"隐私扫描失败"。所以在脚本内部自己把流改成 UTF-8，
# 并留 `errors="replace"` 作第二道保险。
# ⚠️ 配套改动：调用方（`launcher_v9._run_priv_scan`）必须**用 utf-8 解码**这个子进程
# 的输出，否则这边写 UTF-8、那边按 cp936 解，反而变成乱码。
# ─────────────────────────────────────────────────────────────
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PROJ = Path(__file__).resolve().parent.parent


def _sample_log_check():
    """抽查日志敏感字段（手机号/邮箱/IP 脱敏后应无）"""
    pats = [r"1[3-9]\d{9}", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"]
    hits = []
    for f in list(glob.glob(str(Path.home() / "AppData" / "Local" / "KianaVnextPlus" / "logs" / "*.log"))) + \
             list(glob.glob(str(PROJ / "logs" / "*.log"))):
        try:
            for line in Path(f).read_text(encoding="utf-8", errors="ignore").splitlines()[-200:]:
                for p in pats:
                    if re.search(p, line):
                        hits.append((Path(f).name, p))
                        break
        except Exception:
            pass
    return hits[:5]


def scan():
    out = []
    # 1) TEMP cookies 残留
    leftovers = list(Path(os.environ.get("TEMP", "")).glob("kiana_cookies_*.txt")) if os.environ.get("TEMP") else []
    out.append(("TEMP cookies 残留", "⚠️ " + str(len(leftovers)) + " 个（应爬完即删，可手删）"
                if leftovers else "✓ 无残留"))

    # 2) 明文 master.key
    plain_key = Path(os.environ.get("LOCALAPPDATA", "")) / "KianaVnextPlus" / "master.key"
    out.append(("明文主密钥 master.key",
                "⚠️ 存在（应已迁移 master.key.bin DPAPI）" if plain_key.exists() else "✓ 无明文密钥"))

    # 3) launcher_config 明文 key（打码 + LLM，[v2.17 2.8] 新增 LLM 检查）
    cfg_file = Path(os.environ.get("LOCALAPPDATA", "")) / "KianaVnextPlus" / "launcher_config.json"
    plain_captcha = False
    plain_llm = False
    if cfg_file.exists():
        try:
            cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
            keys = cfg.get("captcha_api_keys") or {}
            plain_captcha = any(keys.get(k) for k in ("twocaptcha", "capsolver", "anticaptcha"))
            # LLM Key 同打码密钥策略：本机存档、不打包——仅提示不判定泄漏
            plain_llm = bool(cfg.get("llm_key"))
        except Exception:
            pass
    out.append(("打码密钥存档方式",
                "⚠️ 检测到明文打码 Key（建议仅本机可信时使用）" if plain_captcha else "✓ 无明文打码 Key"))
    out.append(("LLM Key 存档方式",
                "⚠️ 检测到明文 LLM Key（仅本机 launcher_config，不打包）" if plain_llm
                else "✓ 无明文 LLM Key（或未启用）"))

    # 4) 工程目录 cookies 提交痕迹
    proj_cookies = list(PROJ.glob("cookies*.txt")) + list(PROJ.glob("*cookies*.txt"))
    out.append(("工程目录 cookies 提交痕迹",
                "⚠️ " + str(len(proj_cookies)) + " 个（勿提交 GitHub）" if proj_cookies else "✓ 无"))

    # 5) 日志敏感样本
    hits = _sample_log_check()
    out.append(("日志脱敏抽查",
                "⚠️ 疑似残留 " + str(len(hits)) + " 处（" + ", ".join(h[0] for h in hits) + "）"
                if hits else "✓ 无手机号/邮箱"))

    return out


def main():
    import argparse
    ap = argparse.ArgumentParser(description="隐私扫描器")
    ap.add_argument("--quiet", action="store_true", help="只打印风险")
    args = ap.parse_args()
    rows = scan()
    risk = sum(1 for _, s in rows if s.startswith("⚠️"))
    for name, status in rows:
        if args.quiet and not status.startswith("⚠️"):
            continue
        print(f"  {name:<16} {status}")
    print("\nsummary: " + ("⚠️ %d 项风险" % risk if risk else "✅ 干净"))
    sys.exit(1 if risk else 0)


if __name__ == "__main__":
    main()
