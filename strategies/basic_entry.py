# strategies/basic_entry.py
from __future__ import annotations
import time
from typing import List, Optional, Dict, Any

from .basic_utils import (
    Item, Signal,
    _sorted_items, _signal_to_dict,
    easing_from_thr, momentum_vs_prev_candle_ohlc,
)

INIT_WATCH_SEC = 15 * 60  # ✅ INIT 이후 15분간 INIT2/INIT3 감시


def get_long_entry_signal(
    price: float,
    ma100: float,
    prev3_candle: Optional[Dict[str, Any]],
    open_items: List[Item],
    ma_threshold: float = 0.01,
    momentum_threshold: float = 0.001,
    reentry_cooldown_sec: int = 30 * 60,
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
    entry_easing = easing_from_thr(ma_threshold)
    ma_thr_eff = max(0.0, float(ma_threshold) - float(entry_easing))

    # ------------------------------------------------------------
    # ✅ INIT2/INIT3 WATCH
    # - oldest 가 INIT일 때만
    # - INIT 이후 15분 이내
    # - INIT price 기준으로 ma_thr_eff * 1, * 2 닿으면 트리거
    # - items==1 -> INIT2, items==2 -> INIT3
    # ------------------------------------------------------------
    if items:
        # Item = (sid, ts, entry_price, tag)
        oldest_id, oldest_ts, oldest_entry_price, oldest_tag = items[0]

        if str(oldest_tag) == "INIT":
            init_age_sec = max(0, (now_ms - int(oldest_ts)) // 1000)
            if init_age_sec <= INIT_WATCH_SEC:
                init_price = float(oldest_entry_price or 0.0)

                if init_price > 0 and len(items) in (1, 2):
                    k = 1 if len(items) == 1 else 2
                    mode = "INIT2" if k == 1 else "INIT3"

                    trigger_price = init_price * (1 - ma_thr_eff * k)  # LONG: 더 아래로
                    if price <= trigger_price:
                        ma_delta_pct = (price - ma100) / max(ma100, 1e-12) * 100.0
                        s = Signal(
                            ok=True,
                            kind="ENTRY",
                            side="LONG",
                            reasons=[
                                mode,
                                f"#ENTRY {next_no}",
                                f"INIT_PRICE -{(ma_thr_eff * k) * 100:.2f}%",
                                f"⏱ init_age={init_age_sec}s",
                            ],
                            price=price,
                            ma100=ma100,
                            ma_delta_pct=ma_delta_pct,
                            momentum_pct=mom,
                            thresholds={
                                "ma": ma_thr_eff,
                                "momentum": float(momentum_threshold),
                                "entry_easing": entry_easing,
                                "init_watch_sec": INIT_WATCH_SEC,
                                "k": k,
                                "init_price": init_price,
                                "trigger_price": trigger_price,
                            },
                            extra={
                                "is_init_follow": True,
                                "init_follow_k": k,
                                "anchor_init_signal_id": oldest_id,
                                "anchor_init_price": init_price,
                                "init_age_sec": init_age_sec,
                            },
                        )
                        return _signal_to_dict(s)

    # ------------------------------------------------------------
    # ✅ SCALE_IN (기존 로직 그대로)
    # ------------------------------------------------------------
    if items:
        newest_id, newest_ts, newest_entry_price, _newest_tag = items[-1]

        held_sec = max(0, (now_ms - int(newest_ts)) // 1000)
        if held_sec < int(reentry_cooldown_sec):
            return None

        adverse = price < float(newest_entry_price)
        mom_ok = (-mom) > float(momentum_threshold)
        ma_ok = price <= ma100 * (1 - ma_thr_eff / 2)

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
            thresholds={"ma": ma_thr_eff, "momentum": float(momentum_threshold), "entry_easing": entry_easing},
            extra={"is_scale_in": True, "anchor_open_signal_id": newest_id},
        )
        return _signal_to_dict(s)

    # ------------------------------------------------------------
    # ✅ INIT (기존 로직 그대로)
    # ------------------------------------------------------------
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
        thresholds={"ma": ma_thr_eff, "momentum": float(momentum_threshold), "entry_easing": entry_easing},
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
    reentry_cooldown_sec: int = 30 * 60,
) -> Optional[Dict[str, Any]]:
    if price is None or ma100 is None or prev3_candle is None:
        return None

    mom = momentum_vs_prev_candle_ohlc(price, prev3_candle)
    if mom is None:
        return None

    entry_easing = easing_from_thr(ma_threshold)
    ma_thr_eff = max(0.0, float(ma_threshold) - float(entry_easing))

    items = _sorted_items(open_items)

    MAX_OPEN = 4
    if len(items) >= MAX_OPEN:
        return None

    next_no = len(items) + 1
    now_ms = int(time.time() * 1000)

    # ------------------------------------------------------------
    # ✅ INIT2/INIT3 WATCH (SHORT)
    # ------------------------------------------------------------
    if items:
        oldest_id, oldest_ts, oldest_entry_price, oldest_tag = items[0]

        if str(oldest_tag) == "INIT":
            init_age_sec = max(0, (now_ms - int(oldest_ts)) // 1000)
            if init_age_sec <= INIT_WATCH_SEC:
                init_price = float(oldest_entry_price or 0.0)

                if init_price > 0 and len(items) in (1, 2):
                    k = 1 if len(items) == 1 else 2
                    mode = "INIT2" if k == 1 else "INIT3"

                    trigger_price = init_price * (1 + ma_thr_eff * k)  # SHORT: 더 위로
                    if price >= trigger_price:
                        ma_delta_pct = (price - ma100) / max(ma100, 1e-12) * 100.0
                        s = Signal(
                            ok=True,
                            kind="ENTRY",
                            side="SHORT",
                            reasons=[
                                mode,
                                f"#ENTRY {next_no}",
                                f"INIT_PRICE +{(ma_thr_eff * k) * 100:.2f}%",
                                f"⏱ init_age={init_age_sec}s",
                            ],
                            price=price,
                            ma100=ma100,
                            ma_delta_pct=ma_delta_pct,
                            momentum_pct=mom,
                            thresholds={
                                "ma": ma_thr_eff,
                                "momentum": float(momentum_threshold),
                                "entry_easing": entry_easing,
                                "init_watch_sec": INIT_WATCH_SEC,
                                "k": k,
                                "init_price": init_price,
                                "trigger_price": trigger_price,
                            },
                            extra={
                                "is_init_follow": True,
                                "init_follow_k": k,
                                "anchor_init_signal_id": oldest_id,
                                "anchor_init_price": init_price,
                                "init_age_sec": init_age_sec,
                            },
                        )
                        return _signal_to_dict(s)

    # ------------------------------------------------------------
    # ✅ SCALE_IN (기존 로직 그대로)
    # ------------------------------------------------------------
    if items:
        newest_id, newest_ts, newest_entry_price, _newest_tag = items[-1]

        held_sec = max(0, (now_ms - int(newest_ts)) // 1000)
        if held_sec < int(reentry_cooldown_sec):
            return None

        adverse = price > float(newest_entry_price)
        mom_ok = mom > float(momentum_threshold)
        ma_ok = price >= ma100 * (1 + ma_thr_eff / 2)

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
            thresholds={"ma": ma_thr_eff, "momentum": float(momentum_threshold), "entry_easing": entry_easing},
            extra={"is_scale_in": True, "anchor_open_signal_id": newest_id},
        )
        return _signal_to_dict(s)

    # ------------------------------------------------------------
    # ✅ INIT (기존 로직 그대로)
    # ------------------------------------------------------------
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
        thresholds={"ma": ma_thr_eff, "momentum": float(momentum_threshold), "entry_easing": entry_easing},
        extra={"is_scale_in": False},
    )
    return _signal_to_dict(s)