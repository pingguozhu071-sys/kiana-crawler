"""Cookie 弹药库（v2.13 阶段 5：健康分降序轮换取用 + 失败降分冷却）

多账号 cookies 管理：Fernet 加密落库（明文绝不入库）→ 按健康分降序轮换取用
→ 请求级反馈（成功复位/失败 -0.25 分 + 300s 冷却）。
与 frontier 主写队列解耦：低频操作走独立同步 sqlite 连接。

用法（CLI）:
    python -m kiana_vnext_plus cookie-add --site bilibili --name acc1 --file cookies.txt
    python -m kiana_vnext_plus cookie-list [--site bilibili]
引擎接入：page_processor 请求前 acquire(site)（config cookie_armory_enabled=true 时）。
"""
import time
import sqlite3
import logging
import base64

logger = logging.getLogger(__name__)

_FAIL_PENALTY = 0.25
_COOLDOWN_SECONDS = 300.0
_MIN_SCORE = 0.0


def _derive_fernet(master_password: str):
    """从 master_password 派生 Fernet（复用 config master.key 体系——不新增密钥源）"""
    from cryptography.fernet import Fernet
    import hashlib
    digest = hashlib.pbkdf2_hmac("sha256", master_password.encode(), b"kiana_armory_v1", 100_000)
    return Fernet(base64.urlsafe_b64encode(digest))


class CookieArmory:
    """多账号 Cookie 弹药库（加密存储 + 健康分轮换 + 失败冷却）"""

    def __init__(self, db_path: str, master_password: str):
        self.db_path = str(db_path)
        self._fernet = _derive_fernet(master_password)
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS accounts (
                site TEXT, name TEXT, state_blob TEXT, health_score REAL DEFAULT 1.0,
                cooldown_until REAL DEFAULT 0, success_count INTEGER DEFAULT 0,
                fail_count INTEGER DEFAULT 0, updated_at REAL,
                PRIMARY KEY (site, name))""")

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
                    ((site or "").lower(), name, blob, 1.0, 0.0, time.time()))
            logger.info(f"弹药库: 账号 {name}@{site} 已入库（加密）")
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
                        "FROM accounts WHERE site=? ORDER BY health_score DESC", ((site or "").lower(),))
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
    def acquire(self, site: str) -> str | None:
        """按健康分降序取一个非冷却账号的 cookie 串；全冷却/无账号返回 None"""
        try:
            now = time.time()
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT name, state_blob FROM accounts "
                    "WHERE site=? AND cooldown_until<? AND health_score>? "
                    "ORDER BY health_score DESC, updated_at DESC LIMIT 1",
                    ((site or "").lower(), now, _MIN_SCORE))
                row = cur.fetchone()
            if not row:
                return None
            try:
                return self._decrypt(row[1])
            except Exception as e:
                # [v2.18 P2-5] 主密码不匹配（InvalidToken）原被吞成 debug——用户只看到
                # "无可用账号"却不知道根因是密码错了/库文件损坏
                from cryptography.fernet import InvalidToken
                if isinstance(e, InvalidToken):
                    logger.warning("弹药库: 解密失败——master_password 不匹配或库文件已损坏（账号按不可用处理）")
                else:
                    logger.debug(f"acquire decrypt 失败: {e}")
                return None
        except Exception as e:
            logger.debug(f"acquire 失败: {e}")
            return None

    def report_success(self, site: str, name: str):
        """成功：健康分向 1.0 恢复"""
        try:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE accounts SET health_score=min(1.0, health_score+0.1), "
                    "cooldown_until=0, success_count=success_count+1, updated_at=? "
                    "WHERE site=? AND name=?",
                    (time.time(), (site or "").lower(), name))
        except Exception:
            pass

    def report_failure(self, site: str, name: str):
        """失败（403/429/风控）：-0.25 分 + 300s 冷却；分归零即坐冷板凳"""
        try:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE accounts SET health_score=max(?, health_score-?), "
                    "cooldown_until=?, fail_count=fail_count+1, updated_at=? "
                    "WHERE site=? AND name=?",
                    (_MIN_SCORE, _FAIL_PENALTY, time.time() + _COOLDOWN_SECONDS,
                     time.time(), (site or "").lower(), name))
            logger.warning(f"弹药库: 账号 {name}@{site} 失败，冷却 {_COOLDOWN_SECONDS:.0f}s")
        except Exception:
            pass


def parse_cookie_file(path: str) -> str:
    """Netscape cookies.txt → 'k=v; k2=v2' 串（供 add_account 入库）"""
    pairs = []
    with open(path, encoding="utf-8-sig", errors="ignore") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            parts = s.split("\t")
            if len(parts) >= 7:
                pairs.append(f"{parts[5]}={parts[6]}")
    return "; ".join(pairs)
