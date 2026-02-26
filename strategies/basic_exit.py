# strategies/basic_exit.py
from __future__ import annotations
import time
from typing import List, Optional, Dict, Any

from .basic_utils import (
    Item,
    _sorted_items, build_open_index, fmt_targets_idx,
    fmt_dur_smh_d, fmt_pct2,
    easing_from_thr, momentum_vs_prev_candle_ohlc,
)


def get_exit_signal(
    *,
    side: str,
    price: float,
    ma100: float,
    prev3_candle: Optional[Dict[str, Any]],
    open_items: List[Item],

    ma_threshold: float = 0.01,
    time_limit_sec: int = None,
    near_touch_window_sec: int = 60 * 60,

    momentum_threshold: float = 0.001,

    scaleout_cooldown_sec: int = 30 * 60,
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

    def _scaleout_on_cooldown() -> bool:
        if last_scaleout_ts_ms is None:
            return False
        return (now_ms - int(last_scaleout_ts_ms)) < int(scaleout_cooldown_sec) * 1000

    items = _sorted_items(open_items)
    open_idx = build_open_index(items)
    total_n = len(items)

    oldest_id, oldest_ts, _, _tag0 = items[0]
    newest_id, newest_ts, _, _tagN = items[-1]

    oldest_elapsed_sec = max(0, (now_ms - int(oldest_ts)) // 1000)
    newest_elapsed_sec = max(0, (now_ms - int(newest_ts)) // 1000)

    x = int(near_touch_window_sec)
    y = int(time_limit_sec)

    exit_easing = easing_from_thr(ma_threshold)
    ma_thr_eff = max(0.0, float(ma_threshold) - float(exit_easing))

    # 1) TIME_LIMIT: oldest 1개만 청산
    if oldest_elapsed_sec > y:
        age_label = f"⏱ old:{fmt_dur_smh_d(oldest_elapsed_sec)} new:{fmt_dur_smh_d(newest_elapsed_sec)}"
        return {
            "kind": "EXIT",
            "mode": "TIME_LIMIT",
            "targets": [oldest_id],
            "anchor_open_signal_id": oldest_id,
            "reasons": ["TIME_LIMIT", "CLOSE_OLDEST_ONLY", f"#EXIT {fmt_targets_idx(open_idx, [oldest_id])}/{total_n}", age_label],
            "thresholds": {"ma": ma_thr_eff, "exit_easing": exit_easing, "x": x, "y": y},
        }

    # ------------------------------------------------------------
    # STOP_LOSS / TAKE_PROFIT: oldest 1개 기준
    # ------------------------------------------------------------
    oldest_id, oldest_ts, oldest_entry_price, _tag0 = items[0]
    try:
        oldest_entry = float(oldest_entry_price or 0.0)
    except Exception:
        oldest_entry = 0.0

    def _sl_tp_policy_by_age(elapsed_sec: int) -> tuple[str, float]:
        s = int(elapsed_sec or 0)
        if s < 60 * 60:
            return ("_1H", 3.0)
        if s < 2 * 60 * 60:
            return ("1H_2H", 2.5)
        if s < 12 * 60 * 60:
            return ("2H_12H", 2.0)
        if s < 24 * 60 * 60:
            return ("12H_24H", 1.5)
        return ("24H_", 1.0)

    age_band, age_factor = _sl_tp_policy_by_age(oldest_elapsed_sec)

    if age_factor > 0.0 and oldest_entry > 0 and price is not None:
        if side == "LONG":
            pnl_pct = (float(price) - oldest_entry) / oldest_entry
        elif side == "SHORT":
            pnl_pct = (oldest_entry - float(price)) / oldest_entry
        else:
            pnl_pct = 0.0

        tp_pct = float(ma_thr_eff) * float(age_factor)
        sl_pct = float(ma_thr_eff) * float(age_factor)

        held_txt = fmt_dur_smh_d(oldest_elapsed_sec)

        if pnl_pct <= -sl_pct:
            return {
                "kind": "EXIT",
                "mode": "STOP_LOSS",
                "targets": [oldest_id],
                "anchor_open_signal_id": oldest_id,
                "reasons": [
                    f"SL({age_band})",
                    "CLOSE_OLDEST_ONLY",
                    f"#EXIT {fmt_targets_idx(open_idx, [oldest_id])}/{total_n}",
                    f"held={held_txt}",
                    f"OLDEST_PNL -{fmt_pct2(sl_pct)}",
                    f"pnl={fmt_pct2(pnl_pct)}",
                ],
                "thresholds": {
                    "sl_pct": sl_pct,
                    "tp_pct": tp_pct,
                    "age_factor": float(age_factor),
                    "age_band": age_band,
                    "held_sec": int(oldest_elapsed_sec),
                    "oldest_entry_price": oldest_entry,
                    "ma": ma_thr_eff,
                    "exit_easing": exit_easing,
                    "x": x, "y": y,
                },
                "extra": {
                    "oldest_entry_price": oldest_entry,
                    "pnl_pct": pnl_pct,
                    "close_oldest_only": True,
                    "age_factor": float(age_factor),
                    "age_band": age_band,
                    "held_sec": int(oldest_elapsed_sec),
                    "sl_tp_tag": f"SL({age_band})",
                },
            }

        if pnl_pct >= tp_pct:
            return {
                "kind": "EXIT",
                "mode": "TAKE_PROFIT",
                "targets": [oldest_id],
                "anchor_open_signal_id": oldest_id,
                "reasons": [
                    f"TP({age_band})",
                    "CLOSE_OLDEST_ONLY",
                    f"#EXIT {fmt_targets_idx(open_idx, [oldest_id])}/{total_n}",
                    f"held={held_txt}",
                    f"OLDEST_PNL +{fmt_pct2(tp_pct)}",
                    f"pnl={fmt_pct2(pnl_pct)}",
                ],
                "thresholds": {
                    "sl_pct": sl_pct,
                    "tp_pct": tp_pct,
                    "age_factor": float(age_factor),
                    "age_band": age_band,
                    "held_sec": int(oldest_elapsed_sec),
                    "oldest_entry_price": oldest_entry,
                    "ma": ma_thr_eff,
                    "exit_easing": exit_easing,
                    "x": x, "y": y,
                },
                "extra": {
                    "oldest_entry_price": oldest_entry,
                    "pnl_pct": pnl_pct,
                    "close_oldest_only": True,
                    "age_factor": float(age_factor),
                    "age_band": age_band,
                    "held_sec": int(oldest_elapsed_sec),
                    "sl_tp_tag": f"TP({age_band})",
                },
            }

    # ------------------------------------------------------------
    # RISK_CONTROL
    # ------------------------------------------------------------
    PROFIT_TAKE_PCT = 0.003  # 0.3%
    if len(items) in (3, 4):
        avg_entry = sum(float(ep) for (_sid, _ts, ep, _tag) in items) / max(len(items), 1)

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
            if len(items) == 3:
                target_ids = [oldest_id]
                mode = "RISK_CONTROL_3"
                extra = {"risk_control": True, "close_oldest_only": True}
            else:
                target_ids = [sid for (sid, _, _) in items]
                mode = "RISK_CONTROL_4"
                extra = {"risk_control": True, "close_all": True}

            return {
                "kind": "EXIT",
                "mode": mode,
                "targets": target_ids,
                "anchor_open_signal_id": target_ids[0],
                "reasons": [
                    "RISK_CONTROL",
                    f"#EXIT {fmt_targets_idx(open_idx, target_ids)}/{total_n}",
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
                    **extra,
                },
            }

    # ------------------------------------------------------------
    # touch band
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

    # NORMAL touched -> close all
    if band == "NORMAL" and touched:
        targets = [sid for (sid, _ts, _ep, _tag) in items]
        reasons = [
            "NORMAL", "CLOSE_ALL",
            f"#EXIT {fmt_targets_idx(open_idx, targets)}/{total_n}",
            head, band_label,
            f"⏱ old:{fmt_dur_smh_d(oldest_elapsed_sec)} new:{fmt_dur_smh_d(newest_elapsed_sec)}",
        ]
        return {
            "kind": "EXIT",
            "mode": "NORMAL",
            "targets": targets,
            "anchor_open_signal_id": oldest_id,
            "reasons": reasons,
            "thresholds": {"ma": ma_thr_eff, "exit_easing": exit_easing, "x": x, "y": y},
        }

    # SCALE_OUT (newest only) - momentum 제거 + "직전 진입가(prev lot)" 기준
    if len(items) >= 2 and (not _scaleout_on_cooldown()):
        # newest는 청산 타겟, 기준 가격은 prev(직전) 진입가
        newest_id, newest_ts, _newest_entry_price, _tagN = items[-1]
        _prev_id, _prev_ts, prev_entry_price, _prev_tag = items[-2]

        try:
            prev_entry = float(prev_entry_price or 0.0)
        except Exception:
            prev_entry = 0.0

        if prev_entry > 0 and price is not None:
            if side == "LONG":
                # LONG: price가 prev_entry 이상으로 회복 + MA100 위로 ma_eff/2
                ref_ok = price >= prev_entry
                ma_ok = price >= ma100 * (1 + ma_thr_eff / 2)
                sign_ma = "+"
            elif side == "SHORT":
                # SHORT: price가 prev_entry 이하로 회귀 + MA100 아래로 ma_eff/2
                ref_ok = price <= prev_entry
                ma_ok = price <= ma100 * (1 - ma_thr_eff / 2)
                sign_ma = "-"
            else:
                ref_ok = ma_ok = False
                sign_ma = ""

            if ref_ok and ma_ok:
                return {
                    "kind": "EXIT",
                    "mode": "SCALE_OUT",
                    "targets": [newest_id],  # ✅ newest만 청산
                    "anchor_open_signal_id": newest_id,
                    "reasons": [
                        "SCALE_OUT",
                        f"#EXIT {fmt_targets_idx(open_idx, [newest_id])}/{total_n}",
                        "REF=PREV_ENTRY",
                        f"MA100 {sign_ma}{(ma_thr_eff / 2) * 100:.2f}%",
                        f"⏱ new:{fmt_dur_smh_d(newest_elapsed_sec)}",
                        f"CD {scaleout_cooldown_sec}s" if scaleout_cooldown_sec else "",
                    ],
                    "thresholds": {
                        "ma": ma_thr_eff,
                        "exit_easing": exit_easing,
                        "scaleout_cooldown_sec": int(scaleout_cooldown_sec),
                        "x": x, "y": y,
                        "prev_entry_price": float(prev_entry),
                    },
                    "extra": {
                        "scale_out_latest_only": True,
                        "ref_prev_entry_price": float(prev_entry),
                    },
                }

    # INIT_OUT
    if len(items) == 1 and (not _scaleout_on_cooldown()):
        mom = momentum_vs_prev_candle_ohlc(price, prev3_candle) if prev3_candle is not None else None
        if mom is not None:
            mom_thr = float(momentum_threshold)

            if side == "LONG":
                ma_ok = price >= ma100 * (1 + ma_thr_eff / 2)
                mom_ok = mom > mom_thr
                sign_ma = "+"
                sign_mom = "+"
            elif side == "SHORT":
                ma_ok = price <= ma100 * (1 - ma_thr_eff / 2)
                mom_ok = mom < -mom_thr
                sign_ma = "-"
                sign_mom = "-"
            else:
                ma_ok = mom_ok = False
                sign_ma = sign_mom = ""

            if ma_ok and mom_ok:
                only_id, _, _, _tag0 = items[0]
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

    # NEAR_TOUCH
    if band == "NEAR" and touched:
        reasons = [
            "NEAR_TOUCH",
            f"#EXIT {fmt_targets_idx(open_idx, [newest_id])}/{total_n}",
            head,
            band_label,
            f"⏱ old:{fmt_dur_smh_d(oldest_elapsed_sec)} new:{fmt_dur_smh_d(newest_elapsed_sec)}",
        ]
        return {
            "kind": "EXIT",
            "mode": "NEAR_TOUCH",
            "targets": [newest_id],
            "anchor_open_signal_id": newest_id,
            "reasons": reasons,
            "thresholds": {"ma": ma_thr_eff, "exit_easing": exit_easing, "x": x, "y": y},
        }

    return None