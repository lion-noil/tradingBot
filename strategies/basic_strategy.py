# strategies/basic_strategy.py
from datetime import datetime, timedelta

def should_enter_short(price, ma100, prev):
    return price > ma100 * 1.002 and (price - prev) / prev > 0.001

def should_enter_long(price, ma100, prev):
    return price < ma100 * 0.998 and (prev - price) / prev > 0.001

def should_exit(price, ma100, position_time):
    if not position_time:
        return False
    duration = datetime.now() - position_time
    near_ma = abs(price - ma100) / ma100 < 0.0001
    return duration >= timedelta(minutes=10) or near_ma
