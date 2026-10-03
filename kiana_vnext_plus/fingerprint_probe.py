# -*- coding: utf-8 -*-
"""页面结构指纹与变化告警（M2 线上探针）

**要解决的问题**：工程有 700+ 条离线用例，它们证明的是"规则**没坏**"，
**证明不了"站点还能用"**。这两个命题之间的差额，就是本模块要补的。

**做法**：给每个规则/解析目标算一个**结构指纹**——**不是全文哈希**
（全文哈希改个日期、换个推荐位就报警，噪声大到没法用），
而是"页面上关键节点长什么样"的摘要：把 DOM 里每个元素的
`标签` 与 `标签.首个类名` 作为 token，再复用工程既有的 `parser.simhash_64`
做 64 位摘要。**文本内容不参与**，所以"内容变了"不会报警，"结构变了"才会。

三条纪律（来自施工方案 M2，都有回归锁死）：
  ① 指纹是**观测数据**——落运行期数据根，**不进版本库**；
  ② 探针失败（超时 / 被拦 / 解析不了）**不计入**"结构变了"的判定——
     站点临时抽风 ≠ 规则坏了。这是本模块最容易写错的一条；
  ③ 连续 N 次低于阈值才判"结构变了"，单次噪声只告警。
"""
import json
import logging
import os
from typing import List, Optional, Tuple

from .parser import simhash_64, simhash_hamming
from .rate_limiter import RateLimiter
from .url_utils import safe_get

try:
    from lxml import html as _lxml_html
except Exception:                                   # pragma: no cover
    _lxml_html = None

logger = logging.getLogger(__name__)

# ── 判定结果 ──
VERDICT_OK = "ok"                 # 结构正常（相似度达标）
VERDICT_WARN = "warn"             # 低于阈值但未达连续次数——**先只告警**
VERDICT_CHANGED = "changed"       # 连续 N 次低于阈值 → 判"结构变了"
PROBE_FAILED = "probe_failed"     # 取不到指纹 → **不计入判定**，也不惩罚
VERDICT_BASELINE = "baseline"     # 首次探测：只建基线，不判定

DEFAULT_SIMILARITY_THRESHOLD = 0.90
DEFAULT_STRIKES_NEEDED = 3
_MAX_ELEMENTS = 3000              # 超大页面截断，避免指纹计算被单页拖死
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "path"})


def structure_tokens(html_text: str) -> List[str]:
    """把 HTML 的**结构**抽成 token 序列（不含文本内容）。

    每个元素贡献 1~2 个 token：`<tag>`，有 class 时再加 `<tag>.<首个类名>`。
    `script/style/...` 跳过（它们随站点改版频繁变动，且与"页面结构"无关）。
    """
    if not html_text or _lxml_html is None:
        return []
    try:
        doc = _lxml_html.fromstring(html_text)
    except Exception:
        return []
    out: List[str] = []
    try:
        for el in doc.iter():
            tag = getattr(el, "tag", None)
            if not isinstance(tag, str):
                continue                     # 注释 / 处理指令
            tag = tag.lower()
            if tag in _SKIP_TAGS:
                continue
            out.append(tag)
            cls = el.get("class") if hasattr(el, "get") else None
            if cls:
                first = str(cls).split()[0].strip()
                if first:
                    out.append(f"{tag}.{first}")
            if len(out) >= _MAX_ELEMENTS:
                break
    except Exception as e:
        logger.debug(f"structure_tokens 遍历异常: {e}")
        return []
    return out


def structure_fingerprint(html_text: str) -> int:
    """结构指纹：结构 token 的 64 位 SimHash。**取不到（解析失败/空页）返回 0**。

    返回 0 是一个**显式信号**：调用方据此走"探针失败"分支，
    **不得**当成"结构变了"（见 `decide`）。
    """
    toks = structure_tokens(html_text)
    if not toks:
        return 0
    return int(simhash_64(" ".join(toks)))


def similarity(a: int, b: int) -> float:
    """两个指纹的相似度 = 1 - Hamming/64（0.0~1.0）"""
    if not a or not b:
        return 0.0
    return 1.0 - (simhash_hamming(int(a), int(b)) / 64.0)


def decide(prev_fp: int, cur_fp: int, *, strikes: int = 0,
           threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
           strikes_needed: int = DEFAULT_STRIKES_NEEDED
           ) -> Tuple[str, float, int]:
    """判定一次探测结果 → `(verdict, similarity, new_strikes)`。

    **探针失败优先**：任一侧指纹为 0（页面取不到 / 解析不了 / 被拦），
    返回 `PROBE_FAILED` 且 **strikes 原样返回**（不累计、不惩罚）。
    这条是本模块的核心纪律——站点临时抽风不能把规则判死。
    """
    if not prev_fp or not cur_fp:
        return PROBE_FAILED, 0.0, strikes
    sim = similarity(prev_fp, cur_fp)
    if sim >= threshold:
        return VERDICT_OK, sim, 0            # 恢复正常 → 连续计数清零
    n = strikes + 1
    if n >= strikes_needed:
        return VERDICT_CHANGED, sim, n
    return VERDICT_WARN, sim, n


class FingerprintStore:
    """指纹的**观测数据**存储（不进版本库）。

    默认落在 `data_root()/probe/fingerprints.json`；构造函数可注入任意根目录，
    便于测试与自定义部署。
    """

    def __init__(self, root: Optional[str] = None):
        if root is None:
            from .config import data_root
            root = os.path.join(str(data_root()), "probe")
        self.dir = str(root)
        self.path = os.path.join(self.dir, "fingerprints.json")

    def load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as e:
            # 读失败不静默：否则"指纹全丢"会被当成"全是新站点"
            logger.warning(f"指纹库读取失败（按空库处理）: {e}")
            return {}

    def save(self, data: dict) -> bool:
        try:
            os.makedirs(self.dir, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)       # 原子替换，避免半截文件
            return True
        except Exception as e:
            logger.warning(f"指纹库写入失败: {e}")
            return False

    def get(self, key: str) -> dict:
        v = self.load().get(key)
        return v if isinstance(v, dict) else {}

    def put(self, key: str, fingerprint: int, *, url: str = "", meta: Optional[dict] = None) -> dict:
        data = self.load()
        rec = {"fingerprint": int(fingerprint), "url": url, "strikes": 0}
        if meta:
            rec.update(meta)
        data[key] = rec
        self.save(data)
        return rec


class StructureProbe:
    """线上结构探针：**只做结构比对**——不抓内容、不入库、不进任务列表。

    与爬虫的关系是"旁路"：
      · **独立限流桶**（`limiter`）——绝不挤占正常抓取的额度；
      · 取页面**必须走 `url_utils.safe_get`**（SSRF 闸：入口校验 + 逐跳复检），
        这是工程红线，探针不是例外；
      · 失败只是"没探到"，**不产生任何结构判定**，也不改动既有基线记录。

    `changed` 之后**不自动接受新结构**——保持告警直到人工确认，
    因为"站点改版了"是**需要人决策**的事，不该由探针悄悄把新结构当成正常。
    确认走 `accept()`。
    """

    def __init__(self, *, store: Optional[FingerprintStore] = None,
                 limiter: Optional[RateLimiter] = None,
                 timeout: float = 15.0,
                 threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
                 strikes_needed: int = DEFAULT_STRIKES_NEEDED,
                 max_chars: int = 2_000_000):
        self.store = store if store is not None else FingerprintStore()
        # 独立桶：速率压得很低（探针是低频旁路），且**有自己的实例**，不与抓取共享
        self.limiter = limiter if limiter is not None else RateLimiter(
            global_rate=1.0, global_burst=2.0, domain_rate=0.2, domain_burst=1.0,
            probe_interval=0, probe_count=0)
        self.timeout = float(timeout)
        self.threshold = float(threshold)
        self.strikes_needed = int(strikes_needed)
        self.max_chars = int(max_chars)

    @staticmethod
    def _result(verdict: str, *, key: str, similarity: float = 0.0,
                strikes: int = 0, fingerprint: int = 0, detail: str = "") -> dict:
        return {"key": key, "verdict": verdict, "similarity": round(similarity, 4),
                "strikes": strikes, "fingerprint": int(fingerprint), "detail": detail}

    def _failed(self, key: str, detail: str) -> dict:
        """探针失败：**只报告，不改动任何既有记录**（基线、连续次数都不动）。

        **如实回显当前存储的连续次数**，而不是报 0——报 0 会让人以为计数被清零了，
        而实际上我们一个字都没改。"没改"与"清零"是两回事，混淆它们会让
        "连续 3 次才判 changed"这条纪律看起来失效。
        """
        logger.info(f"[探针] {key} 未取到结构：{detail}")
        try:
            rec = self.store.get(key)
            strikes = int(rec.get("strikes") or 0)
            prev = int(rec.get("fingerprint") or 0)
        except Exception:
            strikes, prev = 0, 0
        return self._result(PROBE_FAILED, key=key, strikes=strikes,
                            fingerprint=prev, detail=detail)

    async def probe(self, key: str, url: str, *, session, headers: Optional[dict] = None) -> dict:
        """探测一个目标 → 结果字典（`verdict` 见模块顶部常量）。

        任何"没取到/解析不出"的情形一律 `probe_failed`：**不计入结构判定**。
        """
        if not key or not url:
            return self._failed(key, "缺少 key 或 url")

        try:
            await self.limiter.acquire(url)          # 独立桶限速
        except Exception as e:
            return self._failed(key, f"限速器异常: {type(e).__name__}")

        try:
            resp = await safe_get(session, url, headers=headers, timeout=int(self.timeout))
        except Exception as e:
            return self._failed(key, f"请求异常: {type(e).__name__}")
        if resp is None:
            # safe_get 被闸拦下或请求失败都返回 None —— 一律不判死
            return self._failed(key, "被安全闸拦截或请求失败")

        try:
            html = getattr(resp, "text", "") or ""
        except Exception:
            html = ""
        if not html:
            return self._failed(key, "响应为空")
        if len(html) > self.max_chars:
            html = html[:self.max_chars]

        cur = structure_fingerprint(html)
        if not cur:
            return self._failed(key, "页面取到但解析不出结构")

        rec = self.store.get(key)
        prev = int(rec.get("fingerprint") or 0)
        strikes = int(rec.get("strikes") or 0)

        if not prev:
            # 首次：**只建基线**，不作判定（没有基准就没有"变化"可言）
            self.store.put(key, cur, url=url, meta={"strikes": 0})
            return self._result(VERDICT_BASELINE, key=key, similarity=1.0,
                                strikes=0, fingerprint=cur, detail="已建立基线")

        verdict, sim, n = decide(prev, cur, strikes=strikes,
                                 threshold=self.threshold,
                                 strikes_needed=self.strikes_needed)
        # 落盘：连续计数随结果更新；**changed 时不动基准指纹**（等人确认）
        self.store.put(key, prev, url=url, meta={"strikes": n,
                                                 "last_seen_fp": int(cur),
                                                 "last_similarity": round(sim, 4)})
        detail = {
            VERDICT_OK: "结构未变",
            VERDICT_WARN: f"相似度 {sim:.2f} 低于阈值 {self.threshold:.2f}（第 {n} 次，先只告警）",
            VERDICT_CHANGED: f"连续 {n} 次低于阈值 → 结构很可能已变（**待人工确认**，"
                             f"确认后跑 accept 重建基线）",
        }.get(verdict, "")
        return self._result(verdict, key=key, similarity=sim, strikes=n,
                            fingerprint=cur, detail=detail)

    def accept(self, key: str, url: str = "") -> bool:
        """人工确认"新结构就是新的正常"，把当前观测指纹设为新基线并清零告警。"""
        rec = self.store.get(key)
        fp_new = int(rec.get("last_seen_fp") or 0)
        if not fp_new:
            logger.warning(f"[探针] accept 失败：{key} 没有可接受的观测指纹")
            return False
        self.store.put(key, fp_new, url=url or rec.get("url", ""), meta={"strikes": 0})
        logger.info(f"[探针] {key} 已按新结构重建基线")
        return True
