# bots/trade_bot.py
import time
from typing import List

from .signals.pipeline import build_log_upload
from .trade_config import TradeConfig
from core.engines import CandleEngine, IndicatorEngine, JumpDetector
from core.execution import ExecutionEngine
from core.redis_client import redis_client

from .market.indicators import IndicatorState, bind_refresher
from .market.jump_reporting import JumpService
from .market.market_sync import MarketSync, MarketSyncConfig

from .state.entry_signal_store import EntrySignalStore
from .state.bot_state import BotState

from .reporting.status_reporter import StatusReporter, StatusReporterDeps
from .trading.signal_processor import SignalProcessor, SignalProcessorDeps, TradeAction
from .trading.trade_executor import TradeExecutor, TradeExecutorDeps


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
            default_exit_ma_threshold=self.config.default_exit_ma_threshold,
            min_ma_threshold=self.config.min_ma_threshold,  # ✅
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

            ma_check_enabled_map=self.state.ma_check_enabled,  # ✅ 추가
            min_ma_threshold=self.state.min_ma_threshold,  # ✅ 추가 (IndicatorState에 이 필드가 있어야 함)
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

        # entry store
        self.entry_store = EntrySignalStore(redis_client, self.symbols, name=self.namespace)

        # bootstrap
        self.state.asset = self.market.bootstrap(
            symbols=self.symbols,
            signal_only=self.signal_only,
            leverage=self.leverage,
            asset=self.state.asset,
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
            ),
        )

        # signal processor (signal decision only -> returns actions)
        self.signal_processor = SignalProcessor(
            system_logger=self.system_logger,
            deps=SignalProcessorDeps(
                get_asset=lambda: self.state.asset,
                get_now_ma100=lambda s: self.state.now_ma100.get(s),
                get_prev3_candle=lambda s: self.state.prev3_candle.get(s),
                get_ma_threshold=lambda s: self.state.ma_threshold.get(s),
                get_min_ma_threshold=lambda: self.state.min_ma_threshold,
                get_momentum_threshold=lambda s: self.state.momentum_threshold.get(s),
                get_exit_ma_threshold=lambda s: (
                    self.state.exit_ma_threshold.get(s)
                    if self.state.exit_ma_threshold.get(s) is not None
                    else self.config.default_exit_ma_threshold
                ),
                is_signal_only=lambda: self.signal_only,
                get_max_effective_leverage=lambda: self.max_effective_leverage,
                get_position_max_hold_sec=lambda: self.config.position_max_hold_sec,
                get_near_touch_window_sec=lambda: self.config.near_touch_window_sec,
                entry_store_get=lambda sym, side: self.entry_store.get(sym, side),
                entry_store_set=lambda sym, side, ts: self.entry_store.set(sym, side, ts),
                log_signal=lambda sym, sig: build_log_upload(
                    self.trading_logger,redis_client,sig,sym,self.namespace,keep_days=10,),
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
            # 1) 시세 읽기 + 캔들 누적/REST 백필 + 확정봉 반영 + 지표 refresh(+ jump 상태 업데이트), indicator계산(cross pct 계산)
            price = self.market.tick(symbol, now)

            # 2) 시그널 생성 (청산 → 진입) => action list 반환
            actions: List[TradeAction] = await self.signal_processor.process_symbol(symbol, price)

            # 3) 실행(주문/동기화)
            for act in actions:
                if act.action == "OPEN":
                    await self.trade_executor.open_position(act.symbol, act.side, act.price)
                elif act.action == "CLOSE":
                    await self.trade_executor.close_position(act.symbol, act.side, act.qty)

        # 4) 상태/로그 스냅샷(변화 감지 및 리포팅)
        self.reporter.tick(now)
