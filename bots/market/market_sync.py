# bots/market/market_sync.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Callable, Any, Dict, List

from .ws_freshness import ws_is_fresh
from .bootstrap import bootstrap_candles_for_symbol, bootstrap_all_symbols  # ✅ 추가


OnPriceFn = Callable[[str, float, Optional[float]], None]
RefreshFn = Callable[[str], None]
GetThrFn = Callable[[str], Optional[float]]

@dataclass
class MarketSyncConfig:
    ws_stale_sec: float
    ws_global_stale_sec: float
    candles_num: int


class MarketSync:
    """
    - WS 가격 수집
    - WS freshness 판단
    - 진행중 봉 누적 or REST 백필
    - 확정봉 반영
    - 필요 시 지표 refresh 호출

    TradeBot에서 _price_record/_candle_backfill/_candle_record를 대체.
    """

    def __init__(
            self,
            ws: Any,
            rest: Any,
            candle_engine: Any,
            *,
            refresh_indicators: RefreshFn,
            cfg: MarketSyncConfig,
            system_logger=None,
            on_price: Optional[OnPriceFn] = None,

            jump_service: Optional[Any] = None,  # ✅ 추가
            get_ma_threshold: Optional[GetThrFn] = None,  # ✅ 추가
    ):
        self.ws = ws
        self.rest = rest
        self.candle = candle_engine
        self.refresh_indicators = refresh_indicators
        self.system_logger = system_logger
        self.on_price = on_price
        self.jump_service = jump_service
        self.get_ma_threshold = get_ma_threshold
        self.cfg = cfg
        self._subscribed = set()
        self._last_backfill_at = {}   # ✅ symbol -> time.time() (epoch sec)



        # 내부 상태(TradeBot에서 빼기 대상)
        self._rest_fallback_on = {}
        self._stale_counts = {}
        self._last_closed_minute = {}

    def bootstrap(
            self,
            *,
            symbols: List[str],
            signal_only: bool,
            leverage: float,
            asset: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        TradeBot의 부트스트랩(초기 캔들/지표 + 자산/포지션 로드)을 여기로 이관.
        - signal_only=True : 캔들/지표만
        - signal_only=False: 자산/포지션 + 캔들/지표
        """
        # ✅ 0) WS 구독은 MarketSync 책임 (중복 구독 방지)
        need = [s for s in symbols if s not in self._subscribed]
        if need:
            subscribe = getattr(self.ws, "subscribe_symbols", None)
            if callable(subscribe):
                try:
                    subscribe(*need)
                    self._subscribed.update(need)
                    if self.system_logger:
                        self.system_logger.debug(f"[MarketSync] subscribed: {need}")
                except Exception as e:
                    if self.system_logger:
                        self.system_logger.error(f"[MarketSync] subscribe failed: {e}")

        if signal_only:
            for sym in symbols:
                bootstrap_candles_for_symbol(
                    rest_client=self.rest,
                    candle_engine=self.candle,
                    refresh_indicators=self.refresh_indicators,
                    symbol=sym,
                    candles_num=self.cfg.candles_num,
                    system_logger=self.system_logger,
                )
            if self.system_logger:
                self.system_logger.debug("[MarketSync] signal_only 부트스트랩 완료(캔들/인디케이터)")
            return asset

        # 주문 모드: 기존 bootstrap_all_symbols 결과를 asset에 반영
        new_asset = bootstrap_all_symbols(
            rest_client=self.rest,
            candle_engine=self.candle,
            refresh_indicators=self.refresh_indicators,
            symbols=symbols,
            leverage=leverage,
            asset=asset,
            candles_num=self.cfg.candles_num,
            system_logger=self.system_logger,
        )
        if self.system_logger:
            self.system_logger.debug("[MarketSync] 주문 모드 부트스트랩 완료(자산/포지션+캔들/인디케이터)")
        return new_asset

    def ensure_symbol(self, symbol: str) -> None:
        self._rest_fallback_on.setdefault(symbol, False)
        self._stale_counts.setdefault(symbol, 0)
        self._last_closed_minute.setdefault(symbol, None)
        self._last_backfill_at.setdefault(symbol, 0.0)   # ✅ 추가

    def apply_config(self, cfg: MarketSyncConfig) -> None:
        self.cfg = cfg

    def _can_backfill_now(self, symbol: str, now_ts: float, cooldown_sec: float = 30.0) -> bool:
        last = float(self._last_backfill_at.get(symbol, 0.0) or 0.0)
        if (now_ts - last) < float(cooldown_sec):
            return False
        self._last_backfill_at[symbol] = float(now_ts)
        return True

    def get_price(self, symbol: str, now_ts: float) -> Optional[float]:
        get_p = getattr(self.ws, "get_price", None)
        if not callable(get_p):
            return None
        price = get_p(symbol)

        get_ts = getattr(self.ws, "get_last_exchange_ts", None)
        exchange_ts = get_ts(symbol) if callable(get_ts) else now_ts
        if exchange_ts is None:
            exchange_ts = now_ts

        if price is not None and self.on_price:
            try:
                self.on_price(symbol, float(price), float(exchange_ts))
            except Exception:
                pass

        return float(price) if price is not None else None

    def _backfill_or_accumulate(self, symbol: str, price: Optional[float], now_ts: float) -> None:
        """
        - WS fresh면 ticker로 진행중 봉 누적
        - stale면 REST 백필 + 지표갱신
        """
        use_ws = ws_is_fresh(self.ws, symbol, self.cfg.ws_stale_sec, self.cfg.ws_global_stale_sec)
        if use_ws:
            get_ts = getattr(self.ws, "get_last_exchange_ts", None)
            ts = (get_ts(symbol) if callable(get_ts) else now_ts) or now_ts
            if price is not None:
                self.candle.accumulate_with_ticker(symbol, float(price), float(ts))

            if self._rest_fallback_on[symbol]:
                self._rest_fallback_on[symbol] = False
                if self.system_logger:
                    self.system_logger.info(f"[{symbol}] ✅ WS 복구, 실시간 집계 재개")

            self._stale_counts[symbol] = 0
            return

        # stale
        self._stale_counts[symbol] += 1
        if self._stale_counts[symbol] < 2:
            return

        if not self._rest_fallback_on[symbol]:
            self._rest_fallback_on[symbol] = True
            if self.system_logger:
                self.system_logger.error(f"[{symbol}] ⚠️ WS stale → REST 백필")


        # ✅ 쿨다운: 너무 자주 REST 때리지 않기
        if not self._can_backfill_now(symbol, now_ts, cooldown_sec=30.0):
            return

        self.rest.update_candles(
            self.candle.get_candles(symbol),
            symbol=symbol,
            count=self.cfg.candles_num
        )
        self.refresh_indicators(symbol)

    def _apply_confirmed_kline_if_any(self, symbol: str) -> None:
        """
        확정봉이 새로 닫혔으면 candle 반영 + 지표 갱신
        """
        get_ck = getattr(self.ws, "get_last_confirmed_kline", None)
        if not callable(get_ck):
            return

        k = get_ck(symbol, "1")
        if not (k and k.get("confirm")):
            return

        k_start_minute = int(k["start"] // 60000)
        if k_start_minute is None or k_start_minute == self._last_closed_minute[symbol]:
            return

        self.candle.apply_confirmed_kline(symbol, k)
        self.refresh_indicators(symbol)
        self._last_closed_minute[symbol] = k_start_minute

    def _backfill_if_candle_gap(self, symbol: str, now_ts: float) -> None:
        """
        WS 연결은 살아있어도(HB) 캔들/확정봉이 뒤쳐지면 REST로 갭 메움.
        - now_ts: epoch seconds
        """
        # 현재 minute (1분봉 기준)
        now_min = int((now_ts * 1000) // 60000)

        last_closed = self._last_closed_minute.get(symbol)

        # 아직 확정봉을 한 번도 못 받았거나, 2분 이상 뒤쳐지면 백필
        if last_closed is None or (now_min - int(last_closed)) >= 2:
            if self.system_logger:
                self.system_logger.warning(f"[{symbol}] ⛏️ candle gap → REST backfill (last_closed={last_closed}, now_min={now_min})")

            # ✅ 추가
            if not self._can_backfill_now(symbol, now_ts, cooldown_sec=30.0):
                return

            try:
                self.rest.update_candles(
                    self.candle.get_candles(symbol),
                    symbol=symbol,
                    count=self.cfg.candles_num,
                )
                self.refresh_indicators(symbol)
                self._last_closed_minute[symbol] = now_min - 1   # ✅ 추가(권장)
            except Exception as e:
                if self.system_logger:
                    self.system_logger.error(f"[{symbol}] REST backfill failed: {e}")


    def tick(self, symbol: str, now_ts: float) -> Optional[float]:
        """
        TradeBot 1틱에서 심볼 단위로 호출.
        순서:
        1) 가격 읽기(+ on_price 훅)
        2) accumulate/backfill
        3) confirmed kline 반영 + refresh(cross pct 계산포함)
        """
        self.ensure_symbol(symbol)   # ✅ 여기 추가
        price = self.get_price(symbol, now_ts)
        self._backfill_or_accumulate(symbol, price, now_ts)
        self._apply_confirmed_kline_if_any(symbol)

        # ✅ 추가: HB 때문에 stale 안 떠도 캔들 갭이면 REST로 메움
        self._backfill_if_candle_gap(symbol, now_ts)

        # ✅ tick 끝에서 jump 상태 갱신 (TradeBot에서 제거할 부분)
        if self.jump_service and self.get_ma_threshold:
            try:
                self.jump_service.update(symbol, self.get_ma_threshold(symbol))
            except Exception:
                pass

        return price
