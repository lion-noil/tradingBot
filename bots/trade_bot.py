# bots/trade_bot.py
import asyncio
import time
from typing import List
from bots.state.signals import OpenSignalsIndex, record_and_index_signal
from .trade_config import TradeConfig
from strategies.s1_reversion import S1Params
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
        self._last_exit_ts_ms: dict[tuple[str, str], int] = {}  # ✅ S1 쿨다운용
        # 심볼별 피드 stale 상태(장 마감 추정). 전이 시 1회만 로그하기 위한 플래그.
        self._feed_stale: dict[str, bool] = {}
        self._warmup_last_scaleout_ts()

        self.open_signals_index = OpenSignalsIndex()
        self.open_signals_index.load_from_redis(
            namespace=self.namespace,
            symbols=self.symbols,
        )

        if (getattr(self.config, "strategy", "basic") or "basic").lower() == "s1":
            self._warmup_s1_last_exit()

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
                ),

                # ✅ S1 전용 deps (strategy="s1"일 때만 사용; basic은 호출 안 함)
                get_recent_closes=lambda s: [
                    c["close"] for c in self.candle.get_candles(s) if c.get("close") is not None
                ],
                get_open_s1_positions=lambda sym, side: self.open_signals_index.list_open_s1(
                    namespace=self.namespace, symbol=sym, side=(side or "").upper()
                ),
                get_last_exit_ts_ms=lambda sym, side: self._last_exit_ts_ms.get(
                    ((sym or "").upper(), (side or "").upper())),
                set_last_exit_ts_ms=lambda sym, side, ts_ms: self._last_exit_ts_ms.__setitem__(
                    ((sym or "").upper(), (side or "").upper()), int(ts_ms)),
            ),
            strategy=getattr(self.config, "strategy", "basic"),
            s1_params=S1Params(
                win=int(getattr(self.config, "s1_win", 10080)),
                k1=float(getattr(self.config, "s1_k1", 2.5)),
                b=float(getattr(self.config, "s1_b", 2.0)),
                cooldown_sec=int(getattr(self.config, "s1_cooldown_sec", 12 * 3600)),
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

    def _warmup_s1_last_exit(self, *, count: int = 5000):
        """S1 쿨다운 복원: 쿨다운 기간만큼 거슬러 올라가 (sym,side)별 최신 EXIT ts 적재."""
        key = f"trading:{self.namespace}:signals"
        now_ms = int(time.time() * 1000)
        look_ms = (int(getattr(self.config, "s1_cooldown_sec", 12 * 3600)) + 60) * 1000
        rows = redis_client.xrevrange(key, max="+", min=f"{now_ms - look_ms}-0", count=count) or []
        for _sid, fields in rows:
            f = {}
            for fk, fv in (fields or {}).items():
                if isinstance(fk, (bytes, bytearray)): fk = fk.decode("utf-8", "ignore")
                if isinstance(fv, (bytes, bytearray)): fv = fv.decode("utf-8", "ignore")
                f[str(fk)] = str(fv)
            if (f.get("kind") or "").upper() != "EXIT":
                continue
            sym = (f.get("symbol") or "").upper()
            side = (f.get("side") or "").upper()
            ts = f.get("ts_ms")
            if not (sym and side and ts):
                continue
            try:
                ts_i = int(ts)
            except Exception:
                continue
            ek = (sym, side)
            if ek not in self._last_exit_ts_ms:  # newest-first → 처음이 최신
                self._last_exit_ts_ms[ek] = ts_i

    def _apply_config(self, cfg: TradeConfig) -> None:
        self.ws_stale_sec = cfg.ws_stale_sec
        self.ws_global_stale_sec = cfg.ws_global_stale_sec
        self.entry_percent = cfg.entry_percent

    def _feed_is_fresh(self, symbol: str) -> bool:
        """이 심볼의 시세 피드가 살아있는지(= 장이 열려있는지) 판정.

        ⚠️ 전역 heartbeat가 아니라 '이 심볼이 직접 갱신된 시각'만 본다.
        ws_is_fresh()는 전역 recv(하트비트)로도 fresh를 주기 때문에, US 지수처럼
        해당 심볼만 마감돼 틱이 끊겨도 다른 심볼/하트비트에 묻혀 'fresh'로 통과한다.
        여기서는 심볼별 recv(monotonic)만 보므로 마감 심볼을 정확히 stale로 잡는다.
        monotonic 기반이라 broker 서버 타임존 오프셋 문제도 없다.
        """
        get_recv = getattr(self.ws, "get_last_recv_time", None)
        if callable(get_recv):
            recv = get_recv(symbol)  # 심볼별 monotonic 수신시각
            if recv is not None:
                return (time.monotonic() - float(recv)) <= float(self.ws_stale_sec)

        # 폴백: 거래소 ts(epoch). recv를 못 쓰는 컨트롤러용.
        get_ex = getattr(self.ws, "get_last_exchange_ts", None)
        ex = get_ex(symbol) if callable(get_ex) else None
        if ex is None:
            # 한 번도 들어온 적 없는 심볼 → 아직 거래 금지(보수적으로 stale 취급)
            return False
        ex = float(ex)
        ex = ex / 1000.0 if ex > 1e12 else ex  # ms→sec 정규화
        return (time.time() - ex) <= float(self.ws_stale_sec)

    async def run_once(self):
        loop = asyncio.get_running_loop()
        for symbol in self.symbols:
            try:
                now = time.time()
                # market.tick()은 WS stale 시 동기 REST 백필(블로킹 requests ×N)을 수행한다.
                # 이벤트 루프에서 직접 돌리면 그동안 루프가 얼어 안전브레이커(loop.call_later)까지
                # 못 떠 영구 hang이 됨 → executor 스레드로 빼서 루프가 항상 살아있게 한다.
                # tick은 run_once에서만 순차 await로 호출되므로 동시 tick이 없고(캔들 엔진 유일 접근자),
                # WS 컨트롤러는 자체 락으로 thread-safe → 추가 락 불필요.
                price = await loop.run_in_executor(None, self.market.tick, symbol, now)

                # ✅ 세션/피드 게이트: 이 심볼의 시세 피드가 stale면(장 마감 등)
                #    신호 생성 자체를 건너뛴다. tick()/get_price()는 장 마감 후에도
                #    마지막 캐시 가격을 그대로 반환하므로, 이 게이트가 없으면 죽은
                #    가격으로 ENTRY/EXIT가 발생해 거래소가 10018(Market closed)로 거절한다.
                if not self._feed_is_fresh(symbol):
                    if not self._feed_stale.get(symbol):
                        self._feed_stale[symbol] = True
                        if self.system_logger:
                            self.system_logger.debug(  # 마감/피드정지는 정상 → 텔레그램 안 보냄(파일만)
                                f"[{symbol}] ⏸️ 시세 피드 stale(장 마감 추정) → 신호 처리 보류"
                            )
                    continue
                if self._feed_stale.get(symbol):
                    self._feed_stale[symbol] = False
                    if self.system_logger:
                        self.system_logger.debug(f"[{symbol}] ▶️ 시세 피드 복구 → 신호 처리 재개")  # 텔레그램 안 보냄

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
