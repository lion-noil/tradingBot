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

def build_open_index(sorted_items: List[Item]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for i, (sid, _, _) in enumerate(sorted_items or [], start=1):
        out[str(sid)] = i
    return out


def fmt_targets_idx(open_idx: Dict[str, int], targets: List[str]) -> str:
    """
    targets의 순번을 "2,4,5" 형태로
    """
    nums = []
    for sid in targets or []:
        n = open_idx.get(str(sid))
        if n is not None:
            nums.append(n)
    nums = sorted(set(nums))
    return ",".join(str(x) for x in nums) if nums else "—"


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


def easing_from_thr(thr: float, *, div: float = 30.0, lo: float = 0.0, hi: float = 0.002) -> float:
    """
    easing = thr / 30
    thr=0.006(0.6%) -> 0.0002(0.02%)
    thr=0.03(3.0%)  -> 0.001 (0.10%)
    """
    try:
        t = float(thr)
    except Exception:
        return 0.0
    e = t / float(div)
    if e < lo:
        e = lo
    if e > hi:
        e = hi
    return e


# ---------- 엔트리 신호 ----------
def get_long_entry_signal(
        price: float,
        ma100: float,
        prev3_candle: Optional[Dict[str, Any]],
        open_items: List[Item],
        ma_threshold: float = 0.01,
        momentum_threshold: float = 0.001,
        reentry_cooldown_sec: int = 60 * 60,
) -> Optional[Dict[str, Any]]:


    if price is None or ma100 is None or prev3_candle is None:
        return None

    mom = momentum_vs_prev_candle_ohlc(price, prev3_candle)
    if mom is None:
        return None

    items = _sorted_items(open_items)

    MAX_OPEN = 4
    if len(items) >= MAX_OPEN:
        return None

    next_no = len(items) + 1

    now_ms = int(time.time() * 1000)
    entry_easing = easing_from_thr(ma_threshold)  # ✅ 내부 계산
    ma_thr_eff = max(0.0, float(ma_threshold) - float(entry_easing))
    # ✅ 물타기(SCALE_IN): "불리 + MOM"만 (MA 조건은 보지 않음)
    if items:
        newest_id, newest_ts, newest_entry_price = items[-1]

        held_sec = max(0, (now_ms - int(newest_ts)) // 1000)
        if held_sec < int(reentry_cooldown_sec):
            return None

        adverse = price < float(newest_entry_price)  # LONG: 더 낮아야(유리)
        mom_ok = (-mom) > float(momentum_threshold)  # 3m 하락 모멘텀
        ma_ok = price <= ma100 * (1 - ma_thr_eff / 2)  # ✅ MA100 - mom_thr%

        if not (adverse and mom_ok and ma_ok):
            return None

        ma_delta_pct = (price - ma100) / max(ma100, 1e-12) * 100.0
        s = Signal(
            ok=True,
            kind="ENTRY",
            side="LONG",
            reasons=[
                "SCALE_IN",
                f"#ENTRY {next_no}",
                f"MA100 -{momentum_threshold * 100:.2f}%",
                f"3m -{momentum_threshold * 100:.2f}%",
            ],
            price=price,
            ma100=ma100,
            ma_delta_pct=ma_delta_pct,
            momentum_pct=mom,
            thresholds={"ma": ma_thr_eff, "momentum": momentum_threshold, "entry_easing": entry_easing},
            extra={"is_scale_in": True, "anchor_open_signal_id": newest_id},
        )
        return _signal_to_dict(s)

    # ✅ 첫진입(ENTRY): MA + MOM (둘 다 필요)
    ma_ok = price < ma100 * (1 - float(ma_thr_eff))
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
            f"#ENTRY {next_no}",
            f"MA100 -{ma_thr_eff * 100:.2f}%",
            f"3m -{momentum_threshold * 100:.2f}%",
        ],
        price=price,
        ma100=ma100,
        ma_delta_pct=ma_delta_pct,
        momentum_pct=mom,
        thresholds={"ma": ma_thr_eff, "momentum": momentum_threshold, "entry_easing": entry_easing},
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
        reentry_cooldown_sec: int = 60 * 60,
) -> Optional[Dict[str, Any]]:
    if price is None or ma100 is None or prev3_candle is None:
        return None

    mom = momentum_vs_prev_candle_ohlc(price, prev3_candle)
    if mom is None:
        return None
    entry_easing = easing_from_thr(ma_threshold)
    items = _sorted_items(open_items)

    MAX_OPEN = 4
    if len(items) >= MAX_OPEN:
        return None

    next_no = len(items) + 1

    now_ms = int(time.time() * 1000)
    ma_thr_eff = max(0.0, float(ma_threshold) - float(entry_easing))
    # ✅ 물타기(SCALE_IN): adverse + MOM + (MA100에서 mom_thr 만큼 유리)
    if items:
        newest_id, newest_ts, newest_entry_price = items[-1]

        held_sec = max(0, (now_ms - int(newest_ts)) // 1000)
        if held_sec < int(reentry_cooldown_sec):
            return None

        adverse = price > float(newest_entry_price)  # SHORT: 더 높아야(유리)
        mom_ok = mom > float(momentum_threshold)  # 3m 상승 모멘텀
        ma_ok = price >= ma100 * (1 + ma_thr_eff / 2)  # ✅ MA100 + mom_thr%

        if not (adverse and mom_ok and ma_ok):
            return None

        ma_delta_pct = (price - ma100) / max(ma100, 1e-12) * 100.0
        s = Signal(
            ok=True,
            kind="ENTRY",
            side="SHORT",
            reasons=[
                "SCALE_IN",
                f"#ENTRY {next_no}",
                f"MA100 +{momentum_threshold * 100:.2f}%",
                f"3m +{momentum_threshold * 100:.2f}%",
            ],
            price=price,
            ma100=ma100,
            ma_delta_pct=ma_delta_pct,
            momentum_pct=mom,
            thresholds={"ma": ma_thr_eff, "momentum": momentum_threshold, "entry_easing": entry_easing},

            extra={"is_scale_in": True, "anchor_open_signal_id": newest_id},
        )
        return _signal_to_dict(s)

    # ✅ 첫진입(ENTRY): MA + MOM (둘 다 필요)
    ma_ok = price > ma100 * (1 + float(ma_thr_eff))
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
            f"#ENTRY {next_no}",
            f"MA100 +{ma_threshold * 100:.2f}%",
            f"3m +{momentum_threshold * 100:.2f}%",
        ],
        price=price,
        ma100=ma100,
        ma_delta_pct=ma_delta_pct,
        momentum_pct=mom,
        thresholds={"ma": ma_thr_eff, "momentum": momentum_threshold, "entry_easing": entry_easing},
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
        time_limit_sec: int = None,
        near_touch_window_sec: int = 60 * 60,

        momentum_threshold: float = 0.001,

        # ✅ 추가
        scaleout_cooldown_sec: int = 60 * 60,
        last_scaleout_ts_ms: Optional[int] = None,
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

    # ✅ SCALE_OUT 쿨다운 체크 함수
    def _scaleout_on_cooldown() -> bool:
        if last_scaleout_ts_ms is None:
            return False
        return (now_ms - int(last_scaleout_ts_ms)) < int(scaleout_cooldown_sec) * 1000

    items = _sorted_items(open_items)  # oldest -> newest
    open_idx = build_open_index(items)
    total_n = len(items)

    oldest_id, oldest_ts, _ = items[0]
    newest_id, newest_ts, _ = items[-1]

    oldest_elapsed_sec = max(0, (now_ms - int(oldest_ts)) // 1000)
    newest_elapsed_sec = max(0, (now_ms - int(newest_ts)) // 1000)

    x = int(near_touch_window_sec)
    y = int(time_limit_sec)
    exit_easing = easing_from_thr(ma_threshold)  # ✅ 내부 계산
    ma_thr_eff = max(0.0, float(ma_threshold) - float(exit_easing))



    # 1) 절대 종료: newest 기준 (전체 청산)
    if oldest_elapsed_sec > y:
        age_label = f"⏱ old:{fmt_dur_smh_d(oldest_elapsed_sec)} new:{fmt_dur_smh_d(newest_elapsed_sec)}"
        return {
            "kind": "EXIT",
            "mode": "TIME_LIMIT",
            "targets": [oldest_id],  # ✅ oldest 1개만
            "anchor_open_signal_id": oldest_id,  # ✅ anchor도 oldest
            "reasons": ["TIME_LIMIT", "CLOSE_OLDEST_ONLY", f"#EXIT {fmt_targets_idx(open_idx, [oldest_id])}/{total_n}" ,age_label],
            "thresholds": {"ma": ma_thr_eff, "exit_easing": exit_easing, "x": x, "y": y},
        }

    # ------------------------------------------------------------
    # ✅ (NEW) RISK_CONTROL: open 3~4개면 평균진입가 대비 +0.3% 익절시 "마지막 진입 1개" 정리
    # ------------------------------------------------------------
    PROFIT_TAKE_PCT = 0.002  # 0.2%

    if len(items) in (3, 4):
        avg_entry = sum(float(ep) for (_, _, ep) in items) / max(len(items), 1)

        if side == "LONG":
            hit = price >= avg_entry * (1 + PROFIT_TAKE_PCT)
            profit_sign = "+"
        elif side == "SHORT":
            hit = price <= avg_entry * (1 - PROFIT_TAKE_PCT)
            profit_sign = "-"
        else:
            hit = False
            profit_sign = ""
        if hit:
            return {
                "kind": "EXIT",
                "mode": "RISK_CONTROL",
                "targets": [newest_id],
                "anchor_open_signal_id": newest_id,
                "reasons": [
                    "RISK_CONTROL",
                    f"#EXIT {fmt_targets_idx(open_idx, [newest_id])}/{total_n}",
                    f"AVG_ENTRY {profit_sign}{PROFIT_TAKE_PCT * 100:.2f}%",
                ],
                "thresholds": {
                    "profit_take_pct": PROFIT_TAKE_PCT,
                    "avg_entry_price": float(avg_entry),
                    "x": x, "y": y,
                    "ma": ma_thr_eff,
                    "exit_easing": exit_easing,
                },
                "extra": {
                    "avg_entry_price": float(avg_entry),
                    "profit_take_pct": PROFIT_TAKE_PCT,
                    "risk_control": True,
                    "close_latest_only": True,
                },
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
        trigger_pct = ma_thr_eff
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
        reasons = ["NORMAL", "CLOSE_ALL", f"#EXIT {fmt_targets_idx(open_idx, targets)}/{total_n}", head, band_label,
                   f"⏱ old:{fmt_dur_smh_d(oldest_elapsed_sec)} new:{fmt_dur_smh_d(newest_elapsed_sec)}"]
        return {
            "kind": "EXIT",
            "mode": "NORMAL",
            "targets": targets,
            "anchor_open_signal_id": oldest_id,
            "reasons": reasons,
            "thresholds": {"ma": ma_thr_eff, "exit_easing": exit_easing, "x": x, "y": y},
        }

    # ------------------------------------------------------------
    # ✅ 4) SCALE_OUT: 터치와 무관하게 언제든 발생 가능
    #    (단, 위에서 NORMAL 전량이 먼저 먹었으니 NORMAL 터치에 뺏기지 않음)
    # ------------------------------------------------------------
    if len(items) >= 2 and (not _scaleout_on_cooldown()):
        mom = momentum_vs_prev_candle_ohlc(price, prev3_candle) if prev3_candle is not None else None
        if mom is not None:
            mom_thr = float(momentum_threshold)
            if side == "LONG":
                ma_ok = price >= ma100 * (1 + ma_thr_eff / 3)
                mom_ok = mom > mom_thr
                sign_ma = "+"
                sign_mom = "+"
            elif side == "SHORT":
                ma_ok = price <= ma100 * (1 - ma_thr_eff / 3)
                mom_ok = mom < -mom_thr
                sign_ma = "-"
                sign_mom = "-"
            else:
                ma_ok = mom_ok = False
                sign_ma = sign_mom =  ""

            if ma_ok and mom_ok:
                return {
                    "kind": "EXIT",
                    "mode": "SCALE_OUT",
                    "targets": [newest_id],
                    "anchor_open_signal_id": newest_id,
                    "reasons": [
                        "SCALE_OUT",
                        f"#EXIT {fmt_targets_idx(open_idx, [newest_id])}/{total_n}",
                        f"MA100 {sign_ma}{(ma_thr_eff / 3) * 100:.2f}%",
                        f"3m {sign_mom}{mom_thr * 100:.2f}%",
                        f"⏱ new:{fmt_dur_smh_d(newest_elapsed_sec)}",
                        f"CD {scaleout_cooldown_sec}s" if scaleout_cooldown_sec else "",
                    ],
                    "thresholds": {
                        "ma": ma_thr_eff,
                        "exit_easing": exit_easing,
                        "momentum": mom_thr,
                        "scaleout_cooldown_sec": int(scaleout_cooldown_sec),
                        "x": x, "y": y,
                    },
                    "extra": {
                        "scale_out_latest_only": True,
                        "momentum_pct": mom,
                    },
                }
    # ------------------------------------------------------------
    # ✅ 4.5) INIT_OUT: 1개일 때만 + (최근 30분 scaleout 없을 때만)
    #    - MA 기준은 1/2
    #    - mom 조건은 scaleout과 동일
    # ------------------------------------------------------------
    if len(items) == 1 and (not _scaleout_on_cooldown()):
        mom = momentum_vs_prev_candle_ohlc(price, prev3_candle) if prev3_candle is not None else None
        if mom is not None:
            mom_thr = float(momentum_threshold)

            if side == "LONG":
                ma_ok = price >= ma100 * (1 + ma_thr_eff / 2)  # ✅ 1/2
                mom_ok = mom > mom_thr
                sign_ma = "+"
                sign_mom = "+"
            elif side == "SHORT":
                ma_ok = price <= ma100 * (1 - ma_thr_eff / 2)  # ✅ 1/2
                mom_ok = mom < -mom_thr
                sign_ma = "-"
                sign_mom = "-"
            else:
                ma_ok = mom_ok = False
                sign_ma = sign_mom = ""

            if ma_ok and mom_ok:
                only_id, _, _ = items[0]
                return {
                    "kind": "EXIT",
                    "mode": "INIT_OUT",
                    "targets": [only_id],
                    "anchor_open_signal_id": only_id,
                    "reasons": [
                        "INIT_OUT",
                        f"#EXIT {fmt_targets_idx(open_idx, [only_id])}/{total_n}",
                        f"MA100 {sign_ma}{(ma_thr_eff / 2) * 100:.2f}%",
                        f"3m {sign_mom}{mom_thr * 100:.2f}%",
                        f"⏱ held:{fmt_dur_smh_d(newest_elapsed_sec)}",
                        f"CD {scaleout_cooldown_sec}s" if scaleout_cooldown_sec else "",
                    ],
                    "thresholds": {
                        "ma": ma_thr_eff,
                        "exit_easing": exit_easing,
                        "momentum": mom_thr,
                        "scaleout_cooldown_sec": int(scaleout_cooldown_sec),
                        "x": x, "y": y,
                    },
                    "extra": {
                        "momentum_pct": mom,
                        "init_out": True,
                    },
                }

    # ------------------------------------------------------------
    # ✅ 5) NEAR_TOUCH: NEAR 구간에서만, touched일 때 newest 1개 청산
    # ------------------------------------------------------------
    if band == "NEAR" and touched:
        reasons = ["NEAR_TOUCH", f"#EXIT {fmt_targets_idx(open_idx, [newest_id])}/{total_n}",head, band_label,
                   f"⏱ old:{fmt_dur_smh_d(oldest_elapsed_sec)} new:{fmt_dur_smh_d(newest_elapsed_sec)}"]
        return {
            "kind": "EXIT",
            "mode": "NEAR_TOUCH",
            "targets": [newest_id],
            "anchor_open_signal_id": newest_id,
            "reasons": reasons,
            "thresholds": {"ma": ma_thr_eff, "exit_easing": exit_easing, "x": x, "y": y},
        }

    return None
