import time
import logging
from typing import List, Dict
from .url_utils import url_hash, normalize_url, extract_domain, is_media_stream_url

# [FIXED & MODIFIED] v2.11 惰性导入：redis 包不在构建依赖清单，换机构建时硬 import 必炸。
# 改为 try/except——模块可被无条件 import，仅 frontier_backend=redis 时实例化才需要 redis。
try:
    import redis.asyncio as redis
    HAS_REDIS = True
except ImportError:
    redis = None
    HAS_REDIS = False

logger = logging.getLogger(__name__)

POP_BATCH_LUA = """
local queue_key = KEYS[1]
local leased_key = KEYS[2]
local job_prefix = ARGV[1]
local worker_id = ARGV[2]
local limit = tonumber(ARGV[3])
local now = tonumber(ARGV[4])
local lease_time = tonumber(ARGV[5])
-- 清理过期租约
local expired = redis.call('ZRANGEBYSCORE', leased_key, '-inf', now)
for i=1,#expired do
    redis.call('ZREM', leased_key, expired[i])
    redis.call('HSET', job_prefix .. expired[i], 'status', 'pending')
end
local hashes = redis.call('ZRANGE', queue_key, 0, limit-1)
if #hashes == 0 then
    return {}
end
for i=1,#hashes do
    redis.call('ZREM', queue_key, hashes[i])
    redis.call('ZADD', leased_key, lease_time, hashes[i])
    redis.call('HSET', job_prefix .. hashes[i], 'status', 'leased', 'leased_at', now, 'worker_id', worker_id)
end
local result = {}
for i=1,#hashes do
    local data = redis.call('HGETALL', job_prefix .. hashes[i])
    table.insert(result, data)
end
return result
"""


class RedisFrontier:
    """Redis 分布式前沿队列，Lua 脚本保证原子性

    当 Redis 不可用时自动降级到 SQLite 后端，确保单机模式也能正常运行。
    """

    def __init__(self, redis_url, result_frontier):
        if not HAS_REDIS:
            raise RuntimeError("frontier_backend=redis 但未安装 redis 包（pip install redis）")
        self.redis = redis.from_url(redis_url, decode_responses=True)
        self.result_db = result_frontier
        self.lease_timeout = 300
        self._script = self.redis.register_script(POP_BATCH_LUA)
        # 降级标志：Redis 连接失败后设为 True，后续操作直接走 SQLite
        self._fallback_mode = False
        self._fallback_check_interval = 60  # 降级后每 60 秒尝试恢复 Redis
        self._last_fallback_check = 0.0

    async def init_async(self):
        # 确保 SQLite 后端已初始化
        if hasattr(self.result_db, 'init_async'):
            await self.result_db.init_async()
        # 测试 Redis 连接
        try:
            await self.redis.ping()
            logger.info("Redis frontier: 连接成功")
            self._fallback_mode = False
        except Exception as e:
            logger.warning(f"Redis frontier: 连接失败，降级到 SQLite 模式: {e}")
            self._fallback_mode = True

    async def _try_redis_recovery(self):
        """定期尝试恢复 Redis 连接"""
        now = time.monotonic()
        if now - self._last_fallback_check < self._fallback_check_interval:
            return
        self._last_fallback_check = now
        try:
            await self.redis.ping()
            logger.info("Redis frontier: 连接已恢复，切换回 Redis 模式")
            self._fallback_mode = False
        except Exception:
            pass  # 仍然不可用，保持降级模式

    async def push(self, url, depth=0, priority=5, force=False, parent_hash=None):
        """[v6 修复] 补齐 `force` / `parent_hash` 形参。

        **原先签名只有 `(url, depth, priority)`**，而 `crawler` 的**入口播种**路径调用的是
        `push(url, depth=0, priority=1, force=True, parent_hash=...)` —— 于是
        `frontier_backend="redis"` 时**第一步就抛 TypeError**，整场爬取根本起不来。
        （v2.19.6 只为 `mark_failed` 补过签名，没检查其余方法；这个洞一直在。）

        `force` 的语义与 SQLite 对齐：**非 force 时已存在就不覆盖**（等价 `INSERT OR IGNORE`）。
        原 Redis 实现**无条件 hset 覆盖**，等于永远是 force——去重是坏的。

        [本轮修复] 「媒体流分片不入页面队列」的闸在**本后端也要有**：
        `frontier.push` 里那道闸只覆盖 SQLite 路径与降级路径，Redis 直连路径
        （下面 `_fallback_mode` 为假时的 zadd/hset）**不经过它**。
        判据仍是 `url_utils.is_media_stream_url`（**唯一实现**，不在本文件复制一份逻辑）——
        与 `is_private_url` 同一种用法：判据一份，决策点随后端各一处。
        """
        if is_media_stream_url(url):
            logger.debug(f"页面入队拦截（媒体流分片不是页面）: {str(url)[:80]}")
            return
        # 降级模式：直接走 SQLite
        if self._fallback_mode:
            await self._try_redis_recovery()
            if self._fallback_mode:
                await self.result_db.push(url, depth, priority,
                                          force=force, parent_hash=parent_hash)
                return

        uh = url_hash(url)
        if not force:
            # 与 SQLite 的 INSERT OR IGNORE 同义：已存在则**保留原状态**（去重）
            try:
                if await self.redis.exists(f"frontier:job:{uh}"):
                    return
            except Exception as e:
                logger.warning(f"Redis exists 检查失败，按 force 处理: {e}")
        job = {
            "url_hash": uh, "normalized_url": normalize_url(url), "domain": extract_domain(url),
            "depth": depth, "priority": priority, "status": "pending", "scheduled_at": time.time(),
            "retry_count": 0, "max_retries": 3, "parent_hash": parent_hash or "",
        }
        score = priority * 1e10 + job["scheduled_at"]
        try:
            async with self.redis.pipeline(transaction=True) as pipe:
                pipe.zadd("frontier:queue", {uh: score})
                pipe.hset(f"frontier:job:{uh}", mapping=job)
                await pipe.execute()
        except Exception as e:
            logger.warning(f"Redis push 失败，降级到 SQLite: {e}")
            self._fallback_mode = True
            await self.result_db.push(url, depth, priority,
                                      force=force, parent_hash=parent_hash)

    async def pop_batch(self, limit=10, worker_id="worker1", strategy="bfs") -> List[Dict]:
        """[v6 修复] 补齐 `strategy` 形参。

        同样地，`crawler` 调用时传 `strategy=...`（bfs/dfs/bff），而本方法没有该形参
        → **TypeError**。而 `crawler` 对 `pop_batch` 有"退避重试 3 次后 re-raise"的包装，
        所以这个 TypeError 会让**整场爬取直接终止**。

        ⚠️ **如实说明能力边界**：Redis 路径用 Lua 脚本从 `frontier:queue`（按
        priority+时间 计分的 zset）取任务，**只实现优先级/BFS 次序**，
        dfs/bff 未实现——这里**显式告警**而不是静默忽略，
        因为"参数看起来生效、实际没生效"正是本工程最忌讳的一类。
        """
        if strategy != "bfs":
            logger.warning(f"Redis 后端暂只支持 bfs 次序，收到 strategy={strategy!r} "
                           f"——本次仍按 bfs 出队（如需 dfs/bff 请用 SQLite 后端）")
        # 降级模式：直接走 SQLite
        if self._fallback_mode:
            await self._try_redis_recovery()
            if self._fallback_mode:
                return await self.result_db.pop_batch(limit, worker_id, strategy=strategy)

        now = time.time()
        lease_time = now + self.lease_timeout
        try:
            results = await self._script(
                keys=["frontier:queue", "frontier:leased"],
                args=["frontier:job:", worker_id, limit, now, lease_time]
            )
            jobs = []
            for arr in results:
                data = {}
                for i in range(0, len(arr), 2):
                    data[arr[i]] = arr[i + 1]
                jobs.append(data)
            return jobs
        except Exception as e:
            logger.warning(f"Redis pop_batch 失败，降级到 SQLite: {e}")
            self._fallback_mode = True
            return await self.result_db.pop_batch(limit, worker_id, strategy=strategy)

    async def mark_done(self, url_hash, leased_at=None):
        """[v2.19.6 修复·审查发现] 对齐 FrontierDB 的新签名：`leased_at` 为可选
        CAS 参数。本后端无租约字段（Redis 侧用 leased 集合表达），故降级为
        "无条件置 done"并在此说明——调用方（page_processor 304 分支）会传该参数，
        不同步签名会在 redis 后端下抛 TypeError 导致该页永久无法完成。
        另：为完整对齐，`mark_done_checked` / `heartbeat_lease` 亦在下方以降级实现补齐。"""
        if self._fallback_mode:
            await self.result_db.mark_done(url_hash, leased_at=leased_at)
            return
        try:
            await self.redis.zrem("frontier:leased", url_hash)
            await self.redis.delete(f"frontier:job:{url_hash}")
        except Exception as e:
            logger.warning(f"Redis mark_done 失败，降级到 SQLite: {e}")
            self._fallback_mode = True
            await self.result_db.mark_done(url_hash, leased_at=leased_at)

    async def mark_done_checked(self, url_hash, leased_at):
        """[v2.19.6] CAS 语义在 Redis 后端的降级实现：无租约字段可校验 →
        退化为无条件置 done 并返回 True（诚实降级，不做无法保证的 CAS 承诺）。"""
        await self.mark_done(url_hash, leased_at=leased_at)
        return True

    async def heartbeat_lease(self, url_hash, leased_at) -> bool:
        """[v2.19.6] Redis 后端无 lease_expires 字段 → 心跳为空操作（返回 True
        表示"当前仍持有"，避免调用方误判易主而放弃结果）。"""
        return True

    async def mark_failed(self, url_hash, retry=True, delay=30, throttled=False):
        """[v6 修复] 与 `frontier.FrontierDB.mark_failed` 共用**同一套重试策略**
        （`frontier.next_retry_state`）。

        此前这里是**独立实现**，于是 SQLite 后端历次修好的三件事在 Redis 后端
        **全部缺失**（本轮逐条比对确认）：

          ① **指数+抖动退避**（v2.14"重试风暴"修复）—— 这里仍是固定 `delay`，
             等于退回"同域 50 任务 30s 后同步重新出队形成周期性打波"的版本；
          ② **限流不消耗 retry_count**（v2.17 E-P1-3）—— 这里忽略 `throttled`，
             "3 次限流即 dead"这个已修掉的 bug 在 Redis 上仍在；
          ③ **`status != 'done'` 守卫**（v2.19.6 看门狗修复）—— 这里没有守卫，
             会把**已完成**的任务打回 retry（整页重爬 + 重复导出）。

        [v2.19.6] 原注释写"限流语义在 Redis 后端暂不单独记账（退化为普通重试）"——
        现在它**真的**单独记账了。
        """
        if self._fallback_mode:
            await self.result_db.mark_failed(url_hash, retry, delay, throttled=throttled)
            return
        try:
            data = await self.redis.hgetall(f"frontier:job:{url_hash}")
            if not data:
                # Redis 中没有，尝试 SQLite（同样把 throttled 带下去，别丢语义）
                await self.result_db.mark_failed(url_hash, retry, delay, throttled=throttled)
                return
            # ③ done 守卫：看门狗可能在页面**已赢 CAS（status=done）**但仍在持久化阶段
            # 取消任务；原实现会把 done 打回 retry（整页重爬 + 重复导出）。
            if str(data.get("status", "")).lower() == "done":
                logger.debug(f"mark_failed 跳过已完成任务（done 守卫）: {url_hash}")
                return
            # ① ② 策略由唯一实现给出
            from .frontier import next_retry_state
            st = next_retry_state(retry, data.get("retry_count", 0), data.get("max_retries", 3),
                                  data.get("throttle_count", 0), delay=delay, throttled=throttled)
            if st["status"] == "dead":
                await self.redis.hset(f"frontier:job:{url_hash}", mapping={
                    "status": "dead", "throttle_count": st["throttle_count"]})
                await self.redis.zrem("frontier:leased", url_hash)
            else:
                await self.redis.hset(f"frontier:job:{url_hash}", mapping={
                    "status": "retry", "retry_count": st["retry_count"],
                    "throttle_count": st["throttle_count"],
                    "scheduled_at": st["scheduled_at"]})
                await self.redis.zadd("frontier:queue", {url_hash: st["scheduled_at"]})
        except Exception as e:
            logger.warning(f"Redis mark_failed 失败，降级到 SQLite: {e}")
            self._fallback_mode = True
            await self.result_db.mark_failed(url_hash, retry, delay, throttled=throttled)

    async def write_page(self, *args, **kwargs):
        await self.result_db.write_page(*args, **kwargs)

    async def write_extracted(self, *args, **kwargs):
        await self.result_db.write_extracted(*args, **kwargs)

    async def write_error(self, *args, **kwargs):
        await self.result_db.write_error(*args, **kwargs)

    async def is_visited(self, url_hash):
        if self._fallback_mode:
            return await self.result_db.is_visited(url_hash)
        try:
            return await self.redis.exists(f"frontier:job:{url_hash}") or await self.result_db.is_visited(url_hash)
        except Exception:
            return await self.result_db.is_visited(url_hash)

    async def get_counts(self):
        if self._fallback_mode:
            return await self.result_db.get_counts()
        try:
            return {"pending": await self.redis.zcard("frontier:queue")}
        except Exception:
            self._fallback_mode = True
            return await self.result_db.get_counts()

    async def get_pending_videos(self, limit=10):
        return await self.result_db.get_pending_videos(limit)

    async def count_video_by_domain(self, domain):
        return await self.result_db.count_video_by_domain(domain)

    async def count_done_by_domain(self, domain):
        return await self.result_db.count_done_by_domain(domain)

    # ════════════════════════════════════════════════════════════════
    # [v6 修复] 补齐**引擎实际会调用**但本后端缺失的 6 个方法。
    #
    #   它们缺失时不是"降级"，而是 `AttributeError` —— 例如 `flush()` 在
    #   `crawler.py:928`（种子入队之后）就会被调用，于是
    #   `frontier_backend="redis"` 时**爬取根本无法完成第一轮循环**。
    #
    #   数据归属决定实现方式：这些状态的**真身在 SQLite 侧**（冷却表、去重、
    #   结果计数、写队列），故与上面 `get_pending_videos` 等一致地直接委托给
    #   `result_db`；Redis 只负责队列（pending/leased）本身。
    # ════════════════════════════════════════════════════════════════
    async def flush(self):
        """把写队列落库。**crawler 每轮都调**——缺它就是 AttributeError。"""
        await self.result_db.flush()

    async def mark_duplicate(self, url_hash, duplicate_of):
        await self.result_db.mark_duplicate(url_hash, duplicate_of)

    async def count_done_total(self):
        return await self.result_db.count_done_total()

    async def set_domain_cooldown(self, domain, until_epoch: float, tier: int):
        await self.result_db.set_domain_cooldown(domain, until_epoch, tier)

    async def clear_domain_cooldown(self, domain):
        await self.result_db.clear_domain_cooldown(domain)

    async def load_active_cooldowns(self) -> dict:
        return await self.result_db.load_active_cooldowns()

    async def add_video_download(self, video_url, domain):
        await self.result_db.add_video_download(video_url, domain)

    async def update_video_status(self, video_url, status, progress=0.0, file_path=None, file_size=None):
        await self.result_db.update_video_status(video_url, status, progress, file_path, file_size)

    async def get_video_stats(self):
        return await self.result_db.get_video_stats()

    async def adjust_priority(self, url_hash, delta: int):
        return await self.result_db.adjust_priority(url_hash, delta)

    async def close(self):
        try:
            # 修复：redis-py 5.x 废弃 close()，推荐 aclose()
            if hasattr(self.redis, 'aclose'):
                await self.redis.aclose()
            else:
                await self.redis.close()
        except Exception:
            pass
        await self.result_db.close()
