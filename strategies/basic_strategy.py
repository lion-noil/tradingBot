# strategies/basic_strategy.py
from __future__ import annotations
from dataclasses import dataclass
import time
from typing import Optional, List, Dict, Any

@dataclass
class Signal:
    ok: bool
    kind: str
    side: str
    reasons: List[str]
    price: float
    ma100: float
    ma_delta_pct: float
    momentum_pct: Optional[float]
    thresholds: Dict[str, float]
    extra: Dict[str, Any] = None

def _fmt_edge(seconds: int) -> str:
    if seconds % 3600 == 0:
        return f"{seconds//3600}h"
    if seconds % 60 == 0:
        return f"{seconds//60}m"
    return f"{seconds}s"

def _fmt_dur(sec: int | None) -> str:
    if sec is None:
        return "N/A"
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    if h: return f"{h}h {m}m {s}s"
    if m: return f"{m}m {s}s"
    return f"{s}s"
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
        reasons.append(f"MA100 -{ma_threshold*100:.2f}%")
    if (prev - price) / max(prev, 1e-12) > momentum_threshold:
        reasons.append(f"3m -{momentum_threshold*100:.2f}%")
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
        reasons.append(f"MA100 +{ma_threshold*100:.2f}%")
    if (price - prev) / max(prev, 1e-12) > momentum_threshold:
        reasons.append(f"3m +{momentum_threshold*100:.2f}%")
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

def get_exit_signal(
    position: str,
    price: float,
    ma100: float,
    recent_entry_time: Optional[int] = None,   # ms
    ma_threshold: float = 0.005,               # 0.5% (사용자 평소값)
    exit_ma_threshold: float = -0.0005,         # -0.05% (근접 직전)
    time_limit_sec: int = 24 * 3600,           # 24시간 초과 시 무조건 EXIT
    near_touch_window_sec: int = 60 * 60
) -> Optional["Signal"]:
    """
    position: "LONG" | "SHORT"
    recent_entry_time: 엔트리 시각(ms). 없으면 시간기반 로직 없이 MA 기준만 적용(일반 기준 사용).
    """
    now_ms = int(time.time() * 1000)

    # 1) 경과시간 계산(시그널 체인 기준)
    chain_elapsed_sec = None
    if recent_entry_time is not None:
        chain_elapsed_sec = max(0, (now_ms - recent_entry_time) // 1000)

    x = near_touch_window_sec
    y = time_limit_sec

    # 2) 절대 종료 먼저(가장 강한 조건)
    if chain_elapsed_sec is not None and chain_elapsed_sec > y:
        # 문자열은 '성립'이므로 이제 생성
        reasons = [
            f"⏰ {y / 3600:.1f}h 초과",
            f"⛳: {_fmt_edge(y)}~",
            f"⏱ : {_fmt_dur(chain_elapsed_sec)}"
        ]
        ma_delta = (price - ma100) / (ma100 if ma100 != 0 else 1e-12)
        return Signal(
            ok=True, kind="EXIT", side=position, reasons=reasons,
            price=price, ma100=ma100, ma_delta_pct=ma_delta,
            momentum_pct=None,
            thresholds={"ma": ma_threshold, "exit_ma": exit_ma_threshold},
        )

    # 3) 트리거 선택만 계산 (문자열 생성 X)
    if chain_elapsed_sec is None:
        trigger_pct = ma_threshold
        trigger_name = "일반"
        band_label = "⛳: N/A"
    elif chain_elapsed_sec <= x:
        trigger_pct = exit_ma_threshold
        trigger_name = "근접"
        band_label = f"⛳: 0~{_fmt_edge(x)}"
    else:
        trigger_pct = ma_threshold
        trigger_name = "일반"
        band_label = f"⛳: {_fmt_edge(x)}~{_fmt_edge(y)}"

    # 4) 터치 성립 여부만 먼저 판단 (문자열 생성 X)
    touched = False
    if position == "LONG":
        touched = price >= ma100 * (1 + trigger_pct)
    elif position == "SHORT":
        touched = price <= ma100 * (1 - trigger_pct)
    else:
        return None

    if not touched:
        return None  # 성립 안 하면 바로 끝 (문자열/계산 낭비 없음)

    # 5) 여기서부터 '성립'이므로 필요한 문자열/계산 생성
    pct_val = trigger_pct * 100
    if position == "LONG":
        head = f"MA100 +{pct_val:.4f}% {trigger_name}"
    else:
        head = f"MA100 -{pct_val:.4f}% {trigger_name}"

    reasons: List[str] = [
        head,
        band_label,
        f"⏱ : {_fmt_dur(chain_elapsed_sec)}",
    ]

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
    )