# bots/trade_bot.py
import time
from typing import List
from bots.state.signals import (
    record_signal_with_ts,
)
from bots.state.signals import OpenSignalsIndex
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
    get_lot_ex_lot_id
)
import json  # trade_bot.py 상단에 추가 (없으면)

from datetime import datetime
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("Asia/Seoul")


class TradeBot:
    def __init__(
            self,
            ws_controller,
            rest_controller,
            manual_queue,
            system_logger=None,
            trading_logger=None,
            symbols=("BTCUSDT",),
            signal_only: bool = False,
            config: TradeConfig | None = None,
    ):
        self.ws = ws_controller
        self.rest = rest_controller
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

        self._warmup_symbol_rules()

        self.open_signals_index = OpenSignalsIndex()
        self.open_signals_index.load_from_redis(
            namespace=self.namespace,
            symbols=self.symbols,
        )

        # lots cache
        self.lots_index = LotsIndex(namespace=self.namespace, redis_cli=redis_client)
        self.lots_index.load_from_redis(symbols=self.symbols)

        # executor (orders + sync)
        self.trade_executor = TradeExecutor(
            rest=self.rest,
            exec_engine=self.exec,
            system_logger=self.system_logger,
            deps=TradeExecutorDeps(
                is_signal_only=lambda: self.signal_only,
                get_asset=lambda: self.state.asset,
                set_asset=lambda a: setattr(self.state, "asset", a),
                get_entry_percent=lambda sym: self._get_entry_percent_for_symbol(sym),

                open_lot=lambda *, symbol, side, entry_ts_ms, entry_price, qty_total, entry_signal_id=None,
                                ex_lot_id=None: open_lot(
                    namespace=self.namespace,
                    symbol=symbol,
                    side=side,
                    entry_ts_ms=entry_ts_ms,
                    entry_price=entry_price,
                    qty_total=qty_total,
                    entry_signal_id=entry_signal_id,
                    ex_lot_id=ex_lot_id,  # ✅ 추가
                ),
                close_lot_full=lambda *, lot_id: close_lot_full(
                    namespace=self.namespace,
                    lot_id=lot_id,
                ),
                get_lot_qty_total=lambda lot_id: get_lot_qty_total(
                    namespace=self.namespace,
                    lot_id=lot_id,
                ),

                # ✅ 추가: lot_id -> ex_lot_id
                get_lot_ex_lot_id=lambda lot_id: get_lot_ex_lot_id(
                    namespace=self.namespace,
                    lot_id=lot_id,
                ),

                # ✅ on_lot_open: ex_lot_id까지 받도록
                on_lot_open=lambda sym, side, lot_id, entry_ts_ms, qty_total, entry_price, entry_signal_id,
                                   ex_lot_id: self.lots_index.on_open(
                    sym,
                    side,
                    lot_id,
                    entry_ts_ms=entry_ts_ms,
                    qty_total=qty_total,
                    entry_price=entry_price,
                    entry_signal_id=entry_signal_id,
                    ex_lot_id=int(ex_lot_id or 0),  # ✅ 추가
                ),
                on_lot_close=lambda sym, side, lot_id: self.lots_index.on_close(sym, side, lot_id),
            ),
        )

        def _extract_open_signal_id(payload):
            if not isinstance(payload, dict):
                return None
            v = payload.get("open_signal_id") or payload.get("target_open_signal_id")
            return str(v) if v else None

        # ✅ Signal 저장/로그 업로드를 한 곳으로
        def _log_signal(sym, side, kind, price, sig):
            payload = sig if isinstance(sig, dict) else {}

            # ✅ 표준화: kind는 ENTRY/EXIT로 통일 (방어)
            kind_u = (kind or "").upper().strip()
            if kind_u == "OPEN":
                kind_u = "ENTRY"
            elif kind_u == "CLOSE":
                kind_u = "EXIT"

            side_u = (side or "").upper().strip()
            sym_u = (sym or "").upper().strip()

            sig_dict = {
                **payload,
                "kind": kind_u,
                "side": side_u,
                "symbol": sym_u,
                "ts": datetime.now(_TZ).isoformat(),
                "price": price,
                "engine": self.namespace,
            }

            sid, ts = record_signal_with_ts(
                namespace=self.namespace,
                symbol=sym_u,
                side=side_u,
                kind=kind_u,
                price=price,
                payload=sig_dict,
            )

            if kind_u == "ENTRY":
                self.open_signals_index.on_open(
                    namespace=self.namespace,
                    symbol=sym_u,
                    side=side_u,
                    signal_id=sid,
                    ts_ms=ts,
                    entry_price=float(price or 0.0),  # ✅ 추가
                )

            elif kind_u == "EXIT":
                target_open_id = _extract_open_signal_id(payload)
                if target_open_id:
                    self.open_signals_index.on_close_by_id(
                        namespace=self.namespace,
                        symbol=sym_u,
                        side=side_u,
                        open_signal_id=str(target_open_id),
                    )
                else:
                    if self.system_logger:
                        self.system_logger.warning(f"[{sym_u}] EXIT signal missing open_signal_id (side={side_u})")

            # ✅ pipeline 대신: 로그만 남김
            if self.trading_logger:
                try:
                    self.trading_logger.info("SIG " + json.dumps(sig_dict, ensure_ascii=False, default=str))
                except Exception:
                    self.trading_logger.info(f"SIG {sig_dict}")

            return sid, ts

        # signal processor
        self.signal_processor = SignalProcessor(
            system_logger=self.system_logger,
            deps=SignalProcessorDeps(
                get_asset=lambda: self.state.asset,
                get_now_ma100=lambda s: self.state.now_ma100.get(s),
                get_prev3_candle=lambda s: self.state.prev3_candle.get(s),
                get_ma_threshold=lambda s: (
                    self.state.ma_threshold.get(s)
                    if self.state.ma_check_enabled.get(s, True)
                    else None
                ),
                get_momentum_threshold=lambda s: self.state.momentum_threshold.get(s),

                is_signal_only=lambda: self.signal_only,
                get_max_effective_leverage=lambda: self.max_effective_leverage,
                get_position_max_hold_sec=lambda: self.config.position_max_hold_sec,
                get_near_touch_window_sec=lambda: self.config.near_touch_window_sec,
                get_min_ma_threshold=lambda: self.state.min_ma_threshold,
                get_ma_easing=lambda s: self.state.get_ma_easing(s),
                get_open_signal_items=lambda sym, side: self.open_signals_index.list_open(
                    namespace=self.namespace, symbol=sym, side=side.upper(), newest_first=True
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
                get_ma_check_enabled=lambda: self.state.ma_check_enabled,
                get_min_ma_threshold=lambda: self.state.min_ma_threshold,
            ),
        )

    def _warmup_symbol_rules(self) -> None:
        """
        rest controller의 _symbol_rules를 심볼별로 미리 채워둔다.
        - 있으면 get_symbol_rules / fetch_symbol_rules 호출
        - 없으면 로그만 남김(TradeExecutor가 rules 없으면 주문/lot 스킵함)
        """
        rest = self.rest
        syms = [str(s).upper().strip() for s in (self.symbols or []) if s]

        get_fn = getattr(rest, "get_symbol_rules", None)
        fetch_fn = getattr(rest, "fetch_symbol_rules", None)

        for sym in syms:
            try:
                if callable(get_fn):
                    get_fn(sym)
                elif callable(fetch_fn):
                    fetch_fn(sym)
                else:
                    # 함수가 없으면 rules는 외부에서 채워져야 함
                    if self.system_logger:
                        self.system_logger.warning(
                            f"[rules] no get_symbol_rules/fetch_symbol_rules on rest (sym={sym})"
                        )
            except Exception as e:
                if self.system_logger:
                    self.system_logger.warning(f"[rules] warmup failed (sym={sym}) err={e}")

        # 최종 상태 로그(선택)
        try:
            m = getattr(rest, "_symbol_rules", None)
            if self.system_logger and isinstance(m, dict):
                self.system_logger.debug(f"[rules] warmed: {sorted(list(m.keys()))[:20]}")
        except Exception:
            pass

    def _get_entry_percent_for_symbol(self, symbol: str) -> float:
        sym = (symbol or "").upper().strip()
        m = getattr(self.config, "entry_percent_by_symbol", None) or {}
        v = m.get(sym)
        if v is None:
            return float(self.entry_percent)
        try:
            return max(0.001, float(v))
        except Exception:
            return float(self.entry_percent)

    def _apply_config(self, cfg: TradeConfig) -> None:
        self.ws_stale_sec = cfg.ws_stale_sec
        self.ws_global_stale_sec = cfg.ws_global_stale_sec
        self.leverage = cfg.leverage
        self.entry_percent = cfg.entry_percent
        self.max_effective_leverage = cfg.max_effective_leverage

    async def run_once(self):
        for symbol in self.symbols:
            try:
                now = time.time()
                price = self.market.tick(symbol, now)

                actions: List[TradeAction] = await self.signal_processor.process_symbol(symbol, price)

                for act in actions:
                    if act.action == "ENTRY":
                        if act.price is None:
                            continue
                        await self.trade_executor.open_position(
                            act.symbol,
                            act.side,
                            act.price,
                            entry_signal_id=act.signal_id,
                        )


                    elif act.action == "EXIT":
                        if not act.close_open_signal_id:
                            if self.system_logger:
                                self.system_logger.warning(
                                    f"[{act.symbol}] EXIT skip: missing close_open_signal_id (side={act.side})"
                                )
                            continue

                        lot_id = self.lots_index.find_open_lot_id_by_entry_signal_id(
                            act.symbol,
                            (act.side or "").upper(),
                            act.close_open_signal_id,
                        )
                        if not lot_id:
                            if self.system_logger:
                                self.system_logger.warning(
                                    f"[{act.symbol}] EXIT skip: lot_id not found for open_signal_id={act.close_open_signal_id} side={act.side}"
                                )
                            continue
                        await self.trade_executor.close_position(
                            act.symbol, act.side, lot_id, exit_signal_id=act.signal_id
                        )
            except Exception as e:
                if self.system_logger:
                    self.system_logger.exception(f"[{symbol}] run_once error: {e}")
                continue

        self.reporter.tick(time.time())
