import time
import logging
from typing import List, Dict
from .url_utils import url_hash, normalize_url, extract_domain

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

    async def push(self, url, depth=0, priority=5):
        # 降级模式：直接走 SQLite
        if self._fallback_mode:
            await self._try_redis_recovery()
            if self._fallback_mode:
                await self.result_db.push(url, depth, priority)
                return

        uh = url_hash(url)
        job = {
            "url_hash": uh, "normalized_url": normalize_url(url), "domain": extract_domain(url),
            "depth": depth, "priority": priority, "status": "pending", "scheduled_at": time.time(),
            "retry_count": 0, "max_retries": 3
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
            await self.result_db.push(url, depth, priority)

    async def pop_batch(self, limit=10, worker_id="worker1") -> List[Dict]:
        # 降级模式：直接走 SQLite
        if self._fallback_mode:
            await self._try_redis_recovery()
            if self._fallback_mode:
                return await self.result_db.pop_batch(limit, worker_id)

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
            return await self.result_db.pop_batch(limit, worker_id)

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
        """[v2.19.6] 补 `throttled` 形参（对齐 FrontierDB）——限流语义在 Redis
        后端暂不单独记账（退化为普通重试），但不再因签名不符抛 TypeError。"""
        if self._fallback_mode:
            await self.result_db.mark_failed(url_hash, retry, delay)
            return
        try:
            data = await self.redis.hgetall(f"frontier:job:{url_hash}")
            if not data:
                # Redis 中没有，尝试 SQLite
                await self.result_db.mark_failed(url_hash, retry, delay)
                return
            retry_count = int(data.get("retry_count", 0))
            max_retries = int(data.get("max_retries", 3))
            # 修复：off-by-one，与 frontier.py 保持一致
            if retry and retry_count < max_retries:
                await self.redis.hset(f"frontier:job:{url_hash}", mapping={
                    "status": "retry", "retry_count": retry_count + 1, "scheduled_at": time.time() + delay
                })
                await self.redis.zadd("frontier:queue", {url_hash: time.time() + delay})
            else:
                await self.redis.hset(f"frontier:job:{url_hash}", "status", "dead")
                await self.redis.zrem("frontier:leased", url_hash)
        except Exception as e:
            logger.warning(f"Redis mark_failed 失败，降级到 SQLite: {e}")
            self._fallback_mode = True
            await self.result_db.mark_failed(url_hash, retry, delay)

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
