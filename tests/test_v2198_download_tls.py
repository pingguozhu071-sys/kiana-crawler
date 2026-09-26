# -*- coding: utf-8 -*-
"""[v2.19.8] 下载会话 TLS 指纹缺失的回归测试。

背景（用本工程采集 asmr-300 时实测暴露）：`MediaDownloader.init_session()` 与
`UniversalDownloader.init()` 建 curl_cffi 会话时只传 timeout/headers，**没传
`impersonate`** → 落在"不模拟指纹"的默认档。同一文件、同一代理、不落盘、交叉轮替
取样：不带指纹 4.71-8.17 MB/s，带指纹（chrome136）12.09-25.26 MB/s。

这不只是性能：工程把 curl_cffi 定位为"TLS 指纹模拟主通道"（requirements 注释如此），
协议通道 `protocol_engine` 确实传了 impersonate，唯独下载路径漏了 —— 同一次任务里
页面请求与媒体请求的指纹不一致，本身是检测信号。

两条断言：
① 结构：两个建会话处必须显式传 `impersonate=` 且取自工程自己的 TLS 池（不硬编码）；
② 运行时：会话对象实际带上指纹（curl_cffi 会把该值挂在 session 上）。
"""
import ast
import asyncio
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CASES = [
    ("kiana_vnext_plus/media_downloader.py", "init_session"),
    ("kiana_vnext_plus/universal_downloader.py", "init"),
]


def _func_node(src: str, name: str):
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


@pytest.mark.parametrize("rel,func_name", CASES)
def test_session_constructed_with_impersonate(rel, func_name):
    """建会话时必须显式传 impersonate，且值来自工程 TLS 池。"""
    src = (ROOT / rel).read_text(encoding="utf-8")
    fn = _func_node(src, func_name)
    assert fn is not None, f"{rel} 里找不到 {func_name}"

    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "AsyncSession"]
    assert calls, f"{rel}:{func_name} 未构造 AsyncSession"

    kw_names = {kw.arg for c in calls for kw in c.keywords}
    assert "impersonate" in kw_names, (
        f"{rel}:{func_name} 构造 AsyncSession 时没传 impersonate —— "
        "会落在无指纹默认档（实测吞吐掉到 1/2~1/4）")

    # 值必须取自工程自己的池，不得硬编码字面量（避免与协议通道指纹不一致）
    assert "TLS_IMPERSONATE_POOL" in src, f"{rel} 未引用指纹池"


def test_media_downloader_session_really_carries_impersonate(tmp_path):
    """运行时核对：会话对象上确实挂上了池里的指纹。"""
    from kiana_vnext_plus.media_downloader import MediaDownloader
    from kiana_vnext_plus.fingerprint_consistency import TLS_IMPERSONATE_POOL

    dl = MediaDownloader(tmp_path, max_concurrent=1)

    async def _run():
        await dl.init_session()
        try:
            return getattr(dl.session, "impersonate", None)
        finally:
            await dl.close()

    got = asyncio.run(_run())
    assert got == TLS_IMPERSONATE_POOL[0], (
        f"会话指纹 = {got!r}，期望 {TLS_IMPERSONATE_POOL[0]!r}（工程 TLS 池首项）")
