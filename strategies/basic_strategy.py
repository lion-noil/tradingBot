# strategies/basic_strategy.py
from datetime import datetime, timedelta

def get_long_entry_reasons(price, ma100, prev):
    reasons = []
    if price < ma100 * 0.998:
        reasons.append("MA100 대비 -0.2% 이상 하락")
    if (prev - price) / prev > 0.001:
        reasons.append("3분 전 대비 0.1% 이상 급락")
    return reasons if len(reasons) == 2 else []

def get_short_entry_reasons(price, ma100, prev):
    reasons = []
    if price > ma100 * 0.9998:
        reasons.append("MA100 대비 +0.2% 이상 돌파")
    if (price - prev) / prev > 0.001:
        reasons.append("3분 전 대비 0.1% 이상 급등")
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
