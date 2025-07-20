# strategies/basic_strategy.py
from datetime import datetime, timedelta
import time
from typing import Optional

def get_long_entry_reasons(price, ma100, prev, recent_entry_time=None):
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

    # 3. 판단: 기술적 조건 2개 충족 + 1시간 경과
    if len(reasons) == 2:
        return reasons
    else:
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

        # 3. 유효한 경우만 반환 (2개만 있을 경우만 진입 허용)
    return reasons if len(reasons) == 2 else []

def get_exit_reasons(position: str, price: float, ma100: float, recent_entry_time: Optional[int] = None) -> list[str]:
    reasons = []

    # 기술적 조건
    if position == "LONG":
        if price > ma100 * 0.9998:
            reasons.append("🔻 MA100 근처 도달 (롱 청산 조건)")
    elif position == "SHORT":
        if price < ma100 * 1.0002:
            reasons.append("🔺 MA100 근처 도달 (숏 청산 조건)")

    # 시간 기반 조건 (2시간 이상 유지)
    if recent_entry_time:
        now_ts = int(time.time() * 1000)
        time_held_sec = (now_ts - recent_entry_time) / 1000
        if time_held_sec >= 7200:
            reasons.append(f"⏰ 진입 후 {int(time_held_sec)}초 경과 (2시간 초과)")

    return reasons

