# -*- coding: utf-8 -*-
"""Kiana 平台 API 统一语义异常层（v2.16.1 阶段3）

设计原则（业界通用做法：“错误分类 + 确定性失败不重试”），本工程独立实现：
  IPBlockError（被 IP 封禁/风控 → 换出口/稍候）
  PlatformAccessError（账号受限/登录过期 → 换账号/重新登录）
  NoteNotFoundError（资源不存在/已删除 → 跳过）
  DataFetchError（普通拉取失败 → 可重试）
"""
import asyncio
import functools


class KianaAPIError(Exception):
    """平台 API 错误基类（携带 status/code/platform 供上层审计与日志）"""

    def __init__(self, message, status=None, code=None, platform=None):
        super().__init__(message)
        self.status = status
        self.code = code
        # [v2.17 4.3] 平台标识：errors 表 platform/code 结构化列数据源（哪个平台什么码）
        self.platform = platform or ""


class IPBlockError(KianaAPIError):
    """IP 封禁/风控（确定失败：重试也无效，换出口/稍候）"""


class PlatformAccessError(KianaAPIError):
    """账号安全限制/登录态过期（确定失败：重新登录/换账号）"""


class NoteNotFoundError(KianaAPIError):
    """资源不存在/已删除/私密不可见（确定失败：跳过）"""


class DataFetchError(KianaAPIError):
    """普通拉取失败/服务端瞬时错误（可重试）"""


# 平台码表：code → (异常类, 提示语)。未列出的 code 归 DataFetchError（可重试兜底）。
PLATFORM_ERROR_CODES = {
    "douyin": {
        2155: (IPBlockError, "请求过频/IP 风控（稍后重试或换出口）"),
        2190: (PlatformAccessError, "登录态过期-2190（重新导出抖音 cookies）"),
        2192: (PlatformAccessError, "登录态过期-2192（重新导出抖音 cookies）"),
        71: (NoteNotFoundError, "视频已删除/不可见"),
        8: (DataFetchError, "服务端错误-8"),
    },
    "xhs": {
        300012: (IPBlockError, "IP 拦截（换出口）"),
        300011: (PlatformAccessError, "账号安全限制（换账号）"),
        -510000: (NoteNotFoundError, "笔记不存在"),
        -510001: (NoteNotFoundError, "笔记已删除"),
    },
    "bilibili": {
        -404: (NoteNotFoundError, "权限不足或资源不存在（会员/登录）"),
        -412: (IPBlockError, "风控校验-412"),
        -352: (IPBlockError, "风控校验-352"),
        401: (PlatformAccessError, "未登录/权限不足"),
    },
}


def error_hint(platform, code, default_message="未知平台错误码"):
    """取码表提示语（供不抛异常、走 dict 返回值的调用方生成可读错误）"""
    cls, hint = PLATFORM_ERROR_CODES.get(platform, {}).get(code, (DataFetchError, default_message))
    if code is None:
        return hint
    if hint == default_message:
        return f"{hint} (code={code})"
    return f"{hint} (code={code})"


def raise_for_code(platform, code, default=DataFetchError, message=""):
    """按码表抛语义异常（未命中 → default 异常）

    [v2.19 标注] 当前生产链路**零调用**，原因已核实并记录（非疏漏）：
    现有平台 resolver（douyin/kuaishou/xhs）采用**返回 dict 约定**
    （`{"ok": False, "error": ..., "status": ...}`），属"值传递"风格；
    本函数服务的是**异常风格**调用方。两者是风格选择、不是重复实现——
    强行让 resolver 改抛异常会牵动其全部调用方（含 GUI 错误展示），风险大于收益。

    何时该用它：新增 resolver 选择异常风格时，或需在深层函数"就地中断并携带平台码"
    （免去逐层 return 错误 dict）时：
        from .api_errors import raise_for_code
        raise_for_code("douyin", status_code)   # 抛 IPBlockError / NoteNotFoundError / ...

    与码表的关系：本函数是 PLATFORM_ERROR_CODES 的"抛异常"出口；
    `error_hint()` 是同一码表的"取值"出口（已被 douyin_resolver 真实使用）。"""
    cls, hint = PLATFORM_ERROR_CODES.get(platform, {}).get(
        code, (default, message or f"status={code}"))
    raise cls(f"{platform} code={code}: {hint}", code=code, platform=platform)


def retry_whitelist(retries=3, delay=1.0,
                    no_retry=(IPBlockError, PlatformAccessError, NoteNotFoundError)):
    """确定性错误不重试的白名单重试装饰器（asyncio）。

    [v2.19 标注] 当前生产链路**零调用**，原因已核实（非疏漏）：
    唯一需要"按平台码决定重试"的现存调用点（douyin_resolver 的 detail API）是**同步**
    函数，且其重试判定与响应体解析交织（按 status_code 决定是否 continue）——属业务逻辑，
    无法用本装饰器（async + 异常驱动）表达。故本函数是**面向异步调用方的预留设施**
    （xhs/kuaishou 若接入异步签名 API 通道即可直接使用）。

    用法：
        @retry_whitelist(retries=3, delay=1.0)
        async def fetch_profile(...): ...
    语义：no_retry 中的异常立即上抛（换账号/换出口/跳过）；
    其他异常重试 retries 次（间隔 delay 秒，指数放宽由调用方控制）。"""
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(*a, **kw):
            last = None
            for i in range(retries):
                try:
                    return await fn(*a, **kw)
                except no_retry:
                    raise
                except Exception as e:  # noqa: BLE001 —— 其余异常走重试
                    last = e
                    if i < retries - 1:
                        await asyncio.sleep(delay * (i + 1))
            raise last
        return wrapper
    return deco
