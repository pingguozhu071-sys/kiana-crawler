# -*- coding: utf-8 -*-
"""统一媒体输出 schema media_schema.py（v2.17 3.1）

设计参考 02-spiders data2tasks（统一 9 字段协议）与 03 hybrid 归一化（平台→同构键表）
——只学结构设计，实现为本工程重写（结构化媒体条目落盘/入队/审计的唯一入口）。

各平台 resolver 的 dict 输出（douyin/xhs/kuaishou/music163 + 未来平台）经
from_resolver 收敛为 MediaItem；旧 resolve() 返回 dict 保留兼容层
（to_dict(from_resolver(...)) —— 不破坏现有调用）。

[v2.17 B1c → v2.19.8 更正] 收口已接通：四个平台 resolver 的产物均经 from_resolver
收敛为 MediaItem（douyin/xhs/kuaishou/music163 各自 import from_resolver + to_dict），
下载侧按统一 schema 取直链（优先 media.streams[0].url，退回平台历史键）。
"""
from __future__ import annotations

import dataclasses
from typing import List


@dataclasses.dataclass
class MediaStream:
    """单条直链（已无水印预处理）——质量降序由调用方保证"""
    url: str
    quality: str = ""      # 平台原始质量字段（如 "1080p" / "bitrate=1118" / "exhigh"）
    container: str = ""    # mp4/m4a/mp3/...
    no_watermark: bool = True


@dataclasses.dataclass
class MediaItem:
    """统一媒体条目（v2.17 3.1）"""
    source: str            # douyin|xhs|kuaishou|music163|bilibili|generic
    media_id: str
    kind: str              # video|image_set|audio
    title: str = ""
    desc: str = ""
    author: str = ""
    create_time: int = 0
    cover_urls: List[str] = dataclasses.field(default_factory=list)
    streams: List[MediaStream] = dataclasses.field(default_factory=list)
    images: List[str] = dataclasses.field(default_factory=list)
    method: str = ""       # 提取通道（审计：page_state/abogus/weapi/...）
    raw: dict = dataclasses.field(default_factory=dict)  # 平台原始字段（审计/重跑）


def from_resolver(source: str, payload: dict) -> MediaItem:
    """各平台 resolver dict → MediaItem（未列出的源走 generic 兜底）"""
    payload = payload or {}
    if source == "douyin":
        vurl = str(payload.get("video_url") or "")
        return MediaItem(
            source=source, media_id=str(payload.get("aweme_id") or ""),
            kind="video" if vurl else "image_set",
            cover_urls=list(payload.get("images") or []),
            streams=[MediaStream(url=vurl, quality=f"{payload.get('bitrate', 0)}bps",
                                 container="mp4",
                                 no_watermark=("playwm" not in vurl))] if vurl else [],
            method=payload.get("method", ""), raw=payload)
    if source == "music163":
        murl = str(payload.get("media_url") or "")
        return MediaItem(
            source=source, media_id=str(payload.get("song_id") or ""),
            kind=str(payload.get("type") or "audio"),
            streams=[MediaStream(url=murl, quality=payload.get("level", ""),
                                 container="m4a")] if murl else [],
            method=payload.get("method", ""), raw=payload)
    if source in ("xhs", "kuaishou"):
        vurl = str(payload.get("video_url") or "")
        imgs = list(payload.get("images") or [])
        return MediaItem(
            source=source, media_id=str(payload.get("note_id") or
                                        payload.get("video_id") or ""),
            kind="video" if vurl else ("image_set" if imgs else "image_set"),
            desc=str(payload.get("desc") or ""), images=imgs,
            streams=[MediaStream(url=vurl, container="mp4")] if vurl else [],
            method=payload.get("method", ""), raw=payload)
    # generic 兜底（未知源不抛——按字段探测）
    vurl = str(payload.get("video_url") or payload.get("media_url") or "")
    imgs = list(payload.get("images") or [])
    return MediaItem(
        source=source, media_id=str(payload.get("media_id") or ""),
        kind="video" if vurl else ("audio" if payload.get("type") == "audio" else "image_set"),
        title=str(payload.get("title") or ""), desc=str(payload.get("desc") or ""),
        images=imgs,
        streams=[MediaStream(url=vurl)] if vurl else [],
        method=str(payload.get("method") or ""), raw=payload)


def to_dict(item: MediaItem) -> dict:
    """MediaItem → dict（落盘/入队序列化；与旧 resolver dict 兼容共存）"""
    return {
        "source": item.source, "media_id": item.media_id, "kind": item.kind,
        "title": item.title, "desc": item.desc, "author": item.author,
        "create_time": item.create_time, "cover_urls": list(item.cover_urls),
        "streams": [{"url": s.url, "quality": s.quality, "container": s.container,
                     "no_watermark": s.no_watermark} for s in item.streams],
        "images": list(item.images), "method": item.method,
    }


def is_valid(item: MediaItem) -> bool:
    """完整性校验（ItemPipeline/落盘前）：source+media_id 且至少一个可用流/图"""
    if not item.source or not item.media_id:
        return False
    return bool(item.streams or item.images)


__all__ = ["MediaStream", "MediaItem", "from_resolver", "to_dict", "is_valid"]
