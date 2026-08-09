import os
import logging
import sys
from dotenv import load_dotenv

# 本地运行:加载项目根目录的 .env(优先于默认值,不覆盖已设置的环境变量)
# Docker 中没有 .env 文件,该调用为空操作,不受影响。
load_dotenv()

logger = logging.getLogger(__name__)

class Settings:
    IMAP_HOST: str
    IMAP_PORT: int
    IMAP_USER: str
    IMAP_PWD:  str
    LOG_LEVEL: str

    def __init__(self):
        self.IMAP_HOST       = os.environ.get("IMAP_HOST", "")
        self.IMAP_PORT       = int(os.environ.get("IMAP_PORT", "993"))
        self.IMAP_USER       = os.environ.get("IMAP_USER", "")
        self.IMAP_PWD        = os.environ.get("IMAP_PWD",  "")
        self.WEBHOOK         = os.environ.get("WEBHOOK",   "")
        self.MAILBOX         = os.environ.get("MAILBOX", "INBOX")
        self.PAST_UNSEEN     = os.environ.get("PAST_UNSEEN", "false").lower() == "true"
        self.ATTACH          = os.environ.get("ATTACH", "true").lower() == "true"
        self.FLUSH_DB        = os.environ.get("FLUSH_DB", "false").lower() == "true"
        self.LOG_LEVEL       = os.environ.get("LOG_LEVEL", "INFO").upper()
        self.WEBHOOK_RETRIES = int(os.environ.get("WEBHOOK_RETRIES", "3"))
        self.MAX_ATTACH_MB   = int(os.environ.get("MAX_ATTACH_MB", "10"))
        self.IMAP_TIMEOUT    = int(os.environ.get("IMAP_TIMEOUT", "30"))
        self.DB_PATH         = os.environ.get("DB_PATH", "/app/data/data.db")
        self.CUSTOM_SENDER   = os.environ.get("CUSTOM_SENDER", "")
        self._validate()

    def _validate(self):
        mandatory = {
            "IMAP_HOST": self.IMAP_HOST,
            "IMAP_USER": self.IMAP_USER,
            "IMAP_PWD":  self.IMAP_PWD,
        }
        # 用自定义推送脚本时 WEBHOOK 不是必填(脚本可自行决定推送到哪)
        if not self.CUSTOM_SENDER:
            mandatory["WEBHOOK"] = self.WEBHOOK
        missing = [name for name, val in mandatory.items() if not val]
        if missing:
            logger.error(
                "Missing mandatory environment variables: %s — fix your config and restart the container.",
                ', '.join(missing)
            )
            sys.exit(1)
        if self.CUSTOM_SENDER and not os.path.isfile(self.CUSTOM_SENDER):
            logger.error(
                "CUSTOM_SENDER file not found: %s — check the path and restart the container.",
                self.CUSTOM_SENDER
            )
            sys.exit(1)

settings = Settings()
