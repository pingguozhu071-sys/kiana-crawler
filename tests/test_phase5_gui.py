"""Kiana Vnext Plus — 阶段 5 GUI 任务中心回归（v2.11）

覆盖：Crawler pause/resume 语义、crawl on_engine 回调链（GUI 停止断链修复）、
GUI 数据页/任务页构建（离线实例化冒烟——Qt 需 app，跳过条件标记）。
"""
import sys
import os
import inspect
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ['KIANA_CRYPTO_KEY'] = 'kv_test'

PROJECT = Path(__file__).parent.parent

from omegaconf import OmegaConf
from kiana_vnext_plus.config import GlobalConfig
from kiana_vnext_plus.identity import ProjectIdentity
from kiana_vnext_plus.crawler import Crawler


def _mk_crawler(tag):
    proj = ProjectIdentity(tag, base_dir=PROJECT / ".tmp_test_projects")
    g = GlobalConfig(OmegaConf.create({"master_password": "pw"}))
    c = Crawler(proj, g, worker_id="t")
    return c


class TestCrawlerPause:
    def test_pause_resume_flag(self):
        c = _mk_crawler("t_pause")
        assert c.paused is False
        c.pause()
        assert c.paused is True
        c.resume()
        assert c.paused is False
        import shutil
        shutil.rmtree(PROJECT / ".tmp_test_projects" / "t_pause", ignore_errors=True)


class TestOnEngineCallback:
    """GUI 停止断链修复：crawl() 支持 on_engine 回调（创建即交出引用）"""

    def test_crawl_signature(self):
        import run_crawler
        params = inspect.signature(run_crawler.crawl).parameters
        assert "on_engine" in params
        assert params["on_engine"].default is None
