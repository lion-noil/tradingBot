# core/trade_bot.py
from datetime import datetime
from utils.logger import setup_logger
from core.data_fetcher import get_real_data
from strategies.basic_strategy import should_enter_long, should_enter_short, should_exit

logger = setup_logger()

class TradeBot:
    def __init__(self, controller, command_queue):
        self.bybit = controller
        self.command_queue = command_queue
        self.position = None
        self.position_time = None
        self.running = True

    def run_once(self, price, ma100, prev):
        if self.position is None:
            if should_enter_short(price, ma100, prev):
                self.bybit.sell_market_100(price, ma100)
                self.position = 'short'
                self.position_time = datetime.now()
            elif should_enter_long(price, ma100, prev):
                self.bybit.buy_market_100(price, ma100)
                self.position = 'long'
                self.position_time = datetime.now()
        elif should_exit(price, ma100, self.position_time):
            self.bybit.close_position_market(price, ma100)
            self.position = None
            self.position_time = None

    def manual_long_entry(self):
        price_now, ma, _ = get_real_data()
        logger.info("🟢 수동 롱 진입")
        self.bybit.buy_market_100(price_now, ma)
        self.position = 'long'
        self.position_time = datetime.now()

    def manual_short_entry(self):
        price_now, ma, _ = get_real_data()
        logger.info("🔴 수동 숏 진입")
        self.bybit.sell_market_100(price_now, ma)
        self.position = 'short'
        self.position_time = datetime.now()

    def manual_exit(self):
        price_now, ma, _ = get_real_data()
        if self.position:
            logger.info(f"🔁 수동 청산 ({self.position})")
            self.bybit.close_position_market(price_now, ma)
            self.position = None
            self.position_time = None
        else:
            logger.warning("⚠️ 청산할 포지션 없음")

    def process_manual_commands(self):
        while not self.command_queue.empty():
            cmd = self.command_queue.get()
            if cmd == "buy":
                self.manual_long_entry()
            elif cmd == "sell":
                self.manual_short_entry()
            elif cmd == "close":
                self.manual_exit()
