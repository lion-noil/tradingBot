# bots/market/market_sync.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Callable, Any, Dict, List

from .ws_freshness import ws_is_fresh
from .bootstrap import bootstrap_candles_for_symbol
import uuid
import time  # 파일 상단에 추가

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
        self._last_backfill_at = {}  # ✅ symbol -> time.time() (epoch sec)

        # ✅ 전역 백필 폭주 방지 (최소 변경)
        self._global_last_backfill_at = 0.0   # 전역 쿨다운(초)
        self._backfill_inflight = set()       # 심볼 중복 백필 방지


        # 내부 상태(TradeBot에서 빼기 대상)
        self._rest_fallback_on = {}
        self._stale_counts = {}
        self._last_closed_minute = {}

    def bootstrap(self, *, symbols: List[str]) -> None:
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

        # 1) 캔들 백필 + 2) 인디케이터 refresh (bootstrap_candles_for_symbol 안에서 수행)
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
            self.system_logger.debug("[MarketSync] bootstrap 완료(캔들/인디케이터)")

    def ensure_symbol(self, symbol: str) -> None:
        self._rest_fallback_on.setdefault(symbol, False)
        self._stale_counts.setdefault(symbol, 0)
        self._last_closed_minute.setdefault(symbol, None)
        self._last_backfill_at.setdefault(symbol, 0.0)  # ✅ 추가


    def _can_backfill_now(self, symbol: str, now_ts: float, cooldown_sec: float = 30.0) -> bool:
        last = float(self._last_backfill_at.get(symbol, 0.0) or 0.0)
        if (now_ts - last) < float(cooldown_sec):
            return False
        self._last_backfill_at[symbol] = float(now_ts)
        return True


    def _can_backfill_global_now(self, now_ts: float, cooldown_sec: float = 3.0) -> bool:
        """
        심볼이 여러 개일 때 stale가 동시에 터지면
        REST 백필이 연달아/다발로 나가면서 네트워크/DNS를 더 악화시킬 수 있음.
        -> 프로세스 내 전역 쿨다운으로 '버스트'를 줄인다.
        """
        last = float(self._global_last_backfill_at or 0.0)
        if (now_ts - last) < float(cooldown_sec):
            return False
        self._global_last_backfill_at = float(now_ts)
        return True

    def _enter_backfill(self, symbol: str) -> bool:
        """같은 심볼에 대한 중복 백필 방지"""
        if symbol in self._backfill_inflight:
            return False
        self._backfill_inflight.add(symbol)
        return True

    def _exit_backfill(self, symbol: str) -> None:
        self._backfill_inflight.discard(symbol)


    def _infer_last_closed_minute_from_engine(self, symbol: str) -> Optional[int]:
        try:
            candles = self.candle.get_candles(symbol)
        except Exception:
            return None
        if not candles:
            return None

        last = candles[-1] or {}
        m = last.get("minute")
        return int(m) if m is not None else None

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

        # ✅ 심볼별 쿨다운
        if not self._can_backfill_now(symbol, now_ts, cooldown_sec=30.0):
            return

        # ✅ 전역 쿨다운 (연쇄 백필 버스트 방지)
        if not self._can_backfill_global_now(now_ts, cooldown_sec=3.0):
            return

        # ✅ 같은 심볼 중복 백필 방지
        if not self._enter_backfill(symbol):
            return

        try:
            self.rest.update_candles(
                self.candle.get_candles(symbol),
                symbol=symbol,
                count=self.cfg.candles_num
            )
            try:
                self.refresh_indicators(symbol)
            except Exception as e:
                if self.system_logger:
                    self.system_logger.warning(
                        f"[{symbol}] refresh_indicators failed: {e}"
                    )
        except Exception as e:
            # 네트워크/DNS 흔들릴 때 예외가 바깥으로 퍼지는 걸 방지
            if self.system_logger:
                self.system_logger.warning(
                    f"❌ [REST backfill] ({symbol}) failed: {e}"
                )
        finally:
            self._exit_backfill(symbol)

    def _apply_confirmed_kline_if_any(self, symbol: str) -> bool:
        get_ck = getattr(self.ws, "get_last_confirmed_kline", None)
        if not callable(get_ck):
            return False

        k = get_ck(symbol, "1")
        if not (k and k.get("confirm")):
            return False

        k_start_minute = int(k["start"] // 60000)
        if k_start_minute == self._last_closed_minute[symbol]:
            return False

        self.candle.apply_confirmed_kline(symbol, k)
        self.refresh_indicators(symbol)
        self._last_closed_minute[symbol] = k_start_minute
        return True

    def _sec_into_minute(self, now_ts: float) -> float:
        # now_ts: epoch seconds
        return now_ts - (int(now_ts) // 60) * 60

    def _backfill_if_candle_gap(self, symbol: str, now_ts: float) -> None:
        now_min = int((now_ts * 1000) // 60000)

        GRACE_SEC = 8.0
        if self._sec_into_minute(now_ts) < GRACE_SEC:
            return

        expected_closed = now_min - 1  # ✅ "지금 시점에서 닫혀 있어야 정상인 마지막 분"
        engine_last = self._infer_last_closed_minute_from_engine(symbol)

        # 엔진 캔들이 없으면(초기) 과격 백필 금지
        if engine_last is None:
            return

        # ✅ 진짜 갭: 닫혀 있어야 하는 분(expected_closed) 기준으로 2개 이상 밀릴 때만
        if (expected_closed - int(engine_last)) < 2:
            return

    def tick(self, symbol: str, now_ts: float) -> Optional[float]:
        self.ensure_symbol(symbol)  # ✅ 여기 추가
        price = self.get_price(symbol, now_ts)
        self._backfill_or_accumulate(symbol, price, now_ts)

        did_close = self._apply_confirmed_kline_if_any(symbol)

        if not did_close:
            self._backfill_if_candle_gap(symbol, now_ts)

        # ✅ tick 끝에서 jump 상태 갱신 (TradeBot에서 제거할 부분)
        if self.jump_service and self.get_ma_threshold:
            try:
                self.jump_service.update(symbol, self.get_ma_threshold(symbol))
            except Exception:
                pass

        return price
