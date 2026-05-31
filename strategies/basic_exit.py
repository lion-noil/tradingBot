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


BOOST_FROM_INIT = "BOOST_FROM_INIT"
BOOST_FROM_SCALE_IN = "BOOST_FROM_SCALE_IN"
BOOST_TAGS = {BOOST_FROM_INIT, BOOST_FROM_SCALE_IN}

BOOST_FAIL_SEC = 20 * 60          # 20분 지나면 실패 BOOST로 간주
BOOST_TIMEOUT_SEC = 30 * 60       # 30분 지나면 BOOST 강제 종료

BOOST_ALL_AVG_TP_PCT = 0.003      # 전체 평균 +0.3%
BOOST_ANCHOR_AVG_TP_PCT = 0.005   # anchor + boost 평균 +0.5%

def _is_boost_tag(tag: str) -> bool:
    return str(tag) in BOOST_TAGS


def _anchor_tag_for_boost(boost_tag: str) -> str:
    if boost_tag == BOOST_FROM_INIT:
        return "INIT"
    if boost_tag == BOOST_FROM_SCALE_IN:
        return "SCALE_IN"
    return ""


def _avg_entry(items: List[Item]) -> float:
    vals = []
    for _sid, _ts, ep, _tag in items:
        try:
            v = float(ep or 0.0)
            if v > 0:
                vals.append(v)
        except Exception:
            pass
    return sum(vals) / len(vals) if vals else 0.0


def _find_boost_groups(items: List[Item]):
    """
    각 BOOST를 자기보다 이전에 있는 가장 가까운 anchor에 묶는다.
    anchor별 BOOST 그룹을 모두 반환한다.
    """
    groups = []

    for boost_tag in (BOOST_FROM_INIT, BOOST_FROM_SCALE_IN):
        anchor_tag = _anchor_tag_for_boost(boost_tag)

        anchors = []
        boosts = []

        for item in items:
            sid, ts, ep, tag = item
            if str(tag) == anchor_tag:
                anchors.append(item)
            elif str(tag) == boost_tag:
                boosts.append(item)

        if not anchors or not boosts:
            continue

        # anchor별 그룹 생성
        for i, anchor in enumerate(anchors):
            _aid, anchor_ts, _aep, _atag = anchor

            next_anchor_ts = None
            if i + 1 < len(anchors):
                _nid, next_ts, _nep, _ntag = anchors[i + 1]
                next_anchor_ts = int(next_ts)

            group_boosts = []
            for b in boosts:
                _bid, b_ts, _bep, _btag = b
                b_ts_i = int(b_ts)

                # anchor 이후이고, 다음 같은 종류 anchor 전까지의 BOOST만 묶음
                if b_ts_i > int(anchor_ts) and (next_anchor_ts is None or b_ts_i < next_anchor_ts):
                    group_boosts.append(b)

            if group_boosts:
                groups.append({
                    "boost_tag": boost_tag,
                    "anchor_tag": anchor_tag,
                    "anchor": anchor,
                    "boost_items": group_boosts,
                })

    return groups

def get_exit_signal(
        *,
        side: str,
        price: float,
        ma100: float,
        prev3_candle: Optional[Dict[str, Any]],
        open_items: List[Item],

        ma_threshold: float = 0.01,
        time_limit_sec: int = None,
        near_touch_window_sec: int = 30 * 60,

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
            "reasons": ["TIME_LIMIT", "CLOSE_OLDEST_ONLY", f"#EXIT {fmt_targets_idx(open_idx, [oldest_id])}/{total_n}",
                        age_label],
            "thresholds": {"ma": ma_thr_eff, "exit_easing": exit_easing, "x": x, "y": y},
        }

    # ------------------------------------------------------------
    # STOP_LOSS / TAKE_PROFIT(age: oldest)
    # ------------------------------------------------------------
    oldest_id, oldest_ts, oldest_entry_price, _tag0 = items[0]
    try:
        oldest_entry = float(oldest_entry_price or 0.0)
    except Exception:
        oldest_entry = 0.0

    # SL: 항상 ma_thr_eff * 7
    age_band_sl, age_factor_sl = ("ALL", 7.0)

    # TP: 1시간 전에는 ma_thr_eff * 2, 1시간 후에는 ma_thr_eff * 1
    if oldest_elapsed_sec < 60 * 60:
        age_band_tp, age_factor_tp = ("_1H", 2.0)
    else:
        age_band_tp, age_factor_tp = ("1H_", 1.0)

    if price is not None:
        sl_pct = float(ma_thr_eff) * float(age_factor_sl)
        tp_pct = float(ma_thr_eff) * float(age_factor_tp)

        # -------------------------
        # SL: oldest 기준
        # -------------------------
        if oldest_entry > 0:
            if side == "LONG":
                pnl_old_pct = (float(price) - oldest_entry) / oldest_entry
            elif side == "SHORT":
                pnl_old_pct = (oldest_entry - float(price)) / oldest_entry
            else:
                pnl_old_pct = 0.0

            held_old_txt = fmt_dur_smh_d(oldest_elapsed_sec)

            if pnl_old_pct <= -sl_pct:
                return {
                    "kind": "EXIT",
                    "mode": "STOP_LOSS",
                    "targets": [oldest_id],
                    "anchor_open_signal_id": oldest_id,
                    "reasons": [
                        f"SL({age_band_sl})",
                        "CLOSE_OLDEST_ONLY",
                        f"#EXIT {fmt_targets_idx(open_idx, [oldest_id])}/{total_n}",
                        f"held= {held_old_txt}",
                        f"OLDEST_PNL -{fmt_pct2(sl_pct)}",
                        f"pnl={fmt_pct2(pnl_old_pct)}",
                    ],
                    "thresholds": {
                        "sl_pct": sl_pct,
                        "tp_pct": tp_pct,
                        "age_factor_sl": float(age_factor_sl),
                        "age_band_sl": age_band_sl,
                        "age_factor_tp": float(age_factor_tp),
                        "age_band_tp": age_band_tp,
                        "held_sec": int(oldest_elapsed_sec),
                        "oldest_entry_price": oldest_entry,
                        "ma": ma_thr_eff,
                        "exit_easing": exit_easing,
                        "x": x, "y": y,
                    },
                    "extra": {
                        "oldest_entry_price": oldest_entry,
                        "pnl_pct": pnl_old_pct,
                        "close_oldest_only": True,
                        "age_factor_sl": float(age_factor_sl),
                        "age_band_sl": age_band_sl,
                        "held_sec": int(oldest_elapsed_sec),
                        "sl_tp_tag": f"SL({age_band_sl})",
                    },
                }

        # ------------------------------------------------------------
        # BOOST EXIT
        # ------------------------------------------------------------
        # BOOST는 INIT/SCALE_IN anchor 이후 생긴 단기 포지션.
        # 청산 대상은 anchor 제외, BOOST 포지션만.
        if price is not None:
            boost_groups = _find_boost_groups(items)

            for g in boost_groups:
                boost_tag = g["boost_tag"]
                anchor_tag = g["anchor_tag"]
                anchor_item = g["anchor"]
                boost_items = g["boost_items"]

                anchor_id, anchor_ts, anchor_entry_price, _anchor_tag = anchor_item
                anchor_age_sec = max(0, (now_ms - int(anchor_ts)) // 1000)

                target_ids = [sid for (sid, _ts, _ep, _tag) in boost_items]
                if not target_ids:
                    continue

                all_avg = _avg_entry(items)
                anchor_boost_avg = _avg_entry([anchor_item] + boost_items)

                if all_avg <= 0 or anchor_boost_avg <= 0:
                    continue

                # 1) BOOST_TIMEOUT: anchor 후 30분 경과 → BOOST 전부 강제청산
                if anchor_age_sec >= BOOST_TIMEOUT_SEC:
                    return {
                        "kind": "EXIT",
                        "mode": "BOOST_TIMEOUT",
                        "targets": target_ids,
                        "anchor_open_signal_id": target_ids[0],
                        "reasons": [
                            "BOOST_TIMEOUT",
                            f"ANCHOR={anchor_tag}",
                            f"BOOST={boost_tag}",
                            f"CLOSE_BOOST_{len(target_ids)}",
                            f"#EXIT {fmt_targets_idx(open_idx, target_ids)}/{total_n}",
                            f"⏱ anchor_age={fmt_dur_smh_d(anchor_age_sec)}",
                        ],
                        "thresholds": {
                            "boost_timeout_sec": BOOST_TIMEOUT_SEC,
                            "anchor_age_sec": int(anchor_age_sec),
                            "anchor_boost_avg_entry": float(anchor_boost_avg),
                            "all_avg_entry": float(all_avg),
                            "ma": ma_thr_eff,
                            "exit_easing": exit_easing,
                            "x": x,
                            "y": y,
                        },
                        "extra": {
                            "boost_exit": True,
                            "boost_tag": boost_tag,
                            "anchor_tag": anchor_tag,
                            "anchor_signal_id": anchor_id,
                            "close_boost_n": len(target_ids),
                        },
                    }

                # 2) BOOST_FAIL_EXIT: 20분 이후, anchor+boost 평균 이상이면 BOOST 청산
                if anchor_age_sec >= BOOST_FAIL_SEC:
                    if side == "LONG":
                        fail_hit = price >= anchor_boost_avg
                        fail_sign = ">="
                    elif side == "SHORT":
                        fail_hit = price <= anchor_boost_avg
                        fail_sign = "<="
                    else:
                        fail_hit = False
                        fail_sign = ""

                    if fail_hit:
                        return {
                            "kind": "EXIT",
                            "mode": "BOOST_FAIL_EXIT",
                            "targets": target_ids,
                            "anchor_open_signal_id": target_ids[0],
                            "reasons": [
                                "BOOST_FAIL_EXIT",
                                f"ANCHOR={anchor_tag}",
                                f"BOOST={boost_tag}",
                                f"CLOSE_BOOST_{len(target_ids)}",
                                f"#EXIT {fmt_targets_idx(open_idx, target_ids)}/{total_n}",
                                f"PRICE {fail_sign} ANCHOR_BOOST_AVG",
                                f"⏱ anchor_age={fmt_dur_smh_d(anchor_age_sec)}",
                            ],
                            "thresholds": {
                                "boost_fail_sec": BOOST_FAIL_SEC,
                                "anchor_age_sec": int(anchor_age_sec),
                                "anchor_boost_avg_entry": float(anchor_boost_avg),
                                "all_avg_entry": float(all_avg),
                                "ma": ma_thr_eff,
                                "exit_easing": exit_easing,
                                "x": x,
                                "y": y,
                            },
                            "extra": {
                                "boost_exit": True,
                                "boost_tag": boost_tag,
                                "anchor_tag": anchor_tag,
                                "anchor_signal_id": anchor_id,
                                "close_boost_n": len(target_ids),
                            },
                        }

                # 3) BOOST_TP: 전체 평균 +0.3% 또는 anchor+boost 평균 +0.5%
                if side == "LONG":
                    all_avg_hit = price >= all_avg * (1 + BOOST_ALL_AVG_TP_PCT)
                    anchor_avg_hit = price >= anchor_boost_avg * (1 + BOOST_ANCHOR_AVG_TP_PCT)
                    profit_sign = "+"
                elif side == "SHORT":
                    all_avg_hit = price <= all_avg * (1 - BOOST_ALL_AVG_TP_PCT)
                    anchor_avg_hit = price <= anchor_boost_avg * (1 - BOOST_ANCHOR_AVG_TP_PCT)
                    profit_sign = "-"
                else:
                    all_avg_hit = False
                    anchor_avg_hit = False
                    profit_sign = ""

                if all_avg_hit or anchor_avg_hit:
                    mode = "BOOST_TP_ALL_AVG" if all_avg_hit else "BOOST_TP_ANCHOR_AVG"
                    hit_label = (
                        f"ALL_AVG {profit_sign}{BOOST_ALL_AVG_TP_PCT * 100:.2f}%"
                        if all_avg_hit
                        else f"ANCHOR_BOOST_AVG {profit_sign}{BOOST_ANCHOR_AVG_TP_PCT * 100:.2f}%"
                    )

                    return {
                        "kind": "EXIT",
                        "mode": mode,
                        "targets": target_ids,
                        "anchor_open_signal_id": target_ids[0],
                        "reasons": [
                            mode,
                            f"ANCHOR={anchor_tag}",
                            f"BOOST={boost_tag}",
                            f"CLOSE_BOOST_{len(target_ids)}",
                            f"#EXIT {fmt_targets_idx(open_idx, target_ids)}/{total_n}",
                            hit_label,
                            f"⏱ anchor_age={fmt_dur_smh_d(anchor_age_sec)}",
                        ],
                        "thresholds": {
                            "boost_all_avg_tp_pct": BOOST_ALL_AVG_TP_PCT,
                            "boost_anchor_avg_tp_pct": BOOST_ANCHOR_AVG_TP_PCT,
                            "anchor_age_sec": int(anchor_age_sec),
                            "anchor_boost_avg_entry": float(anchor_boost_avg),
                            "all_avg_entry": float(all_avg),
                            "ma": ma_thr_eff,
                            "exit_easing": exit_easing,
                            "x": x,
                            "y": y,
                        },
                        "extra": {
                            "boost_exit": True,
                            "boost_tag": boost_tag,
                            "anchor_tag": anchor_tag,
                            "anchor_signal_id": anchor_id,
                            "close_boost_n": len(target_ids),
                            "all_avg_hit": bool(all_avg_hit),
                            "anchor_avg_hit": bool(anchor_avg_hit),
                        },
                    }

        # -------------------------
        # TP: oldest 기준
        # -------------------------
        if oldest_entry > 0 and pnl_old_pct >= tp_pct:
            return {
                "kind": "EXIT",
                "mode": "TAKE_PROFIT",
                "targets": [oldest_id],
                "anchor_open_signal_id": oldest_id,
                "reasons": [
                    f"TP({age_band_tp})",
                    "CLOSE_OLDEST_ONLY",
                    f"#EXIT {fmt_targets_idx(open_idx, [oldest_id])}/{total_n}",
                    f"held={held_old_txt}",
                    f"OLDEST_PNL +{fmt_pct2(tp_pct)}",
                    f"pnl={fmt_pct2(pnl_old_pct)}",
                ],
                "thresholds": {
                    "sl_pct": sl_pct,
                    "tp_pct": tp_pct,
                    "age_factor_sl": float(age_factor_sl),
                    "age_band_sl": age_band_sl,
                    "age_factor_tp": float(age_factor_tp),
                    "age_band_tp": age_band_tp,
                    "held_sec": int(oldest_elapsed_sec),
                    "oldest_entry_price": oldest_entry,
                    "ma": ma_thr_eff,
                    "exit_easing": exit_easing,
                    "x": x, "y": y,
                },
                "extra": {
                    "oldest_entry_price": oldest_entry,
                    "pnl_pct": pnl_old_pct,
                    "close_oldest_only": True,
                    "age_factor_tp": float(age_factor_tp),
                    "age_band_tp": age_band_tp,
                    "held_sec": int(oldest_elapsed_sec),
                    "sl_tp_tag": f"TP({age_band_tp})",
                },
            }

    # ------------------------------------------------------------
    # RISK_CONTROL
    # ------------------------------------------------------------
    # 포지션 평균가를 기준으로 트리거됨.
    # 5개부터 발동, 모두 평균가 기준 0.3% 수익권이면 발동
    PROFIT_TAKE_PCT = 0.003  # 0.3%

    if len(items) >= 5:
        avg_entry = sum(float(ep) for (_sid, _ts, ep, _tag) in items) / max(len(items), 1)
        n = len(items)

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
            if n == 5:
                close_n = 1
                mode = "RISK_CONTROL_5_CLOSE_1"
            elif n == 6:
                close_n = 2
                mode = "RISK_CONTROL_6_CLOSE_2"
            elif n == 7:
                close_n = 3
                mode = "RISK_CONTROL_7_CLOSE_3"
            elif n == 8:
                close_n = 4
                mode = "RISK_CONTROL_8_CLOSE_4"
            elif n == 9:
                close_n = 4
                mode = "RISK_CONTROL_9_CLOSE_4"
            else:  # n >= 10
                close_n = 5
                mode = "RISK_CONTROL_10_CLOSE_5"

            target_ids = [sid for (sid, _ts, _ep, _tag) in items[:close_n]]

            return {
                "kind": "EXIT",
                "mode": mode,
                "targets": target_ids,
                "anchor_open_signal_id": target_ids[0],
                "reasons": [
                    "RISK_CONTROL",
                    f"CLOSE_OLDEST_{close_n}",
                    f"#EXIT {fmt_targets_idx(open_idx, target_ids)}/{total_n}",
                    f"AVG_ENTRY {profit_sign}{PROFIT_TAKE_PCT * 100:.2f}%",
                ],
                "thresholds": {
                    "profit_take_pct": PROFIT_TAKE_PCT,
                    "avg_entry_price": float(avg_entry),
                    "close_n": int(close_n),
                    "x": x,
                    "y": y,
                    "ma": ma_thr_eff,
                    "exit_easing": exit_easing,
                },
                "extra": {
                    "avg_entry_price": float(avg_entry),
                    "profit_take_pct": PROFIT_TAKE_PCT,
                    "risk_control": True,
                    "close_oldest_n": int(close_n),
                },
            }

    # ------------------------------------------------------------
    # touch band
    # ------------------------------------------------------------
    newest_id, newest_ts, newest_entry_price, _tagN = items[-1]
    try:
        newest_entry = float(newest_entry_price or 0.0)
    except Exception:
        newest_entry = 0.0

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

    cond1 = False
    cond2 = False
    if side == "LONG":
        if band == "NEAR":
            cond1 = price >= ma100 * (1 + trigger_pct)  # 기존 MA 기준
            cond2 = price >= newest_entry * (1 + ma_thr_eff * 0.7)  # 70% 기준
            touched = cond1 or cond2
        else:
            touched = price >= ma100 * (1 + trigger_pct)


    elif side == "SHORT":
        if band == "NEAR":
            cond1 = price <= ma100 * (1 - trigger_pct)
            cond2 = price <= newest_entry * (1 - ma_thr_eff * 0.7)
            touched = cond1 or cond2
        else:
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
    # 포지션 1개, 기대수익/2, momentum
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
