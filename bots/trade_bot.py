# bots/trade_bot.py
import time
from typing import List
from bots.state.signals import (
    record_signal_with_ts,
    open_push,
    open_pop,
)
from bots.state.signals import OpenSignalsIndex, OpenSignalStats
from .signals.pipeline import build_log_upload
from .trade_config import TradeConfig
from core.engines import CandleEngine, IndicatorEngine, JumpDetector
from core.execution import ExecutionEngine
from core.redis_client import redis_client
from .market.indicators import IndicatorState, bind_refresher
from .market.jump_reporting import JumpService
from .market.market_sync import MarketSync, MarketSyncConfig
from .state.bot_state import BotState
from .reporting.status_reporter import StatusReporter, StatusReporterDeps
from .trading.signal_processor import SignalProcessor, SignalProcessorDeps, TradeAction
from .trading.trade_executor import TradeExecutor, TradeExecutorDeps
from bots.state.lots import (
    open_lot,
    close_lot_full,
    get_lot_qty_total,
    LotsIndex,
)


class TradeBot:
    def __init__(
            self,
            bybit_websocket_controller,
            bybit_rest_controller,
            manual_queue,
            system_logger=None,
            trading_logger=None,
            symbols=("BTCUSDT",),
            signal_only: bool = False,
            config: TradeConfig | None = None,
    ):
        self.ws = bybit_websocket_controller
        self.rest = bybit_rest_controller
        self.manual_queue = manual_queue
        self.system_logger = system_logger
        self.trading_logger = trading_logger
        self.symbols: List[str] = list(symbols)

        # config
        self.config = (TradeConfig().normalized() if config is None else config.normalized())
        self.namespace: str = getattr(self.config, "name", None) or "bybit"
        self.config.to_redis(redis_client, publish=True)
        self.signal_only = bool(getattr(self.config, "signal_only", signal_only))

        # engines
        self.candle = CandleEngine(candles_num=self.config.candles_num)
        self.indicator = IndicatorEngine(
            min_thr=self.config.indicator_min_thr,
            max_thr=self.config.indicator_max_thr,
            target_cross=self.config.target_cross,
        )
        self.jump = JumpDetector(history_num=10, polling_interval=0.5)

        self.exec = ExecutionEngine(
            self.rest,
            system_logger,
            trading_logger,
            taker_fee_rate=0.00055,
            engine_name=self.namespace,
        )

        self._apply_config(self.config)

        # state
        self.state = BotState(
            symbols=self.symbols,
            default_ma_easing=self.config.default_ma_easing,
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
        self.state.asset = self.market.bootstrap(
            symbols=self.symbols,
            signal_only=self.signal_only,
            leverage=self.leverage,
            asset=self.state.asset,
        )

        self.open_signals_index = OpenSignalsIndex()
        self.open_signals_index.load_from_redis(
            namespace=self.namespace,
            symbols=self.symbols,
        )

        # lots cache
        self.lots_index = LotsIndex()
        self.lots_index.load_from_redis(
            redis_client,
            namespace=self.namespace,
            symbols=self.symbols,
        )

        # executor (orders + sync)
        self.trade_executor = TradeExecutor(
            rest=self.rest,
            exec_engine=self.exec,
            system_logger=self.system_logger,
            deps=TradeExecutorDeps(
                is_signal_only=lambda: self.signal_only,
                get_asset=lambda: self.state.asset,
                set_asset=lambda a: setattr(self.state, "asset", a),
                get_entry_percent=lambda: self.entry_percent,

                open_lot=lambda *, symbol, side, entry_ts_ms, entry_price, qty_total, entry_signal_id=None: open_lot(
                    namespace=self.namespace,
                    symbol=symbol,
                    side=side,
                    entry_ts_ms=entry_ts_ms,
                    entry_price=entry_price,
                    qty_total=qty_total,
                    entry_signal_id=entry_signal_id,
                ),
                close_lot_full=lambda *, lot_id: close_lot_full(
                    namespace=self.namespace,
                    lot_id=lot_id,
                ),
                get_lot_qty_total=lambda lot_id: get_lot_qty_total(
                    namespace=self.namespace,
                    lot_id=lot_id,
                ),

                on_lot_open=lambda sym, side, lot_id, entry_ts_ms, qty_total, entry_price: self.lots_index.on_open(
                    sym,
                    side,
                    lot_id,
                    entry_ts_ms=entry_ts_ms,
                    qty_total=qty_total,
                    entry_price=entry_price,
                ),
                on_lot_close=lambda sym, side, lot_id: self.lots_index.on_close(sym, side, lot_id),

                # ✅ executor가 lot_id=None일 때 직접 선택하려면 필수
                pick_open_lot_ids=lambda sym, side, policy, limit: self.lots_index.pick_open_lot_ids(
                    sym, side, policy=policy, limit=limit
                ),
            ),
        )

        # ✅ Signal 저장/로그 업로드를 한 곳으로
        def _log_signal(sym, side, kind, price, sig):
            sid, ts = record_signal_with_ts(
                namespace=self.namespace,
                symbol=sym,
                side=side,
                kind=kind,
                price=price,
                payload=sig,
            )

            # ✅ open 상태 반영 (체결/lot과 무관하게 "신호" 기준으로만)
            kind_u = (kind or "").upper()
            side_u = (side or "").upper()

            if kind_u == "OPEN":
                self.open_signals_index.on_open(
                    namespace=self.namespace,
                    symbol=sym,
                    side=side_u,
                    signal_id=sid,
                    ts_ms=ts,
                )
            elif kind_u == "CLOSE":
                self.open_signals_index.on_close(
                    namespace=self.namespace,
                    symbol=sym,
                    side=side_u,
                    policy="LIFO",  # 네 정책대로
                )

            build_log_upload(self.trading_logger, redis_client, sig, sym, self.namespace, keep_days=10)
            return sid, ts

        # signal processor
        self.signal_processor = SignalProcessor(
            system_logger=self.system_logger,
            deps=SignalProcessorDeps(
                get_asset=lambda: self.state.asset,
                get_now_ma100=lambda s: self.state.now_ma100.get(s),
                get_prev3_candle=lambda s: self.state.prev3_candle.get(s),
                get_ma_threshold=lambda s: self.state.ma_threshold.get(s),
                get_momentum_threshold=lambda s: self.state.momentum_threshold.get(s),

                is_signal_only=lambda: self.signal_only,
                get_max_effective_leverage=lambda: self.max_effective_leverage,
                get_position_max_hold_sec=lambda: self.config.position_max_hold_sec,
                get_near_touch_window_sec=lambda: self.config.near_touch_window_sec,
                get_min_ma_threshold=lambda: self.state.min_ma_threshold,
                get_ma_easing=lambda s: self.state.get_ma_easing(s),
                get_open_signal_stats=lambda sym, side: self.open_signals_index.stats(
                    symbol=sym,
                    side=side.upper(),
                ),

                log_signal=_log_signal,
            ),
        )

        # reporter
        self.reporter = StatusReporter(
            system_logger=self.system_logger,
            deps=StatusReporterDeps(
                get_wallet=lambda: (self.state.asset.get("wallet") or {}),
                get_symbols=lambda: self.symbols,
                get_jump_state=lambda: self.jump_service.get_state_map(),
                get_ma_threshold=lambda: self.state.ma_threshold,
                get_now_ma100=lambda: self.state.now_ma100,
                get_positions_by_symbol=lambda: (self.state.asset.get("positions") or {}),
                get_price=lambda s, now_ts: self.market.get_price(s, now_ts),
                get_taker_fee_rate=lambda: getattr(self.exec, "TAKER_FEE_RATE", 0.00055),
            ),
        )

    def _apply_config(self, cfg: TradeConfig) -> None:
        self.ws_stale_sec = cfg.ws_stale_sec
        self.ws_global_stale_sec = cfg.ws_global_stale_sec
        self.leverage = cfg.leverage
        self.entry_percent = cfg.entry_percent
        self.max_effective_leverage = cfg.max_effective_leverage

    async def run_once(self):
        now = time.time()
        for symbol in self.symbols:
            price = self.market.tick(symbol, now)

            actions: List[TradeAction] = await self.signal_processor.process_symbol(symbol, price)

            for act in actions:
                if act.action == "OPEN":
                    await self.trade_executor.open_position(
                        act.symbol,
                        act.side,
                        act.price,
                        entry_signal_id=act.signal_id,
                    )

                elif act.action == "CLOSE":
                    # ✅ lot_id는 실행 직전에 고른다 (신호/체결 분리 유지)
                    lot_id = act.lot_id
                    if not lot_id:
                        ids = self.lots_index.pick_open_lot_ids(
                            act.symbol,
                            (act.side or "").upper(),
                            policy="LIFO",
                            limit=1,
                        )
                        lot_id = ids[0] if ids else None

                    await self.trade_executor.close_position(
                        act.symbol,
                        act.side,
                        lot_id,
                        exit_signal_id=act.signal_id,
                    )

        self.reporter.tick(now)
