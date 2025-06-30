# core/trade_bot.py
from datetime import datetime
from utils.logger import setup_logger
from core.data_fetcher import get_real_data
from strategies.basic_strategy import should_enter_long, should_enter_short, should_exit
import asyncio  # 파일 상단에 추가
logger = setup_logger()

class TradeBot:
    def __init__(self, controller, manual_queue):
        self.bybit = controller
        self.manual_queue = manual_queue
        self.position = None
        self.position_time = None
        self.running = True

    async def run_once(self, price, ma100, prev):
        # ✅ 수동 명령 처리
        if not self.manual_queue.empty():
            command = await self.manual_queue.get()
            if self.position is None:
                if command == "long":
                    await self.bybit.buy_market_100(price, ma100)
                    self.position = 'long'
                    self.position_time = datetime.now()
                elif command == "short":
                    await self.bybit.sell_market_100(price, ma100)
                    self.position = 'short'
                    self.position_time = datetime.now()
            elif command == "close":
                await self.bybit.close_position_market(price, ma100)
                self.position = None
                self.position_time = None

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
