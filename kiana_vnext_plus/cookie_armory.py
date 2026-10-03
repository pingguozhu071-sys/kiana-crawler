"""Cookie 弹药库（v2.13 阶段 5，学 Jormungandr cookie_armory.py:36-153 的健康轮换模型）

多账号 cookies 管理：Fernet 加密落库（明文绝不入库）→ 按健康分降序轮换取用
→ 请求级反馈（成功复位/失败 -0.25 分 + 300s 冷却）。
与 frontier 主写队列解耦：低频操作走独立同步 sqlite 连接。

用法（CLI）:
    python -m kiana_vnext_plus cookie-add --site bilibili --name acc1 --file cookies.txt
    python -m kiana_vnext_plus cookie-list [--site bilibili]
引擎接入：**尚未接线**（M1-c 的目标是 page_processor 抓取阶段取身份）。
此前此处写"page_processor 请求前 acquire(site)（config cookie_armory_enabled=true 时）"——
`cookie_armory_enabled` 这个配置键**全仓不存在**、引擎也从不 import 本模块，
是**假陈述**；已如实改正，接线完成后改为陈述现状。
"""
import time
import sqlite3
import logging
import base64
from pathlib import Path
from dataclasses import dataclass
from enum import Enum

# [v6 收敛] accounts 建表/加列的唯一 schema 源（此处曾有一份逐字相同的重复 DDL）
from .frontier import ensure_accounts_schema

logger = logging.getLogger(__name__)

_FAIL_PENALTY = 0.25
_COOLDOWN_SECONDS = 300.0
_MIN_SCORE = 0.0
# [v6 修缺陷] 冷却到期后的"复活分"。
# 原实现：失败扣 0.25、钳到 max(0.0, ...)，而 acquire 要求 health_score > 0.0
# → **连败 4 次（1.0 − 4×0.25 = 0.0）后该身份永久选不中**，也就永远等不到
# report_success 的 +0.1 回血 = 事实上的永久拉黑，且没有任何告警。
# 冷板凳的语义应当是"暂时靠后"，不是"永久删除"。故冷却到期即复活到本下限，
# 它因分数最低而排在选择序列最后 —— 惩罚效果保留，死锁消除。
_REVIVE_SCORE = 0.1
# 额度窗口长度（quota_limit=0 表示不限，此时本值不生效）
_QUOTA_WINDOW_SECONDS = 3600.0


class UnavailableReason(str, Enum):
    """取不到身份的**可读**原因（调用方据此走诚实降级，而不是重试到死）"""
    NOT_PROVIDED = "not_provided"      # 该站从未入库任何身份（用户没提供）
    ALL_COOLING = "all_cooling"        # 有身份，但全部在冷却中
    QUOTA_EXHAUSTED = "quota_exhausted"  # 有身份，但本窗口额度已用尽
    DECRYPT_FAILED = "decrypt_failed"  # 选中了身份但解密失败（主密码不符/库损坏）
    DB_ERROR = "db_error"              # 库故障（与"没有身份"必须区分）


class ReportResult(str, Enum):
    """请求级反馈（三态语义的细化：确定失败 / 可重试 / 非身份问题）"""
    OK = "ok"
    THROTTLED = "throttled"            # 限流：扣分 + 冷却，可重试
    LOGIN_EXPIRED = "login_expired"    # 登录失效：扣分 + 冷却，需重新登录
    BLOCKED = "blocked"                # 被封：扣分 + 冷却，不可重试
    NETWORK = "network"                # 网络问题：**不惩罚身份**（见 report 注释）


@dataclass(frozen=True)
class Acquired:
    site: str
    name: str
    cookie: str


@dataclass(frozen=True)
class Unavailable:
    site: str
    reason: UnavailableReason
    detail: str = ""


def armory_db_path() -> str:
    """身份库（`accounts` 表）的**唯一**落盘位置 —— 所有调用方都必须走这里。

    [v6 修复·接线审查发现] 原来**三处各算各的路径**：
      · `crawler.py`      → `self.project.get_db_path()`
                            = `<输出目录>\\cli_<URL哈希>\\frontier.db`
      · `cli.py cookie-add/list` → `ProjectIdentity(args.project)`
                            = `projects\\default\\frontier.db`
    两者**永远不是同一个文件** ⇒ 存进去的账号，**爬取永远读不到**；
    更糟的是爬取那份**随首个 URL 变**，每个新 URL 都是**一个空库**。
    也就是「按站身份池」这个功能**建好了、却从没真正接通**
    （与第 36 轮 `cookie_armory_enabled` 缺一跳接线是同一类病）。

    **修法**：账号是**用户级数据**（是你自己的号，跨任务复用），
    不是任务级状态 —— 所以放**固定的数据根**（`config.data_root()`），
    不跟任务输出目录走。这样"存"和"取"必然指向同一个文件。

    单一实现：`crawler` 与 `cli` 都调本函数，**不许再各自拼路径**。
    """
    from .config import data_root
    return str(Path(data_root()) / "armory.db")


# 需要剥掉的子域前缀：它们指向**同一个站点**，身份应当互通。
# 判据是"同一套 cookies 能同时用在这些子域上"——`www.bilibili.com` 与
# `space.bilibili.com` 用的是同一份 B站登录态，分成两个键就是同一个号存两次。
_SITE_SUBDOMAIN_PREFIXES = (
    "www.", "www2.", "m.", "mobile.", "api.", "space.", "player.", "search.",
    "passport.", "account.", "live.", "t.", "u.", "i.", "message.", "show.",
    "link.", "b23.", "en.", "cn.", "jp.", "tw.", "hk.",
)


def site_lookup_candidates(site: str) -> list:
    """查库时该试哪些键 —— **按优先级排序**，第一个命中即用。

    [v6 补充·真机实测] 光靠 `normalize_site` 的**硬编码前缀表治不全**：
    真机又冒出 `message.bilibili.com`（表里当时没有 `message.`）→ 查不到身份。
    前缀永远列不完，所以加一条**通用兜底**：再试一次"可注册域"（最后两段）。

    为什么兜底是安全的：同一注册域下的子域**本来就共用 cookies**
    （B站的 `.bilibili.com` cookie 在 `www` / `space` / `message` 上都生效），
    所以按注册域归组正是"同一份登录态"的正确粒度。

    返回示例：`["message.bilibili.com", "bilibili.com"]`
    """
    first = normalize_site(site)
    out = [first] if first else []
    parts = first.split(".")
    if len(parts) > 2:
        root = ".".join(parts[-2:])
        if root and root not in out:
            out.append(root)
    return out


def normalize_site(site: str) -> str:
    """把站点名/域名归一到**同一站点的同一个键** —— 存与查必须走同一个。

    [v6 修复·真机实测] 原来两边各算各的（`(site or "").lower()`），看着一样，
    但**调用方喂进来的东西不一样**：
      · 导入时用户/文件名给的是 `bilibili.com`；
      · 引擎抓取时给的是 `domain` = **`www.bilibili.com`**。
    ⇒ 精确匹配永远失败，日志刷：
      `[身份] 域 www.bilibili.com 不可用（not_provided）：该站未入库任何身份`
      —— 而库里**明明躺着**那条身份。**"存了却查不到"**，功能等于没做。

    这与 `armory_db_path()` 是同一类病：**同一个概念在两处各算一遍**。
    现在只此一份实现，`add_account` / `acquire_identity` / `list_accounts`
    与两个导入入口（GUI、CLI）**全部改走它**。
    """
    s = (site or "").strip().lower()
    # ⚠️ 先剥协议再切路径：反过来会把 `https://x/y` 切成 `https`
    #（我第一版就是先 `split("/")`，实测 `https://www.bilibili.com/x` → `'https'`）
    if "//" in s:
        s = s.split("//", 1)[1]
    s = s.split("/")[0].split(":")[0]        # 去路径与端口
    changed = True
    while changed:                            # 反复剥（`www.m.xxx` 也归到一起）
        changed = False
        for p in _SITE_SUBDOMAIN_PREFIXES:
            if s.startswith(p) and len(s) > len(p):
                s = s[len(p):]
                changed = True
    return s


def _derive_fernet(master_password: str):
    """从 master_password 派生 Fernet（复用 config master.key 体系——不新增密钥源）"""
    from cryptography.fernet import Fernet
    import hashlib
    digest = hashlib.pbkdf2_hmac("sha256", master_password.encode(), b"kiana_armory_v1", 100_000)
    return Fernet(base64.urlsafe_b64encode(digest))


class CookieArmory:
    """多账号 Cookie 弹药库（加密存储 + 健康分轮换 + 失败冷却）"""

    def __init__(self, db_path: str, master_password: str, *,
                 fail_penalty: float = _FAIL_PENALTY,
                 cooldown_seconds: float = _COOLDOWN_SECONDS,
                 revive_score: float = _REVIVE_SCORE,
                 quota_window_seconds: float = _QUOTA_WINDOW_SECONDS):
        """惩罚量与额度窗口可按站/按部署覆盖——默认值与模块常量一致（零行为变化）。

        这些参数给 M1 的"按站配置"留出接口（惩罚不该对所有站点一刀切）。
        """
        self.db_path = str(db_path)
        self._fernet = _derive_fernet(master_password)
        self._fail_penalty = float(fail_penalty)
        self._cooldown_seconds = float(cooldown_seconds)
        self._revive_score = float(revive_score)
        self._quota_window_seconds = float(quota_window_seconds)
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def _init_db(self):
        """建表走**唯一** schema 源（`frontier.ensure_accounts_schema`）。

        [v6] 此处原有一份与 frontier 逐字相同的 `CREATE TABLE IF NOT EXISTS accounts`：
        两份都能建新表，但**加列时只改一处**就会让另一入口打开的库表结构落后。
        现已收敛，故 CookieArmory 与 FrontierDB 谁先打开同一个库，结果一致。
        """
        with self._connect() as conn:
            ensure_accounts_schema(conn)

    # ── 加解密 ──
    def _encrypt(self, cookie_str: str) -> str:
        return self._fernet.encrypt(cookie_str.encode("utf-8")).decode("ascii")

    def _decrypt(self, blob: str) -> str:
        return self._fernet.decrypt(blob.encode("ascii")).decode("utf-8")

    # ── 管理 ──
    def add_account(self, site: str, name: str, cookie_str: str) -> bool:
        """新增/更新账号（同 (site,name) 覆盖）。cookie_str 为 'k=v; k2=v2' 或 Netscape 原文。"""
        try:
            blob = self._encrypt(cookie_str)
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO accounts "
                    "(site, name, state_blob, health_score, cooldown_until, success_count, fail_count, updated_at) "
                    "VALUES (?,?,?,?,?,0,0,?)",
                    (normalize_site(site), name, blob, 1.0, 0.0, time.time()))
            logger.info(f"弹药库: 账号 {name}@{normalize_site(site)} 已入库（加密）")
            return True
        except Exception as e:
            logger.error(f"add_account 失败: {e}")
            return False

    def list_accounts(self, site: str = None) -> list[dict]:
        """账号清单（不含 cookie 明文）：site/name/健康分/冷却/成败计数"""
        out = []
        try:
            with self._connect() as conn:
                if site:
                    cur = conn.execute(
                        "SELECT site,name,health_score,cooldown_until,success_count,fail_count "
                        "FROM accounts WHERE site=? ORDER BY health_score DESC", (normalize_site(site),))
                else:
                    cur = conn.execute(
                        "SELECT site,name,health_score,cooldown_until,success_count,fail_count "
                        "FROM accounts ORDER BY site, health_score DESC")
                for row in cur.fetchall():
                    out.append(dict(zip(
                        ("site", "name", "health", "cooldown_left",
                         "success", "fail"),
                        (row[0], row[1], round(row[2], 2),
                         max(0, int(row[3] - time.time())), row[4], row[5]))))
        except Exception as e:
            logger.error(f"list_accounts 失败: {e}")
        return out

    # ── 引擎接入 ──
    def acquire_identity(self, site: str, *, purpose: str = "fetch"):
        """取一个最健康的可用身份 → `Acquired` 或 `Unavailable`。**不抛异常**。

        与 `acquire()` 的区别：返回结构化结果（带不可用**原因**）并做额度记账；
        `acquire()` 保留为薄包装，返回值语义不变（CLI 与既有测试继续可用）。

        原子性：**选择与占额在同一个 `BEGIN IMMEDIATE` 写事务内**完成。
        否则 N 个协程会同时选中同一个身份（读→改→写跨了事务边界就是真竞态）。
        解密是慢操作，刻意放在事务**外**，不占写锁。
        """
        site_l = normalize_site(site)   # [v6] 与 add_account **同一个**归一化，否则存了查不到
        # [v6 补充] 归一化之后**再挑一个真的有账号的键**：
        # 硬编码前缀表永远列不全（真机就冒出过 `message.bilibili.com`），
        # 所以按 `site_lookup_candidates` 逐个探一次，命中即用；
        # 一个都没有就保持归一化键（与改前行为一致，只是错误信息里的站点名更准）。
        # 这是**只读探测**，放在 BEGIN IMMEDIATE 之前，不影响下面的原子性。
        try:
            _cands = site_lookup_candidates(site)
            if len(_cands) > 1:
                _probe = self._connect()
                try:
                    for _cand in _cands:
                        _n = _probe.execute(
                            "SELECT COUNT(*) FROM accounts WHERE site=?", (_cand,)).fetchone()[0]
                        if _n:
                            site_l = _cand
                            break
                finally:
                    _probe.close()
        except Exception as e:
            # 探测失败**不许**改变行为：退回归一化键（宁可查不到，也不抛）
            logger.debug(f"site 候选探测失败，按归一化键走: {e}")
        now = time.time()
        conn = None
        name = None
        blob = None
        try:
            # 连接本身也会失败（库文件缺失/路径是目录/权限）——必须在 try 内，
            # 否则"不抛异常"的契约在第一步就破了（本文件的自测抓到过这一条）
            conn = self._connect()
            conn.isolation_level = None          # 关隐式事务，自己控制 BEGIN/COMMIT
            conn.execute("BEGIN IMMEDIATE")
            try:
                # ① 冷板凳复活：冷却已到期且分数归零者回到下限（修"永久拉黑"，见 _REVIVE_SCORE）
                conn.execute(
                    "UPDATE accounts SET health_score=MAX(health_score, ?) "
                    "WHERE site=? AND cooldown_until<? AND health_score<=?",
                    (self._revive_score, site_l, now, _MIN_SCORE))
                # ② 额度窗口滚动：窗口已过期则重置计数
                conn.execute(
                    "UPDATE accounts SET quota_used=0, quota_window_start=? "
                    "WHERE site=? AND quota_window_start IS NOT NULL "
                    "AND quota_window_start>0 AND quota_window_start<=?",
                    (now, site_l, now - self._quota_window_seconds))
                # ③ 选择（quota_limit<=0 表示不限额度）
                row = conn.execute(
                    "SELECT name, state_blob FROM accounts "
                    "WHERE site=? AND cooldown_until<? AND health_score>? "
                    "AND (quota_limit<=0 OR quota_used<quota_limit) "
                    "ORDER BY health_score DESC, updated_at DESC LIMIT 1",
                    (site_l, now, _MIN_SCORE)).fetchone()
                if row is None:
                    total = conn.execute(
                        "SELECT COUNT(*) FROM accounts WHERE site=?", (site_l,)).fetchone()[0]
                    exhausted = 0
                    if total:
                        exhausted = conn.execute(
                            "SELECT COUNT(*) FROM accounts WHERE site=? AND cooldown_until<? "
                            "AND health_score>? AND quota_limit>0 AND quota_used>=quota_limit",
                            (site_l, now, _MIN_SCORE)).fetchone()[0]
                    conn.execute("COMMIT")
                    if not total:
                        return Unavailable(site_l, UnavailableReason.NOT_PROVIDED,
                                           "该站未入库任何身份（请先提供 cookies）")
                    if exhausted:
                        return Unavailable(site_l, UnavailableReason.QUOTA_EXHAUSTED,
                                           "本窗口额度已用尽")
                    return Unavailable(site_l, UnavailableReason.ALL_COOLING,
                                       "全部身份在冷却中")
                name, blob = row[0], row[1]
                conn.execute(
                    "UPDATE accounts SET quota_used=quota_used+1, "
                    "quota_window_start=CASE WHEN quota_window_start IS NULL "
                    "OR quota_window_start<=0 THEN ? ELSE quota_window_start END, "
                    "updated_at=? WHERE site=? AND name=?",
                    (now, now, site_l, name))
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:            # 回滚本身失败不能再掩盖原始异常
                    logger.debug("弹药库: ROLLBACK 失败（原始异常优先）", exc_info=True)
                raise
        except Exception as e:
            logger.error(f"弹药库: acquire 失败（库故障）: {e}", exc_info=True)
            return Unavailable(site_l, UnavailableReason.DB_ERROR, str(e)[:120])
        finally:
            if conn is not None:
                conn.close()

        # 解密在事务外（慢操作不占写锁）
        try:
            return Acquired(site_l, name, self._decrypt(blob))
        except Exception as e:
            from cryptography.fernet import InvalidToken
            if isinstance(e, InvalidToken):
                # [v2.18 P2-5] 主密码不匹配原被吞成 debug——用户只看到"无可用账号"，
                # 不知道根因是密码错了/库损坏。这里给出可读原因。
                logger.warning("弹药库: 解密失败——master_password 不匹配或库文件已损坏（身份按不可用处理）")
                return Unavailable(site_l, UnavailableReason.DECRYPT_FAILED,
                                   "解密失败：主密码不匹配或库文件损坏")
            logger.error(f"弹药库: decrypt 异常: {e}", exc_info=True)
            return Unavailable(site_l, UnavailableReason.DECRYPT_FAILED, str(e)[:120])

    # ── 请求级反馈 ──
    def _record_ok(self, site: str, name: str) -> bool:
        """成功：健康分向 1.0 恢复、清冷却、记 last_ok_at（健康检查 TTL 的判据）"""
        now = time.time()
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "UPDATE accounts SET health_score=min(1.0, health_score+0.1), "
                    "cooldown_until=0, success_count=success_count+1, updated_at=?, "
                    "last_ok_at=?, last_error_kind=NULL WHERE site=? AND name=?",
                    (now, now, normalize_site(site), name))
                return cur.rowcount > 0
        except Exception as e:
            # 原先此处是 `except Exception: pass`：记账失败无声无息，健康分悄悄失真
            logger.error(f"弹药库: 成功反馈落库失败 {name}@{site}: {e}", exc_info=True)
            return False

    def _record_penalty(self, site: str, name: str, kind: str) -> bool:
        """扣分 + 冷却 + 记失败原因（原先落库失败被静默吞掉）"""
        now = time.time()
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "UPDATE accounts SET health_score=max(?, health_score-?), "
                    "cooldown_until=?, fail_count=fail_count+1, updated_at=?, "
                    "last_error_kind=?, last_error_at=? WHERE site=? AND name=?",
                    (_MIN_SCORE, self._fail_penalty, now + self._cooldown_seconds,
                     now, kind, now, normalize_site(site), name))
                ok = cur.rowcount > 0
            if ok:
                logger.warning(f"弹药库: 身份 {name}@{site} {kind}，"
                               f"冷却 {self._cooldown_seconds:.0f}s")
            return ok
        except Exception as e:
            logger.error(f"弹药库: 失败反馈落库失败 {name}@{site} ({kind}): {e}", exc_info=True)
            return False

    def _record_note(self, site: str, name: str, kind: str) -> bool:
        """只记原因与时间，**不扣分、不冷却**（用于"不是身份的问题"）"""
        now = time.time()
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "UPDATE accounts SET last_error_kind=?, last_error_at=?, updated_at=? "
                    "WHERE site=? AND name=?",
                    (kind, now, now, normalize_site(site), name))
                return cur.rowcount > 0
        except Exception as e:
            logger.error(f"弹药库: 反馈落库失败 {name}@{site} ({kind}): {e}", exc_info=True)
            return False

    def report(self, site: str, name: str, result) -> bool:
        """请求级反馈（唯一入口）。返回**是否成功落库**（不再静默失败）。

        `ReportResult.NETWORK` **不惩罚身份**：网络抖动不等于身份坏了。
        把线路问题算成失败，会白扣 0.25 分并冷却 300s —— 这正是工程反复踩的
        "看起来有防护、实际误杀"那一类（对照：健康探针超时也不判死）。
        """
        try:
            r = result if isinstance(result, ReportResult) else ReportResult(str(result))
        except ValueError:
            logger.error(f"弹药库: 未知的反馈结果 {result!r}（按网络问题处理，不惩罚身份）")
            r = ReportResult.NETWORK
        if r is ReportResult.OK:
            return self._record_ok(site, name)
        if r is ReportResult.NETWORK:
            return self._record_note(site, name, r.value)
        return self._record_penalty(site, name, r.value)

    # ── 兼容包装（返回值语义与 v2.13 一致，内部走上面的统一实现）──
    def acquire(self, site: str) -> str | None:
        """按健康分降序取一个非冷却身份的 cookie 串；不可用返回 None"""
        got = self.acquire_identity(site)
        return got.cookie if isinstance(got, Acquired) else None

    def report_success(self, site: str, name: str) -> bool:
        """成功：健康分向 1.0 恢复"""
        return self._record_ok(site, name)

    def report_failure(self, site: str, name: str) -> bool:
        """失败（403/429/风控）：扣分 + 冷却；分归零即坐冷板凳（冷却到期会自动复活）"""
        return self._record_penalty(site, name, ReportResult.THROTTLED.value)


def parse_cookie_file(path: str) -> str:
    """Netscape cookies.txt → 'k=v; k2=v2' 串（供 add_account 入库）"""
    # [v6 收敛] 本处**无域过滤**（整文件入库）→ 直接用唯一入口，不再自己 split('\t')
    from .cookie_utils import parse_cookie_file_text
    return parse_cookie_file_text(path)
