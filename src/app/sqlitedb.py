import sqlite3
import logging

logger = logging.getLogger(__name__)

class SqliteDb:
    def __init__(self, db_path, account: str = ""):
        self.account = account
        # timeout:多账户线程同时写库时的忙等待上限(避免 database is locked)
        self.conn = sqlite3.connect(db_path, timeout=5)
        self._init_tables()
        self.email_uids = self._load_uids()

    def _init_tables(self):
        # WAL:多账户并发读不阻塞,写事务互不等待太久
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS email_uids (
                id      INTEGER PRIMARY KEY,
                uid     INTEGER UNIQUE
            )
        """)
        # 多账户迁移:UID 只在同一账户内有意义,唯一约束必须带 account。
        # 老库(仅 uid 列)首次打开时重建表,历史记录归入 'default' 账户。
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(email_uids)")}
        if "account" not in cols:
            logger.warning("Migrating email_uids table for multi-account support...")
            self.conn.execute("ALTER TABLE email_uids RENAME TO email_uids_old")
            self.conn.execute("""
                CREATE TABLE email_uids (
                    id      INTEGER PRIMARY KEY,
                    account TEXT    NOT NULL DEFAULT 'default',
                    uid     INTEGER NOT NULL,
                    UNIQUE(account, uid)
                )
            """)
            self.conn.execute(
                "INSERT INTO email_uids (id, account, uid) "
                "SELECT id, 'default', uid FROM email_uids_old")
            self.conn.execute("DROP TABLE email_uids_old")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        self.conn.commit()

    def _load_uids(self):
        rows = self.conn.execute(
            "SELECT uid FROM email_uids WHERE account = ?", (self.account,)).fetchall()
        return {row[0] for row in rows}

    def flush_uids(self):
        # FLUSH_DB 语义:清空所有账户(由启动流程在创建 worker 前调用)
        self.conn.execute("DELETE FROM email_uids")
        self.conn.commit()
        self.email_uids = set()

    def insert_uid(self, uid):
        self.conn.execute(
            "INSERT OR IGNORE INTO email_uids (account, uid) VALUES (?, ?)",
            (self.account, uid),
        )
        self.conn.commit()
        self.email_uids.add(uid)

    def get_meta(self, key):
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key, value):
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    @staticmethod
    def migrate_account_names(db_path, mapping: dict[str, str]) -> bool:
        """老账户名(default/1/2…) → 新邮箱名。须在 worker 线程前调用(主线程建连接)。
        mapping = {旧账户名: 新邮箱};幂等:无匹配行时 UPDATE 无效果,重复执行无害。
        返回是否实际迁移了数据(供调用方决定是否打日志)。"""
        if not mapping:
            return False
        conn = sqlite3.connect(db_path, timeout=5)
        changed = False
        try:
            for old, new in mapping.items():
                if old == new:
                    continue
                cur = conn.execute(
                    "UPDATE email_uids SET account = ? WHERE account = ?", (new, old))
                changed = changed or cur.rowcount > 0
                cur = conn.execute(
                    "UPDATE meta SET key = ? WHERE key = ?",
                    (f"{new}:uidvalidity", f"{old}:uidvalidity"),
                )
                changed = changed or cur.rowcount > 0
            conn.commit()
        finally:
            conn.close()
        return changed
