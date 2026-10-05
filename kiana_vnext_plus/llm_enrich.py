# -*- coding: utf-8 -*-
"""LLM 批量增强 llm_enrich.py（v2.16.1 阶段5）

任务结束后的"数据流批处理"：扫描 export/data/<域名>/*.jsonl 中未增强的行 →
喂 llm_client.async_llm_process_rows（summarize/classify/entities/keywords）→
结果以 _llm 字段回写原文件（原子替换；失败行保持原样不丢数据）。
默认关闭：GUI「设置 → LLM 智能增强」开关（llm_enabled 默认 False），
Key/地址/模型不齐则不构建客户端（引擎侧保证）。
"""
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from .llm_client import async_llm_process_rows

logger = logging.getLogger(__name__)

ENRICH_FIELD = "_llm"
MAX_TEXT = 8000  # 与 llm_client.MAX_TEXT 同源（逐行截断上限）


async def enrich_project(export_dir, client, task: str = "summarize",
                         budget_month: int = 500, limit: Optional[int] = None) -> dict:
    """扫描 export/data 下所有 jsonl 的未增强行 → 批处理 → 按位置回写。
    返回 {done, failed, skipped, budget_left, files}；任何异常不中断调用方。"""
    stats = {"done": 0, "failed": 0, "skipped": 0, "budget_left": 0, "files": 0}
    pre_skipped = 0  # [v2.17 0-5a] 行级跳过单独累计（结果回填不覆盖）
    try:
        data_dir = Path(export_dir) / "data"
        if not data_dir.is_dir():
            return stats
        rows, meta = [], []  # meta: [(file, line_idx)]
        for jf in sorted(data_dir.rglob("*.jsonl")):
            try:
                lines = jf.read_text(encoding="utf-8").splitlines()
            except Exception:
                continue
            for idx, line in enumerate(lines):
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get(ENRICH_FIELD):
                    continue  # 已增强行不再处理（幂等）
                # [FIXED & MODIFIED] v2.17 0-5a 兼容纠偏：质量闸拒收页（LOW_QUALITY 仍落 jsonl）
                # 与空文本行此前也被 enrich——空壳行消耗 LLM 预算（只会产出垃圾摘要）。
                # 跳过：rejected 标记 / 无 title 且无 text。
                if rec.get("rejected") or (
                        not (rec.get("title") or "") and not (rec.get("text") or "")):
                    pre_skipped += 1
                    continue
                rows.append({
                    "url": str(rec.get("url", ""))[:500],
                    "title": str(rec.get("title", ""))[:200],
                    "text": str(rec.get("text", ""))[:MAX_TEXT],
                })
                meta.append((jf, idx))
        if not rows:
            return stats
        # ── [v2.19.10 修复·真机实测发现] 这一段原本**开工无声** ────────────────────
        # 真机证据（2026-10-04）：整次抓取 `Done in 1463s`，其中 **392.3 秒**全花在
        # 下面这一次 await 上；期间只有心跳在刷 `done=150 ... 100.0%` 的**冻结行**
        # ⇒ 这 392 秒**看起来像空转**，实际是 27 个文件的真增强（结果是真产物）。
        # 那次排查的**两次误判**（先猜"在等连不通的外国域名"、后猜"在等在途页面请求"）
        # 与这段静默直接相关。**要修的是"看不见"，不是"太久"**：
        #   · 只补**开工一行**（行数/文件数）与**返回一行**（耗时）；
        #   · ❌ **不加取消超时** —— 会丢产物；
        #   · ❌ **不改成异步不等待** —— 会变成"结果没写完"的新假成功。
        _n_files = len({str(_f) for _f, _i in meta})
        _t_llm = time.monotonic()
        logger.info(
            f"LLM 增强开始：{len(rows)} 行待处理（来自 {_n_files} 个文件，任务 {task}）"
            f"—— 此阶段是**抓取后处理**，抓取本身已结束，心跳会停在 100%，属正常")
        result = await async_llm_process_rows(client, rows, task=task,
                                              budget_month=budget_month, limit=limit)
        logger.info(
            f"LLM 增强批次返回：{len(rows)} 行，耗时 {time.monotonic() - _t_llm:.1f}s"
            f"（接下来按位置回写原文件，原子替换）")
        # 按位置回写（原子：先写 .tmp 再 replace）
        by_file: dict = {}
        for (jf, idx), out_row in zip(meta, result.get("out_rows") or []):
            if out_row:
                by_file.setdefault(jf, {})[idx] = out_row
        for jf, idx_map in by_file.items():
            try:
                lines = jf.read_text(encoding="utf-8").splitlines()
                for idx, out_row in idx_map.items():
                    try:
                        rec = json.loads(lines[idx])
                    except Exception:
                        continue
                    rec[ENRICH_FIELD] = {"task": task, "result": out_row}
                    lines[idx] = json.dumps(rec, ensure_ascii=False)
                tmp = jf.with_suffix(".jsonl.tmp")
                tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
                os.replace(tmp, jf)
                stats["files"] += 1
            except Exception as e:
                logger.warning(f"LLM 增强回写失败({jf}): {e}")
        stats["done"] = result.get("done", 0)
        stats["failed"] = result.get("failed", 0)
        stats["skipped"] = result.get("skipped", 0) + pre_skipped
        stats["budget_left"] = result.get("budget_left", 0)
        return stats
    except Exception as e:
        logger.warning(f"LLM 增强失败（不影响任务结果）: {e}")
        return stats
