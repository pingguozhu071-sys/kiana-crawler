"""Kiana 自研增强件（v2.x 逐版演进，非外部代码搬运）

本模块承载爬虫的横向功能件：数据导出（JSONL/CSV/XLSX/SQLite/Markdown）、
磁盘 HTTP 缓存会话、增量检查点、去重/节流、产物保鲜与质量评分——
与任何具体外部框架代码无对应关系；名称与能力面为业界通用做法（数据导出/管道/
看门狗等属常规工程词汇），实现均在本仓库内有版本演进证据（commit 链）。
权威来源声明见 docs/来源与合规声明.md。"""
import asyncio
import json
import logging
import re
import time
import csv
import hashlib
from pathlib import Path
from typing import Optional
from collections import defaultdict
from datetime import datetime

logger=logging.getLogger(__name__)

# ═══ 1. Sitemap Discovery ═══
async def discover_sitemap(domain: str, session=None) -> list[str]:
    """Auto-discover sitemap.xml / robots.txt sitemaps

    [v2.19.7 安全·扫描发现] 两处裸 `session.get(f'https://{domain}...')`：domain 来自
    待爬站（种子或外链域），把它当成 `169.254.169.254` 这类内网地址即直连内网（robots
    /sitemap 的**响应码差异**还能当端口/主机探测器用）。改走 `safe_get` 逐跳校验。"""
    try:
        # [FIXED & MODIFIED] curl_cffi 替换 aiohttp（本机 aiohttp 外网全超时）
        from curl_cffi.requests import AsyncSession
        from .url_utils import safe_get
        urls = []
        close_session = not session
        if close_session:
            session = AsyncSession(timeout=15)
        try:
            # Try robots.txt
            robots_url = f'https://{domain}/robots.txt'
            resp = await safe_get(session, robots_url, timeout=15)
            if resp is not None and resp.status_code == 200:
                text = resp.text
                urls.extend(re.findall(r'^Sitemap:\s*(.+)$', text, re.MULTILINE | re.IGNORECASE))
            # Try common paths
            for path in ['/sitemap.xml', '/sitemap_index.xml', '/sitemap.php', '/wp-sitemap.xml']:
                resp = await safe_get(session, f'https://{domain}{path}', timeout=15)
                if resp is not None and resp.status_code == 200:
                    urls.append(f'https://{domain}{path}')
        finally:
            if close_session:
                await session.close()
        return list(set(urls))
    except Exception:
        return []

async def parse_sitemap(url: str, session=None) -> list[str]:
    """Parse sitemap XML, return all URLs

    [v2.19.7 安全·扫描发现] 原为裸 `session.get(url)`（libcurl 默认跟随最多 30 跳，
    且入口零私网判定）→ `sitemap_discover` 播种链可被"指向内网"的 sitemap 变成跳板。
    现走 `safe_get`：入口校验 + 手动逐跳复检。"""
    try:
        # [FIXED & MODIFIED] curl_cffi 替换 aiohttp（本机 aiohttp 外网全超时）
        from curl_cffi.requests import AsyncSession
        from .url_utils import safe_get
        close_session = not session
        if close_session:
            session = AsyncSession(timeout=15)
        try:
            resp = await safe_get(session, url, timeout=15)
            if resp is None or resp.status_code != 200:
                return []
            text = resp.text
        finally:
            if close_session:
                await session.close()
        # XML regex — handles both <url><loc> and <sitemap><loc>
        locs = re.findall(r'<loc>\s*(https?://[^<]+)\s*</loc>', text)
        return list(set(locs))
    except Exception:
        return []

# ═══ 2. Multi-format Export ═══
class DataExporter:
    """Export crawled data to JSONL, CSV, Excel"""
    def __init__(self, output_dir: Path):
        self.dir=output_dir;self.dir.mkdir(parents=True,exist_ok=True)
        self.buffer=defaultdict(list)

    def add(self, data: dict, domain: str):
        self.buffer[domain].append(data)
        if len(self.buffer[domain])>=50:
            self._flush_domain(domain)

    def add_jsonl(self, domain: str, data: dict):
        """[FIXED & MODIFIED] v2.10.6 B4 单行即时追加（论坛快速路径等绕开缓冲的场景复用，
        落盘格式/位置与 _flush_domain 完全一致——统一走 exporter 单管道）"""
        try:
            safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(domain))[:120] or "unknown"
            d = self.dir / safe
            d.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            with open(d / f'{ts}.jsonl', 'a', encoding='utf-8') as f:
                f.write(json.dumps(data, ensure_ascii=False) + '\n')
        except Exception:
            pass

    def add_markdown(self, domain: str, data: dict):
        """[FIXED & MODIFIED] v2.11 Markdown 快照导出（正文/标题/图片——广撒网产物可读性）"""
        try:
            safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(domain))[:120] or "unknown"
            d = self.dir / safe / "markdown"
            d.mkdir(parents=True, exist_ok=True)
            title = str(data.get("title") or "").strip() or "untitled"
            url = str(data.get("url") or "")
            lines = [f"# {title}", "", f"> 来源: {url}", ""]
            body = str(data.get("text") or "")
            lines.append(body[:200000])
            imgs = data.get("images") or []
            if imgs:
                lines += ["", "## 图片", ""]
                for p in imgs[:50]:
                    lines.append(f"![img]({p})")
            name = re.sub(r'[<>:"/\\|?*]', '_', title)[:60] or "untitled"
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            (d / f"{ts}_{name}.md").write_text("\n".join(lines), encoding="utf-8")
        except Exception:
            pass

    def _flush_domain(self, domain: str):
        rows=self.buffer[domain]
        if not rows:return
        # Windows 安全目录名：域名含端口(:)等非法字符 → 替换
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', domain)[:120] or "unknown"
        d=self.dir/safe;d.mkdir(exist_ok=True)
        ts=datetime.now().strftime('%Y%m%d_%H%M%S')

        # JSONL
        with open(d/f'{ts}.jsonl','a',encoding='utf-8') as f:
            for r in rows:f.write(json.dumps(r,ensure_ascii=False)+'\n')

        # CSV
        keys=sorted(set().union(*(r.keys() for r in rows)))
        csv_path=d/f'{ts}.csv'
        # [FIXED & MODIFIED] v2.16.1 表头重复：f.tell()==0 在追加模式恒真 → 每次 flush
        # 都重写表头；改为按"文件是否存在且 >0 字节"判定。
        # [v2.18 P1-4] BOM 中部污染：原 encoding='utf-8-sig' 在追加模式每次 flush 都
        # 重新插 BOM（同秒二次 flush 时插在文件中部）→ Excel/pandas 首字段静默损坏。
        # 改为 utf-8 + 仅新文件首写时手写一个 BOM。
        is_new = (not csv_path.exists()) or csv_path.stat().st_size == 0
        with open(csv_path,'a',encoding='utf-8',newline='') as f:
            w=csv.DictWriter(f,fieldnames=keys[:20],extrasaction='ignore')
            if is_new:
                f.write('\ufeff')
                w.writeheader()
            for r in rows:w.writerow(r)

        self.buffer[domain]=[]

    def flush_all(self):
        for domain in list(self.buffer.keys()):
            self._flush_domain(domain)

    def export_xlsx(self, path, rows: list[dict]):
        """[FIXED & MODIFIED] v2.13 阶段4 xlsx 导出：按域分 sheet + 汇总页 + 自适应列宽。
        rows 为已摊平的 dict 列表；openpyxl 惰性导入（缺依赖仅影响 xlsx 通道）。
        返回写入行数；失败返回 -1。"""
        try:
            from openpyxl import Workbook
            from openpyxl.utils import get_column_letter
        except ImportError:
            logger.warning("xlsx 导出需要 openpyxl（pip install openpyxl）")
            return -1
        try:
            # [v2.18 P3-6] rows 形态守卫：传字符串/单 dict 时给明确错误而非笼统 -1
            if rows is None:
                return 0
            if isinstance(rows, dict):
                rows = [rows]
            elif isinstance(rows, str) or not hasattr(rows, "__iter__"):
                raise TypeError(f"export_xlsx rows 应为 dict 列表，收到 {type(rows).__name__}")
            if not rows:
                return 0
            wb = Workbook()
            # 汇总页
            ws = wb.active
            ws.title = "汇总"
            ws.append(["域名", "条数"])
            by_domain = defaultdict(list)
            for r in rows:
                dom = str(r.get("url", "")).split("/")[2] if "://" in str(r.get("url", "")) else str(r.get("url", ""))[:30] or "unknown"
                by_domain[dom].append(r)
            for dom in sorted(by_domain):
                ws.append([dom, len(by_domain[dom])])
            # 每域一 sheet（Windows sheet 名 31 字符上限 + 非法字符清洗）
            for dom, dom_rows in sorted(by_domain.items()):
                safe = re.sub(r'[<>:"/\\|?*]', "_", dom)[:28] or "sheet"
                sheet = wb.create_sheet(safe)
                keys = list(dict.fromkeys(k for r in dom_rows for k in r.keys()))[:24]
                sheet.append(keys)
                for r in dom_rows:
                    sheet.append([str(r.get(k, ""))[:2000] if not isinstance(r.get(k), (int, float)) else r.get(k) for k in keys])
                # 自适应列宽（取每列前 200 行最长值，上限 60）
                for ci, k in enumerate(keys, 1):
                    width = max((len(str(r.get(k, ""))) for r in dom_rows[:200]), default=8)
                    sheet.column_dimensions[get_column_letter(ci)].width = min(max(width + 2, 10), 60)
                sheet.freeze_panes = "A2"
            wb.save(str(path))
            return len(rows)
        except Exception as e:
            logger.error(f"xlsx 导出失败: {e}")
            return -1

    def export_sqlite(self, path, rows: list[dict]):
        """[v2.17 4.1] sqlite 导出：media/scrape 两表（本地方便查询/跨域汇总——
        sqlite3 标准库，无新依赖）。

        - media:  按 media_id 幂等 upsert（media_id 缺省回退 video_url/url——幂等不重复）；
        - scrape: 按 url 幂等 upsert（重跑不重复入库）；
        - 分类规则：含媒体特征键（media_id/video_url/stream_url/quality）→ media 表，
          其余（文章/论坛页）→ scrape 表，列表/图片存 JSON 文本。
        返回写入行数；失败返回 -1。"""
        try:
            import sqlite3 as _sq
            if not rows:
                return 0
            _p = str(path)
            conn = _sq.connect(_p)
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS media (
                    media_id TEXT PRIMARY KEY, domain TEXT, source TEXT, title TEXT,
                    url TEXT, stream_url TEXT, quality TEXT,
                    downloaded INTEGER DEFAULT 0, ts REAL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS scrape (
                    url TEXT PRIMARY KEY, domain TEXT, title TEXT, text TEXT,
                    images TEXT, ts REAL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS idx_media_domain ON media(domain);
                CREATE INDEX IF NOT EXISTS idx_scrape_domain ON scrape(domain);
            """)
            med_rows, scr_rows = [], []
            _now = time.time()
            for r in rows:
                if not isinstance(r, dict):
                    continue
                dom = str(r.get("domain") or "")
                url = str(r.get("url") or r.get("page_url") or "")
                if not dom and url:
                    try:
                        from urllib.parse import urlparse
                        dom = urlparse(url).hostname or ""
                    except Exception:
                        dom = ""
                is_media = bool(r.get("media_id") or r.get("video_url")
                                or r.get("stream_url") or r.get("quality")
                                or r.get("duration"))
                if is_media:
                    mid = str(r.get("media_id") or r.get("video_url") or url or "unknown")[:512]
                    # [v2.18 P3-8] ts 坏值（"abc"等）只降级为当前时间，不再让整个导出归零
                    try:
                        _ts = float(r.get("ts") or _now)
                    except (TypeError, ValueError):
                        _ts = _now
                    med_rows.append((
                        mid, dom, str(r.get("source") or dom),
                        str(r.get("title") or "")[:500],
                        url,
                        str(r.get("stream_url") or r.get("video_url") or "")[:1000],
                        str(r.get("quality") or r.get("resolution") or "")[:32],
                        1 if r.get("downloaded") else 0,
                        _ts,
                    ))
                else:
                    scr_rows.append((
                        url, dom, str(r.get("title") or "")[:500],
                        str(r.get("text") or "")[:100000],
                        json.dumps(r.get("images") or [], ensure_ascii=False),
                        float(r.get("ts") or _now),
                    ))
            if med_rows:
                conn.executemany(
                    "INSERT INTO media (media_id, domain, source, title, url, stream_url, "
                    "quality, downloaded, ts) VALUES (?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(media_id) DO UPDATE SET domain=excluded.domain, "
                    "source=excluded.source, title=excluded.title, url=excluded.url, "
                    "stream_url=excluded.stream_url, quality=excluded.quality, "
                    "downloaded=excluded.downloaded, ts=excluded.ts", med_rows)
            if scr_rows:
                conn.executemany(
                    "INSERT INTO scrape (url, domain, title, text, images, ts) "
                    "VALUES (?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET "
                    "domain=excluded.domain, title=excluded.title, text=excluded.text, "
                    "images=excluded.images, ts=excluded.ts", scr_rows)
            # [v2.17 2-A] FTS5 全文检索：导出快照即建索引（零新依赖——sqlite 内置；
            # 每次导出重建=快照语义；查法 SELECT ... FROM scrape_fts WHERE scrape_fts MATCH ?）
            try:
                conn.execute("DROP TABLE IF EXISTS scrape_fts")
                # [v2.18 P2-8] trigram 分词：unicode61 不切中文 → 连续中文 MATCH 命中率 0
                # （旧测试样本带空格假绿）。trigram 需 SQLite ≥3.34，无则退回默认分词。
                try:
                    conn.execute("""CREATE VIRTUAL TABLE scrape_fts
                                    USING fts5(title, text, content='scrape',
                                               content_rowid='rowid', tokenize='trigram')""")
                except Exception:
                    conn.execute("""CREATE VIRTUAL TABLE scrape_fts
                                    USING fts5(title, text, content='scrape',
                                               content_rowid='rowid')""")
                conn.execute("INSERT INTO scrape_fts(scrape_fts) VALUES('rebuild')")
            except Exception as e:
                logger.debug(f"FTS5 索引跳过（当前 sqlite 无 FTS5 支持则无索引）: {e}")
            conn.commit()
            conn.close()
            return len(rows)
        except Exception as e:
            logger.error(f"sqlite 导出失败: {e}")
            return -1

# ═══ 3. Incremental/Resume Crawling ═══
class CrawlCheckpoint:
    """Save/restore crawl state for resume"""
    def __init__(self, path: Path):
        self.path=path

    def save(self, state: dict):
        # [FIXED & MODIFIED] v2.10.5 不污染调用方输入（原直接改 state 加 _timestamp）
        _s = dict(state)
        _s['_timestamp']=time.time()
        self.path.write_text(json.dumps(_s,ensure_ascii=False),encoding='utf-8')

    def load(self) -> Optional[dict]:
        if self.path.exists():
            return json.loads(self.path.read_text(encoding='utf-8'))
        return None

# ═══ 4. Cookie Persistence ═══
class CookieStore:
    """Persist cookies across sessions for authenticated crawling"""
    def __init__(self, path: Path):
        self.path=path

    def save(self, cookies: dict):
        self.path.write_text(json.dumps(cookies),encoding='utf-8')

    def load(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text(encoding='utf-8'))
        return {}

# ═══ 5. Request Dedup (Bloom-like filter) ═══
class RequestDedup:
    """Fast in-memory URL deduplication"""
    def __init__(self, size: int = 1_000_000):
        self.seen=set()
        self.max_size=size

    def is_duplicate(self, url: str) -> bool:
        h=hashlib.md5(url.encode()).hexdigest()
        if h in self.seen:return True
        if len(self.seen)>=self.max_size:self.seen.clear()
        self.seen.add(h)
        return False

# ═══ 6. Auto-Throttle (backpressure) ═══
class AutoThrottle:
    """Adaptive rate limiter based on response latency and errors"""
    def __init__(self, min_delay: float = 0.1, max_delay: float = 5.0, target_latency: float = 2.0):
        self.delay=min_delay
        self.min_delay=min_delay
        self.max_delay=max_delay
        self.target=target_latency
        self._last_adjust=time.monotonic()

    async def wait(self):
        await asyncio.sleep(self.delay)

    def adjust(self, latency: float, is_error: bool = False):
        """PID-like: too fast → slow down, errors → back off"""
        if time.monotonic()-self._last_adjust<1.0:return
        self._last_adjust=time.monotonic()
        if is_error:
            self.delay=min(self.max_delay,self.delay*2)
        elif latency>self.target:
            self.delay=min(self.max_delay,self.delay*1.2)
        else:
            self.delay=max(self.min_delay,self.delay*0.9)

# ═══ 8. Task Dir Retention（v2.11 产物保鲜）═══
def prune_task_dirs(base, keep_days: int = 7, max_gb: float = 50.0) -> int:
    """清理过期/超量的历史任务目录（cli_*），返回删除数。

    规则：先删 mtime 超过 keep_days 天的；若总占用仍超 max_gb，按最旧继续删。
    正在运行的任务目录（<2h 新建）永不触碰。默认保留 7 天 / 50GB（config 可调）。"""
    try:
        base = Path(base)
        if not base.is_dir():
            return 0
        import time as _time
        now = _time.time()
        cutoff = now - max(int(keep_days), 0) * 86400
        protected_cutoff = now - 7200  # 2 小时内新建 = 可能在跑
        dirs = []
        for d in base.glob("cli_*"):
            try:
                if not d.is_dir():
                    continue
                mt = d.stat().st_mtime
                if mt > protected_cutoff:
                    continue  # 可能正在运行
                total = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
                dirs.append((mt, total, d))
            except Exception:
                continue
        removed = 0
        # 1) 过期清理（最旧先删）
        for mt, total, d in sorted(dirs, key=lambda t: t[0]):
            if mt <= cutoff:
                try:
                    import shutil
                    shutil.rmtree(d, ignore_errors=True)
                    removed += 1
                except Exception:
                    pass
        # 2) 容量兜底（仍超 max_gb → 继续删最旧）
        def _total_used():
            return sum(t for mt, t, d in dirs if d.exists()) / (1024 ** 3)
        budget = max(float(max_gb), 0)
        if budget > 0:
            for mt, total, d in sorted(dirs, key=lambda t: t[0]):
                if _total_used() <= budget:
                    break
                if d.exists():
                    try:
                        import shutil
                        shutil.rmtree(d, ignore_errors=True)
                        removed += 1
                    except Exception:
                        pass
        if removed:
            logger.info(f"产物保鲜: 清理 {removed} 个过期任务目录（保留 {keep_days} 天/{max_gb}GB）")
        return removed
    except Exception:
        return 0

# ═══ 7. Data Validation Pipeline ═══
class DataValidator:
    """Validate extracted data completeness"""
    @staticmethod
    def score(data: dict) -> float:
        """Score page data quality 0-1"""
        points=0;total=0
        for field,weight in [('title',3),('description',2),('text',5),
                              ('url',1),('images',1),('videos',2)]:
            total+=weight
            val=data.get(field)
            if val:
                if isinstance(val,str):points+=weight if len(val)>10 else weight//2
                elif isinstance(val,(list,dict)):points+=weight if len(val)>0 else 0
        return points/total if total else 0
