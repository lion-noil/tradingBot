# strategies/basic_strategy.py
from datetime import datetime, timedelta
import time

def get_long_entry_reasons(price, ma100, prev):
    reasons = []
    if price < ma100 * 0.998:
        reasons.append("MA100 대비 -0.2% 이상 하락")
    if (prev - price) / prev > 0.001:
        reasons.append("3분 전 대비 0.1% 이상 급락")
    return reasons if len(reasons) == 2 else []

def get_short_entry_reasons(price, ma100, prev, recent_entry_time=None):
    reasons = []

    # 1. 기술적 조건
    if price > ma100 * 0.9998:
        reasons.append("MA100 대비 +0.2% 이상 돌파")
    if (price - prev) / prev > 0.001:
        reasons.append("3분 전 대비 0.1% 이상 급등")

    # 2. 시간 조건 검사: 최근 진입 1시간 미만이면 제한
    if recent_entry_time:
        now_ts = int(time.time() * 1000)
        if (now_ts - recent_entry_time) < 3600 * 1000:
            reasons.append("추매 제한")  # 이유는 남기되, 판단은 개수로

        # 3. 유효한 경우만 반환 (2개만 있을 경우만 진입 허용)
    return reasons if len(reasons) == 2 else []


def get_exit_reasons(position: str, price: float, ma100: float) -> list[str]:
    reasons = []

    if position == "LONG":
        if price > ma100 * 0.9998:  # MA100보다 0.02% 이내로 접근
            reasons.append("🔻 MA100 근처 도달 (롱 청산 조건)")
    elif position == "SHORT":
        if price < ma100 * 1.0002:  # MA100보다 -0.02% 이내로 접근
            reasons.append("🔺 MA100 근처 도달 (숏 청산 조건)")

    return reasons
