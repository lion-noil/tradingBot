# app/main.py
import sys
from typing import Literal
import signal, os, asyncio, logging, threading, time
from collections import deque

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
from bots.trade_config import SecretsConfig
from fastapi import FastAPI
from pydantic import BaseModel

from asyncio import Queue

from bots.trade_bot import TradeBot
from bots.trade_config import make_bybit_config, make_mt5_signal_config
from utils.logger import setup_logger

# ── 거래소 컨트롤러들 ───────────────────────────────────
from controllers.bybit.bybit_ws_controller import BybitWebSocketController
from controllers.bybit.bybit_rest_controller import BybitRestController

from controllers.mt5.mt5_ws_controller import Mt5WebSocketController
from controllers.mt5.mt5_rest_controller import Mt5RestController

class ManualOrderRequest(BaseModel):
    percent: float = 10
    symbol: str | None = None
    engine: Literal["BYBIT", "MT5"] = "BYBIT"

class ManualCloseRequest(BaseModel):
    side: Literal["LONG", "SHORT"]
    symbol: str | None = None
    engine: Literal["BYBIT", "MT5"] = "BYBIT"


class BurstWarningTerminator(logging.Handler):
    """
    window_sec 동안 WARNING(이상) 로그가 threshold회 이상 발생하면 프로세스를 종료.
    """

    def __init__(self, threshold: int = 3, window_sec: float = 5.0, grace_sec: float = 0.2):
        super().__init__()
        self.threshold = threshold
        self.window_sec = window_sec
        self.grace_sec = grace_sec
        self._ts = deque()
        self._lock = threading.Lock()
        self._armed = True
        logging.captureWarnings(True)  # warnings.warn 도 logging으로 보냄

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
        # uvicorn 그레이스풀 셧다운 유도 (SIGINT)
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


# ── 로거 설정 ───────────────────────────────────

system_logger = setup_logger(
    "system",
    logger_level=logging.DEBUG,
    console_level=logging.DEBUG,
    file_level=logging.INFO,
    enable_telegram=True,
    telegram_level=logging.INFO,
    exclude_sig_in_file=False,
    telegram_mode="both",
)

_terminator = BurstWarningTerminator(threshold=5, window_sec=10.0, grace_sec=0.2)
system_logger.addHandler(_terminator)

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
)

# ── FastAPI 앱 ───────────────────────────────────
app = FastAPI()
manual_queue_bybit: Queue = Queue()
manual_queue_mt5: Queue = Queue()

# Bybit용 봇 & 컨트롤러
bot_bybit: TradeBot | None = None
bybit_ws_controller: BybitWebSocketController | None = None
bybit_rest_controller: BybitRestController | None = None

# MT5용 봇 & 컨트롤러
bot_mt5: TradeBot | None = None
mt5_ws_controller: Mt5WebSocketController | None = None
mt5_rest_controller: Mt5RestController | None = None


async def warmup_with_ws_prices(bot: TradeBot, ws, name: str):
    """
    워밍업 동안 WS에서 직접 가격을 읽어 JumpDetector에 채운다.
    bot / ws / 이름을 파라미터로 받아서 공용으로 사용.
    """
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
    # 1) 워밍업: 가격 샘플이 충분히 쌓일 때까지 대기
    await warmup_with_ws_prices(bot, ws, name)

    # 2) 메인 루프
    while True:
        try:
            await bot.run_once()
            await asyncio.sleep(0.5)
        except Exception as e:
            system_logger.error(f"❌ [{name}] bot_loop 오류: {e}")
            await asyncio.sleep(10)


@app.on_event("startup")
async def startup_event():
    global bot_bybit, bybit_ws_controller, bybit_rest_controller
    global bot_mt5, mt5_ws_controller, mt5_rest_controller

    system_logger.debug("🚀 FastAPI 기반 봇 서버 시작")
    sec = SecretsConfig.from_env()


    # ── Bybit 설정 ───────────────────────────────
    if sec.enable_bybit:
        cfg_bybit = make_bybit_config()
        symbols_bybit = tuple(getattr(cfg_bybit, "symbols", []) or [])
        system_logger.debug(f"🔧 Bybit symbols={symbols_bybit}, config={cfg_bybit.as_dict()}")

        bybit_ws_controller = BybitWebSocketController(symbols=symbols_bybit, system_logger=system_logger)
        bybit_rest_controller = BybitRestController(system_logger=system_logger)

        bot_bybit = TradeBot(
            bybit_ws_controller,
            bybit_rest_controller,
            manual_queue_bybit,
            system_logger=system_logger,
            trading_logger=trading_logger,
            symbols=symbols_bybit,
            signal_only=getattr(cfg_bybit, "signal_only", False),
            config=cfg_bybit,
        )
        asyncio.create_task(bot_loop(bot_bybit, bybit_ws_controller, "BYBIT"))
    else:
        system_logger.debug("⏭️ ENABLE_BYBIT=0 → Bybit 스킵")

    if sec.enable_mt5:
        cfg_mt5 = make_mt5_signal_config()
        symbols_mt5 = tuple(getattr(cfg_mt5, "symbols", []) or [])
        system_logger.debug(f"🔧 MT5 symbols={symbols_mt5}, config={cfg_mt5.as_dict()}")

        mt5_ws_controller = Mt5WebSocketController(symbols=symbols_mt5, system_logger=system_logger)
        mt5_rest_controller = Mt5RestController(system_logger=system_logger)

        bot_mt5 = TradeBot(
            mt5_ws_controller,
            mt5_rest_controller,
            manual_queue_mt5,
            system_logger=system_logger,
            trading_logger=trading_logger,
            symbols=symbols_mt5,
            # MT5는 기본 시그널-only → config.signal_only 없으면 True
            signal_only=getattr(cfg_mt5, "signal_only", True),
            config=cfg_mt5,
        )

        # ── 봇 루프 실행 (Bybit + MT5 둘 다) ─────────────────
        asyncio.create_task(bot_loop(bot_mt5, mt5_ws_controller, "MT5"))


def _get_exec_engine(bot: TradeBot):
    ex = getattr(bot, "exec", None)
    if ex is None:
        raise RuntimeError("ExecutionEngine not found on bot (expected bot.exec)")
    return ex

def _pos_idx_from_side(side_hint: str) -> int:
    return 1 if side_hint.upper() == "LONG" else 2

def _order_side_open(side_hint: str) -> str:
    return "Buy" if side_hint.upper() == "LONG" else "Sell"

def _order_side_close(side_hint: str) -> str:
    return "Sell" if side_hint.upper() == "LONG" else "Buy"

def _call_submit_market_order(rest, *, symbol: str, order_side: str, qty: float, position_idx: int, reduce_only: bool):
    """
    submit_market_order 시그니처가 프로젝트마다 조금 달라도 최대한 맞춰서 호출.
    """
    fn = getattr(rest, "submit_market_order", None)
    if not callable(fn):
        raise RuntimeError("rest.submit_market_order not found")

    # 1) 가장 흔한: (symbol, order_side, qty, position_idx=..., reduce_only=...)
    try:
        return fn(symbol, order_side, qty, position_idx=position_idx, reduce_only=reduce_only)
    except TypeError:
        pass

    # 2) (symbol, order_side, qty, position_idx, reduce_only)
    try:
        return fn(symbol, order_side, qty, position_idx, reduce_only)
    except TypeError:
        pass

    # 3) (symbol, order_side, position_idx, qty, reduce_only)
    try:
        return fn(symbol, order_side, position_idx, qty, reduce_only)
    except TypeError:
        pass

    # 4) 키워드 형태
    try:
        return fn(symbol=symbol, order_side=order_side, qty=qty, position_idx=position_idx, reduce_only=reduce_only)
    except TypeError as e:
        raise RuntimeError(f"submit_market_order call failed: {e}")

@app.post("/test/run")
async def test_run_once():
    """
    원샷 테스트:
      - MT5: ETHUSD LONG OPEN -> 대기 -> LONG CLOSE
      - BYBIT: ETHUSDT LONG OPEN -> 대기 -> LONG CLOSE
    """
    results = {}

    side_hint = "LONG"
    settle_wait_sec = 1.2

    # ---- MT5 ----
    if bot_mt5 is None or mt5_rest_controller is None:
        results["MT5"] = {"ok": False, "error": "MT5 bot/rest not running (ENABLE_MT5=0?)"}
    else:
        try:
            ex = _get_exec_engine(bot_mt5)
            symbol = "ETHUSD"
            pos_idx = _pos_idx_from_side(side_hint)

            # OPEN
            def mt5_open_fn(*_a, **_k):
                return _call_submit_market_order(
                    mt5_rest_controller,
                    symbol=symbol,
                    order_side=_order_side_open(side_hint),
                    qty=0.01,
                    position_idx=pos_idx,
                    reduce_only=False,
                )

            open_res = await ex.execute_and_sync(
                mt5_open_fn,
                position_detail=None,
                symbol=symbol,
                side=side_hint,        # ✅ 엔진 힌트(팝됨)
                expected="OPEN",       # ✅ 엔진 expected override(팝됨)
            )

            await asyncio.sleep(settle_wait_sec)

            # CLOSE
            def mt5_close_fn(*_a, **_k):
                return _call_submit_market_order(
                    mt5_rest_controller,
                    symbol=symbol,
                    order_side=_order_side_close(side_hint),
                    qty=0.01,
                    position_idx=pos_idx,
                    reduce_only=True,
                )

            # 손익 로그용 avg_price는 실제 포지션 avg를 가져오는 게 베스트지만,
            # 최소 테스트는 None이어도 "체결 완료" 로그는 찍힘.
            close_res = await ex.execute_and_sync(
                mt5_close_fn,
                position_detail=None,
                symbol=symbol,
                side=side_hint,
                expected="CLOSE",
            )

            results["MT5"] = {"ok": True, "symbol": symbol, "open": open_res, "close": close_res}
        except Exception as e:
            results["MT5"] = {"ok": False, "error": str(e)}

    # ---- BYBIT ----
    if bot_bybit is None or bybit_rest_controller is None:
        results["BYBIT"] = {"ok": False, "error": "BYBIT bot/rest not running (ENABLE_BYBIT=0?)"}
    else:
        try:
            ex = _get_exec_engine(bot_bybit)
            symbol = "ETHUSDT"
            pos_idx = _pos_idx_from_side(side_hint)

            # OPEN
            def bybit_open_fn(*_a, **_k):
                return _call_submit_market_order(
                    bybit_rest_controller,
                    symbol=symbol,
                    order_side=_order_side_open(side_hint),
                    qty=0.01,
                    position_idx=pos_idx,
                    reduce_only=False,
                )

            open_res = await ex.execute_and_sync(
                bybit_open_fn,
                position_detail=None,
                symbol=symbol,
                side=side_hint,
                expected="OPEN",
            )

            await asyncio.sleep(settle_wait_sec)

            # CLOSE
            def bybit_close_fn(*_a, **_k):
                return _call_submit_market_order(
                    bybit_rest_controller,
                    symbol=symbol,
                    order_side=_order_side_close(side_hint),
                    qty=0.01,
                    position_idx=pos_idx,
                    reduce_only=True,
                )

            close_res = await ex.execute_and_sync(
                bybit_close_fn,
                position_detail=None,
                symbol=symbol,
                side=side_hint,
                expected="CLOSE",
            )

            results["BYBIT"] = {"ok": True, "symbol": symbol, "open": open_res, "close": close_res}
        except Exception as e:
            results["BYBIT"] = {"ok": False, "error": str(e)}

    return {"ok": True, "results": results}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)
