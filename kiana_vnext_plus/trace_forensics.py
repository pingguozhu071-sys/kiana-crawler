# -*- coding: utf-8 -*-
"""失败取证：trace 归档的**脱敏**与存放（M2-d）

**要解决的问题**：白皮书 §8 写着"有几个线上解析器失败原因尚未定位"。
补证据的工具其实就在既有依赖里——patchright/playwright 自带 trace 录制
（DOM + 网络 + 控制台）。但**直接归档是危险的**：

  · trace 里有**完整请求 URL**（含签名参数 `?token=…`）；
  · 有**请求/响应头**（含 `Cookie` / `Authorization`）；
  · 有**页面 DOM**（登录后才可见的内容）。

它比日志更危险：日志有脱敏 Filter 兜着，trace 是**原始结构**，筛选器够不着。

三条红线（写在代码里，防后人"顺手清掉脱敏"）：

  ① **归档前必须过脱敏**——有对抗性回归：构造含 `?token=` 与 Cookie 头的假 trace，
     断言被抹掉。脱敏失效比不归档更糟：它给人一种"已经处理过了"的错觉。
  ② **归档目录不参与任何导出**——与 `frontier` / `video_downloads` 同样的原则：
     敏感 URL 收口在导出侧，而这份根本不出现在交付物里。
  ③ **归档落运行期数据根，不进仓库**——反面教材就在本工程里：
     `tests/assets/gui_shots/` 是**被 git 跟踪的**，测试截图一旦落进仓库就会被打包、
     提交、公开（历史上正是像素级泄漏的来源）。取证归档更不能重蹈。
"""
import json
import logging
import os
import re
import zipfile
from typing import Optional

from .sanitizer import (
    SENSITIVE_HEADERS, sanitize_text, sanitize_url,
)

logger = logging.getLogger(__name__)

# trace 里算"文本、需要脱敏"的条目后缀（Playwright trace = zip，内含这几个文件）
_TEXT_SUFFIXES = (".trace", ".network", ".json", ".txt", ".md", ".html", ".har")

# JSON 解析不了时的**保守兜底**：宁可多抹，不可漏抹
_FALLBACK_HDR_RE = re.compile(
    r'(?i)("?(?:cookie|set-cookie|authorization|proxy-authorization|x-api-key)"?'
    r'\s*[:=]\s*"?)[^",\s}]{3,}')

REDACTED = "[REDACTED]"


def _is_binary(data: bytes) -> bool:
    """粗判二进制：含 NUL 即认为不是文本（trace 里有 PNG 等资源）"""
    return b"\x00" in data[:2048]


def _sanitize_node(node):
    """递归脱敏：敏感头的值 → [REDACTED]；URL 形态字符串 → 去签名参数。"""
    if isinstance(node, dict):
        # Playwright 的 headers 两种形态：{"cookie": "..."} 与
        # [{"name": "cookie", "value": "..."}]
        name = node.get("name")
        if name is None:
            name = node.get("key")
        if isinstance(name, str) and name.lower() in SENSITIVE_HEADERS and "value" in node:
            out = dict(node)
            out["value"] = REDACTED
            return out
        return {k: (REDACTED if isinstance(k, str) and k.lower() in SENSITIVE_HEADERS
                    else _sanitize_node(v))
                for k, v in node.items()}
    if isinstance(node, list):
        return [_sanitize_node(x) for x in node]
    if isinstance(node, str):
        return sanitize_url(sanitize_text(node))
    return node


def sanitize_trace_text(text: str) -> str:
    """逐行脱敏 trace 文本。

    能解析成 JSON 的行走**结构脱敏**（可靠，能处理 headers 数组形态）；
    解析不了的行走**保守正则兜底**——宁可多抹，不可漏抹。
    """
    if not text:
        return text
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append(line)
            continue
        try:
            obj = json.loads(stripped)
        except Exception:
            out.append(_FALLBACK_HDR_RE.sub(r"\1" + REDACTED, sanitize_url(sanitize_text(line))))
            continue
        out.append(json.dumps(_sanitize_node(obj), ensure_ascii=False))
    return "\n".join(out)


def sanitize_trace_archive(src: str, dst: str) -> dict:
    """把 trace 归档（zip）逐条目脱敏后写到 `dst`。返回统计字典。

    **不修改原文件**——原始 trace 只读，脱敏产物另存，便于对照。
    文本条目才脱敏；二进制资源（截图等）原样复制，但**不参与任何导出**。
    """
    stats = {"entries": 0, "text_sanitized": 0, "changed": 0, "binary_copied": 0}
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            stats["entries"] += 1
            low = item.filename.lower()
            treat_as_text = low.endswith(_TEXT_SUFFIXES) or not _is_binary(data)
            if not treat_as_text:
                zout.writestr(item, data)
                stats["binary_copied"] += 1
                continue
            try:
                text = data.decode("utf-8")
            except Exception:
                zout.writestr(item, data)
                stats["binary_copied"] += 1
                continue
            clean = sanitize_trace_text(text)
            if clean != text:
                stats["changed"] += 1
            zout.writestr(item, clean.encode("utf-8"))
            stats["text_sanitized"] += 1
    return stats


def trace_archive_dir(root: Optional[str] = None) -> str:
    """取证归档目录：**运行期数据根**之下（正常情况下不在仓库内）。

    ⚠️ 便携模式（`KIANA_PORTABLE=1`）下数据根会落成 `<仓库>/KianaData/`——
    该目录已在 `.gitignore` 里；`archive_is_ignored()` 会把这个前提**断言成测试**。
    """
    if root is not None:
        return str(root)
    from .config import data_root
    return os.path.join(str(data_root()), "traces")


def archive_is_outside_repo(root: Optional[str] = None) -> bool:
    """归档目录**不在仓库内**，或位于已被 `.gitignore` 覆盖的 `KianaData/` 之下。"""
    try:
        from pathlib import Path
        repo = Path(__file__).resolve().parent.parent
        d = Path(trace_archive_dir(root)).resolve()
        if repo not in d.parents and d != repo:
            return True                      # 在仓库外 —— 正常
        # 在仓库内：只有 KianaData/（便携模式）可接受，它已被 .gitignore 覆盖
        return "KianaData" in d.parts
    except Exception:
        return False


def archive_trace(src_zip: str, *, root: Optional[str] = None, name: Optional[str] = None,
                  keep_original: bool = True) -> Optional[dict]:
    """把一份 trace **脱敏后**归档到运行期数据根。返回 `{path, stats}`；失败返回 None。

    **本目录不参与任何导出**（红线②）——它含 DOM 与网络记录，是诊断材料，不是交付物。
    """
    try:
        src = str(src_zip)
        if not os.path.exists(src):
            logger.warning(f"[取证] 源 trace 不存在: {src}")
            return None
        out_dir = trace_archive_dir(root)
        os.makedirs(out_dir, exist_ok=True)
        base = name or os.path.basename(src)
        if not base.endswith(".zip"):
            base += ".zip"
        dst = os.path.join(out_dir, base)
        stats = sanitize_trace_archive(src, dst)
        logger.info(f"[取证] trace 已脱敏归档: {dst} {stats}")
        if not keep_original:
            try:
                os.remove(src)
            except Exception as e:
                logger.warning(f"[取证] 原始 trace 删除失败（保留）: {e}")
        return {"path": dst, "stats": stats}
    except Exception as e:
        # 取证失败不能拖垮任务本身，但**必须可读**
        logger.warning(f"[取证] 归档失败（原始 trace 保留，未删）: {e}")
        return None
