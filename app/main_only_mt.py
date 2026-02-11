# app/main_only_mt.py
import sys
from typing import Literal
import signal, os, asyncio, logging, threading, time
from collections import deque

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI
from pydantic import BaseModel
from asyncio import Queue

from bots.trade_bot import TradeBot
from bots.trade_config import make_mt5_signal_config
from utils.logger import setup_logger

from controllers.mt5.mt5_ws_controller import Mt5WebSocketController
from controllers.mt5.mt5_rest_controller import Mt5RestController

from utils.local_action_sender import LocalActionSender, Target

class ManualOrderRequest(BaseModel):
    percent: float = 10
    symbol: str | None = None


class ManualCloseRequest(BaseModel):
    side: Literal["LONG", "SHORT"]
    symbol: str | None = None


class BurstWarningTerminator(logging.Handler):
    def __init__(self, threshold: int = 3, window_sec: float = 5.0, grace_sec: float = 0.2):
        super().__init__()
        self.threshold = threshold
        self.window_sec = window_sec
        self.grace_sec = grace_sec
        self._ts = deque()
        self._lock = threading.Lock()
        self._armed = True
        logging.captureWarnings(True)

    def emit(self, record: logging.LogRecord):
        if record.levelno < logging.WARNING or not self._armed:
            return
        now = time.monotonic()
        with self._lock:
            self._ts.append(now)
            cutoff = now - self.window_sec
            while self._ts and self._ts[0] < cutoff:
                self._ts.popleft()
            if len(self._ts) >= self.threshold:
                self._armed = False
                logging.getLogger("system").error(
                    f"🚨 WARNING {len(self._ts)}회/{self.window_sec:.1f}s → 안전 종료 시도"
                )
                self._shutdown()

    def _shutdown(self):
        def _kill():
            try:
                os.kill(os.getpid(), signal.SIGINT)
            except Exception:
                raise SystemExit(1)

        try:
            loop = asyncio.get_running_loop()
            if loop.is_running():
                loop.call_later(self.grace_sec, _kill)
                return
        except RuntimeError:
            pass
        _kill()

tg_bot = os.getenv("Noil2_TELEGRAM_CHAT_ID")
tg_chat = os.getenv("TELEGRAM_CHAT_ID")

system_logger = setup_logger(
    "system",
    logger_level=logging.DEBUG,
    console_level=logging.DEBUG,
    file_level=logging.INFO,
    enable_telegram=True,
    telegram_level=logging.INFO,
    exclude_sig_in_file=False,
    telegram_mode="both",
    telegram_bot_token=tg_bot,      # ✅ 주입
    telegram_chat_id=tg_chat,       # ✅ 주입
)
system_logger.addHandler(BurstWarningTerminator(threshold=10, window_sec=10.0, grace_sec=0.2))

trading_logger = setup_logger(
    "trading",
    logger_level=logging.DEBUG,
    console_level=logging.DEBUG,
    file_level=logging.INFO,
    enable_telegram=True,
    telegram_level=logging.INFO,
    write_signals_file=True,
    signals_filename="signals.jsonl",
    exclude_sig_in_file=False,
    telegram_mode="both",
    telegram_bot_token=tg_bot,      # ✅ 주입
    telegram_chat_id=tg_chat,       # ✅ 주입
)

app = FastAPI()
manual_queue: Queue = Queue()

bot_mt5: TradeBot | None = None
mt5_ws_controller: Mt5WebSocketController | None = None
mt5_rest_controller: Mt5RestController | None = None


async def warmup_with_ws_prices(bot: TradeBot, ws, name: str):
    MIN_TICKS = bot.jump.history_num
    while True:
        try:
            missing: dict[str, int] = {}
            for sym in bot.symbols:
                price = ws.get_price(sym)
                exchange_ts = ws.get_last_exchange_ts(sym)
                if price is not None:  # ✅ 여기 수정
                    bot.jump.record_price(sym, price, exchange_ts)
                cur = len(bot.jump.price_history.get(sym, []))
                if cur < MIN_TICKS:
                    missing[sym] = cur

            if not missing:
                system_logger.debug(f"✅ [{name}] 데이터 준비 완료, 메인 루프 시작")
                return

            system_logger.debug(f"⏳ [{name}] 데이터 준비 중... (부족: {missing})")
            await asyncio.sleep(0.5)

        except Exception as e:
            system_logger.error(f"❌ [{name}] warmup 오류: {e}")
            await asyncio.sleep(1.0)


async def bot_loop(bot: TradeBot, ws, name: str):
    await warmup_with_ws_prices(bot, ws, name)
    while True:
        try:
            await bot.run_once()
            await asyncio.sleep(0.5)
        except Exception as e:
            system_logger.error(f"❌ [{name}] bot_loop 오류: {e}")
            await asyncio.sleep(10)

def _env(key: str, default: str = "") -> str:
    return (os.getenv(key) or default).strip()


@app.on_event("startup")
async def startup_event():
    global bot_mt5, mt5_ws_controller, mt5_rest_controller

    system_logger.debug("🚀 FastAPI 기반 MT5 시그널 서버 시작")

    cfg_mt5 = make_mt5_signal_config()
    symbols_mt5 = tuple(getattr(cfg_mt5, "symbols", []) or [])  # ✅ 튼튼하게
    system_logger.debug(f"🔧 MT5 symbols={symbols_mt5}, config={cfg_mt5.as_dict()}")

    PRICE_REST_URL = _env(f"MT5_PRICE_REST_URL", "")
    PRICE_WS_URL = _env(f"MT5_PRICE_WS_URL", "")

    mt5_ws_controller = Mt5WebSocketController(symbols=symbols_mt5, system_logger=system_logger,price_ws_url=PRICE_WS_URL)
    mt5_rest_controller = Mt5RestController(system_logger=system_logger,price_base_url=PRICE_REST_URL)
    local_sender = LocalActionSender(
        targets=[
            Target("127.0.0.1", 9010),
        ],
        system_logger=system_logger,
        ping_sec=10,
    )

    local_sender.start()  # ✅ 중요: send 없어도 바로 연결/감시 시작

    bot_mt5 = TradeBot(
        mt5_ws_controller,
        mt5_rest_controller,
        manual_queue,
        system_logger=system_logger,
        trading_logger=trading_logger,
        symbols=symbols_mt5,
        config=cfg_mt5,
        action_sender=local_sender,  # ✅ 추가
    )

    asyncio.create_task(bot_loop(bot_mt5, mt5_ws_controller, "MT5"))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main_only_mt:app", host="127.0.0.1", port=8002, reload=False)
