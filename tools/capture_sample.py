# -*- coding: utf-8 -*-
"""规则样本采集：把真实页面的 DOM 存成测试资产（M2-e 的前置）

**为什么需要它**：新增站点规则必须配**真实页面样本**（门禁第 8 项会核对清单），
而工程纪律明确写着"**不瞎选选择器**"——选择器要对着真实 DOM 写。
以前这一步只能靠人工（浏览器打开 → F12 → 复制 outerHTML → 存文件），
本工具把它变成一条命令。

**安全前提（重要，别跳过）**：样本会**提交进仓库**。所以本工具的默认行为是
**只写到运行期数据根**，不碰仓库；确认干净后才用 `--install` 拷进 `tests/assets/`。
落盘前统一过 `sanitize_text`（邮箱等），但**自动脱敏不能替代肉眼确认**。

用法:
  python tools/capture_sample.py --url https://post.smzdm.com/p/xxxx
  python tools/capture_sample.py --url <URL> --name smzdm_note --install
  python tools/capture_sample.py --url <URL> --proxy http://127.0.0.1:7897   # 需走代理的站

采集完还要**手动**做两件事（本工具不代劳，因为都需要人判断）：
  ① 写规则 YAML（`python -m kiana_vnext_plus.cli rule-new` 生成骨架）；
  ② 把规则名 → 样本名登记进 `tools/release_check.py` 的 `_SAMPLE_ALIAS`。
     **不登记的话门禁不会发现样本缺失**（这是已登记的已知缺口）。
"""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36")

# 生成样本名时要丢掉的通用标签
_TLD = {"com", "cn", "net", "org", "io", "co", "me", "tv", "cc", "info", "xyz"}
_GENERIC = {"www", "m", "mp", "post", "api", "app", "web", "static", "cdn", "img"}
# 注册局式标签：它们属于域名但**没有辨识度**（`mp.weixin.qq.com` 该叫 weixin 而不是 qq）
_REGISTRY = {"qq", "com", "net", "org", "gov", "edu", "co", "cn", "sina", "sohu"}


def derive_name(url: str) -> str:
    """从 URL 推一个样本名（用于 `<name>_sample.html`）。

    **从右往左找第一个"有辨识度"的标签**，跳过 TLD、注册局式标签与通用子域：
      `post.smzdm.com`   → `smzdm`
      `mp.weixin.qq.com` → `weixin`（不是 `qq`）
    推不出时回退 `sample`。
    """
    try:
        from urllib.parse import urlsplit
        host = (urlsplit(url).hostname or "").lower()
    except Exception:
        host = ""
    parts = [p for p in host.split(".") if p]
    while parts and parts[-1] in _TLD:
        parts.pop()
    name = ""
    for p in reversed(parts):
        if p not in _REGISTRY and p not in _GENERIC:
            name = p
            break
    if not name:
        name = parts[-1] if parts else "sample"
    return "".join(ch if (ch.isalnum() or ch in "_-") else "_" for ch in name) or "sample"


def capture_dir(root=None) -> str:
    """默认落运行期数据根（**不进仓库**）——与取证归档同一条纪律。"""
    if root is not None:
        return str(root)
    from kiana_vnext_plus.config import data_root
    return os.path.join(str(data_root()), "samples")


def _default_fetch(url: str, timeout: int, proxy: str):
    """走工程的安全闸取页面（红线：动态 URL 一律过闸，工具也不是例外）"""
    from kiana_vnext_plus.url_utils import safe_urlopen
    resp = safe_urlopen(url, headers={"User-Agent": UA}, timeout=timeout, proxy=proxy)
    if resp is None:
        return None
    with resp:
        return resp.read()


def capture(url: str, name: str = None, *, fetcher=None, root=None,
            timeout: int = 20, proxy: str = "") -> dict:
    """抓取一个真实页面并落盘成样本。返回 `{ok, path, name, bytes, redacted}` 或 `{ok:False, error}`。

    **不抛异常**——错误一律转成可读的 `error`，供命令行直接展示。
    """
    if not url or not str(url).startswith(("http://", "https://")):
        return {"ok": False, "error": "只接受 http/https URL"}
    nm = name or derive_name(url)

    fetch = fetcher or _default_fetch
    try:
        raw = fetch(url, timeout, proxy)
    except Exception as e:
        return {"ok": False, "error": f"抓取异常: {type(e).__name__}: {e}"}
    if not raw:
        # safe_urlopen 对非法目标返回 None（不抛），这里统一成可读原因
        return {"ok": False, "error": "被安全闸拦截或抓取失败（返回空）"}

    html = raw.decode("utf-8", "ignore") if isinstance(raw, (bytes, bytearray)) else str(raw)
    if not html.strip():
        return {"ok": False, "error": "响应为空（可能需要登录态或走代理）"}

    from kiana_vnext_plus.sanitizer import sanitize_text
    clean = sanitize_text(html)

    out_dir = capture_dir(root)
    try:
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"{nm}_sample.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(clean)
    except Exception as e:
        return {"ok": False, "error": f"落盘失败: {e}"}

    return {"ok": True, "path": path, "name": nm,
            "bytes": len(clean.encode("utf-8")), "redacted": clean != html}


def install(name: str, src: str) -> str:
    """把已确认干净的样本拷进仓库 `tests/assets/`。**需要显式调用**。"""
    dst_dir = ROOT / "tests" / "assets"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / f"{name}_sample.html"
    with open(src, encoding="utf-8") as f:
        data = f.read()
    with open(dst, "w", encoding="utf-8") as f:
        f.write(data)
    return str(dst)


CHECKLIST = """⚠️ 提交前必须自查（样本会进仓库、会被公开）：
  1. 不含**个人数据**：邮箱/手机号/IP/真实昵称——已自动过 sanitize_text，但**仍需肉眼确认**；
  2. 不含**登录态内容**：本人主页、订单、私信、带签名的头像链接；
  3. 体积合理：>2MB 的样本会拖慢测试（可考虑裁剪，但**别改结构**，否则规则选择器失真）；
  4. 采集完还要手动做两件事（本工具不代劳，都需要人判断）：
     · 写规则 YAML（`python -m kiana_vnext_plus.cli rule-new` 生成骨架）；
     · 把「规则名 → 样本名」登记进 tools/release_check.py 的 _SAMPLE_ALIAS——
       **不登记的话门禁不会发现样本缺失**（已知缺口）。"""


def main() -> int:
    ap = argparse.ArgumentParser(description="规则样本采集（真实 DOM → 测试资产）")
    ap.add_argument("--url", required=True, help="目标页面 URL")
    ap.add_argument("--name", default=None, help="样本名（默认从 URL 推）")
    ap.add_argument("--install", action="store_true",
                    help="确认干净后拷进仓库 tests/assets/（默认只落运行期数据根）")
    ap.add_argument("--proxy", default="", help="代理（如 http://127.0.0.1:7897）")
    ap.add_argument("--timeout", type=int, default=20)
    args = ap.parse_args()

    res = capture(args.url, args.name, timeout=args.timeout, proxy=args.proxy)
    if not res.get("ok"):
        print(f"❌ 采集失败：{res.get('error')}")
        return 1
    print(f"✅ 已采集 {res['name']}：{res['bytes']} 字节"
          f"{'（已脱敏）' if res['redacted'] else ''}")
    print(f"   暂存于运行期数据根（**未进仓库**）：{res['path']}")
    if args.install:
        dst = install(res["name"], res["path"])
        print(f"✅ 已拷入仓库：{dst}")
    else:
        print("   确认干净后再用 --install 拷进 tests/assets/")
    print()
    print(CHECKLIST)
    return 0


if __name__ == "__main__":
    sys.exit(main())
