import logging
import threading
import time
from app.config.settings import settings
from app.imap.client import ImapClient
from app.sqlitedb import SqliteDb

logger = logging.getLogger(__name__)


class AccountWorker:
    """单个账户的监听循环:连接 → IDLE → 拉取 → 推送。每账户一个线程。"""

    def __init__(self, account):
        self.account = account
        self.logger = logging.getLogger(f"app.manager.{account.name}")
        self.db = SqliteDb(settings.DB_PATH, account=account.name)
        self.first_connect = True

    def run(self):
        if len(self.db.email_uids):
            self.logger.debug("List of known email uids : %s", self.db.email_uids)

        attempt = 0
        while True:
            try:
                with ImapClient(self.account) as client:
                    client.select_mailbox(self.account.mailbox)
                    self._check_uidvalidity(client)
                    attempt = 0

                    unseens = client.fetch_unseen_uids()

                    # If PAST_UNSEEN set to True, and first connection, forward all unseen email
                    if self.account.past_unseen and self.first_connect:
                        if len(unseens):
                            self.logger.info("Unseen emails at startup: %s.", len(unseens))
                            self.manage_unseens(client, unseens)

                    # If PAST_UNSEEN set to False, and first connection, register all unseen email to the database
                    elif self.first_connect:
                        self.logger.info("Unseen emails at startup: %s. Watching for new ones only.", len(unseens))
                        for unseen in unseens:
                            uid = int(unseen.decode())
                            # If uid is not already in database
                            if uid not in self.db.email_uids:
                                self.db.insert_uid(uid)
                                self.logger.info("Registered unseen email: [%s]", uid)

                    # Not a first connection, forward emails that came during a connection error
                    else:
                        if unseens:
                            self.logger.info("Reconnected. Found %s unseen email(s), forwarding.",
                                             len(unseens))
                        self.manage_unseens(client, unseens)

                    self.first_connect = False

                    while True:
                        state = client.idle()
                        if state == "email":
                            unseens = client.fetch_unseen_uids()
                            self.manage_unseens(client, unseens)
                        else:
                            # state == "refresh":10 分钟主动刷新,干净断开,
                            # 立即重连不进退避(异常断开在 idle() 里直接抛
                            # ConnectionError,走外层退避重连,不会到这里)
                            break

            except Exception as e:
                delay = min(60, 10 * max(attempt, 1))
                self.logger.error("Connection error: %s. Reconnecting in %ss...", e, delay)
                attempt += 1
                time.sleep(delay)

    def _check_uidvalidity(self, client):
        """If the mailbox was rebuilt, IMAP UIDs get reused and recorded UIDs become wrong — flush them."""
        uidvalidity = client.uidvalidity
        if uidvalidity is None:
            return
        key = f"{self.account.name}:uidvalidity"
        stored = self.db.get_meta(key)
        if stored is not None and int(stored) != uidvalidity:
            self.logger.warning("UIDVALIDITY changed (%s -> %s): mailbox was rebuilt, flushing UID records.",
                                stored, uidvalidity)
            self.db.flush_uids()
        self.db.set_meta(key, str(uidvalidity))

    def manage_unseens(self, client, unseens):
        for unseen in unseens:
            uid = int(unseen.decode())
            # If uid is not already in database
            if uid in self.db.email_uids:
                self.logger.debug("UID %s 已在数据库,跳过", uid)
                continue
            self.logger.debug("UID %s 待处理:解析 → 推送 → 登记", uid)
            try:
                payload = client.parse_email(unseen)
            except Exception as e:
                # Keep the loop alive: a single bad/deleted message must not
                # take down the whole connection. The UID stays unrecorded and
                # will be retried on the next trigger.
                self.logger.warning("Failed to fetch or parse email [%s]: %s. Will retry later.", uid, e)
                continue
            self.logger.info("Sending unseen email: [%s] : [%s]", uid, payload.subject)
            if self.send_payload(payload):
                self.db.insert_uid(uid)
            else:
                self.logger.error("Push delivery failed for email [%s]. UID kept unrecorded, will retry on next trigger.", uid)

    def send_payload(self, payload) -> bool:
        last_error = None
        for attempt in range(1, settings.PUSH_RETRIES + 1):
            try:
                if self._run_custom_sender(payload):
                    self.logger.info("Push delivered on attempt %s.", attempt)
                    return True
                last_error = "delivery returned failure"
            except Exception as e:
                last_error = str(e)
            if attempt < settings.PUSH_RETRIES:
                self.logger.warning("Push attempt %s/%s failed (%s). Retrying in %ss...",
                                    attempt, settings.PUSH_RETRIES, last_error, 2 ** attempt)
                time.sleep(2 ** attempt)
        self.logger.error("Push failed after %s attempts: %s", settings.PUSH_RETRIES, last_error)
        return False

    def _run_custom_sender(self, payload) -> bool:
        """Run the user's custom sender script with the payload JSON on stdin.

        Contract: exit code 0 = delivered; non-zero = failure (retried by the caller).
        Uses the same interpreter as the service, so the script can reuse installed deps.
        """
        import json
        import subprocess
        import sys

        data = payload.model_dump(by_alias=True)
        self.logger.info("Running custom sender script: %s", settings.CUSTOM_SENDER)
        proc = subprocess.run(
            [sys.executable, settings.CUSTOM_SENDER],
            input=json.dumps(data, ensure_ascii=False).encode(),
            capture_output=True,
            timeout=30,
        )
        if proc.stdout:
            self.logger.debug("Custom sender stdout: %s", proc.stdout.decode(errors="replace")[:500])
        if proc.returncode != 0:
            self.logger.error(
                "Custom sender exited with code %s: %s",
                proc.returncode,
                proc.stderr.decode(errors="replace").strip()[:500],
            )
            return False
        return True


class EmailManager:
    """启动器:为每个账户拉起一个监听线程。"""

    def run(self):
        if settings.FLUSH_DB:
            logger.warning("Database email uids flushed at startup.")
            SqliteDb(settings.DB_PATH).flush_uids()

        accounts = settings.load_accounts()
        logger.info("启动 %d 个账户监听: %s",
                    len(accounts), ", ".join(a.name for a in accounts))

        threads = []
        for acct in accounts:
            # Worker 必须在目标线程内构造:SQLite 连接只能被创建它的线程使用
            t = threading.Thread(target=lambda: AccountWorker(acct).run(),
                                 name=f"imap-{acct.name}", daemon=True)
            t.start()
            threads.append(t)

        # 主线程挂起;worker 均为 daemon 线程,单个账户异常退出不影响其他账户
        for t in threads:
            t.join()
