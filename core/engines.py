# engines.py
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from collections import deque
from typing import Iterable, Optional, Tuple, Deque, Dict, Any, List, Sequence,Mapping
import math, time
from datetime import datetime, timedelta, timezone
KST = timezone(timedelta(hours=9))

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
    # --- 내부: state → deque 반영 ---
    def _close_minute_candle(self, symbol: str, st: CandleState):
        dq = self.candles[symbol]
        item = {"open": st.o, "high": st.h, "low": st.l, "close": st.c, "minute": st.minute}
        if dq and isinstance(dq[-1], dict) and dq[-1].get("minute") == st.minute:
            dq[-1] = item
        else:
            dq.append(item)

Candle = Mapping[str, float]

class IndicatorEngine:
    def __init__(
        self,
        min_thr: float = 0.005,
        max_thr: float = 0.05,
        target_cross: int = 5,
    ):
        self.min_thr = min_thr
        self.max_thr = max_thr
        self.target_cross = target_cross

    @staticmethod
    def ma100_list(prices: Sequence[float]) -> List[Optional[float]]:
        ma100s: List[Optional[float]] = []
        for i in range(len(prices)):
            if i < 99:
                ma100s.append(None)  # MA100 계산 안 되는 구간
            else:
                ma100s.append(sum(prices[i - 99:i + 1]) / 100)
        return ma100s

    def compute_all(
        self,
        candles: Iterable[Candle],
    ) -> Tuple[Optional[float], list, Optional[float], Optional[float], List[Optional[float]]]:
        candles_list = list(candles)  # 한 번만 리스트화해서 여러 번 재사용
        hlc3 = [(c["high"] + c["low"] + c["close"]) / 3 for c in candles_list]  # high/low 평균

        ma100s = self.ma100_list(hlc3)
        if not ma100s:
            return None, [], None, None, []

        cross_times, raw_thr = self._find_optimal_threshold(candles_list, ma100s)

        if raw_thr is None:
            q_thr = None
        else:
            p = (Decimal(str(raw_thr)) * Decimal("100")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            q_thr = float(p) / 100.0
        return cross_times, q_thr, ma100s

    def _count_cross(
            self,
            candles: Sequence[Candle],
            ma100s: Sequence[Optional[float]],
            threshold: float,
            now_kst: Optional[datetime] = None,
            min_cross_interval_sec: int = 3600,
            ) -> Tuple[int, list]:
            if now_kst is None:
                now_kst = datetime.now(KST)

            count = 0
            cross_times = []  # 📌 크로스 발생 시간 저장
            last_state = None  # "above", "below", "in"

            last_cross_time_up = None
            last_cross_time_down = None

            total_len = len(candles)

            for i, (candle, ma) in enumerate(zip(candles, ma100s)):
                if ma is None:  # MA100 계산 안된 구간은 건너뜀
                    continue
                high = candle["high"]
                low = candle["low"]
                close = candle["close"]

                upper = ma * (1 + threshold)
                lower = ma * (1 - threshold)

                # ---- cross 발생 여부 (range 기준) ----
                up_cross = last_state in ("below", "in") and high > upper
                down_cross = last_state in ("above", "in") and low < lower

                cross_time_base = now_kst - timedelta(minutes=total_len - i)

                if up_cross:
                    cross_time = cross_time_base
                    if not last_cross_time_up or (cross_time - last_cross_time_up).total_seconds() > min_cross_interval_sec:
                        count += 1
                        cross_times.append(
                            (
                                "UP",
                                cross_time.strftime("%Y-%m-%d %H:%M:%S"),
                                upper,
                                close,  # 로그에는 close를 남기는 게 보통 보기 좋음
                                ma,
                            )
                        )
                        last_cross_time_up = cross_time

                if down_cross:
                    cross_time = cross_time_base
                    if not last_cross_time_down or (cross_time - last_cross_time_down).total_seconds() > min_cross_interval_sec:
                        count += 1
                        cross_times.append(
                            (
                                "DOWN",
                                cross_time.strftime("%Y-%m-%d %H:%M:%S"),
                                lower,
                                close,
                                ma,
                            )
                        )
                        last_cross_time_down = cross_time

                # ---- 다음 스텝에서의 상태는 close 기준으로 ----
                if close > upper:
                    state = "above"
                elif close < lower:
                    state = "below"
                else:
                    state = "in"

                    last_state = state

            return count, cross_times

    def _find_optimal_threshold(
            self,
            candles: Sequence[Candle],
            ma100s: Sequence[Optional[float]],
            min_thr: Optional[float] = None,
            max_thr: Optional[float] = None,
            target_cross: Optional[int] = None,
            min_cross_interval_sec: int = 3600,
    ) -> Tuple[list, float]:
        if min_thr is None:
            min_thr = self.min_thr
        if max_thr is None:
            max_thr = self.max_thr
        if target_cross is None:
            target_cross = self.target_cross

        left, right = min_thr, max_thr
        optimal = max_thr

        for _ in range(20):
            mid = (left + right) / 2
            crosses, _ = self._count_cross(
                candles, ma100s, mid, min_cross_interval_sec=min_cross_interval_sec
            )

            if crosses > target_cross:
                left = mid
            else:
                optimal = mid
                right = mid

        crosses, cross_times = self._count_cross(
            candles, ma100s, optimal, min_cross_interval_sec=min_cross_interval_sec
        )
        return cross_times, max(optimal, min_thr)

class JumpDetector:
    """최근 n개 가격 히스토리로 급등락 감지"""
    def __init__(self, history_num=10, polling_interval=0.5):
        self.history_num = history_num
        self.polling_interval = polling_interval
        self.price_history: Dict[str, Deque[Tuple[float, float]]] = {}  # symbol -> deque[(ts, price)]

    def ensure_symbol(self, symbol: str):
        if symbol not in self.price_history:
            self.price_history[symbol] = deque(maxlen=self.history_num)

    def record_price(
        self,
        symbol: str,
        price: float,
        exchange_ts: float | None = None,
        recv_ts: float | None = None,
    ):
        """거래소 timestamp + 로컬 timestamp + 가격을 함께 기록"""
        self.ensure_symbol(symbol)
        if not isinstance(price, (int, float)) or not (price > 0) or math.isnan(price) or math.isinf(price):
            return

        recv_ts = recv_ts or time.time()
        exchange_ts = exchange_ts or recv_ts  # fallback

        ph = self.price_history[symbol]
        if ph and recv_ts <= ph[-1][1]:
            recv_ts = ph[-1][1] + 1e-6  # 단조 증가 유지

        ph.append((float(exchange_ts), float(recv_ts), float(price)))

    def check_jump(
            self,
            symbol: str,
            jump_pct: Optional[float],
            use_exchange_ts: bool = True,
            max_age_sec: float = 7.0,
            skew_allow_sec: float = 2.0,
    ) -> Tuple[Optional[str], Optional[float], Optional[float]]:
        """급등락 감지. exchange_ts 기준(default) or recv_ts 기준."""
        self.ensure_symbol(symbol)
        ph = self.price_history[symbol]
        if len(ph) < self.history_num or jump_pct is None:
            return None, None, None

        idx_ts = 0 if use_exchange_ts else 1
        now_ts_sel, now_price = ph[-1][idx_ts], ph[-1][2]

        now_wall = time.time()
        age = now_wall - now_ts_sel
        if age < 0 and abs(age) <= skew_allow_sec:
            age = 0.0
        if age > max_age_sec:
            return None, None, None

        min_sec = self.polling_interval
        max_sec = self.polling_interval * self.history_num

        in_window, dts = False, []
        for entry in list(ph)[:-1]:
            ts = entry[idx_ts]
            past_price = entry[2]
            dt = now_ts_sel - ts
            if dt < 0:
                # 비정상(시간 역행) 샘플은 스킵
                continue
            if min_sec <= dt <= max_sec:
                in_window = True
                dts.append(dt)
                if past_price != 0:
                    change_rate = (now_price - past_price) / past_price
                    if abs(change_rate) >= jump_pct:
                        return ("UP" if change_rate > 0 else "DOWN", min(dts), max(dts))

        if in_window:
            return True, (min(dts) if dts else None), (max(dts) if dts else None)
        return None, None, None