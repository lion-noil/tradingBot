# strategies/basic_strategy.py
from __future__ import annotations
from dataclasses import dataclass
import time
from typing import Optional, List, Dict, Any

@dataclass
class Signal:
    ok: bool                 # 신호 성립 여부
    kind: str                # 'ENTRY' | 'EXIT'
    side: str                # 'LONG' | 'SHORT'
    reasons: List[str]       # 사람이 보는 사유 리스트
    price: float             # 관측가(신호 시점)
    ma100: float
    ma_delta_pct: float      # (price - ma100)/ma100
    momentum_pct: Optional[float]  # (price-prev)/prev (롱은 음수 기대, 숏은 양수)
    thresholds: Dict[str, float]   # {"ma":..., "momentum":...}
    extra: Dict[str, Any] = None   # 보유시간, 코드 등 부가정보

# ---------- 엔트리 신호 ----------
def get_long_entry_signal(
    price: float,
    ma100: float,
    prev: float,
    ma_threshold: float = 0.002,        # 0.2%
    momentum_threshold: float = 0.001,  # 0.1%
    recent_entry_time: Optional[int] = None,   # ms 단위: 마지막 진입 시각
    reentry_cooldown_sec: int = 3600           # 1시간
) -> Optional["Signal"]:
    # 재진입 쿨다운 체크
    if recent_entry_time is not None:
        now_ms = int(time.time() * 1000)
        held_sec = max(0, (now_ms - recent_entry_time) // 1000)
        if held_sec < reentry_cooldown_sec:
            # 쿨다운 중엔 신호 차단
            return None

    reasons: List[str] = []
    if price < ma100 * (1 - ma_threshold):
        reasons.append(f"MA100 대비 -{ma_threshold*100:.2f}% 이상 하락")
    if (prev - price) / max(prev, 1e-12) > momentum_threshold:
        reasons.append(f"3분 전 대비 {momentum_threshold*100:.2f}% 이상 급락")
    if len(reasons) < 2:
        return None

    ma_delta = (price - ma100) / max(ma100, 1e-12)
    momentum = (price - prev) / max(prev, 1e-12)
    return Signal(
        ok=True, kind="ENTRY", side="LONG", reasons=reasons,
        price=price, ma100=ma100, ma_delta_pct=ma_delta,
        momentum_pct=momentum,
        thresholds={"ma": ma_threshold, "momentum": momentum_threshold},
        extra={"reentry_cooldown_sec": reentry_cooldown_sec}
    )


def get_short_entry_signal(
    price: float,
    ma100: float,
    prev: float,
    ma_threshold: float = 0.002,        # 0.2%
    momentum_threshold: float = 0.001,  # 0.1%
    recent_entry_time: Optional[int] = None,   # ms 단위: 마지막 진입 시각
    reentry_cooldown_sec: int = 3600           # 1시간
) -> Optional["Signal"]:
    # 재진입 쿨다운 체크
    if recent_entry_time is not None:
        now_ms = int(time.time() * 1000)
        held_sec = max(0, (now_ms - recent_entry_time) // 1000)
        if held_sec < reentry_cooldown_sec:
            return None

    reasons: List[str] = []
    if price > ma100 * (1 + ma_threshold):
        reasons.append(f"MA100 대비 +{ma_threshold*100:.2f}% 이상 돌파")
    if (price - prev) / max(prev, 1e-12) > momentum_threshold:
        reasons.append(f"3분 전 대비 {momentum_threshold*100:.2f}% 이상 급등")
    if len(reasons) < 2:
        return None

    ma_delta = (price - ma100) / max(ma100, 1e-12)
    momentum = (price - prev) / max(prev, 1e-12)
    return Signal(
        ok=True, kind="ENTRY", side="SHORT", reasons=reasons,
        price=price, ma100=ma100, ma_delta_pct=ma_delta,
        momentum_pct=momentum,
        thresholds={"ma": ma_threshold, "momentum": momentum_threshold},
        extra={"reentry_cooldown_sec": reentry_cooldown_sec}
    )

# ---------- 익싯 신호 ----------
def get_exit_signal(
    position: str,
    price: float,
    ma100: float,
    recent_entry_time: Optional[int] = None,   # ms
    ma_threshold: float = 0.005,               # 0.5% (사용자 평소값)
    exit_ma_threshold: float = 0.0005,         # 0.05% (근접 터치)
    time_limit_sec: int = 24 * 3600,           # 24시간 초과 시 무조건 EXIT
    near_touch_window_sec: int = 30 * 60       # 30분 이내는 근접 기준
) -> Optional["Signal"]:
    """
    position: "LONG" | "SHORT"
    recent_entry_time: 엔트리 시각(ms). 없으면 시간기반 로직 없이 MA 기준만 적용(일반 기준 사용).
    """
    now_ms = int(time.time() * 1000)
    reasons: List[str] = []
    reason_code = None

    # 경과 시간 계산
    held_sec = None
    if recent_entry_time is not None:
        held_sec = max(0, (now_ms - recent_entry_time) // 1000)

    # 1) 24시간 초과면 무조건 EXIT
    if held_sec is not None and held_sec > time_limit_sec:
        hours = time_limit_sec / 3600
        reasons = [f"⏰ 진입 후 {held_sec}초 경과 ({hours:.1f}시간 초과)"]
        reason_code = "TIME_LIMIT"
    else:
        # 2) 시간 구간에 따른 트리거 퍼센트 선택
        if held_sec is not None and held_sec <= near_touch_window_sec:
            trigger_pct = exit_ma_threshold
            window_label = "근접 기준"
            touch_code_suffix = "RETOUCH"
        else:
            # (recent_entry_time 없으면 일반 기준 사용)
            trigger_pct = ma_threshold
            window_label = "일반 기준"
            touch_code_suffix = "TOUCH"

        # 3) MA100 재터치(또는 터치) 조건
        if position == "LONG":
            # 가격이 MA100까지 (1 + trigger_pct) 이상 올라오면 청산
            if price >= ma100 * (1 + trigger_pct):
                pct = trigger_pct * 100
                reasons = [f"🔻 MA100 대비 +{pct:.4f}% {window_label} 도달 (롱 청산)"]
                reason_code = f"MA_{touch_code_suffix}_LONG"
        elif position == "SHORT":
            # 가격이 MA100까지 (1 - trigger_pct) 이하로 내려오면 청산
            if price <= ma100 * (1 - trigger_pct):
                pct = trigger_pct * 100
                reasons = [f"🔺 MA100 대비 -{pct:.4f}% {window_label} 도달 (숏 청산)"]
                reason_code = f"MA_{touch_code_suffix}_SHORT"
        else:
            # 예상치 못한 포지션 문자열 보호
            return None

    if not reasons:
        return None

    ma_delta = (price - ma100) / (ma100 if ma100 != 0 else 1e-12)

    return Signal(
        ok=True,
        kind="EXIT",
        side=position,
        reasons=reasons,
        price=price,
        ma100=ma100,
        ma_delta_pct=ma_delta,
        momentum_pct=None,
        thresholds={"ma": ma_threshold, "exit_ma": exit_ma_threshold},
        extra={
            "reason_code": reason_code,
            "time_held_sec": held_sec
        }
    )

