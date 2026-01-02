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

def momentum_vs_prev_candle_ohlc(price: float, prev_candle: Optional[Dict[str, Any]]) -> Optional[float]:
    """
    현재가(price)와 '3분 전 봉'(prev_candle)의 OHLC 중
    변화율 절대값(|pct|)이 가장 큰 값을 반환.
    """
    if price is None or prev_candle is None:
        return None

    vals = []
    for k in ("open", "high", "low", "close"):
        v = prev_candle.get(k)
        if v is None:
            continue
        try:
            v = float(v)
        except Exception:
            continue
        if v <= 0:
            continue
        vals.append((price - v) / v)

    if not vals:
        return None
    return max(vals, key=lambda x: abs(x))


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
    prev3_candle: Optional[Dict[str, Any]],  # ✅ 바뀜
    ma_threshold: float = 0.002,
    momentum_threshold: float = 0.001,
    recent_entry_time: Optional[int] = None,
    reentry_cooldown_sec: int = 3600
) -> Optional["Signal"]:

    # ✅ 데이터 없으면(휴장/결측) 신호 없음
    if price is None or ma100 is None or prev3_candle is None:
        return None

    # 재진입 쿨다운
    if recent_entry_time is not None:
        now_ms = int(time.time() * 1000)
        held_sec = max(0, (now_ms - recent_entry_time) // 1000)
        if held_sec < reentry_cooldown_sec:
            return None

    # ✅ 3분전 OHLC 기반 모멘텀
    mom = momentum_vs_prev_candle_ohlc(price, prev3_candle)
    if mom is None:
        return None
    MA_EASING = 0.0001
    eff_ma_th = ma_threshold - MA_EASING

    reasons: List[str] = []
    if price < ma100 * (1 - eff_ma_th):
        reasons.append(f"MA100 -{eff_ma_th*100:.2f}%")

    # LONG은 "3분전 대비 하락"이 조건이었으니 mom이 음수일 때만 체크
    if (-mom) > momentum_threshold:
        reasons.append(f"3m -{momentum_threshold*100:.2f}%")

    if len(reasons) < 2:
        return None

    ma_delta = (price - ma100) / max(ma100, 1e-12)
    return Signal(
        ok=True, kind="ENTRY", side="LONG", reasons=reasons,
        price=price, ma100=ma100, ma_delta_pct=ma_delta,
        momentum_pct=mom,  # ✅ OHLC 기준 모멘텀 저장
        thresholds={"ma": ma_threshold, "momentum": momentum_threshold},
        extra={"reentry_cooldown_sec": reentry_cooldown_sec}
    )

def get_short_entry_signal(
    price: float,
    ma100: float,
    prev3_candle: Optional[Dict[str, Any]],  # ✅ 바뀜
    ma_threshold: float = 0.002,
    momentum_threshold: float = 0.001,
    recent_entry_time: Optional[int] = None,
    reentry_cooldown_sec: int = 3600
) -> Optional["Signal"]:

    if price is None or ma100 is None or prev3_candle is None:
        return None

    if recent_entry_time is not None:
        now_ms = int(time.time() * 1000)
        held_sec = max(0, (now_ms - recent_entry_time) // 1000)
        if held_sec < reentry_cooldown_sec:
            return None

    mom = momentum_vs_prev_candle_ohlc(price, prev3_candle)
    if mom is None:
        return None


    MA_EASING = 0.0001
    eff_ma_th = ma_threshold - MA_EASING

    reasons: List[str] = []
    if price > ma100 * (1 + eff_ma_th):
        reasons.append(f"MA100 +{eff_ma_th*100:.2f}%")

    if mom > momentum_threshold:
        reasons.append(f"3m +{momentum_threshold*100:.2f}%")

    if len(reasons) < 2:
        return None

    ma_delta = (price - ma100) / max(ma100, 1e-12)
    return Signal(
        ok=True, kind="ENTRY", side="SHORT", reasons=reasons,
        price=price, ma100=ma100, ma_delta_pct=ma_delta,
        momentum_pct=mom,
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
    time_limit_sec: int = None,
    near_touch_window_sec: int = 60 * 60
) -> Optional["Signal"]:
    """
    position: "LONG" | "SHORT"
    recent_entry_time: 엔트리 시각(ms). 없으면 시간기반 로직 없이 MA 기준만 적용(일반 기준 사용).
    """

    if ma_threshold is None:
        raise ValueError("ma_threshold is required (got None)")
    if exit_ma_threshold is None:
        raise ValueError("exit_ma_threshold is required (got None)")
    if time_limit_sec is None:
        raise ValueError("time_limit_sec is required (got None)")
    if near_touch_window_sec is None:
        raise ValueError("near_touch_window_sec is required (got None)")

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