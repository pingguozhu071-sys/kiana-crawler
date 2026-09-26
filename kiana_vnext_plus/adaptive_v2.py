"""Adaptive Controller v2 — Multi-factor + Data Cleaning Pipeline"""
import asyncio
import logging
import re
from collections import defaultdict
from typing import Optional

logger=logging.getLogger(__name__)

# ═══ Adaptive v2: multi-factor self-tuning ═══
class AdaptiveControllerV2:
    """Multi-factor adaptive controller: response_time + error_rate + ban_score + content_quality

    增强：可绑定 ConcurrencyController（concurrency 参数），调优结果实时作用于全局并发信号量。
    兼容 DynamicPressureController 调用接口：run() 无参、stop()。
    """

    def __init__(self, min_con: int = 10, max_con: int = 200,
                 concurrency: Optional["object"] = None, config: Optional[dict] = None,
                 solver: Optional["object"] = None):
        self.concurrency_value = min_con
        self.min_con = min_con
        self.max_con = max_con
        self._concurrency_ctrl = concurrency   # 绑定的 ConcurrencyController（可选）
        self._config = config or {}
        self._solver = solver
        self._stats = defaultdict(lambda: {'ok': 0, 'err': 0, 'latency': [], 'bans': 0, 'quality': []})
        self._running = False

    async def run(self, period: float = 5.0):
        self._running = True
        while self._running:
            await asyncio.sleep(period)
            self._tune()
            self._pressure_tune()

    def _pressure_tune(self):
        """系统压力分级控制（原 DynamicPressureController 逻辑并入）：
        监控内存+CPU，按 green/yellow/orange/critical 四档调整并发"""
        cfg = self._config
        if not cfg:
            return
        if not cfg.get("pressure_enabled", True):
            return
        try:
            import psutil
            mem = psutil.virtual_memory().percent
            # [FIXED & MODIFIED] v2.14 非阻塞采样（原 interval=0.1 同步睡 100ms，
            # 每 5s 冻结事件循环一次 → 改 None 读瞬时值，配合连续调用差值）
            cpu = psutil.cpu_percent(interval=None)
        except Exception:
            return
        green = cfg.get("mem_threshold_green", 90.0)
        yellow = cfg.get("mem_threshold_yellow", 93.0)
        orange = cfg.get("mem_threshold_orange", 94.0)
        critical = cfg.get("mem_threshold_critical", 97.0)
        # [FIXED & MODIFIED] F7：CPU 阈值改读配置（原硬编码 95/85/70 使 config 的
        # cpu_threshold_* 永不生效——主人改配置毫无作用）
        cpu_critical = cfg.get("cpu_threshold_critical", 95.0)
        cpu_yellow = cfg.get("cpu_threshold_yellow", 85.0)
        cpu_green = cfg.get("cpu_threshold_green", 70.0)
        current = self.concurrency_value
        if mem > critical:
            new_max = max(self.min_con, int(current * 0.5))
            self.concurrency_value = new_max
            self._sync_concurrency()
            # [FIXED & MODIFIED] v2.10.6 D3 双重启竞态：原直接 solver.restart()（无锁）会与
            # solver_engine 内存监控的 _safe_restart（带锁）并发重启浏览器 → 双倍内存峰值（OOM 隐患）。
            # 改走 _safe_restart（带锁+存活双检），若已有重启在途自然跳过。
            if self._solver and hasattr(self._solver, '_safe_restart'):
                try:
                    import asyncio as _a
                    _a.get_event_loop().create_task(self._solver._safe_restart())
                except Exception:
                    pass
            elif self._solver and hasattr(self._solver, 'restart'):
                try:
                    import asyncio as _a
                    _a.get_event_loop().create_task(self._solver.restart())
                except Exception:
                    pass
        elif mem > orange or cpu > cpu_critical:
            self.concurrency_value = max(self.min_con, int(current * (1 - 0.2)))
            self._sync_concurrency()
        elif mem > yellow or cpu > cpu_yellow:
            self.concurrency_value = max(self.min_con, int(current * (1 - 0.1)))
            self._sync_concurrency()
        elif mem < green and cpu < cpu_green:
            self.concurrency_value = min(self.max_con, int(current * (1 + 0.05)))
            self._sync_concurrency()

    def _tune(self):
        total_ok = sum(s['ok'] for s in self._stats.values())
        total_err = sum(s['err'] for s in self._stats.values())
        total = total_ok + total_err
        if total == 0:
            return
        err_rate = total_err / total
        avg_latency = 0
        lat_samples = [l for s in self._stats.values() for l in s['latency'][-20:]]
        if lat_samples:
            avg_latency = sum(lat_samples) / len(lat_samples)
        total_bans = sum(s['bans'] for s in self._stats.values())

        # Decision matrix
        if err_rate > 0.3 or total_bans > 5:
            self.concurrency_value = max(self.min_con, int(self.concurrency_value * 0.6))
        elif avg_latency > 5.0:
            self.concurrency_value = max(self.min_con, int(self.concurrency_value * 0.8))
        elif err_rate < 0.05 and avg_latency < 2.0:
            self.concurrency_value = min(self.max_con, int(self.concurrency_value * 1.15))
        else:
            self.concurrency_value = min(self.max_con, int(self.concurrency_value * 1.05))
        self._sync_concurrency()
        self._reset_stats()

    def _sync_concurrency(self):
        """将调优结果同步到绑定的全局并发控制器"""
        if self._concurrency_ctrl is not None and hasattr(self._concurrency_ctrl, 'adjust_global'):
            try:
                asyncio.get_event_loop().create_task(
                    self._concurrency_ctrl.adjust_global(int(self.concurrency_value), source="adaptive_v2"))
            except Exception:
                pass

    def record(self, domain: str, ok: bool, latency: float = 0, ban_detected: bool = False, quality: float = 0):
        s = self._stats[domain]
        if ok:
            s['ok'] += 1
        else:
            s['err'] += 1
        if latency > 0:
            s['latency'].append(latency)
        if ban_detected:
            s['bans'] += 1
        if quality > 0:
            s['quality'].append(quality)

    def _reset_stats(self):
        for d in list(self._stats.keys()):
            s = self._stats[d]
            s['ok'] = max(0, s['ok'] - 10)
            s['err'] = max(0, s['err'] - 5)
            s['bans'] = max(0, s['bans'] - 5)
            s['latency'] = s['latency'][-30:]
            s['quality'] = s['quality'][-30:]

    def stop(self):
        self._running = False


# ═══ 数据清洗流水线（URL 归一 → 去重 → 质量过滤 → 内容净化）═══
class DataCleaner:
    """URL normalization + dedup + quality filtering + content sanitization"""

    @staticmethod
    def normalize_url(url: str) -> str:
        """Normalize URL: lowercase host, remove fragments, sort query params"""
        from urllib.parse import urlparse,urlunparse,parse_qs,urlencode
        try:
            p=urlparse(url)
            netloc=p.netloc.lower()
            # Sort query params
            qs=parse_qs(p.query,keep_blank_values=True)
            query=urlencode(sorted(qs.items()),doseq=True)
            return urlunparse((p.scheme,netloc,p.path,p.params,query,''))
        except Exception:return url

    @staticmethod
    def clean_html(html: str) -> str:
        """Remove noise from HTML"""
        # Remove scripts and styles
        # [v2.19 修复·测试发现] 原正则写成 `?</\\1>`——在 **raw string** 里 `\\1` 被正则引擎
        # 解读为「转义反斜杠 + 字面 1」，**不是反向引用** → 该行永远匹配不到任何东西，
        # script/style/noscript/iframe/svg 移除**完全失效**（仅注释移除生效）。
        # 该函数此前零测试覆盖，故长期未被发现。改为正确的反向引用 `\1`。
        html=re.sub(r'<(script|style|noscript|iframe|svg)[^>]*>.*?</\1>','',html,flags=re.DOTALL|re.IGNORECASE)
        # Remove comments
        html=re.sub(r'<!--.*?-->','',html,flags=re.DOTALL)
        # Remove excessive whitespace
        html=re.sub(r'\n\s*\n','\n',html)
        return html

    @staticmethod
    def filter_junk_url(url: str, domain: str = "") -> bool:
        """Return True if URL is junk (tracking, ads, etc.)"""
        junk_patterns=[
            'doubleclick','googlesyndication','google-analytics','facebook.com/tr',
            'pixel.','tracker','/beacon','/analytics/','utm_','gclid=','fbclid=',
            '.gif?','spacer','1x1','pixel.gif','pixel.png',
        ]
        normalized=url.lower()
        return any(p in normalized for p in junk_patterns)

    @staticmethod
    def extract_structured(text: str) -> dict:
        """Extract structured data patterns from text"""
        result={'emails':[],'phones':[],'dates':[],'social_links':[]}
        # [v2.19 质量修复] 原此处自维护一份**无量词上界**的旧版邮箱正则（即 v2.18 已判定
        # 为 O(n²) 的那版，1MB 长串可卡死 worker）——改复用 sanitizer 的有上界版本，
        # 消除"已修 bug 在别处复存活"的分叉。
        try:
            from .sanitizer import find_emails as _find_emails
            result['emails']=list(set(_find_emails(text)))[:50]
        except Exception:
            result['emails']=[]
        result['phones']=list(set(re.findall(r'(?:\+86[\s\-]?)?1[3-9]\d{9}',text)))[:5]
        result['dates']=list(set(re.findall(r'\d{4}[-/]\d{1,2}[-/]\d{1,2}',text)))[:10]
        return result
