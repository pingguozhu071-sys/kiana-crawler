# -*- coding: utf-8 -*-
"""脱敏链路自检（门禁第 13 项）—— 用**活的特征串**跑一遍，而不是读代码猜

**为什么需要它**：工程文档写着"全链路脱敏：root logger 过滤器 + print 桥 + Qt handler +
engine 异常 emit + lastResort"，但**此前没有任何一项门禁核对它**。更具体地说：

  · `privacy_sanitize`（`DEFAULT_GLOBAL`）是一个**能全局关掉内容脱敏的开关**，
    关掉后 `page_processor` 的落盘/导出副本**原样写出**（手机号/邮箱/IP）。
    **没有任何检查会因此变红**——这正是一条红线（"cookies/密钥绝不进仓库与日志"）的漏洞。
  · 根 logger 的 Filter、`logging.Handler.__init__` 的自动包装，都是**运行时装配**的，
    静态读代码只能看到"应该装配了"，看不到"实际装配上了没有"。

所以本工具做的是**端到端活体自检**：往一个新 handler 打一条含特征串的日志，
看它**输出里还剩不剩**。判据不是"函数存在"，而是"**字符串真的没了**"。

三类特征串（对应三条真实泄漏面）：
  1. URL 签名参数 `?token=…`      → 走 `sanitize_url`
  2. 请求头 `Cookie: …`           → 走 `sanitize_text` / 头脱敏
  3. 邮箱                          → 走 `sanitize_text`

退出码：0 = 链路完好；1 = **有特征串漏出来了**（FAIL）；2 = 数不出来（SKIP，不冒充 PASS）。
"""
import argparse
import io
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

EXIT_OK = 0
EXIT_LEAK = 1
EXIT_CANNOT_MEASURE = 2

# 特征串：任一个出现在日志输出里就是脱敏失效
CANARY = {
    "url_token": "CANARY_TOKEN_7f3a91d2",
    "cookie": "CANARY_COOKIE_4b8e10c5",
    "email": "canary_user@example.com",
}


def check_switch() -> list:
    """① 开关本身必须是开的（`privacy_sanitize=False` 会让导出副本原样落盘）"""
    problems = []
    try:
        from kiana_vnext_plus.config import DEFAULT_GLOBAL
        val = DEFAULT_GLOBAL.get("privacy_sanitize")
        if val is not True:
            problems.append(f"privacy_sanitize 默认值是 {val!r}，必须是 True"
                            "（关掉后落盘/导出副本原样写出手机号/邮箱/IP）")
    except Exception as e:
        problems.append(f"读取 DEFAULT_GLOBAL 失败: {type(e).__name__}")
    return problems


def check_import_order() -> list:
    """② 脱敏是**模块级副作用**——必须在 basicConfig 之前 import config 才会装配"""
    problems = []
    try:
        import kiana_vnext_plus.config  # noqa: F401  (导入即装配)
        if not getattr(logging.Handler, "_kiana_autosanitized", False):
            problems.append("logging.Handler 未被自动脱敏包装"
                            "（config 的 _install_handler_autosanitize 没生效）")
    except Exception as e:
        problems.append(f"装配检查异常: {type(e).__name__}")
    return problems


def check_live_canary() -> tuple:
    """③ **端到端活体检查**：建一个全新 handler，打日志，看特征串还在不在。

    返回 `(problems, captured)`。捕获不到任何输出 = 数不出来（调用方按 SKIP 处理），
    **不能当作通过**。
    """
    problems = []
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    old_level, root.level = root.level, logging.DEBUG
    root.addHandler(handler)
    try:
        lg = logging.getLogger("kiana.canary")
        lg.warning("请求 https://api.example.com/x?token=%s&a=1", CANARY["url_token"])
        lg.warning("Cookie: SESSDATA=%s; bili_jct=%s",
                   CANARY["cookie"], CANARY["cookie"])
        lg.warning("联系 %s 投稿", CANARY["email"])
    finally:
        root.removeHandler(handler)
        root.level = old_level

    captured = buf.getvalue()
    if not captured.strip():
        return problems, ""            # 数不出来，交给调用方判 SKIP
    for name, secret in CANARY.items():
        if secret in captured:
            problems.append(f"**{name} 泄漏**：日志输出里仍能搜到 {secret}")
    return problems, captured


def main() -> int:
    ap = argparse.ArgumentParser(description="脱敏链路自检（活体特征串）")
    ap.add_argument("--show", action="store_true", help="打印捕获到的日志（已脱敏后）")
    args = ap.parse_args()

    problems = check_switch() + check_import_order()
    live, captured = check_live_canary()
    if not captured.strip():
        print("⏭️ SKIP：活体检查没捕获到任何日志输出——**不计入判定**，也不冒充 PASS")
        for p in problems:
            print(f"   · {p}")
        return EXIT_CANNOT_MEASURE
    problems += live

    if args.show:
        print("捕获到的输出（应已脱敏）：")
        for ln in captured.splitlines():
            print("   |", ln)

    if problems:
        print("❌ 脱敏链路有缺口：")
        for p in problems:
            print("   ·", p)
        return EXIT_LEAK
    print("✅ 脱敏链路完好：3 类特征串（URL 签名参数 / Cookie 头 / 邮箱）"
          "在日志输出里均搜不到；privacy_sanitize 默认开启；Handler 自动包装已生效")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
