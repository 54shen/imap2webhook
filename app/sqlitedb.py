import sqlite3
import logging

logger = logging.getLogger(__name__)

class SqliteDb:
    def __init__(self, db_path):
        self.conn = sqlite3.connect(db_path)
        self._init_tables()
        self.email_uids = self._load_uids()

    def _init_tables(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS email_uids (
                id  INTEGER PRIMARY KEY,
                uid INTEGER UNIQUE
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        self.conn.commit()

    def _load_uids(self):
        rows = self.conn.execute("SELECT uid FROM email_uids").fetchall()
        loaded_uids = {row[0] for row in rows}
        return loaded_uids

    def flush_uids(self):
        self.conn.execute("DELETE FROM email_uids")
        self.conn.commit()
        self.email_uids = set()

    def insert_uid(self, uid):
        self.conn.execute("INSERT OR IGNORE INTO email_uids (uid) VALUES (?)", (uid,))
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
