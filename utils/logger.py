import logging
import os

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

    # 중복 핸들러 방지
    if not logger.handlers:
        logger.addHandler(console_handler)
        logger.addHandler(file_handler)

    return logger
