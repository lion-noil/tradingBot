# strategies/basic_utils.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Tuple, Optional, Dict, Any

# (signal_id, ts_ms, entry_price, tag)
Item = Tuple[str, int, float, str]

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
    for i, (sid, _ts, _ep, _tag) in enumerate(sorted_items or [], start=1):
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


def easing_from_thr(thr: float, *, div: float = 15.0, lo: float = 0.0, hi: float = 0.003) -> float:
    """
    easing = thr / 15
    thr=0.006(0.6%) -> 0.0002(0.04%)
    thr=0.03(3.0%)  -> 0.001 (0.20%)
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