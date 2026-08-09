import logging
import os
from logging.handlers import RotatingFileHandler
from app.config.settings import settings

# 日志文件位置:项目根目录 logs/imap2webhook.log(已加入 .gitignore)
LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "logs")
LOG_PATH = os.path.join(LOG_DIR, "imap2webhook.log")

def setup_logging() -> None:
    level = getattr(logging, settings.LOG_LEVEL, logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-9s | %(name)s | %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )

    # 控制台:按 LOG_LEVEL 显示
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.setLevel(level)

    # 文件:记录全部活动(固定 DEBUG 级别),滚动保留 3 份 × 5MB
    os.makedirs(LOG_DIR, exist_ok=True)
    file_handler = RotatingFileHandler(
        LOG_PATH,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)

    root = logging.getLogger()
    # root 必须放行到 DEBUG,由各 handler 按自己的级别过滤
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(file_handler)
