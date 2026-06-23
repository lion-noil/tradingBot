# app/main_s1.py
# S1(σ-복귀 롱) 신호 봇 — 큰틀(TradeBot)은 그대로, strategy="s1"로만 동작.
# namespace "s1"로 오픈포지션 장부를 분리하고, 신호는 executor-a1(공유 Bybit 계정)로 emit.

import sys
import signal, os, asyncio, logging, threading, time
from collections import deque

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI
from asyncio import Queue
from utils.local_action_sender import LocalActionSender, Target

from bots.trade_bot import TradeBot
from bots.trade_config import make_s1_config
from utils.logger import setup_logger

from controllers.bybit.bybit_ws_controller import BybitWebSocketController
from controllers.bybit.bybit_rest_controller import BybitRestController


class BurstWarningTerminator(logging.Handler):
    def __init__(self, threshold: int = 5, window_sec: float = 10.0, grace_sec: float = 3):
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
                logging.getLogger("system").critical("🧯 종료 직전: 로그/텔레그램 flush 시도")
                logging.shutdown()
            finally:
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


# Bybit 채널 봇 토큰 재사용(폴백: 옛 _CHAT_ID 이름)
tg_bot = os.getenv("Noil1_TELEGRAM_BOT_TOKEN") or os.getenv("Noil1_TELEGRAM_CHAT_ID")
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
    telegram_bot_token=tg_bot,
    telegram_chat_id=tg_chat,
)
system_logger.addHandler(BurstWarningTerminator(threshold=5, window_sec=10.0, grace_sec=3))

trading_logger = setup_logger(
    "trading",
    logger_level=logging.DEBUG,
    console_level=logging.DEBUG,
    file_level=logging.INFO,
    enable_telegram=True,
    telegram_level=logging.INFO,
    write_signals_file=True,
    signals_filename="signals_s1.jsonl",
    exclude_sig_in_file=False,
    telegram_mode="both",
    telegram_bot_token=tg_bot,
    telegram_chat_id=tg_chat,
)

app = FastAPI()
manual_queue: Queue = Queue()

bot_s1: TradeBot | None = None
ws_controller: BybitWebSocketController | None = None
rest_controller: BybitRestController | None = None


def _env(key: str, default: str = "") -> str:
    return (os.getenv(key) or default).strip()


async def warmup_with_ws_prices(bot: TradeBot, ws, name: str):
    MIN_TICKS = bot.jump.history_num
    while True:
        try:
            missing: dict[str, int] = {}
            for sym in bot.symbols:
                price = ws.get_price(sym)
                exchange_ts = ws.get_last_exchange_ts(sym)
                if price is not None:
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


@app.on_event("startup")
async def startup_event():
    global bot_s1, ws_controller, rest_controller

    system_logger.debug("🚀 S1 신호 봇 시작 (strategy=s1, namespace=s1)")

    cfg_s1 = make_s1_config()
    symbols_s1 = tuple(getattr(cfg_s1, "symbols", []) or [])
    if not symbols_s1:
        system_logger.error("⚠️ BYBIT_S1_SYMBOLS 미설정 — S1이 거래할 심볼이 없습니다. .env 확인.")
    system_logger.debug(f"🔧 S1 symbols={symbols_s1}, config={cfg_s1.as_dict()}")

    PRICE_REST_URL = _env("BYBIT_PRICE_REST_URL", "")
    TRADE_REST_URL = _env("BYBIT_TRADE_REST_URL", "")
    PRICE_WS_URL = _env("BYBIT_PRICE_WS_URL", "")

    ws_controller = BybitWebSocketController(
        symbols=symbols_s1, system_logger=system_logger, price_ws_url=PRICE_WS_URL
    )
    rest_controller = BybitRestController(
        system_logger=system_logger, trade_base_url=TRADE_REST_URL, price_base_url=PRICE_REST_URL
    )

    # S1 전용 executor 타겟(없으면 Bybit 기본 타겟 → executor-a1)
    _raw_targets = _env("S1_EXECUTOR_TARGETS", "") or _env("BYBIT_EXECUTOR_TARGETS", "127.0.0.1:9009")
    _targets = [Target(h, int(p)) for t in _raw_targets.split(",") if ":" in t
                for h, p in [t.strip().rsplit(":", 1)]]
    local_sender = LocalActionSender(targets=_targets, system_logger=system_logger, ping_sec=10)
    local_sender.start()

    bot_s1 = TradeBot(
        ws_controller,
        rest_controller,
        manual_queue,
        system_logger=system_logger,
        trading_logger=trading_logger,
        symbols=symbols_s1,
        config=cfg_s1,
        action_sender=local_sender,
        publish_config=False,  # 'bybit' 네임스페이스 공유 → config는 signal-bybit이 소유, S1은 미발행
    )

    asyncio.create_task(bot_loop(bot_s1, ws_controller, "S1"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main_s1:app", host="127.0.0.1", port=18003, reload=False)
