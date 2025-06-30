# utils/logger.py
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
import io

def setup_logger(name="tradebot", log_file="logs/tradebot.log", level=logging.INFO):
    Path("logs").mkdir(exist_ok=True)

    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

    # 🔒 파일 핸들러는 INFO 이상만 저장
    file_handler = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=5, encoding='utf-8')
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)  # usually logging.INFO

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)  # 전체 로그 레벨은 DEBUG

    if not logger.hasHandlers():
        logger.addHandler(file_handler)

        # 🔊 콘솔 핸들러는 DEBUG까지 출력 (현재가 로그 포함)
        console = logging.StreamHandler(io.TextIOWrapper(sys.stdout.detach(), encoding='utf-8'))
        console.setFormatter(formatter)
        console.setLevel(logging.DEBUG)  # <=== 이 줄 중요!
        logger.addHandler(console)

    return logger
