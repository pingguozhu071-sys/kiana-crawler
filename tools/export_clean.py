# -*- coding: utf-8 -*-
"""把爬虫的原始产物整理成**干净的成品**（默认输出到桌面）。

**为什么需要它**：爬虫的原始产出（JSON / JSONL / CSV / markdown / 图片 / 日志 / 数据库）
全混在一个目录里，对人不友好。本工具把它收敛成"一篇一个干净文件 + 一页目录"。

    python tools/export_clean.py --src <爬虫产出目录> --dest <输出目录>

爬虫原始产出（JSON / JSONL / CSV / markdown / 图片 / 日志 / 数据库）混在一个目录里，
对人不友好。本脚本把它收敛成：

    <桌面>/Kiana爬取成果-公众号/
    ├── 目录.md                    ← 一页看清抓了什么
    ├── 01_<标题>.md               ← 每篇一个干净文件（元信息 + 正文 + 配图）
    ├── 02_<标题>.md
    └── 图片/
        ├── 01-01.jpg
        └── ...

**只收"真文章"**：URL 形如 `/s/<id>`、有标题、正文长度达标。
爬虫顺带抓到的页面碎片（`/shop/ssr/`、错误上报接口、登录页）**一律不进成品**。
"""
import json
import re
import sys
import shutil
import urllib.request
from pathlib import Path

# [v6 修复·推仓库前清理] 原来这里把**机主本机的绝对路径**当默认值：
#     SRC  = ... else r"C:\Users\miku0\AppData\Local\Temp\kiana-wx3"
#     DEST = ... else r"C:\Users\miku0\Desktop\Kiana爬取成果-公众号"
# 推到别人的仓库里，这两行会指向**别人机器上不存在的目录**，
# 还顺带暴露了原作者的用户名。**改成两个参数都必填**，报错说清怎么用。
if len(sys.argv) <= 2:
    print("用法：python tools/export_clean.py 「爬虫产物目录」 「输出目录」\n"
          "  例：python tools/export_clean.py \"%TEMP%\\kiana测试\" \"%USERPROFILE%\\Desktop\\成品\"",
          file=sys.stderr)
    raise SystemExit(2)
SRC = Path(sys.argv[1])
DEST = Path(sys.argv[2])
if not SRC.exists():
    print(f"❌ 爬虫产物目录不存在：{SRC}", file=sys.stderr)
    raise SystemExit(2)

# Windows 文件名非法字符
_BAD = re.compile(r'[\\/:*?"<>|\r\n\t]')


def safe_name(s: str, limit: int = 60) -> str:
    s = _BAD.sub("_", (s or "").strip())
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:limit] or "untitled"


# ── 正文清洗 ────────────────────────────────────────────────────────
# 微信文章的正文里混着大量**界面文字**（不是文章内容）。实测抓到两类：
#   开头：标题重复 / `原创` / 作者名 / `在小说阅读器读本章` / `去阅读` /
#         `在公众号小说中沉浸阅读` —— 还会混进"推荐阅读"里**别的文章的作者名**
#   结尾：`视频` `小程序` `赞` `在看` `分享` `留言` `收藏` `听过` / `，轻点两下取消赞` …
# 以及大量**孤立的标点行**（`，` `。` 各占一行）。
_NOISE_EXACT = {
    "原创", "视频", "小程序", "赞", "在看", "分享", "留言", "收藏", "听过",
    "去阅读", "在小说阅读器读本章", "在公众号小说中沉浸阅读",
    "继续滑动看下一个", "轻触阅读原文", "向上滑动看下一个",
    "预览时标签不可点", "关注该公众号", "阅读原文", "知道了", "取消", "允许",
    "写下你的留言", "选择留言身份", "长按识别二维码", "扫码关注",
}
_NOISE_PREFIX = ("，轻点两下取消", "轻点两下取消", "轻点两下", "视频号")
_PUNCT_ONLY = re.compile(r"^[，。、；：！？…·—\-~～\s\"'“”‘’()（）\[\]【】]+$")
# 开头"短行块"：正文开头的标题/作者/界面字样都很短，用它判定头部到哪里为止
_HEAD_SHORT = 12


def clean_body(text: str, title: str, author: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines()]
    kept = []
    for ln in lines:
        if not ln:
            kept.append("")
            continue
        if ln in _NOISE_EXACT or ln.startswith(_NOISE_PREFIX):
            continue
        if _PUNCT_ONLY.match(ln):
            continue
        if ln == title.strip() or (author and ln == author.strip()):
            continue
        kept.append(ln)
    # 掐头：正文开头那一段"短行"（标题/原创/作者/界面词）整块去掉，
    # 但**只掐开头**，且遇到第一个"像正文的行"就停——避免误伤正文里的短句。
    i = 0
    while i < len(kept) and (not kept[i] or len(kept[i]) <= _HEAD_SHORT):
        i += 1
    kept = kept[i:]
    # 去尾：尾部残留的界面词/短行
    j = len(kept)
    while j > 0 and (not kept[j - 1] or len(kept[j - 1]) <= _HEAD_SHORT):
        j -= 1
    kept = kept[:j]
    # 压缩连续空行
    out, blank = [], False
    for ln in kept:
        if not ln:
            if not blank:
                out.append("")
            blank = True
        else:
            out.append(ln)
            blank = False
    return "\n".join(out).strip()


def load_articles(src: Path):
    """从爬虫产出里挑出**真文章**"""
    out = []
    for p in src.rglob("*.json"):
        if "mp.weixin.qq.com" not in str(p):
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        url = str(d.get("url") or "")
        title = str(d.get("title") or "").strip()
        text = str(d.get("text") or "").strip()
        # 真文章判据：/s/ 形态的链接 + 有标题 + 正文够长
        if not re.search(r"mp\.weixin\.qq\.com/s/", url):
            continue
        if not title or len(text) < 100:
            continue
        out.append({
            "title": title, "author": str(d.get("author") or "").strip(),
            "text": text, "url": url,
            "images": [u for u in (d.get("images") or []) if isinstance(u, str)],
            "source_json": p,
        })
        out[-1]["clean"] = clean_body(out[-1]["text"], out[-1]["title"], out[-1]["author"])
    # 同一篇可能被抓多次 → 按 url 去重，保正文最长的
    best = {}
    for a in out:
        k = a["url"]
        if k not in best or len(a["text"]) > len(best[k]["text"]):
            best[k] = a
    return sorted(best.values(), key=lambda a: a["title"])


def fetch_image(url: str, dest: Path) -> bool:
    """下载配图。微信图床对 Referer 敏感，带上来源站；失败就跳过（不阻断）"""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/131.0.0.0 Safari/537.36"),
            "Referer": "https://mp.weixin.qq.com/",
        })
        with urllib.request.urlopen(req, timeout=20) as r:
            blob = r.read()
        if len(blob) < 512:          # 太小 = 占位图/错误页
            return False
        dest.write_bytes(blob)
        return True
    except Exception:
        return False


def ext_of(url: str) -> str:
    u = url.split("?")[0].lower()
    for e in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
        if u.endswith(e):
            return ".jpg" if e == ".jpeg" else e
    m = re.search(r"wx_fmt=(\w+)", url)
    if m:
        return "." + ("jpg" if m.group(1) == "jpeg" else m.group(1))
    return ".jpg"


# ── 视频整理 ────────────────────────────────────────────────────────
def load_videos(src: Path):
    """从 yt-dlp 留下的 `*.info.json` 取元信息，并找到同名的视频/封面文件"""
    out = []
    for p in src.rglob("*.info.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(d, dict) or not d.get("title"):
            continue
        stem = p.name[: -len(".info.json")]
        sib = p.parent
        vid = next((sib / (stem + e) for e in (".mp4", ".mkv", ".webm", ".flv")
                    if (sib / (stem + e)).exists()), None)
        if vid is None:
            continue
        cover = next((sib / (stem + e) for e in (".jpg", ".png", ".webp")
                      if (sib / (stem + e)).exists()), None)
        out.append({
            "title": str(d["title"]),
            "uploader": str(d.get("uploader") or d.get("channel") or ""),
            "duration": d.get("duration") or 0,
            "width": d.get("width"), "height": d.get("height"), "fps": d.get("fps"),
            "views": d.get("view_count"), "upload_date": str(d.get("upload_date") or ""),
            "url": str(d.get("webpage_url") or ""),
            "video": vid, "cover": cover, "bytes": vid.stat().st_size,
        })
    return sorted(out, key=lambda v: v["title"])


def _fmt_dur(sec) -> str:
    try:
        s = int(float(sec))
    except Exception:
        return "?"
    return f"{s // 60}:{s % 60:02d}"


def export_videos(src: Path, dest: Path) -> int:
    vids = load_videos(src)
    if not vids:
        return 0
    (dest / "视频").mkdir(parents=True, exist_ok=True)
    (dest / "封面").mkdir(parents=True, exist_ok=True)
    index = ["# 视频爬取成果", "", f"- 来源目录：`{src}`", f"- 视频数：**{len(vids)}**", ""]
    for i, v in enumerate(vids, 1):
        stem = f"{i:02d}_{safe_name(v['title'])}"
        vname = stem + v["video"].suffix
        shutil.copy2(v["video"], dest / "视频" / vname)
        cover_line = "（无封面）"
        if v["cover"]:
            cname = stem + v["cover"].suffix
            shutil.copy2(v["cover"], dest / "封面" / cname)
            cover_line = f"![封面](封面/{cname})"
        res = f"{v['width']}x{v['height']}" if v["width"] else "?"
        fps = f"{float(v['fps']):.0f}fps" if v["fps"] else "?"
        md = [f"# {v['title']}", "",
              "| 项 | 内容 |", "|---|---|",
              f"| UP 主 | {v['uploader'] or '（未知）'} |",
              f"| 时长 | {_fmt_dur(v['duration'])} |",
              f"| 画质 | {res} {fps} |",
              f"| 体积 | {v['bytes'] / 1048576:.1f} MB |",
              f"| 播放量 | {v['views'] if v['views'] is not None else '?'} |",
              f"| 发布 | {v['upload_date'] or '?'} |",
              f"| 原链接 | {v['url']} |", "",
              "---", "", cover_line, "",
              f"视频文件：[`视频/{vname}`](视频/{vname})"]
        (dest / f"{stem}.md").write_text("\n".join(md), encoding="utf-8")
        index.append(f"{i}. **{v['title']}** — {v['uploader'] or '未知'}，{_fmt_dur(v['duration'])}，"
                     f"{res}，{v['bytes'] / 1048576:.1f}MB → [`视频/{vname}`](视频/{vname})")
    index += ["", "---", "", "> 由 Kiana 爬虫抓取并整理。"]
    (dest / "目录.md").write_text("\n".join(index), encoding="utf-8")
    print(f"✅ 视频成品：{dest}（{len(vids)} 个）")
    for v in vids:
        print(f"   · {v['title'][:44]}  {v['width']}x{v['height']}")
    return len(vids)


def main():
    # 视频产物（`*.info.json` + 同名 mp4）→ 走视频整理
    n_vid = export_videos(SRC, DEST)
    if n_vid:
        return 0

    arts = load_articles(SRC)
    if not arts:
        print("没找到可整理的产物（既没有公众号文章的 JSON，也没有 *.info.json 视频）")
        return 1

    (DEST / "图片").mkdir(parents=True, exist_ok=True)
    index = ["# 公众号爬取成果", "",
             f"- 来源目录：`{SRC}`",
             f"- 文章数：**{len(arts)}**", ""]

    total_imgs = got_imgs = 0
    for i, a in enumerate(arts, 1):
        stem = f"{i:02d}_{safe_name(a['title'])}"
        total_imgs += len(a["images"])
        # 下载配图
        img_lines, saved = [], []
        for j, u in enumerate(a["images"], 1):
            fn = f"{i:02d}-{j:02d}{ext_of(u)}"
            if fetch_image(u, DEST / "图片" / fn):
                got_imgs += 1
                saved.append(fn)
                img_lines.append(f"![配图 {j}](图片/{fn})")
            else:
                img_lines.append(f"- 配图 {j}（未下载成功）：{u}")
        # 写文章
        body = [
            f"# {a['title']}", "",
            "| 项 | 内容 |", "|---|---|",
            f"| 作者 | {a['author'] or '（未标注）'} |",
            f"| 正文字数 | {len(a['clean'])}（原始 {len(a['text'])}，已剔除界面文字） |",
            f"| 配图 | {len(saved)}/{len(a['images'])} 张已下载 |",
            f"| 原文链接 | {a['url']} |", "",
            "---", "", "## 正文", "", clean_body(a["text"], a["title"], a["author"]), "",
        ]
        if img_lines:
            body += ["---", "", "## 配图", ""] + [x + "\n" for x in img_lines]
        (DEST / f"{stem}.md").write_text("\n".join(body), encoding="utf-8")
        index.append(f"{i}. **{a['title']}** — {a['author'] or '未标注'}，"
                     f"{len(a['clean'])} 字，{len(saved)} 图 → [`{stem}.md`]({stem}.md)")

    index += ["", "---", "",
              f"配图合计：**{got_imgs}/{total_imgs}** 张下载成功（失败的已保留原始链接）",
              "", "> 由 Kiana 爬虫抓取并整理；原始产物为 JSON/JSONL/CSV，此处只保留文章本身。"]
    (DEST / "目录.md").write_text("\n".join(index), encoding="utf-8")

    print(f"✅ 成品已生成：{DEST}")
    print(f"   文章 {len(arts)} 篇，配图 {got_imgs}/{total_imgs} 张")
    for a in arts:
        print(f"   · {a['title'][:44]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
