# -*- coding: utf-8 -*-
"""Kiana 发版全面门禁 release_check.py（v2.17 7-A）

用途：发版前一次性跑遍"核发版清单"的各方面——版本一致性、Git 卫生、全量测试、
静态检查、依赖审计、密钥泄漏扫描、规则/资产完整性、验证线、构建面自检。
不替代 Mimosa 深扫（发版前仍须单独跑，见安全审计记录）。

用法：
  python tools/release_check.py                 # 全部门禁（含 2 分钟级全量测试）
  python tools/release_check.py --skip-tests    # 快速面（不含全量测试）
  python tools/release_check.py --skip-network  # 跳过联网项：pip-audit 在线漏洞库 + verify_all 在线线
                                                #   [v2.19.8] 该开关此前是死参数，现真正生效

输出：逐项 [PASS/FAIL/SKIP] + 证据；全部必需项 PASS 则 exit 0，否则 exit 1。
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# [v2.19.9 修复] **门禁自己会在默认环境下崩。** 中文 Windows 的 stdout 是 GBK(cp936)，
# 而本脚本最后要 `print("✅ …")` ⇒ `UnicodeEncodeError: 'gbk' codec can't encode '\u2705'`；
# 更糟的是它崩在**所有检查都跑完之后**的汇总打印上 ⇒ 使用者只看到 traceback、看不到结论
# （真机实测：不带 PYTHONIOENCODING 跑 `--skip-network`，14 项全跑完，然后崩在汇总那一行）。
# 同一个坑已在 `sync_doc_counts.py` / `privacy_scanner.py` 修过，那里的注释写着
# 「以前靠调用方记得设 PYTHONIOENCODING=utf-8 绕过 —— 与『默认就能用』相悖」
# —— **门禁本人被漏掉了**。这里照同一套办：脚本内 reconfigure，不依赖调用方。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"

_LICENSES = ("MIT", "Apache-2.0", "BSD", "GPL", "MPL")
# 扫描豁免目录（密钥扫描与凭据卫生共用；tests/tools 内产物不入分发面）
ex_dirs = {".git", ".tmp_test_projects", "signature_extracts", "__pycache__",
           "tests", "tools", "downloader", "installer", "Docs"}
_KEY_PATTERNS = [
    (r"sk-[A-Za-z0-9]{16,}", "openai/stripe 式 API key"),
    (r"AKIA[0-9A-Z]{16}", "AWS AKIA"),
    (r"-----BEGIN (RSA|EC|DSA|OPENSSH) PRIVATE KEY-----", "私钥块"),
    (r"ghp_[A-Za-z0-9]{20,}", "GitHub PAT"),
]


def _run(cmd, timeout=900, cwd=ROOT):
    # [v2.19.9 修复] **子进程也必须说 UTF-8。** 这里本来就按 `encoding="utf-8"` 解码，
    # 却**从没告诉过子进程要写 UTF-8** ⇒ 中文 Windows 上子进程（Python）看到管道，
    # stdout 落到 GBK(cp936)，一句 `print("✅ …")` 就 `UnicodeEncodeError` 崩掉。
    # 真机实测（**不设** PYTHONIOENCODING 直接跑门禁）：**7/14 FAIL，其中 6 项是这个原因** ——
    # 规则资产 / verify_all / 结构指纹 / 静默失败 / 脱敏链路 / 多份实现，全是"检查本身没跑起来"，
    # **看起来像工程坏了**。也就是说：门禁是否可信，一直取决于**调用方记得设环境变量**，
    # 与工程「默认就能用」的口径相悖（同一个坑已在 sync_doc_counts / privacy_scanner 内部修过）。
    # 修在**唯一**的 spawn 口 —— 全门禁的子进程都走 `_run`，一条解决全部。
    _env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, cwd=str(cwd), encoding="utf-8",
                           errors="ignore", env=_env)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return -1, str(e)


def section(name):
    print(f"\n═══ {name} ═══")


def check_version():
    """四件套版本一致性：VERSION.json / pyproject / nsi / __init__"""
    try:
        vj = json.loads((ROOT / "VERSION.json").read_text(encoding="utf-8"))
        v_version = str(vj.get("latest") or vj.get("version") or "")
    except Exception as e:
        return FAIL, f"VERSION.json 读取失败: {e}", []
    checks = {}
    try:
        py = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        m = re.search(r'version\s*=\s*["\']([^"\']+)["\']', py)
        checks["pyproject"] = m.group(1) if m else None
    except Exception:
        checks["pyproject"] = None
    try:
        ini = (ROOT / "kiana_vnext_plus" / "__init__.py").read_text(encoding="utf-8")
        m = re.search(r"__version__\s*=\s*[\"']([^\"']+)[\"']", ini)
        checks["__init__"] = m.group(1) if m else None
    except Exception:
        checks["__init__"] = None
    try:
        nsi_path = ROOT / "kiana_setup.nsi"
        nsi = nsi_path.read_text(encoding="utf-8", errors="ignore")
        mv = re.search(r'!define\s+VERSION\s+"([\d.]+)"', nsi)
        checks["nsi(VERSION)"] = (mv.group(1) if mv else None) if mv else None
    except Exception:
        checks["nsi"] = None
    bad = {k: v for k, v in checks.items()
           if v is None or (k not in ("nsi(VIProductVersion)", "nsi(VERSION)")
                            and v != v_version)}
    # nsi 用 4 段式版本（2.17.0.0 vs 2.17.0）——前缀比
    nsi = checks.get("nsi(VERSION)") or ""
    if nsi and not nsi.startswith(v_version):
        bad["nsi(VERSION)"] = nsi
    elif not nsi:
        bad["nsi(VERSION)"] = "缺失"
    if bad:
        return FAIL, f"版本不一致: {v_version} vs {bad}", list(checks)
    return PASS, f"版本四件套一致: {v_version}（nsi={nsi}）", list(checks)


def check_git():
    rc, out = _run(["git", "status", "--porcelain"])
    dirty = [l for l in out.splitlines() if l.strip()]
    rc2, _ = _run(["git", "tag", "-l"])
    if not dirty:
        return PASS, "工作区干净; tags=" + ",".join(out.split()[:3]) if False else "工作区干净", []
    return FAIL, f"工作区有未提交变更 {len(dirty)} 条（发版前必须干净）", dirty[:5]


def check_tests(skip=False):
    if skip:
        return SKIP, "(--skip-tests 已跳过)", []
    rc, out = _run([sys.executable, "-m", "pytest", str(ROOT / "tests"), "-o", "addopts=", "-q"])
    m = re.search(r"(\d+) passed", out)
    m2 = re.search(r"(\d+) failed", out)
    failed = int(m2.group(1)) if m2 else 0
    passed = int(m.group(1)) if m else 0
    if passed >= 400 and failed == 0:
        return PASS, f"全量测试 {passed} passed / {failed} failed", []
    return FAIL, f"测试未达标 passed={passed} failed={failed}（期望 ≥400 且 0 失败）", []


def _static_count(rc: int, out: str):
    """静态工具输出 → 错误计数（None = 无法判定，属真异常）。

    [v2.19.8 修复] 原实现只看正则 `Found (\\d+) errors`，于是**把静态错误清干净反而判
    FAIL**：ruff 干净时输出 `All checks passed!`、mypy 干净时输出 `Success: no issues
    found ...`，正则都抓不到 → `int()` 抛 ValueError → 落进 "静态检查异常: FAIL" 分支。
    现在以**退出码**为准（0 = 真的干净 = 0 个错误），非零退出才去抓计数；
    抓不到计数说明工具真异常（崩溃/参数错），这时才 FAIL。
    """
    if rc == 0:
        return 0
    m = re.search(r"Found (\d+) errors", out)
    return int(m.group(1)) if m else None


def check_static():
    findings = {}
    rc, out = _run([sys.executable, "-m", "ruff", "check", str(ROOT / "kiana_vnext_plus")])
    ruff_n = _static_count(rc, out)
    rc2, out2 = _run([sys.executable, "-m", "mypy", str(ROOT / "kiana_vnext_plus")],
                     timeout=900)
    mypy_n = _static_count(rc2, out2)
    findings["ruff"] = ruff_n if ruff_n is not None else out.strip()[:120]
    findings["mypy"] = mypy_n if mypy_n is not None else out2.strip()[:120]
    if ruff_n is None or mypy_n is None:
        return FAIL, f"静态检查异常（非零退出但无计数，工具可能崩溃）: {findings}", list(findings)
    # [v2.19 基线锁死] 原为"基线+10% 余量"（ruff≤100 / mypy≤120）——等于主动容忍
    # 约 100 个静态错误（P2-13 的 F821 未定义名 `DomainState` 正是被该余量放过的）。
    # 现改为**锁定当前实测值并禁止回升**：任何新增静态错误都会让门禁 FAIL。
    # 后续每清理一批可再下调（只降不升）。
    # [v6 收紧] 原为 84/104，而实测已降到 81/103 —— **那 3/1 的余量就是新的隐性容忍**：
    # 84 的上限意味着"再加 3 个静态错误门禁也不会响"，等于重演 P2-13 那个被余量放过的洞。
    # 现按实测值锁死（本轮清理了两处 unused import，另有一处改动使其自然下降）。
    RUFF_MAX, MYPY_MAX = 80, 99
# [v6] mypy 上限 103 → **99**：本轮修 `_ensure_cookie_file` 的返回标注
# （`-> pathlib.Path` 其实到处 `return None`）时**顺带消掉 2 个既有报错**。
# 基线**只许降不许升** —— 降了就当场收紧，否则下次会悄悄涨回去。
    if ruff_n <= RUFF_MAX and mypy_n <= MYPY_MAX:
        ok_main = True
    else:
        ok_main = False

    # ════════════════════════════════════════════════════════════════
    # [v6 补齐] 把 `tools/` 与 `tests/` 纳入**正确性**检查（只跑 pyflakes 的 F 类）。
    #
    #   此前静态检查只覆盖 `kiana_vnext_plus` —— 于是**本会话新增的 8 个门禁工具
    #   自己从未被门禁检查过**，而且 P2-13 那类"F821 未定义名"只要写在 tools/ 里就无人拦。
    #
    #   **只选 F 类，不管 E 类风格**：E501/E702/E402 这些是历史风格，
    #   纳入只会产生噪声、逼人加 noqa；F 类才是真缺陷（未定义名 / 死代码 / 重定义）。
    # ════════════════════════════════════════════════════════════════
    rc3, out3 = _run([sys.executable, "-m", "ruff", "check", "--select", "F",
                      str(ROOT / "tools"), str(ROOT / "tests")])
    f_n = _static_count(rc3, out3)
    findings["ruff_F(tools+tests)"] = f_n if f_n is not None else out3.strip()[:120]
    if f_n is None:
        return FAIL, f"tools/tests 的 F 类检查异常: {findings}", list(findings)
    TOOLS_F_MAX = 42

    if ok_main and f_n <= TOOLS_F_MAX:
        return PASS, (f"ruff={ruff_n} mypy={mypy_n}（锁死 ≤{RUFF_MAX}/≤{MYPY_MAX}）"
                      f"；tools+tests 的 F 类={f_n}（锁死 ≤{TOOLS_F_MAX}）"), list(findings)
    return FAIL, (f"静态超基线 ruff={ruff_n} mypy={mypy_n} "
                  f"tools+tests F={f_n}（锁死 ≤{RUFF_MAX}/≤{MYPY_MAX}/≤{TOOLS_F_MAX}）"), list(findings)


def check_deps(skip_network=False):
    # [v2.19.8 修复] `--skip-network` 的意图是"跳过需要联网的门禁面"（pip-audit 要查在线
    # 漏洞库），但 main() 从未把该参数传进来（死参数）→ 号称跳过却照旧联网，断网时会白等
    # 600s 超时。现真正生效，并如实标注 SKIP（而不是假装 PASS）。
    if skip_network:
        return SKIP, "(--skip-network 已跳过在线漏洞库查询 pip-audit)", []
    req = ROOT / "kiana_vnext_plus" / "requirements.txt"
    rc, out = _run(["pip-audit", "-r", str(req)], timeout=600)
    if rc != 0 and "command" not in out:
        rc2, out2 = _run([sys.executable, "-m", "pip_audit", "-r", str(req)], timeout=600)
        rc, out = rc2, out2
    if "No known vulnerabilities" in out:
        return PASS, "pip-audit: No known vulnerabilities", []
    if rc == 0 and "no known" in out.lower():
        return PASS, "pip-audit: 无已知漏洞", []
    if "No module named" in out or "not found" in out.lower() or "command" in out:
        return SKIP, "pip-audit 未安装（发版机须先装：pip install pip-audit）", []
    return FAIL, f"pip-audit 未通过(rc={rc}): {out[-300:]}", []


def check_secrets():
    """工程白名单外（打包器/构建器/win32 等排除）不得有密钥形式"""
    ex_dirs = {".git", ".tmp_test_projects", "signature_extracts", "__pycache__",
               "tests", "tools", "downloader", "installer", "Docs"}
    hits = []
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in ex_dirs and not d.startswith(".")]
        for f in files:
            if not f.endswith((".py", ".json", ".toml", ".ini", ".yaml", ".yml", ".txt")):
                continue
            p = Path(root) / f
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for pat, desc in _KEY_PATTERNS:
                if re.search(pat, text):
                    hits.append((str(p.relative_to(ROOT)), desc))
    if hits:
        return FAIL, f"发现 {len(hits)} 处密钥形态（应只有占位符/示例）", hits[:5]
    return PASS, "密钥泄漏扫描干净（包内无私钥/真实 API key 形态）", []


def check_credential_hygiene():
    """[v2.17.1 凭据卫生·门禁第 10 项] 公开分发零内置凭据：
    1) 仓库不得出现 cookies 文件（*cookies*.txt / *cookie*.txt）与 Netscape 明文行
    2) README/示例不得出现可用凭据字面量（API key 必须占位符形态）
    3) .gitignore 必须覆盖常见凭据产物（cookies*.txt）"""
    bad = []
    src_root = ROOT
    net_pat = re.compile(r"^\s*\.?[a-z0-9.-]+\t+(TRUE|FALSE)\t")
    for root, dirs, files in os.walk(src_root):
        dirs[:] = [d for d in dirs
                   if d not in ex_dirs and not d.startswith(".")]
        for f in files:
            p = Path(root) / f
            rel = str(p.relative_to(ROOT)).replace("\\", "/")
            if re.search(r"cookie", f, re.I) and f.lower().endswith(".txt"):
                bad.append(f"cookies 文件不得入库: {rel}")
            if f.lower().endswith((".txt", ".md")):
                try:
                    text = p.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                for line in text.splitlines():
                    if line.startswith("#") or not line.strip():
                        continue
                    if net_pat.match(line):
                        bad.append(f"Netscape 明文 cookie 行泄露风险: {rel}")
                        break
    try:
        gi = (ROOT / ".gitignore").read_text(encoding="utf-8", errors="ignore")
        if "cookies" not in gi.lower():
            bad.append(".gitignore 未覆盖 cookies*.txt")
    except Exception:
        bad.append(".gitignore 缺失")
    if bad:
        return FAIL, f"凭据卫生未过（零内置红线）: {bad[:4]}", bad[:4]
    return PASS, "凭据卫生：仓库零 cookies/密钥字面量；示例占位符合规", []


def check_rules_assets():
    """规则全量加载 + 真实样本资产存在性（软性：样本缺失→SKIP 提示）"""
    rule_dir = ROOT / "rules" / "sites"
    rules = sorted(rule_dir.glob("*.yaml"))
    missing = []
    for r in rules:
        rc, out = _run([sys.executable, "-m", "kiana_vnext_plus.cli",
                        "rule-validate", str(r)])
        if rc != 0:
            missing.append((r.name, out[-120:]))
    assets = set(p.name for p in (ROOT / "tests" / "assets").glob("*.html"))
    # [v2.17 打磨轮] 规则→样本 别名表（样本文件命名与规则名不同源——显式登记，
    # 缺失即 FAIL；模板规则不参与）
    _SAMPLE_ALIAS = {
        "netease-news": ["163_article_sample"],
        "sina-news": ["sina_article_sample"],
        "sohu-news": ["sohu_article_sample"],
        "ifeng-news": ["ifeng_article_sample"],
        "weibo-post": ["weibo_post_sample"],
        "zhihu-qa": ["zhihu_question_sample"],
        "douban-book": ["douban_book_sample"],
        "sspai-post": ["sspai_article_sample"],
        "toutiao-article": ["toutiao_article_sample"],
        "36kr-article": ["kr_article_sample"],
        # [v2.19.8 M2-e] 新增两个站点。`capture_sample.py` 的告警里写着
        # "不登记的话门禁不会发现样本缺失（已知缺口）" —— 所以这里必须登记。
        # 注意 smzdm 的实际文件名是**双 sample**（工具自己会加 `_sample` 后缀，
        # 而我传的 --name 里已经带了）—— 照实写，别"顺手改整齐"。
        "smzdm-post": ["smzdm_post_sample"],
        "wechat-article": ["wechat_article_sample"],
    }
    if missing:
        return FAIL, f"规则加载失败 {len(missing)}: {missing[:3]}", [m[0] for m in missing]
    if len(rules) < 10:
        return FAIL, f"规则数 {len(rules)} < 10", []
    miss_samples = []
    for rule, aliases in _SAMPLE_ALIAS.items():
        if not any(any(a in s for s in assets) for a in aliases):
            miss_samples.append(rule)
    if miss_samples:
        return FAIL, f"真实站点缺样本资产: {miss_samples}", miss_samples
    # [v2.19.8 修复] 这句原来是**写死的** `样本 10/10 齐备` ——
    # 我加了两个站点（smzdm / 公众号）后，它**照旧说 10/10**，
    # 登记表里明明已是 12 个。**状态行撒谎比没状态行更坏**（会让人以为登记没生效）。
    # 改成**按实际算出来的数**报。
    _total = len(_SAMPLE_ALIAS)
    return PASS, (f"规则 {len(rules)} 全部加载通过；"
                  f"真实站点样本 {_total}/{_total} 齐备"), []


def check_verify_all(skip_network=False):
    cmd = [sys.executable, str(ROOT / "tools" / "verify_all.py")]
    if skip_network:
        cmd += ["--skip-bili", "--skip-tieba", "--skip-douyin", "--skip-youtube",
                "--skip-music163", "--skip-kuaishou", "--skip-xhs"]
    rc, out = _run(cmd, timeout=900)
    # [v2.19.8 修复] 证据行必须取"总体"那行。此前用 splitlines()[-2] 随手抓倒数第二行，
    # 联网跑时会抓到某条无关探针的 ERROR（如 B 站视频格式不可用）——判定仍是 PASS，
    # 却让一个真 PASS 看起来像假绿，排查时要去翻源码才能确认。
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    verdict = next((ln for ln in lines if "总体" in ln), lines[-1] if lines else "")
    # [v6 M2-c 收紧] 原判定是宽松合取 `rc == 0 and "PASS" in out`：只要输出里**任意位置**
    # 出现 "PASS"（例如某条单项线自己的 ✅）就算过 —— 于是"总体 FAIL、某单项 PASS"
    # 会被误判为 PASS。这是本工程最贵的错误类型（假 PASS）的一个真实入口。
    # verify_all 的总体行**只在全过时**才打印 "总体: ✅ PASS"（见 verify_all.py:170），
    # 故一律以总体行为准，不再看别的行。
    if "总体: ✅ PASS" in out:
        return PASS, "verify_all: " + verdict, []
    return FAIL, f"verify_all 未PASS(rc={rc}): {out[-300:]}", []


def check_structure_probe(skip_network=False):
    """[v6 M2-c] 门禁第 11 项：结构指纹。

    **离线部分永远跑**（样本结构指纹 ↔ 入库基准逐一比对），不符即 FAIL——
    它能抓到两类静默失真：① 样本被替换/截断/编码改坏；② 指纹算法被悄悄改动
    （探针口径一旦漂移，之后所有"结构变了"的告警都不可信）。

    **在线部分仅在未 `--skip-network` 时**尝试（真站目标来自运行期数据根，不在仓库内）。
    数不出来（无网络 / 未配目标 / 全部取不到）→ 退出码 2 → **如实 SKIP**，
    绝不冒充 PASS。
    """
    cmd = [sys.executable, str(ROOT / "tools" / "fingerprint_baseline.py")]
    if not skip_network:
        cmd.append("--online")
    rc, out = _run(cmd, timeout=300)
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    tail = lines[-1] if lines else ""
    if rc == 0:
        # 离线过了不代表在线过了——把"在线没跑"写在明面上，别让 PASS 被读大
        if skip_network:
            return PASS, "离线基准一致；**在线探针未开启**(--skip-network)", []
        return PASS, tail[:120], []
    if rc == 2:
        # 2 = **数不出来**，既不是 PASS 也不是 FAIL —— 如实标注为跳过
        return SKIP, tail[:120], []
    return FAIL, tail[:160], []


def check_silent_failures():
    """[v6 第 12 项] 热路径静默失败扫描（`except: pass` 且无理由注释）。

    `03-终检与闭环方案` 第一组写着"无新增 `except: pass` / 无日志的静默失败分支
    （例外必须写理由注释）"——但这条**此前无法核对**：全仓实测 200+ 处，肉眼扫不出来，
    文档却按"只有两处合法例外"的语气在写。现做成**锁定基线 + 只许降**：

      · 修掉一处 → 跑 `--write-baseline` 更新，让基线变小；
      · 要新增例外 → 在 `except` 行或 `pass` 行写 `# 理由`——
        **写清理由的忽略是工程实践，不是债；不写理由的才是**。

    只扫**热路径**（抓取/网络/安全/身份）：这几条链上的静默失败会直接变成
    "用户看到的现象与真实原因不一致"，是工程最贵的一类；其余位置的清理是独立决策。
    """
    rc, out = _run([sys.executable, str(ROOT / "tools" / "silent_failure_scan.py")],
                   timeout=180)
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    tail = lines[-1] if lines else ""
    if rc == 0:
        return PASS, tail[:130], []
    return FAIL, tail[:170], []


def check_sanitize_chain():
    """[v6 第 13 项] 脱敏链路自检（**活体特征串**，而不是读代码猜）。

    文档写着"全链路脱敏"，但此前**没有任何一项门禁核对它**；而且 `privacy_sanitize`
    是个**能全局关掉内容脱敏的开关**——关掉后落盘/导出副本原样写出，也没有检查会变红。

    本项往一个新 handler 打含特征串的日志，判据是"**输出里真的搜不到**"：
    URL 签名参数 / Cookie 头 / 邮箱三类。
    数不出来（没捕获到日志）→ 退出码 2 → **SKIP**，不冒充 PASS。

    这一项不是形式主义：它上线当天就抓到了 `SESSDATA=…` 原样进日志的真缺口。
    """
    rc, out = _run([sys.executable, str(ROOT / "tools" / "sanitize_chain_check.py")],
                   timeout=120)
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    tail = lines[-1] if lines else ""
    if rc == 0:
        return PASS, tail[:130], []
    if rc == 2:
        return SKIP, tail[:130], []
    return FAIL, tail[:170], []


def check_duplicate_capability():
    """[v6 第 14 项] 「同一能力多份实现」扫描。

    **这是本工程最稳定的缺陷模式**：连续九轮验证下来，出事的全是
    "同一个能力在多处各写一份"——

      · WebRTC 泄漏面 5 份实现，2 份各漏一条路径；下载会话 impersonate 3 份拷贝只修 2 份；
      · UA↔TLS 主通道修过同源绑定而下载链没跟；重试策略 Redis 后端是陈旧分叉（落后 3 次修复）；
      · `push`/`pop_batch` 两后端形参不符 → **TypeError**；防盗链 Referer 三种做法、两种命中 403。

    此前每轮都是**人肉去找**"下一个能力"——这本身不可靠。现工具化：

      · 同名跨模块且**形参不一致** → 阻断（调用点可能 TypeError，第 25 轮那两个就是）；
      · 出现在 ≥3 个模块的名字 → 通用动词，只登记（实测校准：真出事的都是 2 个模块）；
      · `**kwargs` 纯委托 → 视为兼容（否则误报 `write_page` 那类正常委托）。
    """
    rc, out = _run([sys.executable, str(ROOT / "tools" / "duplicate_capability_scan.py")],
                   timeout=180)
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    tail = lines[-1] if lines else ""
    if rc == 0:
        return PASS, tail[:130], []
    return FAIL, tail[:170], []


def check_build_surface():
    """构建面自检：spec 存在/入口存在/关键新模块在 spec 白名单（打包器会自动带 package）"""
    specs = [p for p in ROOT.glob("*.spec")]
    missing = []
    if not specs:
        missing.append("*.spec 缺失（KianaLauncher.spec / KianaCrawler.spec 至少其一）")
    else:
        txt = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in specs)
        if "kiana_vnext_plus" not in txt:
            missing.append("spec 未收集 kiana_vnext_plus 包（若 hooks 自动收集需人工确认）")
        # [v2.17] 模块级引用不做 FAIL：新模块都在 kiana_vnext_plus 包内，PyInstaller
        # 随包收集——仅软提示（样本清单档在规则资产节）
    for f in ("launcher_v9.py", "run_crawler.py"):
        if not (ROOT / f).exists():
            missing.append(f"入口 {f} 缺失")
    if not (ROOT / "kiana_setup.nsi").exists():
        missing.append("kiana_setup.nsi 缺失")
    rc, out = _run([sys.executable, "-m", "py_compile",
                    str(ROOT / "launcher_v9.py"), str(ROOT / "run_crawler.py"),
                    str(ROOT / "kiana_vnext_plus" / "crawler.py")])
    if rc != 0:
        missing.append(f"py_compile 失败: {out[-200:]}")
    if missing:
        return FAIL, "构建面自检未过: " + "; ".join(missing[:4]), missing[:4]
    return PASS, "构建面入口可编译/spec 存在", []


def main():
    ap = argparse.ArgumentParser(description="Kiana 发版全面门禁")
    ap.add_argument("--skip-tests", action="store_true")
    ap.add_argument("--skip-network", action="store_true")
    args = ap.parse_args()

    checks = []
    section("1 版本一致性");    checks.append(("版本一致性", *check_version()))
    section("2 Git 卫生");      checks.append(("Git 卫生", *check_git()))
    section("3 全量测试");      checks.append(("全量测试", *check_tests(args.skip_tests)))
    section("4 静态检查");      checks.append(("静态检查", *check_static()))
    section("5 依赖审计");      checks.append(("依赖审计(pip-audit)", *check_deps(args.skip_network)))
    section("6 密钥泄漏扫描");  checks.append(("密钥扫描", *check_secrets()))
    section("7 凭据卫生（零内置红线）"); checks.append(("凭据卫生", *check_credential_hygiene()))
    section("8 规则/资产完整性"); checks.append(("规则资产", *check_rules_assets()))
    section("9 验证线");        checks.append(("verify_all", *check_verify_all(args.skip_network)))
    section("10 构建面自检");    checks.append(("构建面", *check_build_surface()))
    section("11 结构指纹");      checks.append(("结构指纹", *check_structure_probe(args.skip_network)))
    section("12 静默失败扫描");  checks.append(("静默失败", *check_silent_failures()))
    section("13 脱敏链路");      checks.append(("脱敏链路", *check_sanitize_chain()))
    section("14 同一能力多份实现"); checks.append(("多份实现", *check_duplicate_capability()))

    print("\n\n════════ 发版门禁汇总 ════════")
    fails = 0
    for name, status, detail, evidence in checks:
        mark = {"PASS": "✅", "FAIL": "❌", "SKIP": "⏭️"}[status]
        print(f"  {mark} {name:<14} {status}  {detail[:90]}")
        if status == FAIL:
            fails += 1
    print(f"\n结果: {len(checks) - fails}/{len(checks)} 通过（FAIL={fails}）")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
