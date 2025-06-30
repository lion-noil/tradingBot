# main.py
import time
import threading
from queue import Queue

from controllers.bybit_controller import BybitController
from core.trade_bot import TradeBot
from core.data_fetcher import get_real_data
from utils.keyboard_handler import start_keyboard_listener
from utils.logger import setup_logger

logger = setup_logger()
manual_queue = Queue()

if __name__ == "__main__":
    controller = BybitController()
    bot = TradeBot(controller, manual_queue)

    # 키보드 입력 수신 스레드 시작
    threading.Thread(target=start_keyboard_listener, args=(manual_queue,), daemon=True).start()

    logger.info("🚀 봇 실행 중 (↑ 매수 | ↓ 매도 | → 청산)")

    try:
        while bot.running:
            price_now, ma, prev = get_real_data()

            # 상태 정보는 로그파일이 아닌 콘솔에만 출력
            logger.debug(f"💹 현재가: {price_now}, MA100: {ma}, 3분전: {prev}")

            bot.run_once(price_now, ma, prev)
            bot.process_manual_commands()

            time.sleep(5)

    except KeyboardInterrupt:
        logger.warning("🛑 사용자가 봇 실행을 중단했습니다.")

    finally:
        controller.close()
        logger.info("✅ 봇 종료 완료")
