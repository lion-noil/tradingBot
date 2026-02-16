# bots/trade_bot.py
import time
from typing import List
from bots.state.signals import OpenSignalsIndex, record_and_index_signal
from .trade_config import TradeConfig
from core.engines import CandleEngine, IndicatorEngine, JumpDetector
from core.redis_client import redis_client
from .market.indicators import IndicatorState, bind_refresher
from .market.jump_reporting import JumpService
from .market.market_sync import MarketSync, MarketSyncConfig
from .state.bot_state import BotState
from .reporting.reporting import (
    build_market_status_log,
    extract_market_status_summary,
    should_log_update_market,
)

from .reporting.status_reporter import StatusReporter, StatusReporterDeps
from .trading.signal_processor import SignalProcessor, SignalProcessorDeps, TradeAction

class TradeBot:
    def __init__(
            self,
            ws_controller,
            rest_controller,
            manual_queue,
            action_sender=None,
            system_logger=None,
            trading_logger=None,
            symbols=("BTCUSDT",),
            config: TradeConfig | None = None,
    ):
        self.ws = ws_controller
        self.rest = rest_controller
        self.manual_queue = manual_queue
        self.action_sender = action_sender
        self.system_logger = system_logger
        self.trading_logger = trading_logger
        self.symbols: List[str] = list(symbols)

        # config
        self.config = (TradeConfig().normalized() if config is None else config.normalized())
        self.namespace: str = getattr(self.config, "name", None) or "bybit"
        self.config.to_redis(redis_client, publish=True)

        # engines
        self.candle = CandleEngine(candles_num=self.config.candles_num)
        self.indicator = IndicatorEngine(
            min_thr=self.config.indicator_min_thr,
            max_thr=self.config.indicator_max_thr,
            target_cross=self.config.target_cross,
        )
        self.jump = JumpDetector(history_num=10, polling_interval=0.5)

        self._apply_config(self.config)
        # state
        self.state = BotState(
            symbols=self.symbols,
            min_ma_threshold=self.config.min_ma_threshold,
        )
        self.state.init_defaults()

        self.jump_service = JumpService(self.jump, self.symbols, system_logger=self.system_logger)

        # indicator binding
        self.ind_state = IndicatorState(
            ma100s=self.state.ma100s,
            now_ma100_map=self.state.now_ma100,
            ma_threshold_map=self.state.ma_threshold,
            thr_quantized_map=self.state.thr_quantized,
            momentum_threshold_map=self.state.momentum_threshold,
            prev3_candle_map=self.state.prev3_candle,
            ma_check_enabled_map=self.state.ma_check_enabled,
            min_ma_threshold=self.state.min_ma_threshold,
        )

        self._refresh_indicators_fn = bind_refresher(
            self.candle,
            self.indicator,
            self.ind_state,
            system_logger=self.system_logger,
            redis_client=redis_client,
            namespace=self.namespace,
        )

        # market sync
        self.market = MarketSync(
            ws=self.ws,
            rest=self.rest,
            candle_engine=self.candle,
            refresh_indicators=self._refresh_indicators_fn,
            cfg=MarketSyncConfig(
                ws_stale_sec=self.ws_stale_sec,
                ws_global_stale_sec=self.ws_global_stale_sec,
                candles_num=self.config.candles_num,
            ),
            system_logger=self.system_logger,
            on_price=lambda sym, px, ex_ts: self.jump.record_price(sym, px, ex_ts),
            jump_service=self.jump_service,
            get_ma_threshold=lambda s: self.state.ma_threshold.get(s),
        )

        # bootstrap
        self.market.bootstrap(symbols=self.symbols)
        self._last_scaleout_ts_ms: dict[tuple[str, str], int] = {}
        self._warmup_last_scaleout_ts()

        self.open_signals_index = OpenSignalsIndex()
        self.open_signals_index.load_from_redis(
            namespace=self.namespace,
            symbols=self.symbols,
        )

        # signal processor
        self.signal_processor = SignalProcessor(
            system_logger=self.system_logger,
            deps=SignalProcessorDeps(
                get_now_ma100=lambda s: self.state.now_ma100.get(s),
                get_prev3_candle=lambda s: self.state.prev3_candle.get(s),
                get_ma_threshold=lambda s: (
                    self.state.ma_threshold.get(s)
                    if self.state.ma_check_enabled.get(s, True)
                    else None
                ),
                get_momentum_threshold=lambda s: self.state.momentum_threshold.get(s),

                get_position_max_hold_sec=lambda: self.config.position_max_hold_sec,
                get_near_touch_window_sec=lambda: self.config.near_touch_window_sec,
                get_open_signal_items=lambda sym, side: self.open_signals_index.list_open(
                    namespace=self.namespace, symbol=sym, side=side.upper(), newest_first=True
                ),

                get_last_scaleout_ts_ms=lambda sym, side: self._last_scaleout_ts_ms.get(
                    ((sym or "").upper(), (side or "").upper())),
                set_last_scaleout_ts_ms=lambda sym, side, ts_ms: self._last_scaleout_ts_ms.__setitem__(
                    ((sym or "").upper(), (side or "").upper()), int(ts_ms)),

                log_signal=lambda sym, side, kind, price, sig: record_and_index_signal(
                    namespace=self.namespace,
                    open_index=self.open_signals_index,
                    sym=sym,
                    side=side,
                    kind=kind,
                    price=price,
                    payload=sig,
                    engine=self.namespace,
                    system_logger=self.system_logger,
                    trading_logger=self.trading_logger,
                )
            ),
        )

        # reporter
        self.reporter = StatusReporter(
            system_logger=self.system_logger,
            deps=StatusReporterDeps(
                get_symbols=lambda: self.symbols,
                get_jump_state=lambda: self.jump_service.get_state_map(),
                get_ma_threshold=lambda: self.state.ma_threshold,
                get_now_ma100=lambda: self.state.now_ma100,
                get_price=lambda s, now_ts: self.market.get_price(s, now_ts),
                get_ma_check_enabled=lambda: self.state.ma_check_enabled,
                get_min_ma_threshold=lambda: self.state.min_ma_threshold,
            ),
            build_fn=build_market_status_log,
            extract_fn=extract_market_status_summary,
            should_fn=should_log_update_market,
        )

    def _warmup_last_scaleout_ts(self, *, lookback_sec: int = 30 * 60, count: int = 2000):
        key = f"trading:{self.namespace}:signals"
        now_ms = int(time.time() * 1000)
        min_ms = now_ms - int(lookback_sec) * 1000

        # 최신부터 역순으로 긁기
        # stream id는 "ms-seq" 형태라 대략 ms와 비슷
        min_id = f"{min_ms}-0"
        rows = redis_client.xrevrange(key, max="+", min=min_id, count=count) or []

        def decode_fields(fields: dict) -> dict[str, str]:
            out = {}
            for k, v in (fields or {}).items():
                if isinstance(k, (bytes, bytearray)):
                    k = k.decode("utf-8", "ignore")
                if isinstance(v, (bytes, bytearray)):
                    v = v.decode("utf-8", "ignore")
                out[str(k)] = str(v)
            return out

        for sid, fields in rows:
            is_scaleout = False  # ✅ 매 루프마다 초기화

            f = decode_fields(fields)
            kind = (f.get("kind") or "").upper()
            if kind != "EXIT":
                continue
            rj = f.get("reasons_json") or ""
            if "SCALE_OUT" in rj:
                is_scaleout = True

            sym = (f.get("symbol") or "").upper()
            side = (f.get("side") or "").upper()
            ts_ms = f.get("ts_ms")
            if ts_ms is None:
                continue

            try:
                ts_ms_i = int(ts_ms)
            except Exception:
                continue

            if not is_scaleout:
                continue

            if sym and side:
                k = (sym, side)
                # 최신부터 읽으니까, 처음 발견한 게 최신임
                if k not in self._last_scaleout_ts_ms:
                    self._last_scaleout_ts_ms[k] = ts_ms_i

    def _apply_config(self, cfg: TradeConfig) -> None:
        self.ws_stale_sec = cfg.ws_stale_sec
        self.ws_global_stale_sec = cfg.ws_global_stale_sec
        self.entry_percent = cfg.entry_percent

    async def run_once(self):
        for symbol in self.symbols:
            try:
                now = time.time()
                price = self.market.tick(symbol, now)

                actions: List[TradeAction] = await self.signal_processor.process_symbol(symbol, price)

                for act in actions:
                    if self.action_sender is not None:
                        await self.action_sender.send({
                            "ts_ms": int(time.time() * 1000),
                            "symbol": act.symbol,
                            "action": act.action,
                            "side": (act.side or "").upper() if act.side else None,
                            "price": act.price,
                            "signal_id": act.signal_id,
                            "close_open_signal_id": getattr(act, "close_open_signal_id", None),
                        })

            except Exception as e:
                if self.system_logger:
                    self.system_logger.exception(f"[{symbol}] run_once error: {e}")
                continue

        self.reporter.tick(time.time())
