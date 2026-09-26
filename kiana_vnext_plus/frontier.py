import time
import asyncio
import sqlite3
import aiosqlite
import logging
from .url_utils import url_hash, normalize_url, extract_domain

logger = logging.getLogger(__name__)

# [v2.17 E-P1-4] 爬行策略：ORDER BY 三档（bfs 默认=现状；dfs 后入先出；bff 按 priority 降序，
# priority 已表达相关度——三档只改排序，不动租约/CAS 语义）
_ORDER_BY = {
    "bfs": "ORDER BY priority, scheduled_at",
    "dfs": "ORDER BY priority, scheduled_at DESC",
    "bff": "ORDER BY priority DESC, scheduled_at",
}
LEASE_TIMEOUT = 300


class FrontierDB:
    """SQLite 前沿队列，批量写入，支持租约与重试"""

    def __init__(self, db_path):
        self.db_path = db_path
        self._write_queue = asyncio.Queue()
        self._read_conn = None
        self._flush_task = None
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA cache_size=-131072")   # 128MB (15.4GB 调低)
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("PRAGMA mmap_size=268435456")    # 256MB (15.4GB 调低)
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("""CREATE TABLE IF NOT EXISTS frontier (
                url_hash TEXT PRIMARY KEY, normalized_url TEXT, domain TEXT, depth INTEGER DEFAULT 0,
                priority INTEGER DEFAULT 5, status TEXT DEFAULT 'pending', scheduled_at REAL,
                retry_count INTEGER DEFAULT 0, max_retries INTEGER DEFAULT 3, leased_at REAL,
                worker_id TEXT, created_at REAL DEFAULT (strftime('%s','now')))""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON frontier(status, scheduled_at)")
            conn.execute("""CREATE TABLE IF NOT EXISTS pages (
                url_hash TEXT PRIMARY KEY, status_code INTEGER, content_length INTEGER,
                fetch_time REAL, headers TEXT, content_hash TEXT, simhash INTEGER, duplicate_of TEXT)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS extracted (url_hash TEXT PRIMARY KEY, data_json TEXT)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS errors (url_hash TEXT, error_type TEXT, error_message TEXT, timestamp REAL,
                platform TEXT DEFAULT '', code TEXT DEFAULT '')""")
            conn.execute("""CREATE TABLE IF NOT EXISTS video_downloads (
                video_url TEXT PRIMARY KEY, domain TEXT, status TEXT DEFAULT 'pending',
                file_path TEXT, progress REAL, fail_count INTEGER DEFAULT 0, created_at REAL,
                file_size REAL DEFAULT 0)""")
            # [FIXED & MODIFIED] v2.13 阶段2 冷却/风控等级持久化（原 defense 纯内存重启全丢）
            conn.execute("""CREATE TABLE IF NOT EXISTS cooldowns (
                domain TEXT PRIMARY KEY, until_epoch REAL, tier INTEGER, updated_at REAL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value TEXT)""")
            # [FIXED & MODIFIED] v2.13 阶段5 Cookie 弹药库表（state_blob 由 cookie_armory
            # 用 Fernet 加密——明文 cookies 绝不入库；实际读写走 armory 独立连接）
            conn.execute("""CREATE TABLE IF NOT EXISTS accounts (
                site TEXT, name TEXT, state_blob TEXT, health_score REAL DEFAULT 1.0,
                cooldown_until REAL DEFAULT 0, success_count INTEGER DEFAULT 0,
                fail_count INTEGER DEFAULT 0, updated_at REAL,
                PRIMARY KEY (site, name))""")
            # [FIXED & MODIFIED] v2.14 阶段2 索引补齐（深查：count_done_by_domain 每任务
            # 全表扫描 O(N²)、video_downloads 每 5s 全表轮询、errors 无索引全表排序）
            conn.execute("CREATE INDEX IF NOT EXISTS idx_frontier_domain_status ON frontier(domain, status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_video_status ON video_downloads(status, domain)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_errors_ts ON errors(timestamp)")
            self._migrate(conn)
            conn.commit()

    @staticmethod
    def _migrate(conn):
        """[FIXED & MODIFIED] v2.14 阶段2 版本化迁移：CREATE IF NOT EXISTS 只能补表不能
        补列——v2.10 老库的 pages 表没有 duplicate_of 列，v2.11+ 的 mark_duplicate
        UPDATE 会报错且 flusher 整批丢弃。按 user_version 跑幂等 ALTER。
        [v2.16 M5] v2：frontier 加 parent_hash（血缘审计/任务树溯源）。"""
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(pages)")}
            if "duplicate_of" not in cols:
                conn.execute("ALTER TABLE pages ADD COLUMN duplicate_of TEXT")
            if "simhash" not in cols:
                conn.execute("ALTER TABLE pages ADD COLUMN simhash INTEGER")
            conn.execute("PRAGMA user_version = 1")
            logger.info("DB 迁移 v0→v1：pages 表补 duplicate_of/simhash 列")
        if version < 2:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(frontier)")}
            if "parent_hash" not in cols:
                conn.execute("ALTER TABLE frontier ADD COLUMN parent_hash TEXT")
            conn.execute("PRAGMA user_version = 2")
            logger.info("DB 迁移 v1→v2：frontier 补 parent_hash（血缘审计）")
        if version < 3:
            # [v2.17 4.3] 错误表补 platform/code（"哪个平台什么码"审计）；视频表补 file_size
            # （stats.jsonl bytes 统计源，避免 SUM(file_path 存在性)式昂贵扫描）
            _err_cols = {r[1] for r in conn.execute("PRAGMA table_info(errors)")}
            if "platform" not in _err_cols:
                conn.execute("ALTER TABLE errors ADD COLUMN platform TEXT DEFAULT ''")
            if "code" not in _err_cols:
                conn.execute("ALTER TABLE errors ADD COLUMN code TEXT DEFAULT ''")
            _vid_cols = {r[1] for r in conn.execute("PRAGMA table_info(video_downloads)")}
            if "file_size" not in _vid_cols:
                conn.execute("ALTER TABLE video_downloads ADD COLUMN file_size REAL DEFAULT 0")
            conn.execute("PRAGMA user_version = 3")
            logger.info("DB 迁移 v2→v3：errors 补 platform/code；video_downloads 补 file_size")
        if version < 4:
            # [v2.18 P1-3] 租约到期时间独立列：旧实现只有 leased_at 一个时间戳，
            # 单页最坏处理时长（human_delay+渲染+媒体）超 LEASE_TIMEOUT 即被回收重发
            # → 同 URL 双协程并发处理（媒体/导出双份）。心跳续期不能改 leased_at
            # （会破坏 mark_done_checked 的 CAS 闭环）——加 lease_expires 专列。
            cols = {r[1] for r in conn.execute("PRAGMA table_info(frontier)")}
            if "lease_expires" not in cols:
                conn.execute("ALTER TABLE frontier ADD COLUMN lease_expires REAL")
                # 存量租约回填：按旧语义（leased_at + 300s）延续
                conn.execute(
                    "UPDATE frontier SET lease_expires = leased_at + ? "
                    "WHERE status='leased' AND lease_expires IS NULL", (LEASE_TIMEOUT,))
            conn.execute("PRAGMA user_version = 4")
            logger.info("DB 迁移 v3→v4：frontier 补 lease_expires（租约心跳续期）")
        if version < 5:
            # [v2.19 P1] 连续限流计数：throttled 路径**刻意不递增 retry_count**（"限流≠重试"
            # 是明确设计），但长期 429 的域会让任务永驻 retry → crawler 退出条件
            # `pending+retry == 0` 永不成立（主循环不退出）。加独立计数做兜底：
            # 连续限流达上限即转 dead，任务可正常收尾；成功打卡时清零。
            cols = {r[1] for r in conn.execute("PRAGMA table_info(frontier)")}
            if "throttle_count" not in cols:
                conn.execute("ALTER TABLE frontier ADD COLUMN throttle_count INTEGER DEFAULT 0")
            conn.execute("PRAGMA user_version = 5")
            logger.info("DB 迁移 v4→v5：frontier 补 throttle_count（连续限流兜底）")

    async def init_async(self):
        self._read_conn = await aiosqlite.connect(self.db_path)
        self._read_conn.row_factory = aiosqlite.Row
        # [v2.19 P1] _read_conn 也要 busy_timeout：flusher 持写锁时，本连接上的
        # CAS/读取会抛 "database is locked"，而 mark_done_checked 把任何异常都当
        # "租约已易主"→ 成功页被静默丢弃且不标 done（全仓仅 3 处连接设了该 PRAGMA）。
        try:
            await self._read_conn.execute("PRAGMA busy_timeout=30000")
        except Exception:
            pass
        self._flush_task = asyncio.create_task(self._batch_flusher())
        # [v2.16 M6] errors 生命周期：启动清理过期行（默认保留 30 天，可配）
        try:
            _keep = float(getattr(self, "errors_retention_days", 30) or 30)
            await self._write_queue.put((
                "DELETE FROM errors WHERE timestamp < ?", (time.time() - _keep * 86400,)))
        except Exception:
            pass

    async def _batch_flusher(self):
        while True:
            batch = []
            try:
                item = await self._write_queue.get()
                if item is None:
                    # 修复：None 哨兵也需要 task_done()，否则 flush() 在 close() 后调用会永久死锁
                    self._write_queue.task_done()
                    break
                batch.append(item)
                while not self._write_queue.empty() and len(batch) < 1000:
                    batch.append(self._write_queue.get_nowait())

                queries = {}
                for sql, params in batch:
                    queries.setdefault(sql, []).append(params)

                async with aiosqlite.connect(self.db_path) as db:
                    await db.execute("PRAGMA busy_timeout=30000")
                    await db.execute("BEGIN IMMEDIATE")
                    for sql, params_list in queries.items():
                        await db.executemany(sql, params_list)
                    await db.commit()

            except Exception as e:
                logger.error(f"DB Flush Error: {e}")
                # [FIXED & MODIFIED] v2.14 死信落盘：原整批静默丢弃（数据无声丢失）——
                # 失败批次原样追加到 {db}.deadletter.jsonl 供人工回放/诊断
                try:
                    import json as _json
                    with open(str(self.db_path) + ".deadletter.jsonl", "a",
                              encoding="utf-8") as f:
                        for sql, params in batch:
                            f.write(_json.dumps({"sql": sql, "params": params},
                                                ensure_ascii=False, default=str) + "\n")
                except Exception:
                    pass
                # 修复：异常时不丢失 task_done，否则 join() 会永久阻塞
                await asyncio.sleep(0.5)
            finally:
                # 修复：无论成功或失败都调用 task_done，避免队列死锁
                for _ in batch:
                    self._write_queue.task_done()

    async def push(self, url, depth=0, priority=5, force=False, parent_hash=None):
        # [v2.19.7 安全·扫描发现·**刻意不脱敏**] normalized_url 是"稍后要再请求一次"的
        # 功能数据（page_processor 直接拿它发请求）。若在此跑 sanitize_url 把 ?token=/
        # session= 的值抹成 [REDACTED]，续爬会 403 → 任务永久卡死。敏感 URL 的收口放在
        # **导出侧**（sanitizer.sanitize_record，extracted/jsonl/csv/markdown 副本），
        # 且本表不参与任何导出（cli export 只读 extracted）。请勿"顺手"在这里加脱敏。
        norm = normalize_url(url)
        uh = url_hash(url)
        domain = extract_domain(url)
        if force:
            # [FIXED & MODIFIED] 种子强制重新爬：upsert 重置旧 done/retry 状态为 pending
            # （用户明确输入的种子 URL 无视历史去重——0 pages 根因：INSERT OR IGNORE 吞掉 done 种子）
            await self._write_queue.put((
                "INSERT INTO frontier (url_hash, normalized_url, domain, depth, priority, status, scheduled_at, parent_hash) "
                "VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(url_hash) DO UPDATE SET status='pending', retry_count=0, depth=?, priority=?, scheduled_at=?",
                (uh, norm, domain, depth, priority, 'pending', time.time(), parent_hash, depth, priority, time.time())
            ))
        else:
            await self._write_queue.put((
                "INSERT OR IGNORE INTO frontier (url_hash, normalized_url, domain, depth, priority, status, scheduled_at, parent_hash) VALUES (?,?,?,?,?,?,?,?)",
                (uh, norm, domain, depth, priority, 'pending', time.time(), parent_hash)
            ))

    async def write_page(self, url_hash, status_code, content_length, fetch_time, headers, content_hash, simhash=None):
        # [FIXED & MODIFIED] v2.11 源头钳制：simhash 63 位（SQLite INTEGER 上限 2^63-1；
        # 溢出行会让 flush 的 executemany 整批回滚——frontier 数据静默丢失）
        if simhash is not None:
            simhash = int(simhash) & ((1 << 63) - 1)
        await self._write_queue.put((
            "INSERT OR REPLACE INTO pages (url_hash, status_code, content_length, fetch_time, headers, content_hash, simhash) VALUES (?,?,?,?,?,?,?)",
            (url_hash, status_code, content_length, fetch_time, headers, content_hash, simhash)
        ))

    async def mark_duplicate(self, url_hash, duplicate_of):
        """[FIXED & MODIFIED] v2.11 内容级去重：标记本页为 duplicate_of 的近似重复（SimHash）"""
        await self._write_queue.put((
            "UPDATE pages SET duplicate_of=? WHERE url_hash=?", (duplicate_of, url_hash)))

    # ═══ [FIXED & MODIFIED] v2.13 阶段2 域名冷却持久化（defense 韧性）═══
    async def set_domain_cooldown(self, domain, until_epoch: float, tier: int):
        await self._write_queue.put((
            "INSERT OR REPLACE INTO cooldowns (domain, until_epoch, tier, updated_at) VALUES (?,?,?,?)",
            (domain, float(until_epoch), int(tier), time.time())))

    async def clear_domain_cooldown(self, domain):
        await self._write_queue.put(("DELETE FROM cooldowns WHERE domain=?", (domain,)))

    async def load_active_cooldowns(self) -> dict:
        """读未过期的域名冷却 {domain: (until_epoch, tier)}，顺带清过期行"""
        out = {}
        if self._read_conn is None:
            return out
        now = time.time()
        try:
            async with self._read_conn.execute(
                    "SELECT domain, until_epoch, tier FROM cooldowns WHERE until_epoch > ?",
                    (now,)) as cur:
                async for row in cur:
                    out[row[0]] = (float(row[1]), int(row[2]))
            await self._write_queue.put(
                ("DELETE FROM cooldowns WHERE until_epoch <= ?", (now,)))
        except Exception as e:
            logger.debug(f"load_active_cooldowns failed: {e}")
        return out

    async def set_setting(self, key: str, value: str):
        await self._write_queue.put((
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, str(value))))

    async def get_setting(self, key: str) -> str | None:
        if self._read_conn is None:
            return None
        async with self._read_conn.execute(
                "SELECT value FROM settings WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def mark_done(self, url_hash, leased_at=None):
        """标记任务完成。

        [v2.19 P1] 新增可选 `leased_at`：
        - 传入 → 走 **CAS**（内部委托 mark_done_checked），租约已易主则不改状态，
          返回 False。调用方应据此丢弃结果（防旧持有者把新持有者的 done 打回重爬）。
        - 不传 → 保持旧语义（无条件置 done，入写队列异步落库），返回 True。
          供"本任务无租约"（leased_at 为 None）与内部维护路径使用。
        """
        if leased_at is None:
            await self._write_queue.put(
                ("UPDATE frontier SET status='done', throttle_count=0 WHERE url_hash=?", (url_hash,)))
            return True
        return await self.mark_done_checked(url_hash, leased_at)

    async def heartbeat_lease(self, url_hash, leased_at) -> bool:
        """[v2.18 P1-3] 租约心跳续期：处理中周期性延长 lease_expires，防止单页
        最坏处理时长（human_delay+渲染+媒体）超 LEASE_TIMEOUT(300s) 被回收重发
        → 同 URL 双协程并发处理（媒体/导出双份）。
        CAS 校验 leased_at（只认当前持有者）；不改 leased_at 本身——mark_done_checked
        的 CAS 闭环不受影响。租约已易主返回 False（调用方停止续租）。"""
        if self._read_conn is None:
            return False
        try:
            cur = await self._read_conn.execute(
                "UPDATE frontier SET lease_expires=? WHERE url_hash=? "
                "AND status='leased' AND leased_at=?",
                (time.time() + LEASE_TIMEOUT, url_hash, leased_at))
            await self._read_conn.commit()
            return (cur.rowcount or 0) > 0
        except Exception:
            return False

    async def mark_done_checked(self, url_hash, leased_at):
        """[FIXED & MODIFIED] v2.14 闭环：CAS 校验 leased_at，租约被别人接手则不改状态。

        **返回三态**（[v2.19.6 修复·审查发现] 原实现把任何异常与"CAS 未赢"共用 False，
        导致一次 DB 异常就让**已成功处理的页面被丢弃**，且日志写"租约已易主"把排查
        引向完全无关的方向）：
          True  = 赢得 CAS（状态已置 done）
          False = 租约确实已易主（新持有者在跑，本结果应丢弃）
          None  = CAS 落库失败（DB/磁盘异常），结果**未落盘**，调用方应视为失败重试
        """
        if self._read_conn is None:
            return None                      # 连接未就绪 = 无法落库（非租约易主）
        for _attempt in range(2):
            try:
                cur = await self._read_conn.execute(
                    "UPDATE frontier SET status='done', throttle_count=0 "
                    "WHERE url_hash=? AND status='leased' AND leased_at=?",
                    (url_hash, leased_at))
                await self._read_conn.commit()
                return (cur.rowcount or 0) > 0
            except Exception as e:
                _msg = str(e).lower()
                if _attempt == 0 and ("locked" in _msg or "busy" in _msg):
                    # 瞬时写锁冲突：退避重试（不是租约易主）
                    await asyncio.sleep(0.2)
                    continue
                logger.warning(
                    f"mark_done_checked 落库失败（结果未落盘，按失败处理而非租约易主）: "
                    f"{type(e).__name__}: {e}")
                return None
        return None

    async def mark_failed(self, url_hash, retry=True, delay=30, throttled=False):
        # 修复：检查 _read_conn 是否已初始化
        if self._read_conn is None:
            logger.warning("mark_failed called before init_async, skipping")
            return
        cursor = await self._read_conn.execute(
            "SELECT retry_count, max_retries, COALESCE(throttle_count, 0) FROM frontier WHERE url_hash=?",
            (url_hash,))
        row = await cursor.fetchone()
        if not row:
            return
        retry_count, max_retries, throttle_count = row[0], row[1], (row[2] if len(row) > 2 else 0)
        # 修复：off-by-one，原 retry_count+1 < max_retries 导致 max_retries=3 实际只允许 2 次重试
        if retry and retry_count < max_retries:
            # [FIXED & MODIFIED] v2.14 重试风暴修复（深查 B 级）：原固定 delay=30 且无抖动
            # → 同域 50 任务全失败时 30s 后同步重新出队形成周期性打波。改指数+抖动：
            # 30 × 1.8^retry_count × U(0.7,1.3)
            import random as _rnd
            _exp = min(float(delay) * (1.8 ** retry_count), 600.0)
            _jittered = _exp * _rnd.uniform(0.7, 1.3)
            if throttled:
                # [v2.17 E-P1-3] 限流≠重试：429/503 属"暂态限流"——冷却等待但不消耗
                # retry_count（原实现 3 次限流即 dead；限流不是真失败，不应耗尽重试）
                # [v2.19 P1] 连续限流兜底：不消耗 retry_count 的代价是任务可能**永驻 retry**
                # → 主循环退出条件（pending+retry==0）永不成立。故用独立计数，达上限转 dead。
                # [v2.19.6 修复·审查发现] 三条 UPDATE 统一加 `AND status != 'done'` 守卫：
                # 看门狗（page_timeout，本轮由 0 改为默认 300）在页面**已赢 CAS（status=done）**
                # 但仍在持久化阶段时取消任务 → 原实现会把 done 打回 retry → 整页重爬 +
                # 重复导出，正是 CAS 前置想消灭的后果。
                _THROTTLE_MAX = 10
                if int(throttle_count or 0) + 1 >= _THROTTLE_MAX:
                    logger.warning(f"连续限流达上限({_THROTTLE_MAX})转 dead: {url_hash}")
                    await self._write_queue.put((
                        "UPDATE frontier SET status='dead', throttle_count=0 "
                        "WHERE url_hash=? AND status != 'done'",
                        (url_hash,)))
                else:
                    await self._write_queue.put((
                        "UPDATE frontier SET status='retry', throttle_count=COALESCE(throttle_count,0)+1, "
                        "scheduled_at=? WHERE url_hash=? AND status != 'done'",
                        (time.time() + _jittered, url_hash)))
            else:
                await self._write_queue.put((
                    "UPDATE frontier SET status='retry', retry_count=retry_count+1, scheduled_at=? "
                    "WHERE url_hash=? AND status != 'done'",
                    (time.time() + _jittered, url_hash)))
        else:
            await self._write_queue.put((
                "UPDATE frontier SET status='dead' WHERE url_hash=? AND status != 'done'",
                (url_hash,)))

    async def write_extracted(self, url_hash, data_json):
        await self._write_queue.put(("INSERT OR REPLACE INTO extracted VALUES (?,?)", (url_hash, data_json)))

    async def write_error(self, url_hash, error_type, error_message, platform=None, code=None):
        # [FIXED & MODIFIED] v2.10.5 P2-10 错误表脱敏：异常串常内嵌 URL/token/手机号/IP，
        # 原样入库会泄漏敏感信息到持久化 db。统一经 sanitize_text + sanitize_url 落库。
        # [v2.17 4.3] platform/code 两列：审计可查"哪个平台什么码"（错误串带前缀也行，
        # 但结构化列可 GROUP BY——聚合分析不再靠正则切字符串）。
        try:
            from .sanitizer import sanitize_text, sanitize_url
            _msg = sanitize_text(str(error_message))
            _msg = sanitize_url(_msg)
        except Exception:
            _msg = str(error_message)
        await self._write_queue.put((
            "INSERT INTO errors (url_hash, error_type, error_message, timestamp, platform, code) "
            "VALUES (?,?,?,?,?,?)",
            (url_hash, error_type, _msg, time.time(),
             str(platform or ""), str(code or ""))))

    async def get_recent_errors(self, limit: int = 50) -> list:
        """[FIXED & MODIFIED] v2.13 阶段3 errors 只写不读 → 失败分析视图数据源"""
        if self._read_conn is None:
            return []
        try:
            async with self._read_conn.execute(
                    "SELECT e.timestamp, e.error_type, e.error_message, e.platform, e.code, "
                    "f.normalized_url "
                    "FROM errors e LEFT JOIN frontier f ON e.url_hash = f.url_hash "
                    "ORDER BY e.timestamp DESC LIMIT ?", (int(limit),)) as cur:
                return [dict(r) for r in await cur.fetchall()]
        except Exception as e:
            logger.debug(f"get_recent_errors failed: {e}")
            return []

    async def add_video_download(self, video_url, domain):
        # [v2.19.7 安全·扫描发现·**刻意不脱敏**] 与 frontier.push 同理：video_url 是下载
        # 任务的**钥匙**（签名 CDN 链接），update_video_status 也按它做 WHERE 匹配。抹掉
        # 签名参数 = 视频永远下不动，还会让状态更新匹配不到行。敏感 URL 收口在导出侧。
        # [FIXED & MODIFIED] v2.6.4 UPSERT 重置 pending：原 INSERT OR IGNORE 导致历史 completed/failed
        # 记录阻塞重下（作者空文件夹根因之一：误标 completed 后视频永远不再下载）
        await self._write_queue.put((
            "INSERT INTO video_downloads (video_url, domain, status, created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(video_url) DO UPDATE SET status='pending', progress=0, file_path=NULL, created_at=excluded.created_at",
            (video_url, domain, 'pending', time.time())))

    async def update_video_status(self, video_url, status, progress=0.0, file_path=None, file_size=None):
        # [v2.17 4.2] file_size 可选：completed 时传真实字节数 → stats.jsonl bytes 统计源。
        # COALESCE(?, file_size)：未提供时保留旧值（failed/中间态不覆盖已完成尺寸）。
        await self._write_queue.put((
            "UPDATE video_downloads SET status=?, progress=?, file_path=?, "
            "file_size=COALESCE(?, file_size) WHERE video_url=?",
            (status, progress, file_path, file_size, video_url)))

    async def get_video_stats(self):
        """[v2.17 4.2] 视频完成数 + 累计字节（stats.jsonl videos/bytes 数据源）"""
        if self._read_conn is None:
            return {"completed": 0, "bytes": 0}
        try:
            async with self._read_conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(file_size),0) FROM video_downloads "
                    "WHERE status='completed'") as cur:
                row = await cur.fetchone()
                return {"completed": int(row[0]), "bytes": int(row[1])}
        except Exception:
            return {"completed": 0, "bytes": 0}

    async def pop_batch(self, limit=10, worker_id="worker1", strategy="bfs"):
        """取一批待处理任务（租约式）。strategy=[v2.17 E-P1-4] 爬行策略：
        bfs=默认（优先+入队序升序）；dfs=后入先出（深度优先近似）；
        bff=优先值降序（best-first，priority 表达相关度）。"""
        async with aiosqlite.connect(self.db_path) as write_conn:
            await write_conn.execute("PRAGMA busy_timeout=30000")
            await write_conn.execute("BEGIN IMMEDIATE")
            try:
                # 修复：重试次数用尽的 retry 任务直接 dead（否则永久占用队列 → 主循环永不退出 hang）
                await write_conn.execute(
                    "UPDATE frontier SET status='dead' WHERE status='retry' AND retry_count >= max_retries")
                await write_conn.execute(
                    "UPDATE frontier SET status='pending' WHERE status='leased' "
                    "AND COALESCE(lease_expires, leased_at + ?) < ?",
                    (LEASE_TIMEOUT, time.time()))
                cursor = await write_conn.execute(
                    "SELECT * FROM frontier WHERE (status='pending' OR status='retry') AND "
                    "scheduled_at <= ? AND retry_count < max_retries " + _ORDER_BY.get(
                        strategy, _ORDER_BY["bfs"]) + " LIMIT ?",
                    (time.time(), limit)
                )
                rows = await cursor.fetchall()
                if not rows:
                    await write_conn.commit()
                    return []
                hashes = [r[0] for r in rows]
                # [v2.18 修复] 返回的 job 必须携带本次租约的真实 leased_at/lease_expires：
                # 旧实现 rows 在 UPDATE 前 fetchall——首次租约 leased_at=None（调用方
                # CAS 分支永不生效）、重租 job 带旧 token（mark_done_checked 必然
                # rowcount=0 → 重试结果全被"易主"误丢）
                _now = time.time()
                _exp = _now + LEASE_TIMEOUT
                await write_conn.executemany(
                    "UPDATE frontier SET status='leased', leased_at=?, lease_expires=?, worker_id=? WHERE url_hash=?",
                    [(_now, _exp, worker_id, h) for h in hashes])
                await write_conn.commit()
                cols = [desc[0] for desc in cursor.description]
                out = [dict(zip(cols, row)) for row in rows]
                for _j in out:
                    _j['leased_at'] = _now
                    _j['lease_expires'] = _exp
                return out
            except Exception:
                await write_conn.rollback()
                raise

    async def is_visited(self, url_hash):
        # [v2.17 稳定性门禁] close 后读接口一律空值语义（与 get_recent_errors 对齐——
        # 曾因 _read_conn 置 None 后 get_counts 直爆 AttributeError）
        if self._read_conn is None:
            return False
        cursor = await self._read_conn.execute(
            "SELECT 1 FROM frontier WHERE url_hash=? AND status = 'done'", (url_hash,))
        return bool(await cursor.fetchone())

    async def get_counts(self):
        if self._read_conn is None:
            return {}
        cursor = await self._read_conn.execute("SELECT status, COUNT(*) FROM frontier GROUP BY status")
        return {row[0]: row[1] for row in await cursor.fetchall()}

    async def get_pending_videos(self, limit=10):
        if self._read_conn is None:
            return []
        cursor = await self._read_conn.execute(
            "SELECT * FROM video_downloads WHERE status='pending' LIMIT ?", (limit,))
        return [dict(row) for row in await cursor.fetchall()]

    async def count_video_by_domain(self, domain):
        if self._read_conn is None:
            return 0
        cursor = await self._read_conn.execute(
            "SELECT COUNT(*) FROM video_downloads WHERE domain=?", (domain,))
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def count_done_by_domain(self, domain):
        if self._read_conn is None:
            return 0
        cursor = await self._read_conn.execute(
            "SELECT COUNT(*) FROM frontier WHERE domain=? AND status = 'done'", (domain,))
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def count_done_total(self):
        """[FIXED & MODIFIED] 全站 done 计数（max_pages 总页数限制——原实现从未生效）"""
        if self._read_conn is None:
            return 0
        cursor = await self._read_conn.execute(
            "SELECT COUNT(*) FROM frontier WHERE status = 'done'")
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def adjust_priority(self, url_hash, delta: int):
        """[v2.17 B4b] 证据驱动优先级调整（dynamic_priority 开关；delta 叠加，钳制 0-9）"""
        await self._write_queue.put((
            "UPDATE frontier SET priority = MIN(9, MAX(0, COALESCE(priority,5) + ?)) "
            "WHERE url_hash=?", (int(delta), url_hash)))

    async def close(self):
        # [FIXED & MODIFIED] v2.17 稳定性门禁：幂等化——crawler.run 与 run_crawler 各自
        # 调用 _graceful_shutdown → close 被双调；原实现二次 put(None) 无人消费（flusher
        # 已退出），且重复关 _read_conn 不报错但语义混乱。flush_task 置 None 后二次调用零动作。
        if self._flush_task:
            await self._write_queue.put(None)
            await self._flush_task
            self._flush_task = None
        if self._read_conn:
            await self._read_conn.close()
            self._read_conn = None

    async def flush(self):
        """等待所有待写入队列中的数据刷新到数据库"""
        await self._write_queue.join()
