# strategies/basic_strategy.py
from __future__ import annotations
from dataclasses import dataclass
import time
from typing import List, Tuple, Optional, Dict, Any

Item = Tuple[str, int]  # (signal_id, ts_ms)


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
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
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

    eff_ma_th = ma_threshold

    reasons: List[str] = []
    if price < ma100 * (1 - eff_ma_th):
        reasons.append(f"MA100 -{eff_ma_th * 100:.2f}%")

    # LONG은 "3분전 대비 하락"이 조건이었으니 mom이 음수일 때만 체크
    if (-mom) > momentum_threshold:
        reasons.append(f"3m -{momentum_threshold * 100:.2f}%")

    if len(reasons) < 2:
        return None

    ma_delta_pct = (price - ma100) / max(ma100, 1e-12) * 100.0

    return Signal(
        ok=True, kind="ENTRY", side="LONG", reasons=reasons,
        price=price, ma100=ma100, ma_delta_pct=ma_delta_pct,
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

    eff_ma_th = ma_threshold
    reasons: List[str] = []
    if price > ma100 * (1 + eff_ma_th):
        reasons.append(f"MA100 +{eff_ma_th * 100:.2f}%")

    if mom > momentum_threshold:
        reasons.append(f"3m +{momentum_threshold * 100:.2f}%")

    if len(reasons) < 2:
        return None
    ma_delta_pct = (price - ma100) / max(ma100, 1e-12) * 100.0
    return Signal(
        ok=True, kind="ENTRY", side="SHORT", reasons=reasons,
        price=price, ma100=ma100, ma_delta_pct=ma_delta_pct,
        momentum_pct=mom,
        thresholds={"ma": ma_threshold, "momentum": momentum_threshold},
        extra={"reentry_cooldown_sec": reentry_cooldown_sec}
    )


def get_exit_signal(
        *,
        side: str,
        price: float,
        ma100: float,
        open_items: List[Item],  # ✅ 필수
        ma_threshold: float = 0.005,
        exit_easing: float = 0.0002,
        time_limit_sec: int = None,
        near_touch_window_sec: int = 60 * 60,
) -> Optional[Dict[str, Any]]:
    if ma_threshold is None:
        raise ValueError("ma_threshold is required (got None)")
    if time_limit_sec is None:
        raise ValueError("time_limit_sec is required (got None)")
    if near_touch_window_sec is None:
        raise ValueError("near_touch_window_sec is required (got None)")
    if not open_items:
        return None

    now_ms = int(time.time() * 1000)

    # open_items 정렬 확실히: oldest -> newest
    items = sorted(open_items, key=lambda x: x[1])
    oldest_id, oldest_ts = items[0]
    newest_id, newest_ts = items[-1]

    # elapsed
    oldest_elapsed_sec = max(0, (now_ms - oldest_ts) // 1000)
    newest_elapsed_sec = max(0, (now_ms - newest_ts) // 1000)

    x = int(near_touch_window_sec)
    y = int(time_limit_sec)

    # 1) 절대 종료: newest 기준 (✅ 변경)
    if newest_elapsed_sec > y:
        targets = [sid for sid, _ in items]  # 전체 청산
        return {
            "kind": "EXIT",
            "mode": "TIME_LIMIT",
            "targets": targets,
            "target_open_signal_id": newest_id,  # 대표값도 newest로 두는 게 자연스러움
            "reasons": [
                f"⏰ newest {y / 3600:.1f}h 초과",
                f"oldest={oldest_elapsed_sec}s newest={newest_elapsed_sec}s",
            ],
            "thresholds": {"ma": ma_threshold, "exit_easing": exit_easing, "x": x, "y": y},
        }

    # 2) 구간별 트리거 설정: newest 기준으로 구간 판단
    if newest_elapsed_sec <= x:
        # ✅ 근접: newest 1개만
        trigger_pct = -float(exit_easing)
        trigger_name = "근접"
        band_label = f"⛳: 0~{x}s"
        targets = [newest_id]
        rep_id = newest_id
    else:
        # ✅ 일반: 전체 청산
        trigger_pct = max(0.0, float(ma_threshold) - float(exit_easing))
        trigger_name = "일반"
        band_label = f"⛳: {x}s~{y}s"
        targets = [sid for sid, _ in items]
        rep_id = oldest_id  # 대표값은 oldest로 두는 게 디버깅에 보통 좋음

    # 3) 터치 판정
    if side == "LONG":
        touched = price >= ma100 * (1 + trigger_pct)
    elif side == "SHORT":
        touched = price <= ma100 * (1 - trigger_pct)
    else:
        return None

    if not touched:
        return None

    # 4) 성립 시 메시지 생성
    pct_val = trigger_pct * 100
    head = (
        f"MA100 +{pct_val:.4f}% {trigger_name}"
        if side == "LONG"
        else f"MA100 -{pct_val:.4f}% {trigger_name}"
    )

    return {
        "kind": "EXIT",
        "mode": "NEAR_TOUCH" if newest_elapsed_sec <= x else "NORMAL",
        "targets": targets,
        "target_open_signal_id": rep_id,  # ✅ 대표값(로그/요약용)
        "reasons": [
            head,
            band_label,
            f"⏱ oldest:{oldest_elapsed_sec}s newest:{newest_elapsed_sec}s",
        ],
        "thresholds": {"ma": ma_threshold, "exit_easing": exit_easing, "x": x, "y": y},
    }
