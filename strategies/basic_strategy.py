# strategies/basic_strategy.py
from datetime import datetime, timedelta
import time
from typing import Optional

def get_long_entry_reasons(price, ma100, prev,
                           ma_threshold=0.002, momentum_threshold=0.001):
    reasons = []

    # 1. 기술적 조건
    if price < ma100 * (1 + ma_threshold * -1):  # MA100보다 -x%
        reasons.append(f"MA100 대비 -{ma_threshold*100:.2f}% 이상 하락")
    if (prev - price) / prev > momentum_threshold:
        reasons.append(f"3분 전 대비 {momentum_threshold*100:.2f}% 이상 급락")


    # 3. 기술적 조건이 2개 모두 충족된 경우만 진입
    if len(reasons) == 2:
        return reasons
    return []
def get_short_entry_reasons(price, ma100, prev,
                            ma_threshold=0.002, momentum_threshold=0.001):
    reasons = []

    # 1. 기술적 조건
    if price > ma100 * (1 + ma_threshold):  # MA100보다 +x%
        reasons.append(f"MA100 대비 +{ma_threshold*100:.2f}% 이상 돌파")
    if (price - prev) / prev > momentum_threshold:
        reasons.append(f"3분 전 대비 {momentum_threshold*100:.2f}% 이상 급등")


    # 3. 기술적 조건이 2개 모두 충족된 경우만 진입
    if len(reasons) == 2:
        return reasons
    return []

def get_exit_reasons(
    position: str,
    price: float,
    ma100: float,
    recent_entry_time: Optional[int] = None,
    ma_threshold: float = 0.0005,   # 예: 0.0005 → 0.05% (원래 코드와 동일 기본값)
    time_limit_sec: int = 7200      # 기본 2시간
) -> list[str]:
    # 1. 기술적 조건 (입력 % 기준)
    if position == "LONG" and price > ma100 * (1 - ma_threshold):
        return [f"🔻 MA100 대비 -{ma_threshold*100:.4f}% 근처 도달 (롱 청산 조건)"]
    if position == "SHORT" and price < ma100 * (1 + ma_threshold):
        return [f"🔺 MA100 대비 +{ma_threshold*100:.4f}% 근처 도달 (숏 청산 조건)"]

    # 2. 시간 조건: 진입 후 x초 이상 경과
    if recent_entry_time:
        now_ts = int(time.time() * 1000)
        time_held_sec = (now_ts - recent_entry_time) / 1000
        if time_held_sec >= time_limit_sec:
            hours = time_limit_sec / 3600
            return [f"⏰ 진입 후 {int(time_held_sec)}초 경과 ({hours:.1f}시간 초과)"]

    return []

