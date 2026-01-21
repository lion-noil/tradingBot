# strategies/basic_strategy.py
from __future__ import annotations
from dataclasses import dataclass, field
import time
from typing import List, Tuple, Optional, Dict, Any

# (signal_id, ts_ms, entry_price)
Item = Tuple[str, int, float]


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
    extra: Dict[str, Any] = field(default_factory=dict)


def fmt_dur_smh_d(sec: int) -> str:
    try:
        s = int(sec)
    except Exception:
        return ""
    if s < 0:
        s = 0
    if s < 60:
        return f"{s}s"
    m = (s + 30) // 60
    if m < 60:
        return f"{m}m"
    h = (m + 30) // 60
    if h < 24:
        return f"{h}h"
    d = (h + 12) // 24
    return f"{d}d"


def fmt_pct2(p: float) -> str:
    # p=0.0048 -> "0.48%"
    try:
        return f"{float(p) * 100.0:.2f}%"
    except Exception:
        return "—"


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


def _signal_to_dict(s: Signal) -> Dict[str, Any]:
    return {
        "ok": bool(s.ok),
        "kind": str(s.kind),  # ENTRY / EXIT
        "side": str(s.side),  # LONG/SHORT
        "reasons": list(s.reasons or []),
        "price": float(s.price),
        "ma100": float(s.ma100),
        "ma_delta_pct": float(s.ma_delta_pct),
        "momentum_pct": None if s.momentum_pct is None else float(s.momentum_pct),
        "thresholds": dict(s.thresholds or {}),
        "extra": dict(s.extra or {}),
    }


def _sorted_items(open_items: List[Item]) -> List[Item]:
    # oldest -> newest
    return sorted(open_items or [], key=lambda x: x[1])


# ---------- 엔트리 신호 ----------
def get_long_entry_signal(
        price: float,
        ma100: float,
        prev3_candle: Optional[Dict[str, Any]],
        open_items: List[Item],
        ma_threshold: float = 0.01,
        momentum_threshold: float = 0.001,
        reentry_cooldown_sec: int = 30 * 60,  # 30분 (첫진입/물타기 공통)
) -> Optional[Dict[str, Any]]:
    if price is None or ma100 is None or prev3_candle is None:
        return None

    mom = momentum_vs_prev_candle_ohlc(price, prev3_candle)
    if mom is None:
        return None

    items = _sorted_items(open_items)
    now_ms = int(time.time() * 1000)

    # ✅ 물타기(SCALE_IN): "불리 + MOM"만 (MA 조건은 보지 않음)
    if items:
        newest_id, newest_ts, newest_entry_price = items[-1]

        held_sec = max(0, (now_ms - int(newest_ts)) // 1000)
        if held_sec < int(reentry_cooldown_sec):
            return None

        adverse = price < float(newest_entry_price)  # LONG: 더 낮아야(유리)
        mom_ok = (-mom) > float(momentum_threshold)  # 3m 하락 모멘텀
        ma_ok = price <= ma100 * (1 - ma_threshold/2)  # ✅ MA100 - mom_thr%

        if not (adverse and mom_ok and ma_ok):
            return None

        ma_delta_pct = (price - ma100) / max(ma100, 1e-12) * 100.0
        s = Signal(
            ok=True,
            kind="ENTRY",
            side="LONG",
            reasons=[
                "SCALE_IN",
                f"MA100 -{momentum_threshold * 100:.2f}%",
                f"3m -{momentum_threshold * 100:.2f}%",
            ],
            price=price,
            ma100=ma100,
            ma_delta_pct=ma_delta_pct,
            momentum_pct=mom,
            thresholds={"ma": ma_threshold, "momentum": momentum_threshold},
            extra={"is_scale_in": True, "anchor_open_signal_id": newest_id},
        )
        return _signal_to_dict(s)

    # ✅ 첫진입(ENTRY): MA + MOM (둘 다 필요)
    ma_ok = price < ma100 * (1 - float(ma_threshold))
    mom_ok = (-mom) > float(momentum_threshold)
    if not (ma_ok and mom_ok):
        return None

    ma_delta_pct = (price - ma100) / max(ma100, 1e-12) * 100.0
    s = Signal(
        ok=True,
        kind="ENTRY",
        side="LONG",
        reasons=[
            "INIT",
            f"MA100 -{ma_threshold * 100:.2f}%",
            f"3m -{momentum_threshold * 100:.2f}%",
        ],
        price=price,
        ma100=ma100,
        ma_delta_pct=ma_delta_pct,
        momentum_pct=mom,
        thresholds={"ma": ma_threshold, "momentum": momentum_threshold},
        extra={"is_scale_in": False},
    )
    return _signal_to_dict(s)


def get_short_entry_signal(
        price: float,
        ma100: float,
        prev3_candle: Optional[Dict[str, Any]],
        open_items: List[Item],
        ma_threshold: float = 0.01,
        momentum_threshold: float = 0.001,
        reentry_cooldown_sec: int = 30 * 60,  # 30분 (첫진입/물타기 공통)
) -> Optional[Dict[str, Any]]:
    if price is None or ma100 is None or prev3_candle is None:
        return None

    mom = momentum_vs_prev_candle_ohlc(price, prev3_candle)
    if mom is None:
        return None

    items = _sorted_items(open_items)
    now_ms = int(time.time() * 1000)

    # ✅ 물타기(SCALE_IN): "불리 + MOM"만 (MA 조건은 보지 않음)
    # ✅ 물타기(SCALE_IN): adverse + MOM + (MA100에서 mom_thr 만큼 유리)
    if items:
        newest_id, newest_ts, newest_entry_price = items[-1]

        held_sec = max(0, (now_ms - int(newest_ts)) // 1000)
        if held_sec < int(reentry_cooldown_sec):
            return None

        adverse = price > float(newest_entry_price)  # SHORT: 더 높아야(유리)
        mom_ok = mom > float(momentum_threshold)  # 3m 상승 모멘텀
        ma_ok = price >= ma100 * (1 + ma_threshold/2)  # ✅ MA100 + mom_thr%

        if not (adverse and mom_ok and ma_ok):
            return None

        ma_delta_pct = (price - ma100) / max(ma100, 1e-12) * 100.0
        s = Signal(
            ok=True,
            kind="ENTRY",
            side="SHORT",
            reasons=[
                "SCALE_IN",
                f"MA100 +{momentum_threshold * 100:.2f}%",
                f"3m +{momentum_threshold * 100:.2f}%",
            ],
            price=price,
            ma100=ma100,
            ma_delta_pct=ma_delta_pct,
            momentum_pct=mom,
            thresholds={"ma": ma_threshold, "momentum": momentum_threshold},
            extra={"is_scale_in": True, "anchor_open_signal_id": newest_id},
        )
        return _signal_to_dict(s)

    # ✅ 첫진입(ENTRY): MA + MOM (둘 다 필요)
    ma_ok = price > ma100 * (1 + float(ma_threshold))
    mom_ok = mom > float(momentum_threshold)
    if not (ma_ok and mom_ok):
        return None

    ma_delta_pct = (price - ma100) / max(ma100, 1e-12) * 100.0
    s = Signal(
        ok=True,
        kind="ENTRY",
        side="SHORT",
        reasons=[
            "INIT",
            f"MA100 +{ma_threshold * 100:.2f}%",
            f"3m +{momentum_threshold * 100:.2f}%",
        ],
        price=price,
        ma100=ma100,
        ma_delta_pct=ma_delta_pct,
        momentum_pct=mom,
        thresholds={"ma": ma_threshold, "momentum": momentum_threshold},
        extra={"is_scale_in": False},
    )
    return _signal_to_dict(s)


def get_exit_signal(
        *,
        side: str,
        price: float,
        ma100: float,
        prev3_candle: Optional[Dict[str, Any]],  # ✅ 추가 (mom 계산용)
        open_items: List[Item],  # (sid, ts, entry_price)

        ma_threshold: float = 0.01,
        exit_easing: float = 0.0002,
        time_limit_sec: int = None,
        near_touch_window_sec: int = 60 * 60,

        momentum_threshold: float = 0.001,
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

    items = _sorted_items(open_items)  # oldest -> newest
    oldest_id, oldest_ts, _ = items[0]
    newest_id, newest_ts, _ = items[-1]

    oldest_elapsed_sec = max(0, (now_ms - int(oldest_ts)) // 1000)
    newest_elapsed_sec = max(0, (now_ms - int(newest_ts)) // 1000)

    x = int(near_touch_window_sec)
    y = int(time_limit_sec)

    # 1) 절대 종료: newest 기준 (전체 청산)
    if oldest_elapsed_sec > y:
        age_label = f"⏱ old:{fmt_dur_smh_d(oldest_elapsed_sec)} new:{fmt_dur_smh_d(newest_elapsed_sec)}"
        return {
            "kind": "EXIT",
            "mode": "TIME_LIMIT",
            "targets": [oldest_id],  # ✅ oldest 1개만
            "anchor_open_signal_id": oldest_id,  # ✅ anchor도 oldest
            "reasons": ["TIME_LIMIT", "CLOSE_OLDEST_ONLY", age_label],
            "thresholds": {"ma": ma_threshold, "exit_easing": exit_easing, "x": x, "y": y},
        }
    # ------------------------------------------------------------
    # 2) 구간(touch) 판정만 먼저 계산 (return은 아직!)
    # ------------------------------------------------------------
    if newest_elapsed_sec <= x:
        band = "NEAR"
        trigger_pct = -float(exit_easing)
        trigger_name = "근접"
        band_label = f"⛳ 0~{fmt_dur_smh_d(x)}"
    else:
        band = "NORMAL"
        trigger_pct = max(0.0, float(ma_threshold) - float(exit_easing))
        trigger_name = "일반"
        band_label = f"⛳ {fmt_dur_smh_d(x)}~{fmt_dur_smh_d(y)}"

    if side == "LONG":
        touched = price >= ma100 * (1 + trigger_pct)
    elif side == "SHORT":
        touched = price <= ma100 * (1 - trigger_pct)
    else:
        return None

    pct_abs_txt = fmt_pct2(abs(trigger_pct))
    sign = ("-" if side == "LONG" else "+") if band == "NEAR" else ("+" if side == "LONG" else "-")
    head = f"MA100 {sign}{pct_abs_txt} {trigger_name}"

    # ------------------------------------------------------------
    # ✅ 3) NORMAL 터치면 무조건 전량 청산 (SCALE_OUT보다 우선)
    # ------------------------------------------------------------
    if band == "NORMAL" and touched:
        targets = [sid for (sid, _, _) in items]  # 전부
        reasons = ["NORMAL", "CLOSE_ALL", head, band_label,
                   f"⏱ old:{fmt_dur_smh_d(oldest_elapsed_sec)} new:{fmt_dur_smh_d(newest_elapsed_sec)}"]
        return {
            "kind": "EXIT",
            "mode": "NORMAL",
            "targets": targets,
            "anchor_open_signal_id": oldest_id,
            "reasons": reasons,
            "thresholds": {"ma": ma_threshold, "exit_easing": exit_easing, "x": x, "y": y},
        }

    # ------------------------------------------------------------
    # ✅ 4) SCALE_OUT: 터치와 무관하게 언제든 발생 가능
    #    (단, 위에서 NORMAL 전량이 먼저 먹었으니 NORMAL 터치에 뺏기지 않음)
    # ------------------------------------------------------------
    mom = momentum_vs_prev_candle_ohlc(price, prev3_candle) if prev3_candle is not None else None
    if mom is not None:
        mom_thr = float(momentum_threshold)
        ma_thr = float(ma_threshold)

        newest_entry_price = float(items[-1][2])

        if side == "LONG":
            ma_ok = price >= ma100 * (1 + ma_thr/3)
            mom_ok = mom > mom_thr
            entry_ok = price >= newest_entry_price * (1 + ma_thr/3)
            sign_ma = "+"
            sign_mom = "+"
            sign_ep = "+"
        elif side == "SHORT":
            ma_ok = price <= ma100 * (1 - ma_thr/3)
            mom_ok = mom < -mom_thr
            entry_ok = price <= newest_entry_price * (1 - ma_thr/3)
            sign_ma = "-"
            sign_mom = "-"
            sign_ep = "-"
        else:
            ma_ok = mom_ok = entry_ok = False
            sign_ma = sign_mom = sign_ep = ""

        if ma_ok and mom_ok and entry_ok:
            return {
                "kind": "EXIT",
                "mode": "SCALE_OUT",
                "targets": [newest_id],
                "anchor_open_signal_id": newest_id,
                "reasons": [
                    "SCALE_OUT",
                    f"MA100 {sign_ma}{mom_thr * 100:.2f}%",
                    f"3m {sign_mom}{mom_thr * 100:.2f}%",
                    f"EP {sign_ep}{ma_thr * 100:.2f}%",
                    f"⏱ new:{fmt_dur_smh_d(newest_elapsed_sec)}",
                ],
                "thresholds": {
                    "ma": ma_threshold,
                    "exit_easing": exit_easing,
                    "momentum": mom_thr,
                    "x": x, "y": y,
                },
                "extra": {
                    "scale_out_latest_only": True,
                    "momentum_pct": mom,
                    "newest_entry_price": newest_entry_price,
                },
            }

    # ------------------------------------------------------------
    # ✅ 5) NEAR_TOUCH: NEAR 구간에서만, touched일 때 newest 1개 청산
    # ------------------------------------------------------------
    if band == "NEAR" and touched:
        reasons = ["NEAR_TOUCH", head, band_label,
                   f"⏱ old:{fmt_dur_smh_d(oldest_elapsed_sec)} new:{fmt_dur_smh_d(newest_elapsed_sec)}"]
        return {
            "kind": "EXIT",
            "mode": "NEAR_TOUCH",
            "targets": [newest_id],
            "anchor_open_signal_id": newest_id,
            "reasons": reasons,
            "thresholds": {"ma": ma_threshold, "exit_easing": exit_easing, "x": x, "y": y},
        }

    return None
