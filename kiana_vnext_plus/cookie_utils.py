# -*- coding: utf-8 -*-
"""cookies 多文件解析（v2.17.1 公开分发：密钥全用户自管，可同时填多个）

GUI/CLI 使用同一规则：分号或换行分隔均认；空行/# 注释忽略；返回去重后的路径列表。
"""
from typing import List


def parse_cookie_file_list(text) -> List[str]:
    """用户输入（多行或分号分隔）→ 路径列表（去重、保序；./~ 不展开——按原样交文件层）。"""
    out, seen = [], set()
    for chunk in str(text or "").replace("\r", "\n").replace(";", "\n").split("\n"):
        p = chunk.strip().strip("\"'")
        if not p or p.startswith("#"):
            continue
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out
