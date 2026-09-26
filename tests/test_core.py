"""Kiana Vnext Plus — Test Suite"""
import sys
import os
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent))
os.environ['KIANA_CRYPTO_KEY']='kv'

from kiana_vnext_plus.config import DEFAULT_GLOBAL
from kiana_vnext_plus.enhancements import DataExporter,RequestDedup,AutoThrottle,DataValidator
from kiana_vnext_plus.video_resolver import extract_bilibili_info

class TestConfig:
    def test_default_global_concurrency(self):
        assert DEFAULT_GLOBAL.global_concurrency==200
    def test_browser_memory(self):
        assert DEFAULT_GLOBAL.browser_memory_limit_mb==2048
    def test_min_concurrency(self):
        assert DEFAULT_GLOBAL.min_concurrency==20
    def test_mem_thresholds(self):
        assert DEFAULT_GLOBAL.mem_threshold_green==90.0
        assert DEFAULT_GLOBAL.mem_threshold_critical==97.0

class TestEnhancements:
    def test_exporter_jsonl(self,tmp_path):
        de=DataExporter(tmp_path)
        de.add({'title':'Test','url':'x.com','text':'hello'},'test.com')
        de.flush_all()
        files=list(tmp_path.rglob('*.jsonl'))
        assert len(files)==1
    def test_exporter_csv(self,tmp_path):
        de=DataExporter(tmp_path)
        de.add({'title':'Test','url':'x.com','text':'hello'},'test.com')
        de.flush_all()
        files=list(tmp_path.rglob('*.csv'))
        assert len(files)==1
    def test_dedup(self):
        rd=RequestDedup()
        assert not rd.is_duplicate('http://a.com')
        assert rd.is_duplicate('http://a.com')
    def test_throttle(self):
        at=AutoThrottle()
        assert at.delay==0.1
        at.delay=0.5  # simulate previous adjustments
        assert at.delay>=0.1
    def test_validator_good(self):
        dv=DataValidator()
        score=dv.score({'title':'Hello World','text':'content here','url':'http://x.com','images':['a.jpg']})
        assert score>0.4

class TestBilibiliResolver:
    def test_normal_url(self):
        h='w.__INITIAL_STATE__={"videoData":{"bvid":"BVtest123","cid":1}};'
        r=extract_bilibili_info(h,'https://www.bilibili.com/video/BVtest123')
        assert r['bvid']=='BVtest123'
    def test_url_with_params(self):
        h='w.__INITIAL_STATE__={"videoData":{"bvid":"BVtest123","cid":1}};'
        r=extract_bilibili_info(h,'https://www.bilibili.com/video/BVtest123/?spm_id_from=333.788')
        assert r['bvid']=='BVtest123'
    def test_bangumi_url(self):
        h='w.__INITIAL_STATE__={"videoData":{"cid":1}};'
        r=extract_bilibili_info(h,'https://www.bilibili.com/bangumi/play/ep5127650')
        assert r['bvid']==''
