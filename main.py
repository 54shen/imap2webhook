import os
import sys

# 核心实现位于 src/ 目录(注入后 from app.xxx 原样可用,包名不变)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from app.config.logger import setup_logging
from app.manager import EmailManager

setup_logging()

if __name__ == "__main__":
    manager = EmailManager()
    manager.run()
