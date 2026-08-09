import logging
import requests
import time
from app.config.settings import settings
from app.imap.client import ImapClient
from app.sqlitedb import SqliteDb

logger = logging.getLogger(__name__)

class EmailManager:
    def __init__(self):
        self.db = SqliteDb(settings.DB_PATH)
        self.first_connect = True

    def run(self):
        if settings.FLUSH_DB:
            logger.warning("Database email uids flushed at startup.")
            self.db.flush_uids()

        if len(self.db.email_uids):
            logger.debug("List of known email uids : %s", self.db.email_uids)

        attempt = 0
        while True:
            try:
                with ImapClient() as client:
                    client.select_mailbox(settings.MAILBOX)
                    self._check_uidvalidity(client)
                    attempt = 0

                    unseens = client.fetch_unseen_uids()

                    # If PAST_UNSEEN set to True, and first connection, forward all unseen email
                    if settings.PAST_UNSEEN and self.first_connect:
                        if len(unseens):
                            logger.info("Unseen emails at startup: %s.", len(unseens))
                            self.manage_unseens(client, unseens)

                    # If PAST_UNSEEN set to False, and first connection, register all unseen email to the database
                    elif self.first_connect:
                        logger.info("Unseen emails at startup: %s. Watching for new ones only.", len(unseens))
                        for unseen in unseens:
                            uid = int(unseen.decode())
                            # If uid is not already in database
                            if uid not in self.db.email_uids:
                                self.db.insert_uid(uid)
                                logger.info("Registered unseen email: [%s]", uid)

                    # Not a first connection, forward emails that came during a connection error
                    else:
                        if unseens:
                            logger.warning("Reconnected. Found %s unseen email(s) during interruption, forwarding.",
                                           len(unseens))
                        self.manage_unseens(client, unseens)

                    self.first_connect = False

                    while True:
                        if not client.idle():
                            # IDLE 被服务器关闭:2 秒内重连重进,保持实时性
                            # (避免用轮询——新邮件检测延迟大;IDLE 短盲窗可接受)
                            time.sleep(2)
                            raise ConnectionError("IDLE session ended, reconnecting")
                        unseens = client.fetch_unseen_uids()
                        self.manage_unseens(client, unseens)

            except Exception as e:
                delay = min(60, 10 * max(attempt, 1))
                logger.error("Connection error: %s. Reconnecting in %ss...", e, delay)
                attempt += 1
                time.sleep(delay)

    def _check_uidvalidity(self, client):
        """If the mailbox was rebuilt, IMAP UIDs get reused and recorded UIDs become wrong — flush them."""
        uidvalidity = client.uidvalidity
        if uidvalidity is None:
            return
        stored = self.db.get_meta("uidvalidity")
        if stored is not None and int(stored) != uidvalidity:
            logger.warning("UIDVALIDITY changed (%s -> %s): mailbox was rebuilt, flushing UID records.",
                           stored, uidvalidity)
            self.db.flush_uids()
        self.db.set_meta("uidvalidity", str(uidvalidity))

    def manage_unseens(self, client, unseens):
        for unseen in unseens:
            uid = int(unseen.decode())
            # If uid is not already in database
            if uid in self.db.email_uids:
                logger.debug("UID %s 已在数据库,跳过", uid)
                continue
            logger.debug("UID %s 待处理:解析 → 推送 → 登记", uid)
            try:
                payload = client.parse_email(unseen)
            except Exception as e:
                # Keep the loop alive: a single bad/deleted message must not
                # take down the whole connection. The UID stays unrecorded and
                # will be retried on the next trigger.
                logger.warning("Failed to fetch or parse email [%s]: %s. Will retry later.", uid, e)
                continue
            logger.info("Sending unseen email: [%s] : [%s]", uid, payload.subject)
            if self.send_to_webhook(payload):
                self.db.insert_uid(uid)
            else:
                logger.error("Webhook delivery failed for email [%s]. UID kept unrecorded, will retry on next trigger.", uid)

    def send_to_webhook(self, payload) -> bool:
        last_error = None
        for attempt in range(1, settings.WEBHOOK_RETRIES + 1):
            try:
                if self._deliver(payload):
                    logger.info("Webhook delivered on attempt %s.", attempt)
                    return True
                last_error = "delivery returned failure"
            except Exception as e:
                last_error = str(e)
            if attempt < settings.WEBHOOK_RETRIES:
                logger.warning("Webhook attempt %s/%s failed (%s). Retrying in %ss...",
                               attempt, settings.WEBHOOK_RETRIES, last_error, 2 ** attempt)
                time.sleep(2 ** attempt)
        logger.error("Webhook failed after %s attempts: %s", settings.WEBHOOK_RETRIES, last_error)
        return False

    def _deliver(self, payload) -> bool:
        """Delivery strategy: custom sender script if configured, plain POST otherwise."""
        if settings.CUSTOM_SENDER:
            return self._run_custom_sender(payload)
        response = requests.post(settings.WEBHOOK, json=payload.model_dump(by_alias=True), timeout=10)
        if response.status_code >= 400:
            raise RuntimeError(f"Webhook returned HTTP {response.status_code}")
        return True

    def _run_custom_sender(self, payload) -> bool:
        """Run the user's custom sender script with the payload JSON on stdin.

        Contract: exit code 0 = delivered; non-zero = failure (retried by the caller).
        Uses the same interpreter as the service, so the script can reuse installed deps.
        """
        import json
        import subprocess
        import sys

        data = payload.model_dump(by_alias=True)
        logger.info("Running custom sender script: %s", settings.CUSTOM_SENDER)
        proc = subprocess.run(
            [sys.executable, settings.CUSTOM_SENDER],
            input=json.dumps(data, ensure_ascii=False).encode(),
            capture_output=True,
            timeout=30,
        )
        if proc.stdout:
            logger.debug("Custom sender stdout: %s", proc.stdout.decode(errors="replace")[:500])
        if proc.returncode != 0:
            logger.error(
                "Custom sender exited with code %s: %s",
                proc.returncode,
                proc.stderr.decode(errors="replace").strip()[:500],
            )
            return False
        return True
