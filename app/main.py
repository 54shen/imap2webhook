from app.config.logger import setup_logging
from app.manager import EmailManager

setup_logging()

if __name__ == "__main__":
    manager = EmailManager()
    manager.run()

