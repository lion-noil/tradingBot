# bots/trade_bot.py
import time
import json
from zoneinfo import ZoneInfo
from typing import Any, Optional, Dict, List
from datetime import datetime, timezone, timedelta
from .trade_config import TradeConfig
from core.engines import CandleEngine, IndicatorEngine, JumpDetector
from core.execution import ExecutionEngine
from strategies.basic_strategy import (
    get_short_entry_signal, get_long_entry_signal, get_exit_signal
)
from core.redis_client import redis_client

from .entry_signal_store import EntrySignalStore
from .trade_functions import (
    upload_signal,
    log_jump,
    extract_status_summary,
    bootstrap_candles_for_symbol,
    should_log_update,
    ws_is_fresh,
    build_full_status_log,
    refresh_indicators_for_symbol,
    bootstrap_all_symbols
)

_TZ = ZoneInfo("Asia/Seoul")
KST = timezone(timedelta(hours=9))


class TradeBot:
    def __init__(
            self,
            bybit_websocket_controller,
            bybit_rest_controller,
            manual_queue,
            system_logger=None,
            trading_logger=None,
            symbols=("BTCUSDT",),
            signal_only: bool = False,          # 시그널만 생성할지 여부 (fallback)
            config: TradeConfig | None = None,  # ✅ 외부에서 주입되는 설정
    ):


        # 0) 구성요소(외부 핸들)
        self.ws = bybit_websocket_controller
        self.rest = bybit_rest_controller
        self.manual_queue = manual_queue
        self.system_logger = system_logger
        self.trading_logger = trading_logger
        self.symbols: List[str] = list(symbols)

        # 1) 설정: 외부 config가 있으면 그걸 우선 사용
        if config is None:
            self.config = TradeConfig().normalized()
        else:
            self.config = config.normalized()

        # 🔹 네임스페이스 (bybit / mt5_signal 등)
        #    TradeConfig.name 가 없거나 비어있으면 bybit 를 기본값으로 사용
        self.namespace: str = getattr(self.config, "name", None) or "bybit"

        # Redis에는 항상 현재 config 올려두기
        self.config.to_redis(redis_client, publish=True)  # 브로드캐스트 원치 않으면 publish=False

        # signal_only는 config 값이 우선, 없으면 인자로 받은 값 사용
        self.signal_only = bool(getattr(self.config, "signal_only", signal_only))

        # 2) 엔진/파라미터 주입
        self.target_cross = self.config.target_cross
        self.candle = CandleEngine(candles_num=self.config.candles_num)
        self.indicator = IndicatorEngine(
            min_thr=self.config.indicator_min_thr,
            max_thr=self.config.indicator_max_thr,
            target_cross=self.target_cross
        )
        self.jump = JumpDetector(history_num=10, polling_interval=0.5)
        self.exec = ExecutionEngine(self.rest, system_logger, trading_logger, taker_fee_rate=0.00055)

        self._apply_config(self.config)

        # 4) 상태
        self.asset: Dict[str, Any] = {
            "wallet": {"USDT": 0.0},
            "positions": {s: {} for s in self.symbols},
        }
        self.ma100s: Dict[str, Optional[float]] = {s: None for s in self.symbols}
        self.now_ma100: Dict[str, Optional[float]] = {s: None for s in self.symbols}
        self.ma_threshold: Dict[str, Optional[float]] = {s: None for s in self.symbols}
        self.momentum_threshold: Dict[str, Optional[float]] = {s: None for s in self.symbols}
        self.exit_ma_threshold: Dict[str, float] = {
            s: self.config.default_exit_ma_threshold for s in self.symbols
        }
        self._thr_quantized: Dict[str, Optional[float]] = {s: None for s in self.symbols}
        self.prev: Dict[str, Optional[float]] = {s: None for s in self.symbols}
        self._rest_fallback_on: Dict[str, bool] = {s: False for s in self.symbols}
        self._stale_counts: Dict[str, int] = {s: 0 for s in self.symbols}
        self._last_closed_minute: Dict[str, Optional[int]] = {s: None for s in self.symbols}

        self.jump_state: Dict[str, Dict[str, Any]] = {
            s: {"state": None, "min_dt": None, "max_dt": None, "ts": None} for s in self.symbols
        }

        # 엔트리 시그널 저장소

        self.entry_store = EntrySignalStore(
            redis_client,
            self.symbols,
            name=self.namespace,   # ★ 여기만 바꿔주면 됨
        )

        # 구독 시작
        subscribe = getattr(self.ws, "subscribe_symbols", None)
        if callable(subscribe):
            try:
                subscribe(*self.symbols)
            except Exception:
                pass

        def _refresh_one(sym: str) -> None:
            refresh_indicators_for_symbol(
                self.candle, self.indicator, sym,
                ma100s=self.ma100s,
                now_ma100_map=self.now_ma100,
                ma_threshold_map=self.ma_threshold,
                thr_quantized_map=self._thr_quantized,
                momentum_threshold_map=self.momentum_threshold,
                prev3_candle_map=self.prev,
                system_logger=self.system_logger,
                redis_client=redis_client,
                namespace=self.namespace,
            )

        # 5) 초기 세팅(부트스트랩)
        if self.signal_only:
            # ✅ 시그널-only: 트레이딩 상태는 건드리지 않고,
            #    과거 캔들 + MA100/threshold만 초기화
            for sym in self.symbols:
                bootstrap_candles_for_symbol(
                    rest_client=self.rest,
                    candle_engine=self.candle,
                    refresh_indicators=_refresh_one,
                    symbol=sym,
                    candles_num=self.config.candles_num,
                    system_logger=self.system_logger,
                )
            if self.system_logger:
                self.system_logger.debug("[TradeBot] signal_only 모드: 캔들/인디케이터만 부트스트랩")
        else:
            # ✅ 주문 모드: 기존 동작 유지 (자산/포지션 + 캔들/인디케이터 모두 부트스트랩)
            self.asset = bootstrap_all_symbols(
                rest_client=self.rest,
                candle_engine=self.candle,
                refresh_indicators=_refresh_one,
                symbols=self.symbols,
                leverage=self.leverage,
                asset=self.asset,
                candles_num=self.config.candles_num,
                system_logger=self.system_logger,
            )

        self._last_log_snapshot: Optional[str] = None
        self._last_log_summary: Optional[Dict[str, Any]] = None
        self._last_log_reason: Optional[str] = None

    # ─────────────────────────────────────────────
    # 메인 루프(1틱)
    async def run_once(self):
        now = time.time()
        for symbol in self.symbols:
            # 1) 실시간 가격 기록
            price = self._price_record(symbol)

            # 2) WS 상태에 따라 진행중 봉 누적 혹은 REST 백필
            self._candle_backfill(symbol, price, now)  # ← 인자 추가

            # 3) kline(확정 봉) 반영 → 지표 업데이트
            self._candle_record(symbol)  # ← 인자 추가

            # 4) 급등락 테스트
            self._updown_test(symbol)  # ← 인자 추가

            # 5) 자동매매 (쿨다운은 ExecutionEngine 내부에서 관리)
            if price is None or self.now_ma100[symbol] is None:
                continue

            # 5-1) 청산 처리
            await self._process_exits(symbol, price)

            # 5-1) 진입 처리
            await self._process_entries(symbol, price)

        # 8) 상태 로그 스냅샷/변화 감지
        self._finalize_status_log()

    def _finalize_status_log(self) -> None:
        wallet = (self.asset.get("wallet") or {})
        total_balance, ccy = self._get_total_balance_and_ccy(wallet)
        new_status = build_full_status_log(
            total_usdt=float(total_balance),
            currency=ccy,
            symbols=self.symbols,
            jump_state=self.jump_state,
            ma_threshold=self.ma_threshold,
            now_ma100=self.now_ma100,
            get_price=lambda s: getattr(self.ws, "get_price")(s),
            positions_by_symbol=(self.asset.get("positions") or {}),
            taker_fee_rate=getattr(self.exec, "TAKER_FEE_RATE", 0.00055),
        )
        new_summary = extract_status_summary(new_status, fallback_ma_threshold_pct=None)
        should, reason = should_log_update(self._last_log_summary, new_summary)
        if should:
            if self.system_logger:
                self.system_logger.debug((reason or "") + new_status)
            self._last_log_snapshot = new_status
            self._last_log_summary = new_summary
            self._last_log_reason = reason

    def _price_record(self, symbol: str) -> Optional[float]:
        price = getattr(self.ws, "get_price")(symbol)
        exchange_ts = getattr(self.ws, "get_last_exchange_ts")(symbol)
        if price:
            self.jump.record_price(symbol, price, exchange_ts)
        return price

    def _candle_record(self, symbol: str) -> None:
        get_ck = getattr(self.ws, "get_last_confirmed_kline", None)
        if not callable(get_ck):
            return

        k = get_ck(symbol, "1")
        if not (k and k.get("confirm")):
            return

        k_start_minute = int(k["start"] // 60000)

        if k_start_minute is None or k_start_minute != self._last_closed_minute[symbol]:
            self.candle.apply_confirmed_kline(symbol, k)
            # 지표 갱신
            refresh_indicators_for_symbol(
                self.candle, self.indicator, symbol,
                ma100s=self.ma100s,
                now_ma100_map=self.now_ma100,
                ma_threshold_map=self.ma_threshold,
                thr_quantized_map=self._thr_quantized,
                momentum_threshold_map=self.momentum_threshold,
                prev3_candle_map=self.prev,
                system_logger=self.system_logger,
                redis_client=redis_client,
                namespace=self.namespace
            )
            self._last_closed_minute[symbol] = k_start_minute

    def _candle_backfill(self, symbol: str, price: Optional[float], now_ts: float) -> None:
        use_ws = ws_is_fresh(self.ws, symbol, self.ws_stale_sec, self.ws_global_stale_sec)
        if use_ws:
            ts = getattr(self.ws, "get_last_exchange_ts")(symbol) or now_ts
            if price:
                self.candle.accumulate_with_ticker(symbol, price, float(ts))
            if self._rest_fallback_on[symbol]:
                self._rest_fallback_on[symbol] = False
                if self.system_logger:
                    self.system_logger.info(f"[{symbol}] ✅ WS 복구, 실시간 집계 재개")
            self._stale_counts[symbol] = 0
        else:
            self._stale_counts[symbol] += 1
            if self._stale_counts[symbol] >= 2:
                if not self._rest_fallback_on[symbol]:
                    self._rest_fallback_on[symbol] = True
                    if self.system_logger:
                        self.system_logger.error(f"[{symbol}] ⚠️ WS stale → REST 백필")
                self.rest.update_candles(self.candle.get_candles(symbol), symbol=symbol, count=self.config.candles_num)
                refresh_indicators_for_symbol(
                    self.candle, self.indicator, symbol,
                    ma100s=self.ma100s,
                    now_ma100_map=self.now_ma100,
                    ma_threshold_map=self.ma_threshold,
                    thr_quantized_map=self._thr_quantized,
                    momentum_threshold_map=self.momentum_threshold,
                    prev3_candle_map=self.prev,
                    system_logger=self.system_logger,
                    redis_client=redis_client,
                    namespace=self.namespace
                )

    def _updown_test(self, symbol: str) -> None:
        state, min_dt, max_dt = self.jump.check_jump(symbol, self.ma_threshold.get(symbol))
        self.jump_state[symbol]["state"] = state
        self.jump_state[symbol]["min_dt"] = min_dt
        self.jump_state[symbol]["max_dt"] = max_dt
        self.jump_state[symbol]["ts"] = time.time() if state else self.jump_state[symbol]["ts"]
        log_jump(self.system_logger, symbol, state, min_dt, max_dt)

    async def _process_exits(self, symbol: str, price: float) -> None:
        """5) 청산 시그널 일괄 처리"""
        for side in ["LONG", "SHORT"]:
            recent_time = self.entry_store.get(symbol, side)
            if not recent_time:
                continue

            sig = get_exit_signal(
                side,
                price,
                self.now_ma100[symbol],
                recent_entry_time=recent_time,
                ma_threshold=self.ma_threshold.get(symbol),
                exit_ma_threshold=self.exit_ma_threshold.get(symbol),
                time_limit_sec=24 * 3600,
                near_touch_window_sec=60 * 60
            )
            if not sig:
                continue

            # 엔트리 기록 제거
            self.entry_store.set(symbol, side, None)

            sig_dict = self._build_signal_dict(sig, symbol)
            self._log_and_upload_signal(sig_dict)

            pos_amt = abs(float((self.asset['positions'][symbol].get(side) or {}).get('qty') or 0))
            if pos_amt == 0:
                if self.system_logger:
                    self.system_logger.info(f"({symbol}) EXIT 신호 발생했지만 포지션 {side} 수량 0 → 체결 스킵")
                continue

            await self._close_position(symbol, side, pos_amt)

    def _get_total_balance_usd(self, wallet: dict) -> float:
        # USDT 우선, 없으면 USD
        return float(wallet.get("USDT") or wallet.get("USD") or 0.0)

    def _get_total_balance_and_ccy(self, wallet: dict) -> tuple[float, str]:
        if wallet is None:
            return 0.0, "USD"
        if wallet.get("USDT") not in (None, 0, "0", 0.0):
            return float(wallet.get("USDT") or 0.0), "USDT"
        if wallet.get("USD") not in (None, 0, "0", 0.0):
            return float(wallet.get("USD") or 0.0), "USD"
        # 둘 다 없으면 기본
        return 0.0, "USD"


    async def _process_entries(self, symbol: str, price: float) -> None:
        """6) 진입 시그널(숏/롱) 처리"""

        wallet = (self.asset.get("wallet") or {})
        positions_by_symbol = (self.asset.get("positions") or {})
        pos = (positions_by_symbol.get(symbol) or {})

        # ✅ signal_only면 레버리지 제한 무시(무조건 통과)
        allow_entry = True
        total_balance = self._get_total_balance_usd(wallet)

        # --- Short 진입 ---
        recent_short_signal_time = self.entry_store.get(symbol, "SHORT")
        short_amt = abs(float(((pos.get("SHORT") or {}).get("qty")) or 0.0))
        short_eff_x = (short_amt * price / total_balance) if (total_balance and not self.signal_only) else 0.0

        if allow_entry and (self.signal_only or short_eff_x < self.max_effective_leverage):
            sig_s = get_short_entry_signal(
                price=price, ma100=self.now_ma100[symbol], prev3_candle=self.prev[symbol],
                ma_threshold=self.ma_threshold[symbol],
                momentum_threshold=self.momentum_threshold[symbol],
                recent_entry_time=recent_short_signal_time, reentry_cooldown_sec=60 * 60
            )
            if sig_s:
                now_ms = int(time.time() * 1000)
                sig_dict = self._build_signal_dict(sig_s, symbol)
                self._log_and_upload_signal(sig_dict)
                self.entry_store.set(symbol, "SHORT", now_ms)
                await self._open_position(symbol, "SHORT", price)  # open은 내부 가드로 막기

        # --- Long 진입 ---
        recent_long_signal_time = self.entry_store.get(symbol, "LONG")
        long_amt = abs(float(((pos.get("LONG") or {}).get("qty")) or 0.0))
        long_eff_x = (long_amt * price / total_balance) if (total_balance and not self.signal_only) else 0.0

        if allow_entry and (self.signal_only or long_eff_x < self.max_effective_leverage):
            sig_l = get_long_entry_signal(
                price=price, ma100=self.now_ma100[symbol], prev3_candle=self.prev[symbol],
                ma_threshold=self.ma_threshold[symbol],
                momentum_threshold=self.momentum_threshold[symbol],
                recent_entry_time=recent_long_signal_time, reentry_cooldown_sec=60 * 60
            )
            if sig_l:
                now_ms = int(time.time() * 1000)
                sig_dict = self._build_signal_dict(sig_l, symbol)
                self._log_and_upload_signal(sig_dict)
                self.entry_store.set(symbol, "LONG", now_ms)
                await self._open_position(symbol, "LONG", price)

    def _build_signal_dict(self, sig, symbol: str) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "kind": sig.kind,
            "side": sig.side,
            "symbol": symbol,
            "ts": datetime.now(_TZ).isoformat(),
            "price": sig.price,
            "ma100": sig.ma100,
            "ma_delta_pct": sig.ma_delta_pct,
            "thresholds": sig.thresholds,
            "reasons": sig.reasons,
        }
        if getattr(sig, "extra", None):
            d["extra"] = sig.extra
        return d

    def _log_and_upload_signal(self, sig_dict: Dict[str, Any]) -> None:
        if self.trading_logger:
            self.trading_logger.info('SIG ' + json.dumps(sig_dict, ensure_ascii=False))
        upload_signal(
            redis_client,
            sig_dict,
            namespace=self.namespace,
        )

    async def _close_position(self, symbol: str, side: str, qty: float) -> None:
        if self.signal_only:
            if self.system_logger:
                self.system_logger.info(f"[signal_only] CLOSE 스킵 ({symbol} {side} qty={qty})")
            return

        await self.exec.execute_and_sync(
            self.rest.close_market, self.asset['positions'][symbol][side], symbol,
            symbol, side=side, qty=qty
        )
        self.asset = self.rest.getNsav_asset(asset=self.asset, symbol=symbol, save_redis=True)

    async def _open_position(self, symbol: str, side: str, price: float) -> None:
        if self.signal_only:
            if self.system_logger:
                self.system_logger.info(f"[signal_only] OPEN 스킵 ({symbol} {side} price={price})")
            return

        await self.exec.execute_and_sync(
            self.rest.open_market, self.asset['positions'][symbol][side.upper()], symbol,
            symbol, side, price, self.entry_percent, self.asset['wallet']
        )
        self.asset = self.rest.getNsav_asset(asset=self.asset, symbol=symbol, save_redis=True)

    def _apply_config(self, cfg: TradeConfig) -> None:
        """메모리/엔진 파라미터에 설정 반영(필요한 것만)"""
        self.ws_stale_sec = cfg.ws_stale_sec
        self.ws_global_stale_sec = cfg.ws_global_stale_sec
        self.leverage = cfg.leverage
        self.entry_percent = cfg.entry_percent
        self.max_effective_leverage = cfg.max_effective_leverage

        # 인디케이터 파라미터는 객체 생성 시 주입했지만, 런타임 반영을 원하면 여기서도 반영 가능
        if hasattr(self, "indicator"):
            self.indicator.min_thr = cfg.indicator_min_thr
            self.indicator.max_thr = cfg.indicator_max_thr
            self.indicator.target_cross = cfg.target_cross