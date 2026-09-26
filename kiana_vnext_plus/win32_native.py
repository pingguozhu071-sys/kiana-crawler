"""
Windows Native Optimizations

Applies Windows-specific system tuning for the crawler on 15.4GB / Ultra 7 255H:
- GC tuning: reduce Gen 0 collections under high allocation
- Memory working set trimming: periodic SetProcessWorkingSetSize
- IO priority boost: high-priority IO for SQLite WAL
- CPU affinity: pin worker threads to performance cores (P-cores)
- Large pages: enable large-page support for mmap regions
- System power: ensure High Performance power plan
"""

import gc
import ctypes
import logging
import asyncio

logger = logging.getLogger(__name__)

# Windows kernel32 imports
_kernel32 = ctypes.windll.kernel32

PROCESS_SET_INFORMATION = 0x0200
PROCESS_QUERY_INFORMATION = 0x0400


def apply_gc_tuning(g0=700, g1=10, g2=5):
    """Tune Python GC thresholds for high-throughput crawling.

    Higher thresholds = fewer collections = lower CPU overhead,
    at the cost of potentially higher memory usage.
    """
    gc.set_threshold(g0, g1, g2)
    logger.info(f"GC thresholds set: gen0={g0}, gen1={g1}, gen2={g2}")


def set_high_precision_timer():
    """[FIXED & MODIFIED] v2.11 高精度系统计时器：timeBeginPeriod(1) 把 Windows 默认
    15.6ms 定时器分辨率提到 1ms——asyncio.sleep/网络超时/浏览器渲染等待的抖动显著下降。
    atexit 兜底 timeEndPeriod 恢复（避免系统全局遗留高分辨率功耗）。"""
    try:
        import atexit as _atexit
        _winmm = ctypes.windll.winmm
        _winmm.timeBeginPeriod(1)
        _atexit.register(_winmm.timeEndPeriod, 1)
        logger.info("高精度计时器已启用 (timeBeginPeriod=1ms)")
        return True
    except Exception as e:
        logger.debug(f"高精度计时器启用失败: {e}")
        return False


def trim_working_set():
    """Trim process working set to release unused pages back to OS.
    
    Call periodically to prevent memory bloat from fragmented allocations.
    On 15.4GB systems this is critical to avoid OOM.
    """
    try:
        # Windows API: EmptyWorkingSet
        handle = _kernel32.GetCurrentProcess()
        _kernel32.SetProcessWorkingSetSize(handle, -1, -1)
        logger.debug("Working set trimmed")
    except Exception as e:
        logger.debug(f"Working set trim failed: {e}")


def set_io_priority_high():
    """Set current process IO priority to HIGH for faster disk access.
    
    SQLite WAL commits and media downloads benefit from higher IO priority.
    """
    try:
        PROCESS_MODE_BACKGROUND_BEGIN = 0x00100000
        _kernel32.SetPriorityClass(
            _kernel32.GetCurrentProcess(),
            0x00000080  # HIGH_PRIORITY_CLASS
        )
        logger.debug("IO priority set to HIGH")
    except Exception as e:
        logger.debug(f"IO priority set failed: {e}")


def set_cpu_affinity_to_pcores():
    """Pin main process thread to performance cores (P-cores).
    
    Ultra 7 255H has 6 P-cores + 8 E-cores + 2 LPE cores.
    We pin to P-cores for latency-sensitive async event loops.
    Avoids scheduler migration to E-cores which hurts I/O throughput.
    
    NOTE: This only pins the calling thread (main event loop).
    Worker threads should be allowed to use all cores.
    """
    try:
        # Get system CPU info to map P-cores
        # Ultra 7 255H: P-cores are typically first 6 cores (with HT: cores 0-11)
        # Simplified: use first half of logical processors as P-cores heuristic
        import psutil
        logical = psutil.cpu_count(logical=True)
        physical = psutil.cpu_count(logical=False)
        if logical and physical and logical > physical:
            # Hybrid architecture detected: P-cores have HT, E-cores don't
            # Estimate P-cores as first N where hyperthread pairs exist
            p_cores_logical = min(physical, logical // 2)
            # Create mask for first N logical processors
            mask = (1 << p_cores_logical) - 1
            _kernel32.SetThreadAffinityMask(
                _kernel32.GetCurrentThread(),
                mask
            )
            logger.info(f"CPU affinity pinned to {p_cores_logical} P-cores (mask={mask:#x})")
        else:
            logger.debug("CPU affinity skipped: non-hybrid or unknown topology")
    except Exception as e:
        logger.debug(f"CPU affinity set failed: {e}")


def enable_large_pages():
    """Attempt to enable large page support for memory-mapped regions.
    
    Reduces TLB misses for SQLite mmap regions and large buffers.
    Requires 'Lock pages in memory' user right (gpedit.msc).
    Falls back gracefully if not available.
    """
    try:
        # Try to enable SeLockMemoryPrivilege
        import ctypes.wintypes as w
        advapi32 = ctypes.windll.advapi32

        TOKEN_ADJUST_PRIVILEGES = 0x0020
        TOKEN_QUERY = 0x0008
        SE_PRIVILEGE_ENABLED = 0x00000002

        class LUID(ctypes.Structure):
            _fields_ = [("LowPart", w.DWORD), ("HighPart", w.LONG)]

        class LUID_AND_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("Luid", LUID), ("Attributes", w.DWORD)]

        class TOKEN_PRIVILEGES(ctypes.Structure):
            _fields_ = [("PrivilegeCount", w.DWORD),
                        ("Privileges", LUID_AND_ATTRIBUTES * 1)]

        token = w.HANDLE()
        advapi32.OpenProcessToken(
            _kernel32.GetCurrentProcess(),
            TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
            ctypes.byref(token)
        )

        luid = LUID()
        advapi32.LookupPrivilegeValueW(None, "SeLockMemoryPrivilege", ctypes.byref(luid))

        tp = TOKEN_PRIVILEGES()
        tp.PrivilegeCount = 1
        tp.Privileges[0].Luid = luid
        tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED

        advapi32.AdjustTokenPrivileges(token, False, ctypes.byref(tp), 0, None, None)
        _kernel32.CloseHandle(token)
        logger.info("Large page support enabled")
    except Exception as e:
        logger.debug(f"Large page enable failed (expected on non-admin): {e}")


def ensure_high_performance_power_plan():
    """记录当前电源计划并切换到高性能（爬虫运行期间专用）。

    作者授权：跑爬虫时开性能模式（符合作者"干活开性能"偏好）。
    返回原计划 GUID，供爬虫结束时 restore_power_plan() 恢复，
    保证平时作者自己的 Silent/奥创设置不受影响。
    [v2.16 M7] 三档策略：power_strategy = performance|balanced|battery——balanced 用
    Windows 内置平衡 GUID，battery 用节能 GUID（绿色爬虫档）。
    """
    import os as _os
    strategy = _os.environ.get("KIANA_POWER_STRATEGY", "performance")
    scheme = {"performance": "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c",
              "balanced": "381b4222-f694-41f0-9685-ff5bb260df2e",
              "battery": "a1841308-3541-4fab-bc81-f71556f20b4a"}.get(strategy, "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c")
    try:
        import subprocess
        result = subprocess.run(
            ["powercfg", "/getactivescheme"],
            capture_output=True, text=True, timeout=5
        )
        output = result.stdout.strip()
        import re as _re
        m = _re.search(r'\(([0-9a-fA-F\-]{36})\)', output)
        original = m.group(1) if m else None
        if original == scheme:
            logger.debug(f"Power plan already {strategy}")
            return None
        logger.info(f"Switching to {strategy} for crawl (original: {original})")
        subprocess.run(
            ["powercfg", "/setactive", scheme],
            capture_output=True, timeout=10
        )
        # [FIXED & MODIFIED] F3：atexit 兜底恢复（进程被强杀/未捕获异常/断电恢复后——否则系统永久
        # 停留在高性能 → CPU 满载 → 主人打游戏帧率暴跌、笔记本续航骤降）
        try:
            import atexit as _atexit
            _atexit.register(restore_power_plan, original)
        except Exception:
            pass
        return original
    except Exception as e:
        logger.debug(f"Power plan switch failed: {e}")
        return None


def record_energy_probe() -> dict:
    """[v2.16 M7] 绿色遥测：任务结束输出磁盘/网络/CPU 时间摘要（psutil）"""
    try:
        import psutil as _ps
        proc = _ps.Process()
        io = proc.io_counters()
        cpu = proc.cpu_times()
        return {"cpu_user_s": round(cpu.user, 1), "cpu_system_s": round(cpu.system, 1),
                "disk_read_mb": round(io.read_bytes / 1024 / 1024, 1),
                "disk_written_mb": round(io.write_bytes / 1024 / 1024, 1)}
    except Exception:
        return {}


def restore_power_plan(original_guid):
    """爬虫结束后恢复原来的电源计划（作者日常设置不受影响）"""
    if not original_guid:
        return
    try:
        import subprocess
        subprocess.run(
            ["powercfg", "/setactive", original_guid],
            capture_output=True, timeout=10
        )
        logger.info(f"Power plan restored: {original_guid}")
    except Exception as e:
        logger.debug(f"Power plan restore failed: {e}")


async def memory_recycle_loop(interval: int = 300):
    """Periodic memory recycling loop for long-running crawls.
    
    Runs GC collection and working set trim at configurable intervals.
    Critical for 15.4GB systems to prevent slow memory leak accumulation.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            # Force full GC collection
            collected = gc.collect(2)
            # Trim working set
            trim_working_set()
            if collected > 0:
                logger.debug(f"Memory recycle: {collected} objects collected")
        except Exception as e:
            logger.debug(f"Memory recycle error: {e}")


def apply_all_optimizations(config: dict = None, init_power_plan: bool = True):
    """Apply all Windows-native optimizations based on config.

    [FIXED & MODIFIED] v2.10.5 P2-12 幂等修复：原内部固定调用 ensure_high_performance_power_plan()
    且丢弃返回值，crawler 外层再调一次 → 返回值 None（第二次调用已在高性能计划，返回 None）
    → shutdown 的 restore_power_plan 收到 None 变 no-op，仅靠 atexit 恢复。
    init_power_plan=False 时不切电源，由调用方负责（crawler 拿句柄后可恢复）。

    Args:
        config: GlobalConfig-like dict with keys for gc thresholds etc.
        init_power_plan: 是否在本函数内切换电源计划（crawler 传 False，自行切并保存句柄）
    """
    if init_power_plan:
        pass
    if config is None:
        config = {}

    if config.get("single_machine_optimized", True):
        logger.info("Applying Windows native optimizations for 15.4GB / Ultra 7 255H")

        g0 = config.get("gc_threshold_generation0", 700)
        g1 = config.get("gc_threshold_generation1", 10)
        g2 = config.get("gc_threshold_generation2", 5)
        apply_gc_tuning(g0, g1, g2)

        set_io_priority_high()
        set_cpu_affinity_to_pcores()
        enable_large_pages()
        set_high_precision_timer()
        if init_power_plan:
            ensure_high_performance_power_plan()
        trim_working_set()

        logger.info("Windows native optimizations applied")
    else:
        logger.info("Single-machine optimizations disabled, skipping native tuning")
