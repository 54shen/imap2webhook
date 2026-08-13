import os
import logging
import sys
from dataclasses import dataclass
from dotenv import load_dotenv

# 加载项目根目录的 .env(优先于默认值,不覆盖已设置的环境变量)
load_dotenv()

logger = logging.getLogger(__name__)


def _env_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key)
    return default if val is None else val.lower() == "true"


@dataclass
class AccountConfig:
    """单个 IMAP 账户的配置。

    账户 0("default")来自无前缀的 IMAP_* 变量,行为与旧版单账户完全一致;
    账户 1..N 来自 IMAP{编号}_* 前缀(.env 里编号必须连续,遇断号停止扫描),
    未配置的项继承全局默认值。
    """
    name:           str
    imap_host:      str
    imap_port:      int
    imap_user:      str
    imap_pwd:       str
    imap_ssl_verify: bool
    imap_timeout:   int
    mailbox:        str
    past_unseen:    bool
    attach:         bool
    max_attach_mb:  int


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
        self.IMAP_SSL_VERIFY = _env_bool("IMAP_SSL_VERIFY", True)
        self.IMAP_TIMEOUT    = int(os.environ.get("IMAP_TIMEOUT", "30"))
        self.MAILBOX         = os.environ.get("MAILBOX", "INBOX")
        self.PAST_UNSEEN     = _env_bool("PAST_UNSEEN", False)
        self.ATTACH          = _env_bool("ATTACH", True)
        self.FLUSH_DB        = _env_bool("FLUSH_DB", False)
        self.LOG_LEVEL       = os.environ.get("LOG_LEVEL", "INFO").upper()
        self.PUSH_RETRIES    = int(os.environ.get("PUSH_RETRIES", "3"))
        self.MAX_ATTACH_MB   = int(os.environ.get("MAX_ATTACH_MB", "10"))
        self.DB_PATH         = os.environ.get("DB_PATH", "./data/data.db")
        self.CUSTOM_SENDER   = os.environ.get("CUSTOM_SENDER", "")
        self._validate()

    def _validate(self):
        # 推送只有一条通道:自定义推送脚本(custom_sender.py)
        if not self.CUSTOM_SENDER:
            logger.error(
                "Missing mandatory environment variable: CUSTOM_SENDER — fix your config and restart the service."
            )
            sys.exit(1)
        if not os.path.isfile(self.CUSTOM_SENDER):
            logger.error(
                "CUSTOM_SENDER file not found: %s — check the path and restart the service.",
                self.CUSTOM_SENDER
            )
            sys.exit(1)
        # 账户只有编号形式(IMAP1_* / IMAP2_*…),无前缀默认账户已废除
        if not os.environ.get("IMAP1_HOST"):
            logger.error(
                "No IMAP account configured: .env must define IMAP1_HOST / IMAP1_USER / IMAP1_PWD "
                "(numbered accounts only, e.g. IMAP1_* / IMAP2_*)."
            )
            sys.exit(1)

    def load_accounts(self) -> list[AccountConfig]:
        """按 .env 前缀加载账户:只有 IMAP1_*、IMAP2_*…(编号必须连续,断号停止)。

        账户名 = IMAP_USER(邮箱地址);无前缀的 IMAP_PORT/IMAP_TIMEOUT/MAILBOX 等
        仍作为未配置项的全局默认值。
        """
        accounts = []
        i = 1
        while os.environ.get(f"IMAP{i}_HOST"):
            acct = AccountConfig(
                name=os.environ.get(f"IMAP{i}_USER", ""),
                imap_host=os.environ[f"IMAP{i}_HOST"],
                imap_port=int(os.environ.get(f"IMAP{i}_PORT", str(self.IMAP_PORT))),
                imap_user=os.environ.get(f"IMAP{i}_USER", ""),
                imap_pwd=os.environ.get(f"IMAP{i}_PWD", ""),
                imap_ssl_verify=_env_bool(f"IMAP{i}_SSL_VERIFY", self.IMAP_SSL_VERIFY),
                imap_timeout=int(os.environ.get(f"IMAP{i}_TIMEOUT", str(self.IMAP_TIMEOUT))),
                mailbox=os.environ.get(f"IMAP{i}_MAILBOX", self.MAILBOX),
                past_unseen=_env_bool(f"IMAP{i}_PAST_UNSEEN", self.PAST_UNSEEN),
                attach=_env_bool(f"IMAP{i}_ATTACH", self.ATTACH),
                max_attach_mb=int(os.environ.get(f"IMAP{i}_MAX_ATTACH_MB", str(self.MAX_ATTACH_MB))),
            )
            missing = [k for k, v in (("IMAP_USER", acct.imap_user), ("IMAP_PWD", acct.imap_pwd))
                       if not v]
            if missing:
                logger.error("账户 %s(IMAP%s_*)缺少必填项 %s — 补全 .env 后重启。",
                             acct.name or f"第 {i} 个", i, ", ".join(missing))
                sys.exit(1)
            accounts.append(acct)
            i += 1
        return accounts


settings = Settings()
