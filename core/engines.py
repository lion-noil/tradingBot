# engines.py
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from collections import deque
from typing import Iterable, Optional, Tuple, Deque, Dict, Any
import math, time

@dataclass
class CandleState:
    minute: int  # epoch // 60
    o: float
    h: float
    l: float
    c: float

class CandleEngine:
    """ticker 기반 실시간 1분봉 누적 + kline(confirmed) 반영"""
    def __init__(self, candles_num: int = 10080):
        self.candles: Dict[str, Deque[Dict[str, float]]] = {}   # symbol -> deque[{'open','high','low','close','minute'}]
        self.candles_num = candles_num
        self._state: Dict[str, Optional[CandleState]] = {}      # 진행중 1분봉 상태

    # --- 초기화/접근 ---
    def ensure_symbol(self, symbol: str):
        if symbol not in self.candles:
            self.candles[symbol] = deque(maxlen=self.candles_num)
            self._state[symbol] = None


    def get_candles(self, symbol: str) -> Deque[Dict[str, float]]:
        self.ensure_symbol(symbol)
        return self.candles[symbol]

    def get_state(self, symbol: str) -> Optional[CandleState]:
        return self._state.get(symbol)

    # --- WS ticker로 진행 중 1분봉 누적 ---
    def accumulate_with_ticker(self, symbol: str, price: float, ts_sec: float):
        self.ensure_symbol(symbol)
        minute = int(ts_sec) // 60
        st = self._state[symbol]
        if st is None or st.minute != minute:
            if st is not None:
                self._close_minute_candle(symbol, st)
            self._state[symbol] = CandleState(minute, price, price, price, price)
        else:
            st.h = max(st.h, price)
            st.l = min(st.l, price)
            st.c = price

    def apply_confirmed_kline(self, symbol: str, k: Dict[str, Any]):
        self.ensure_symbol(symbol)
        minute = int(int(k["start"]) // 1000) // 60
        item = {
            "open": float(k["open"]),
            "high": float(k["high"]),
            "low": float(k["low"]),
            "close": float(k["close"]),
            "minute": minute,
        }
        dq = self.candles[symbol]
        if dq and isinstance(dq[-1], dict) and dq[-1].get("minute") == minute:
            dq[-1] = item
        else:
            dq.append(item)
        st = self._state.get(symbol)
        if st and st.minute == minute:
            self._state[symbol] = None

    def _get_closes(self, symbol: str, limit: int | None = None) -> list[float]:
        src = self.candles[symbol]
        if limit is not None:
            # 뒤에서 limit개만 추출 (deque → list 한 번만 캐스팅)
            return [c["close"] for c in list(src)[-limit:]]
        return [c["close"] for c in src]

    # --- 내부: state → deque 반영 ---
    def _close_minute_candle(self, symbol: str, st: CandleState):
        dq = self.candles[symbol]
        item = {"open": st.o, "high": st.h, "low": st.l, "close": st.c, "minute": st.minute}
        if dq and isinstance(dq[-1], dict) and dq[-1].get("minute") == st.minute:
            dq[-1] = item
        else:
            dq.append(item)

class IndicatorEngine:
    """MA/threshold 계산만 담당 (외부에서 ma100_list, find_optimal_threshold 주입)"""
    def __init__(self, min_thr=0.005, max_thr=0.03, target_cross=5):
        self.min_thr = min_thr
        self.max_thr = max_thr
        self.target_cross = target_cross

    def compute_all(self, closes: Iterable[float], ma100_list_fn, find_thr_fn) -> Tuple[Optional[float], Optional[float], Optional[float], list[float]]:
        ma100s = ma100_list_fn(closes)
        if not ma100s:
            return None, None, None, []
        raw_thr = find_thr_fn(closes, ma100s, min_thr=self.min_thr, max_thr=self.max_thr, target_cross=self.target_cross)
        thr = self._quantize(raw_thr)
        now_ma100 = ma100s[-1]
        mom_thr = (thr / 3) if thr is not None else None
        return now_ma100, thr, mom_thr, ma100s

    def _quantize(self, thr: float | None) -> float | None:
        if thr is None:
            return None
        p = (Decimal(str(thr)) * Decimal('100')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        return float(p) / 100.0

class JumpDetector:
    """최근 n개 가격 히스토리로 급등락 감지"""
    def __init__(self, history_num=10, polling_interval=0.5):
        self.history_num = history_num
        self.polling_interval = polling_interval
        self.price_history: Dict[str, Deque[Tuple[float, float]]] = {}  # symbol -> deque[(ts, price)]

    def ensure_symbol(self, symbol: str):
        if symbol not in self.price_history:
            self.price_history[symbol] = deque(maxlen=self.history_num)

    def record_price(self, symbol: str, price: float, ts: float | None = None):
        self.ensure_symbol(symbol)
        if not isinstance(price, (int, float)) or not (price > 0) or math.isnan(price) or math.isinf(price):
            return
        ts = ts or time.time()
        ph = self.price_history[symbol]
        if ph and ts <= ph[-1][0]:
            ts = ph[-1][0] + 1e-6  # 단조 증가
        ph.append((ts, float(price)))

    def check_jump(self, symbol: str, jump_pct: Optional[float]) -> Tuple[Optional[str], Optional[float], Optional[float]]:
        self.ensure_symbol(symbol)
        ph = self.price_history[symbol]
        if len(ph) < self.history_num or jump_pct is None:
            return None, None, None
        min_sec = self.polling_interval
        max_sec = self.polling_interval * self.history_num
        now_ts, now_price = ph[-1]
        in_window, dts = False, []
        for ts, past_price in list(ph)[:-1]:
            dt = now_ts - ts
            if min_sec <= dt <= max_sec:
                in_window = True
                dts.append(dt)
                if past_price != 0:
                    change_rate = (now_price - past_price) / past_price
                    if abs(change_rate) >= jump_pct:
                        return ("UP" if change_rate > 0 else "DOWN", min(dts), max(dts))
        if in_window:
            return True, min(dts), max(dts)
        return None, None, None
