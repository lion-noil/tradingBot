# engines.py
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from collections import deque
from typing import (
    Iterable,
    Optional,
    Tuple,
    Deque,
    Dict,
    Any,
    List,
    Sequence,
    Mapping,
)
import math
import time
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
        # symbol -> deque[{'open','high','low','close','minute'}]
        self.candles: Dict[str, Deque[Dict[str, Any]]] = {}
        self.candles_num = candles_num
        # 진행중 1분봉 상태
        self._state: Dict[str, Optional[CandleState]] = {}

    # --- 초기화/접근 ---
    def ensure_symbol(self, symbol: str):
        if symbol not in self.candles:
            self.candles[symbol] = deque(maxlen=self.candles_num)
            self._state[symbol] = None

    def get_candles(self, symbol: str) -> Deque[Dict[str, Any]]:
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
            # 이전 minute 마감
            if st is not None:
                self._close_minute_candle(symbol, st)
            # 새 minute 시작
            self._state[symbol] = CandleState(minute, price, price, price, price)
        else:
            st.h = max(st.h, price)
            st.l = min(st.l, price)
            st.c = price

    def apply_confirmed_kline(self, symbol: str, k: Dict[str, Any]):
        self.ensure_symbol(symbol)
        minute = int(int(k["start"]) // 1000) // 60

        dq = self.candles[symbol]

        # 직전 close 확보
        prev_close = dq[-1].get("close") if dq else None
        prev_minute = dq[-1].get("minute") if dq else None

        # ✅ (1) minute 자체가 점프한 경우: 중간 분을 prev_close로 채움
        if prev_close is not None and prev_minute is not None and minute > prev_minute + 1:
            for m in range(prev_minute + 1, minute):
                dq.append({
                    "open": float(prev_close),
                    "high": float(prev_close),
                    "low": float(prev_close),
                    "close": float(prev_close),
                    "minute": int(m),
                })

        o = k.get("open");
        h = k.get("high");
        l = k.get("low");
        c = k.get("close")

        # ✅ (2) 값이 None인 쉬는시간 캔들: prev_close로 평평하게 채움
        if (h is None or l is None or c is None) and prev_close is not None:
            o = prev_close
            h = prev_close
            l = prev_close
            c = prev_close

        item = {
            "open": float(o) if o is not None else None,
            "high": float(h) if h is not None else None,
            "low": float(l) if l is not None else None,
            "close": float(c) if c is not None else None,
            "minute": minute,
        }

        if dq and dq[-1].get("minute") == minute:
            dq[-1] = item
        else:
            dq.append(item)

        st = self._state.get(symbol)
        if st and st.minute == minute:
            self._state[symbol] = None

    # --- 내부: state → deque 반영 ---
    def _close_minute_candle(self, symbol: str, st: CandleState):
        dq = self.candles[symbol]
        item = {
            "open": st.o,
            "high": st.h,
            "low": st.l,
            "close": st.c,
            "minute": st.minute,
        }
        if dq and isinstance(dq[-1], dict) and dq[-1].get("minute") == st.minute:
            dq[-1] = item
        else:
            dq.append(item)


Candle = Mapping[str, Any]


class IndicatorEngine:
    def __init__(
        self,
        min_thr: float = 0.005,
        max_thr: float = 0.05,
        target_cross: int = 5,
    ):
        self.min_thr = float(min_thr)
        self.max_thr = float(max_thr)
        self.target_cross = int(target_cross)

    # ─────────────────────────────────────────────
    # MA100 (None-safe 버전)
    # prices: List[Optional[float]]
    # - 윈도우(100개) 안에 하나라도 None 있으면 결과도 None
    # ─────────────────────────────────────────────
    @staticmethod
    def ma100_list(prices: Sequence[Optional[float]]) -> List[Optional[float]]:
        ma100s: List[Optional[float]] = []
        n = len(prices)
        for i in range(n):
            if i < 99:
                # 샘플이 100개 미만이면 MA100 없음
                ma100s.append(None)
                continue

            window = prices[i - 99 : i + 1]
            # 쉬는 시간(빈 캔들) 포함 → None 있으면 이 시점 MA도 None
            if any(v is None for v in window):
                ma100s.append(None)
            else:
                s = sum(float(v) for v in window)  # type: ignore[arg-type]
                ma100s.append(s / 100.0)
        return ma100s

    # ─────────────────────────────────────────────
    # 전체 지표 계산 (cross_times, q_thr, ma100s)
    # ─────────────────────────────────────────────
    def compute_all(
        self,
        candles: Iterable[Candle],
    ) -> Tuple[List[Tuple[str, str, float, float, float]], Optional[float], List[Optional[float]]]:
        """
        candles: 각 원소는 {"high","low","close","minute",...}
        쉬는 시간 캔들은 high/low/close 가 None일 수 있음.
        """
        candles_list = list(candles)

        # hlc3: 거래 있는 구간만 값, 쉬는 시간은 None
        hlc3: List[Optional[float]] = []
        last = None
        for c in candles_list:
            h = c.get("high")
            l = c.get("low")
            cl = c.get("close")
            if h is None or l is None or cl is None:
                # ✅ None이면 직전 값으로 채움(있을 때만)
                hlc3.append(last)
            else:
                v = (float(h) + float(l) + float(cl)) / 3.0
                hlc3.append(v)
                last = v

        ma100s = self.ma100_list(hlc3)
        if not ma100s:
            return [], None, []

        cross_times, raw_thr = self._find_optimal_threshold(candles_list, ma100s)

        # threshold 양자화(둘째 자리까지)
        if raw_thr is None:
            q_thr = None
        else:
            p = (Decimal(str(raw_thr)) * Decimal("100")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            q_thr = float(p) / 100.0

        return cross_times, q_thr, ma100s

    # ─────────────────────────────────────────────
    # threshold에 따른 cross 횟수 세기
    # ─────────────────────────────────────────────
    def _count_cross(
        self,
        candles: Sequence[Candle],
        ma100s: Sequence[Optional[float]],
        threshold: float,
        now_kst: Optional[datetime] = None,
        min_cross_interval_sec: int = 3600,
    ) -> Tuple[int, List[Tuple[str, str, float, float, float]]]:
        if now_kst is None:
            now_kst = datetime.now(KST)

        count = 0
        cross_times: List[Tuple[str, str, float, float, float]] = []
        last_state: Optional[str] = None  # "above", "below", "in"

        last_cross_time_up: Optional[datetime] = None
        last_cross_time_down: Optional[datetime] = None

        total_len = len(candles)

        for i, (candle, ma) in enumerate(zip(candles, ma100s)):
            # MA가 없으면(샘플 부족, 쉬는 시간 포함) 스킵
            if ma is None:
                continue

            high = candle.get("high")
            low = candle.get("low")
            close = candle.get("close")

            # 가격이 None이면(쉬는 시간) 이 구간도 스킵
            if high is None or low is None or close is None:
                continue

            high = float(high)
            low = float(low)
            close = float(close)

            upper = ma * (1 + threshold)
            lower = ma * (1 - threshold)

            # ---- cross 발생 여부 (range 기준) ----
            up_cross = last_state in ("below", "in") and high > upper
            down_cross = last_state in ("above", "in") and low < lower

            # 현재 캔들 시점(KST 기준) 추정
            cross_time_base = now_kst - timedelta(minutes=total_len - i)

            if up_cross:
                cross_time = cross_time_base
                if (
                    not last_cross_time_up
                    or (cross_time - last_cross_time_up).total_seconds()
                    > min_cross_interval_sec
                ):
                    count += 1
                    cross_times.append(
                        (
                            "UP",
                            cross_time.strftime("%Y-%m-%d %H:%M:%S"),
                            upper,
                            close,  # 로그에는 close 남김
                            ma,
                        )
                    )
                    last_cross_time_up = cross_time

            if down_cross:
                cross_time = cross_time_base
                if (
                    not last_cross_time_down
                    or (cross_time - last_cross_time_down).total_seconds()
                    > min_cross_interval_sec
                ):
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

            # ✅ 들여쓰기 버그 수정: 항상 last_state 갱신
            last_state = state

        return count, cross_times

    # ─────────────────────────────────────────────
    # 최적 threshold 탐색 (이분 탐색)
    # ─────────────────────────────────────────────
    def _find_optimal_threshold(
        self,
        candles: Sequence[Candle],
        ma100s: Sequence[Optional[float]],
        min_thr: Optional[float] = None,
        max_thr: Optional[float] = None,
        target_cross: Optional[int] = None,
        min_cross_interval_sec: int = 3600,
    ) -> Tuple[List[Tuple[str, str, float, float, float]], Optional[float]]:
        if min_thr is None:
            min_thr = self.min_thr
        if max_thr is None:
            max_thr = self.max_thr
        if target_cross is None:
            target_cross = self.target_cross

        left, right = float(min_thr), float(max_thr)
        optimal = right

        # 간단한 이분 탐색으로 target_cross 근처 threshold 찾기
        for _ in range(20):
            mid = (left + right) / 2.0
            crosses, _ = self._count_cross(
                candles, ma100s, mid, min_cross_interval_sec=min_cross_interval_sec
            )

            if crosses > target_cross:
                # 크로스가 너무 많다 → threshold를 더 키움
                left = mid
            else:
                # 크로스가 목표 이하 → 이 값을 후보로 저장, threshold를 낮춰도 되는지 왼쪽 탐색
                optimal = mid
                right = mid

        crosses, cross_times = self._count_cross(
            candles, ma100s, optimal, min_cross_interval_sec=min_cross_interval_sec
        )
        # 최소 min_thr 이하로는 떨어지지 않도록
        return cross_times, max(optimal, min_thr)


class JumpDetector:
    """최근 n개 가격 히스토리로 급등락 감지"""

    def __init__(self, history_num=10, polling_interval=0.5):
        self.history_num = history_num
        self.polling_interval = polling_interval
        # symbol -> deque[(exchange_ts, recv_ts, price)]
        self.price_history: Dict[str, Deque[Tuple[float, float, float]]] = {}

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
        # recv_ts 단조 증가 유지
        if ph and recv_ts <= ph[-1][1]:
            recv_ts = ph[-1][1] + 1e-6

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

        in_window = False
        dts: List[float] = []

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
                        return (
                            "UP" if change_rate > 0 else "DOWN",
                            min(dts),
                            max(dts),
                        )

        if in_window:
            # 급등락은 아닐 때, 단순히 "활성" 상태를 알려주고 싶다면 True 반환
            return True, (min(dts) if dts else None), (max(dts) if dts else None)
        return None, None, None
