"""v2.16 M3：YT 一键下载（PO Token 自动拉起 + 音视频封面）

用法:
    python tools/yt_download.py "<youtube_url>" [--audio] [--out DIR]

流程: 确保 PO Token server(:4416) → yt-dlp 下载（音视频+封面）→ 输出统计。
无干净出口时媒体流可能 403（诚实提示"换干净出口"）。
"""
import sys
import os
import glob
import shutil
import subprocess
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def _ensure_pot():
    """确保 PO Token server 在跑（自动拉起；打包/本机已知路径）"""
    from kiana_vnext_plus.universal_downloader import pot_server_ready, pot_server_ensure
    if pot_server_ready():
        print("[YT] PO Token server 已在运行 (:4416)")
        return True
    if pot_server_ensure():
        print("[YT] PO Token server 已自动拉起")
        return True
    print("[YT] 未能启动 PO Token server——YT 解析可能失败（换干净出口前提示）")
    return False


def _youtube_download(url, out_dir, audio_only=False):
    """用 yt-dlp 下载（PO Token 插件自动生效）"""
    import yt_dlp
    out_dir = pathlib.Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    opts = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "socket_timeout": 60, "ignoreconfig": True,
        "continuedl": True, "nopart": False,
        "retries": 5, "fragment_retries": 5,
        "outtmpl": str(out_dir / "%(title).80s.%(ext)s"),
        "paths": {"home": str(out_dir)},
        # 使用打包/本机 ffmpeg（与主引擎同策略）
        "ffmpeg_location": _ffmpeg_dir(),
    }
    if audio_only:
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}]
    else:
        opts["format"] = "bestvideo+bestaudio/best"
        opts["merge_output_format"] = "mp4"
        opts["writethumbnail"] = True
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        title = (info or {}).get("title", "?")
        dur = (info or {}).get("duration", 0)
        return title, dur


def _ffmpeg_dir():
    """与 universal_downloader 同策略：打包内 → 系统 PATH → LOCALAPPDATA ffmpeg"""
    import sys as _sys
    meipass = getattr(_sys, "_MEIPASS", "")
    if meipass:
        p = pathlib.Path(meipass) / "bin" / "ffmpeg.exe"
        if p.exists():
            return str(p.parent)
    p = shutil.which("ffmpeg")
    if p:
        # [FIXED & MODIFIED] v2.16.1 ffmpeg_location 应指向含 ffmpeg.exe 的目录——
        # 原 parent.parent 多取了一层（bin/ffmpeg.exe 的上级是 bin 才对）
        return str(pathlib.Path(p).parent)
    local = pathlib.Path(os.environ.get("LOCALAPPDATA", "")) / "ffmpeg"
    for d in local.glob("ffmpeg-*/") if local.exists() else []:
        binp = d / "bin"
        if (binp / "ffmpeg.exe").exists():
            return str(binp)
    return ""


def main():
    import argparse
    ap = argparse.ArgumentParser(description="YT 一键下载")
    ap.add_argument("url")
    ap.add_argument("--audio", action="store_true", help="仅音频(mp3)")
    ap.add_argument("--out", default="", help="输出目录")
    args = ap.parse_args()

    _ensure_pot()
    out = args.out or os.path.join(os.environ.get("LOCALAPPDATA", ""), "KianaVnextPlus", "yt")
    try:
        t0 = time.time()
        title, dur = _youtube_download(args.url, out, audio_only=args.audio)
        elapsed = time.time() - t0
        print(f"✅ 下载完成: {title[:60]}  (时长 {dur}s, 耗时 {elapsed:.0f}s)")
        print(f"  产物目录: {out}")
    except Exception as e:
        msg = str(e)
        if "403" in msg or "Forbidden" in msg:
            print("❌ 媒体流 403——出口 IP 被 YT 风控，请换干净出口（手机热点/家宽）")
        else:
            print(f"❌ 下载失败: {msg[:200]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
