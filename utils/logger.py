import logging
from utils.telegram_notifier import send_telegram_message
import os

class TelegramLogHandler(logging.Handler):
    def __init__(self, bot_token: str, chat_id: str, level=logging.WARNING):
        super().__init__(level)
        self.bot_token = bot_token
        self.chat_id = chat_id

    def emit(self, record):
        try:
            log_entry = self.format(record)
            send_telegram_message(self.bot_token, self.chat_id, log_entry)
        except Exception as e:
            print(f"TelegramLogHandler Error: {e}")

def setup_logger():
    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)

    logger = logging.getLogger("trading-bot")
    logger.setLevel(logging.DEBUG)  # 전체 로거 레벨은 DEBUG 유지

    # 콘솔 핸들러: DEBUG부터 출력
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)

    # 파일 핸들러: INFO 이상만 기록
    file_handler = logging.FileHandler(f"{log_dir}/trading.log", encoding="utf-8")
    file_handler.setLevel(logging.INFO)  # DEBUG는 기록 안 함

    formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    console_handler.setFormatter(formatter)
    file_handler.setFormatter(formatter)

    # 콘솔
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    console_handler.setFormatter(formatter)


    # ✅ 텔레그램 핸들러 추가
    telegram_formatter = logging.Formatter("%(message)s")

    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

    telegram_handler = TelegramLogHandler(
        bot_token=TELEGRAM_BOT_TOKEN,
        chat_id=TELEGRAM_CHAT_ID,
        level=logging.INFO  # WARNING 이상만 전송
    )
    telegram_handler.setFormatter(telegram_formatter)


    # 중복 핸들러 방지
    if not logger.handlers:
        logger.addHandler(console_handler)
        logger.addHandler(file_handler)
        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            logger.addHandler(telegram_handler)

    return logger
