# strategies/basic_strategy.py
from datetime import datetime, timedelta
import time
from typing import Optional

def get_long_entry_reasons(price, ma100, prev, recent_entry_time):
    reasons = []

    # 1. 기술적 조건
    if price < ma100 * 1.0002:
        reasons.append("MA100 대비 -0.2% 이상 하락")
    if (prev - price) / prev > 0.001:
        reasons.append("3분 전 대비 0.1% 이상 급락")

    # 2. 시간 조건: 최근 진입 1시간 이내면 진입 제한
    if recent_entry_time:
        now_ts = int(time.time() * 1000)
        seconds_since_entry = (now_ts - recent_entry_time) / 1000
        if seconds_since_entry < 3600:
            reasons.append(f"최근 롱 진입 {int(seconds_since_entry)}초 전 → 추매 제한")
            return []  # ⛔ 시간 조건 미충족 → 진입 제한

    # 3. 기술적 조건이 2개 모두 충족된 경우만 진입
    if len(reasons) == 2:
        return reasons
    return []

def get_short_entry_reasons(price, ma100, prev, recent_entry_time):
    reasons = []

    ## 1. 기술적 조건
    if price > ma100 * 0.9998:
        reasons.append("MA100 대비 +0.2% 이상 돌파")
    if (price - prev) / prev > 0.001:
        reasons.append("3분 전 대비 0.1% 이상 급등")

    # 2. 시간 조건: 최근 진입 1시간 이내면 진입 제한
    if recent_entry_time:
        now_ts = int(time.time() * 1000)
        seconds_since_entry = (now_ts - recent_entry_time) / 1000
        if seconds_since_entry < 3600:
            reasons.append(f"최근 숏 진입 {int(seconds_since_entry)}초 전 → 추매 제한")
            return []  # ⛔ 추매 제한 → 바로 중단

    # 3. 유효한 경우만 반환 (2개 있을 경우만 진입 허용)
    if len(reasons) == 2:
        return reasons
    return []

def get_exit_reasons(position: str, price: float, ma100: float, recent_entry_time: Optional[int] = None) -> list[str]:
    # 1. 기술적 조건
    if position == "LONG" and price > ma100 * 0.9998:
        return ["🔻 MA100 근처 도달 (롱 청산 조건)"]
    if position == "SHORT" and price < ma100 * 1.0002:
        return ["🔺 MA100 근처 도달 (숏 청산 조건)"]

    # 2. 시간 조건: 진입 후 2시간 이상 경과
    if recent_entry_time:
        now_ts = int(time.time() * 1000)
        time_held_sec = (now_ts - recent_entry_time) / 1000
        if time_held_sec >= 7200:
            return [f"⏰ 진입 후 {int(time_held_sec)}초 경과 (2시간 초과)"]

    return []

