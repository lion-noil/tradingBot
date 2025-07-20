# utils/telegram_handler.py

import logging
import requests

class TelegramLogHandler(logging.Handler):
    def __init__(self, bot_token: str, chat_id: str, level=logging.WARNING):
        super().__init__(level)
        self.bot_token = bot_token
        self.chat_id = chat_id

    def emit(self, record):
        try:
            message = self.format(record)
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "HTML"
            }
            requests.post(url, data=payload, timeout=5)
        except Exception as e:
            print(f"❗ TelegramLogHandler 예외: {e}")
