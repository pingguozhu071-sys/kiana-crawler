# -*- coding: utf-8 -*-
"""防盗链 Referer **唯一实现**守卫

**发现的真问题**：同一个能力此前散在三处、**三种做法**：

| 位置 | 原做法 | 后果 |
|---|---|---|
| `media_downloader._referer_for` | 有 **CDN→主站映射**（`i0.hdslb.com` → bilibili） | ✅ 正确 |
| `universal_downloader` 下载头 | `f"https://{host}/"`（**CDN 自己的主机**） | ❌ 错 Referer |
| `universal_downloader.download_image` | 调用方不传就**完全不发** Referer | ❌ 无 Referer |
| `crawler` 种子图片 | `referer=url`（图片自己的地址） | ❌ 错 Referer + 目录归类也不对 |

而工程自己的注释写着："B站图片 i0.hdslb.com 等 CDN **校验主站 Referer**，
微博/公众号图床同理——**无 Referer 直接 403/空响应**"。

也就是说：**后三种做法正好命中会 403 的那两种形态**，而它们都在图片/文件下载链上。

现实现已收到 `url_utils.referer_for`（唯一实现）。
本文件锁：① 映射行为；② 三个调用点都走唯一实现；③ **反向扫描**——
不许再出现"Referer 由裸 netloc 拼出来"的写法。
"""
import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kiana_vnext_plus.url_utils import referer_for        # noqa: E402

PKG = ROOT / "kiana_vnext_plus"

# 必须走唯一实现的调用点
CALLERS = ("media_downloader.py", "universal_downloader.py", "crawler.py")


class TestRefererMapping(unittest.TestCase):
    def test_cdn_maps_to_main_site(self):
        """CDN 主机必须映射到**主站**——这正是防盗链校验的东西"""
        cases = {
            "https://i0.hdslb.com/bfs/archive/x.jpg": "https://www.bilibili.com/",
            "https://wx1.sinaimg.cn/large/y.jpg": "https://weibo.com/",
            "https://p3-sign.douyinpic.com/z.jpg": "https://www.douyin.com/",
            "https://pic1.zhimg.com/v2-abc.jpg": "https://www.zhihu.com/",
            "https://cdn.steamstatic.com/a.png": "https://store.steampowered.com/",
        }
        for url, want in cases.items():
            self.assertEqual(referer_for(url), want, f"{url} 的主站映射不对")

    def test_unknown_host_falls_back_to_itself(self):
        self.assertEqual(referer_for("https://example.com/a.png"), "https://example.com/")

    def test_bad_input_returns_empty(self):
        """推导不出就返回空串——**不抛异常**，由调用方决定是否省略该头"""
        for bad in ("", "not-a-url", "file:///x", None):
            self.assertEqual(referer_for(bad), "")

    def test_never_returns_a_cdn_referer_for_a_mapped_cdn(self):
        """把"错 Referer"这个形态本身钉死：映射过的 CDN 不得回退成自己"""
        for cdn in ("i0.hdslb.com", "wx1.sinaimg.cn", "pic1.zhimg.com"):
            got = referer_for(f"https://{cdn}/x.jpg")
            self.assertNotIn(cdn, got,
                             f"{cdn} 的 Referer 又变回 CDN 自己了（防盗链会 403）：{got}")


class TestSingleSource(unittest.TestCase):
    def test_all_callers_use_the_shared_helper(self):
        for name in CALLERS:
            src = (PKG / name).read_text(encoding="utf-8")
            self.assertIn("referer_for", src,
                          f"{name} 没有使用 url_utils.referer_for —— 又会分叉")

    def test_image_download_has_a_fallback_referer(self):
        """调用方不传 referer 时**必须**回退推导，不能不发（无 Referer 会 403）"""
        src = (PKG / "universal_downloader.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        hit = False
        for n in ast.walk(tree):
            # `download_image` 是 `async def` → 节点类型是 AsyncFunctionDef，
            # 只查 FunctionDef 会**静默匹配不到**（首版就是这么假绿的）
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and n.name == "download_image":
                body = ast.unparse(n)
                hit = "referer_for" in body and "referer or" in body
        self.assertTrue(hit, "download_image 缺少 referer 回退——无 Referer 会 403")


def _host_is_interpolated(v: ast.JoinedStr) -> bool:
    """f-string 的**主机位**是不是插出来的（如 `f"https://{dom}/"`）。

    `f"https://www.bilibili.com/video/{bvid}"` 的主机是**字面量**，属正当写法，不该报。
    """
    prefix = ""
    for x in v.values:
        if isinstance(x, ast.Constant):
            prefix += str(x.value)
        else:
            break                      # 到第一个插值为止
    if "://" not in prefix:
        return True                    # 整串都是插出来的 → 可疑
    return "/" not in prefix.split("://", 1)[1]


class TestNoRefererFromBareNetloc(unittest.TestCase):
    """③ **反向扫描**：不许再有"Referer 由裸 netloc 拼出来"的写法。

    用 AST 找 `{"Referer": f"https://{...}/"}` 这种字典字面量——
    不扫源码文本，否则**注释里提到这个反模式也会被误判**
    （本仓库的注释里就写着 `f"https://{dom}/"` 作为反面说明）。
    """

    def test_no_referer_built_from_a_bare_netloc(self):
        offenders = []
        for p in sorted(PKG.glob("*.py")):
            try:
                tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
            except Exception:
                continue
            for n in ast.walk(tree):
                if not isinstance(n, ast.Dict):
                    continue
                for k, v in zip(n.keys, n.values):
                    if not (isinstance(k, ast.Constant) and str(k.value).lower() == "referer"):
                        continue
                    # 只报**主机位被插值**的形态（`f"https://{dom}/"`）；
                    # `f"https://www.bilibili.com/video/{bvid}"` 主机是字面量，正当。
                    if isinstance(v, ast.JoinedStr) and _host_is_interpolated(v):
                        offenders.append(f"{p.name}:{n.lineno}")
        self.assertEqual(offenders, [],
                         f"仍有 Referer 由裸 netloc 拼出（CDN 会 403）: {offenders}——"
                         f"应改用 url_utils.referer_for")


if __name__ == "__main__":
    unittest.main()
