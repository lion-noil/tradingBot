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
def get_long_entry_signal(price: float, ma100: float, prev: float,
                          ma_threshold: float = 0.002,   # 0.2%
                          momentum_threshold: float = 0.001) -> Optional[Signal]:
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
        extra={}
    )

def get_short_entry_signal(price: float, ma100: float, prev: float,
                           ma_threshold: float = 0.002,
                           momentum_threshold: float = 0.001) -> Optional[Signal]:
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
        extra={}
    )

# ---------- 익싯 신호 ----------
def get_exit_signal(position: str, price: float, ma100: float,
                    recent_entry_time: Optional[int] = None,
                    ma_threshold: float = 0.0005,   # 0.05%
                    time_limit_sec: int = 7200) -> Optional[Signal]:

    now_ms = int(time.time() * 1000)
    reasons: List[str] = []
    reason_code = None

    if position == "LONG" and price > ma100 * (1 - ma_threshold):
        reasons = [f"🔻 MA100 대비 -{ma_threshold*100:.4f}% 근처 도달 (롱 청산 조건)"]
        reason_code = "MA_RETOUCH_LONG"
    elif position == "SHORT" and price < ma100 * (1 + ma_threshold):
        reasons = [f"🔺 MA100 대비 +{ma_threshold*100:.4f}% 근처 도달 (숏 청산 조건)"]
        reason_code = "MA_RETOUCH_SHORT"
    else:
        if recent_entry_time:
            held_sec = (now_ms - recent_entry_time) / 1000
            if held_sec >= time_limit_sec:
                hours = time_limit_sec / 3600
                reasons = [f"⏰ 진입 후 {int(held_sec)}초 경과 ({hours:.1f}시간 초과)"]
                reason_code = "TIME_LIMIT"

    if not reasons:
        return None

    ma_delta = (price - ma100) / max(ma100, 1e-12)
    return Signal(
        ok=True, kind="EXIT", side=position, reasons=reasons,
        price=price, ma100=ma100, ma_delta_pct=ma_delta,
        momentum_pct=None,
        thresholds={"ma": ma_threshold},
        extra={
            "reason_code": reason_code,
            "time_held_sec": int((now_ms - recent_entry_time)/1000) if recent_entry_time else None
        }
    )

# ---------- 하위 호환: 기존 함수 이름 유지 ----------
def get_long_entry_reasons(price, ma100, prev, ma_threshold=0.002, momentum_threshold=0.001):
    sig = get_long_entry_signal(price, ma100, prev, ma_threshold, momentum_threshold)
    return sig.reasons if sig else []

def get_short_entry_reasons(price, ma100, prev, ma_threshold=0.002, momentum_threshold=0.001):
    sig = get_short_entry_signal(price, ma100, prev, ma_threshold, momentum_threshold)
    return sig.reasons if sig else []

def get_exit_reasons(position: str, price: float, ma100: float,
                     recent_entry_time: Optional[int] = None,
                     ma_threshold: float = 0.0005, time_limit_sec: int = 7200) -> list[str]:
    sig = get_exit_signal(position, price, ma100, recent_entry_time, ma_threshold, time_limit_sec)
    return sig.reasons if sig else []
