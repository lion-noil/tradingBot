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
MAX_OPEN = 10
BOOST_ENTRY_WINDOW_SEC = 15 * 60  # anchor 이후 15분 동안 BOOST 진입 가능
BOOST_MIN_DELAY_SEC = 2 * 60  # anchor 발생 후 최소 2분 뒤부터 BOOST 가능
BOOST_INTERVAL_SEC = 5 * 60  # BOOST끼리는 최소 5분 간격
BOOST_MAX_PER_ANCHOR = 2  # anchor 하나당 BOOST 최대 2번

BOOST_FROM_INIT = "BOOST_FROM_INIT"
BOOST_FROM_SCALE_IN = "BOOST_FROM_SCALE_IN"

BOOST_ANCHOR_TAGS = {"INIT", "SCALE_IN"}
BOOST_TAGS = {BOOST_FROM_INIT, BOOST_FROM_SCALE_IN}


def _find_latest_boost_anchor(items: List[Item]):
    """
    가장 최근 INIT 또는 SCALE_IN을 BOOST anchor로 찾는다.
    Item = (sid, ts, entry_price, tag)
    """
    for sid, ts, entry_price, tag in reversed(items):
        if str(tag) in BOOST_ANCHOR_TAGS:
            return sid, ts, entry_price, str(tag)
    return None


def _boost_tag_from_anchor(anchor_tag: str) -> str:
    if anchor_tag == "INIT":
        return BOOST_FROM_INIT
    if anchor_tag == "SCALE_IN":
        return BOOST_FROM_SCALE_IN
    return "BOOST"


def _boost_items_after_anchor(items: List[Item], anchor_ts: int, boost_tag: str) -> List[Item]:
    out = []
    for item in items:
        sid, ts, entry_price, tag = item
        if int(ts) > int(anchor_ts) and str(tag) == boost_tag:
            out.append(item)
    return out


def get_long_entry_signal(
        price: float,
        ma100: float,
        prev3_candle: Optional[Dict[str, Any]],
        open_items: List[Item],
        boost_attempts_by_anchor: Optional[Dict[str, int]] = None,
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
    # ✅ BOOST_ENTRY (LONG)
    # - INIT 또는 SCALE_IN 이후 15분 내
    # - anchor 후 최소 2분 뒤부터 가능
    # - anchor 하나당 최대 2번
    # - BOOST끼리는 최소 5분 간격
    # - 조건: 하락 모멘텀 만족 또는 anchor보다 불리한 가격
    # ------------------------------------------------------------
    if items:
        anchor = _find_latest_boost_anchor(items)

        if anchor is not None:
            anchor_id, anchor_ts, anchor_entry_price, anchor_tag = anchor
            anchor_age_sec = max(0, (now_ms - int(anchor_ts)) // 1000)

            try:
                anchor_entry = float(anchor_entry_price or 0.0)
            except Exception:
                anchor_entry = 0.0

            boost_tag = _boost_tag_from_anchor(anchor_tag)
            boost_items = _boost_items_after_anchor(items, int(anchor_ts), boost_tag)

            # 현재 열려 있는 BOOST 수
            open_boost_count = len(boost_items)

            # 이 anchor로 지금까지 발생한 BOOST 누적 수
            anchor_key = str(anchor_id)
            lifetime_boost_count = int(
                (boost_attempts_by_anchor or {}).get(anchor_key, open_boost_count)
            )

            # 마지막 BOOST 이후 5분 간격 확인
            last_boost_elapsed_sec = None
            interval_ok = True
            if boost_items:
                _last_boost_id, last_boost_ts, _last_boost_price, _last_boost_tag = boost_items[-1]
                last_boost_elapsed_sec = max(0, (now_ms - int(last_boost_ts)) // 1000)
                interval_ok = last_boost_elapsed_sec >= BOOST_INTERVAL_SEC

            window_ok = (
                    anchor_entry > 0
                    and BOOST_MIN_DELAY_SEC <= anchor_age_sec <= BOOST_ENTRY_WINDOW_SEC
                    and lifetime_boost_count < BOOST_MAX_PER_ANCHOR
                    and interval_ok
            )

            # LONG BOOST 조건
            # 1. 하락 모멘텀 만족
            # 2. anchor 진입가보다 불리한 위치
            mom_ok = (-mom) > float(momentum_threshold)
            adverse_to_anchor = price <= anchor_entry

            if window_ok and (mom_ok or adverse_to_anchor):
                ma_delta_pct = (price - ma100) / max(ma100, 1e-12) * 100.0
                mode = boost_tag

                s = Signal(
                    ok=True,
                    kind="ENTRY",
                    side="LONG",
                    reasons=[
                        mode,
                        f"#ENTRY {next_no}",
                        f"ANCHOR={anchor_tag}",
                        f"BOOST {lifetime_boost_count + 1}/{BOOST_MAX_PER_ANCHOR}",
                        f"⏱ anchor_age={anchor_age_sec}s",
                        f"COND={'MOM' if mom_ok else 'ADVERSE'}",
                    ],
                    price=price,
                    ma100=ma100,
                    ma_delta_pct=ma_delta_pct,
                    momentum_pct=mom,
                    thresholds={
                        "ma": ma_thr_eff,
                        "momentum": float(momentum_threshold),
                        "entry_easing": entry_easing,
                        "boost_entry_window_sec": BOOST_ENTRY_WINDOW_SEC,
                        "boost_min_delay_sec": BOOST_MIN_DELAY_SEC,
                        "boost_interval_sec": BOOST_INTERVAL_SEC,
                        "boost_max_per_anchor": BOOST_MAX_PER_ANCHOR,
                        "anchor_entry_price": anchor_entry,
                        "anchor_age_sec": anchor_age_sec,
                        "open_boost_count": open_boost_count,
                        "lifetime_boost_count": lifetime_boost_count,
                    },
                    extra={
                        "is_boost": True,
                        "boost_tag": boost_tag,
                        "anchor_signal_id": anchor_id,
                        "anchor_tag": anchor_tag,
                        "anchor_entry_price": anchor_entry,
                        "anchor_age_sec": anchor_age_sec,
                        "open_boost_count_before": open_boost_count,
                        "boost_count_before": lifetime_boost_count,
                        "lifetime_boost_count_before": lifetime_boost_count,
                        "last_boost_elapsed_sec": last_boost_elapsed_sec,
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
        boost_attempts_by_anchor: Optional[Dict[str, int]] = None,
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
    # ✅ BOOST_ENTRY (SHORT)
    # - INIT 또는 SCALE_IN 이후 15분 내
    # - anchor 후 최소 2분 뒤부터 가능
    # - anchor 하나당 최대 2번
    # - BOOST끼리는 최소 5분 간격
    # - 조건: 상승 모멘텀 만족 또는 anchor보다 불리한 가격
    # ------------------------------------------------------------
    if items:
        anchor = _find_latest_boost_anchor(items)

        if anchor is not None:
            anchor_id, anchor_ts, anchor_entry_price, anchor_tag = anchor
            anchor_age_sec = max(0, (now_ms - int(anchor_ts)) // 1000)

            try:
                anchor_entry = float(anchor_entry_price or 0.0)
            except Exception:
                anchor_entry = 0.0

            boost_tag = _boost_tag_from_anchor(anchor_tag)
            boost_items = _boost_items_after_anchor(items, int(anchor_ts), boost_tag)

            # 현재 열려 있는 BOOST 수
            open_boost_count = len(boost_items)

            # 이 anchor로 지금까지 발생한 BOOST 누적 수
            anchor_key = str(anchor_id)
            lifetime_boost_count = int(
                (boost_attempts_by_anchor or {}).get(anchor_key, open_boost_count)
            )

            # 마지막 BOOST 이후 5분 간격 확인
            last_boost_elapsed_sec = None
            interval_ok = True
            if boost_items:
                _last_boost_id, last_boost_ts, _last_boost_price, _last_boost_tag = boost_items[-1]
                last_boost_elapsed_sec = max(0, (now_ms - int(last_boost_ts)) // 1000)
                interval_ok = last_boost_elapsed_sec >= BOOST_INTERVAL_SEC

            window_ok = (
                    anchor_entry > 0
                    and BOOST_MIN_DELAY_SEC <= anchor_age_sec <= BOOST_ENTRY_WINDOW_SEC
                    and lifetime_boost_count < BOOST_MAX_PER_ANCHOR
                    and interval_ok
            )

            # SHORT BOOST 조건
            # 1. 상승 모멘텀 만족
            # 2. anchor 진입가보다 불리한 위치
            mom_ok = mom > float(momentum_threshold)
            adverse_to_anchor = price >= anchor_entry

            if window_ok and (mom_ok or adverse_to_anchor):
                ma_delta_pct = (price - ma100) / max(ma100, 1e-12) * 100.0
                mode = boost_tag

                s = Signal(
                    ok=True,
                    kind="ENTRY",
                    side="SHORT",
                    reasons=[
                        mode,
                        f"#ENTRY {next_no}",
                        f"ANCHOR={anchor_tag}",
                        f"BOOST {lifetime_boost_count + 1}/{BOOST_MAX_PER_ANCHOR}",
                        f"⏱ anchor_age={anchor_age_sec}s",
                        f"COND={'MOM' if mom_ok else 'ADVERSE'}",
                    ],
                    price=price,
                    ma100=ma100,
                    ma_delta_pct=ma_delta_pct,
                    momentum_pct=mom,
                    thresholds={
                        "ma": ma_thr_eff,
                        "momentum": float(momentum_threshold),
                        "entry_easing": entry_easing,
                        "boost_entry_window_sec": BOOST_ENTRY_WINDOW_SEC,
                        "boost_min_delay_sec": BOOST_MIN_DELAY_SEC,
                        "boost_interval_sec": BOOST_INTERVAL_SEC,
                        "boost_max_per_anchor": BOOST_MAX_PER_ANCHOR,
                        "anchor_entry_price": anchor_entry,
                        "anchor_age_sec": anchor_age_sec,
                        "open_boost_count": open_boost_count,
                        "lifetime_boost_count": lifetime_boost_count,
                    },
                    extra={
                        "is_boost": True,
                        "boost_tag": boost_tag,
                        "anchor_signal_id": anchor_id,
                        "anchor_tag": anchor_tag,
                        "anchor_entry_price": anchor_entry,
                        "anchor_age_sec": anchor_age_sec,
                        "open_boost_count_before": open_boost_count,
                        "boost_count_before": lifetime_boost_count,
                        "lifetime_boost_count_before": lifetime_boost_count,
                        "last_boost_elapsed_sec": last_boost_elapsed_sec,
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
