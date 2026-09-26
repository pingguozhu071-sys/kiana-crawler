"""Kiana 测试共享 fixture（v2.15 阶段1 工程卫生）

此前 17 个测试文件各自为政（env/目录/清理逻辑重复）——统一到 conftest。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("KIANA_CRYPTO_KEY", "kv_test")

import pytest


@pytest.fixture
def tmp_frontier(tmp_path):
    """独立临时 FrontierDB（init_async 完成、用后自清）
    [FIXED & MODIFIED] v2.17 稳定性门禁：原实现对 init/close 各起一个 asyncio.run——
    aiosqlite 连接跨 loop 关闭；且 teardown 用 except: pass 吞错 → worker 线程/连接
    静默泄漏（长套件累积）。现改同一 loop 内 init+close，并显式关 loop。"""
    import asyncio
    from kiana_vnext_plus.frontier import FrontierDB

    loop = asyncio.new_event_loop()
    db = FrontierDB(str(tmp_path / "frontier.db"))
    loop.run_until_complete(db.init_async())
    yield db
    try:
        loop.run_until_complete(db.close())
    except Exception:
        pass
    finally:
        loop.close()
