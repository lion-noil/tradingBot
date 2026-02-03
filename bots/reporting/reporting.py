# bots/reporting/reporting.py
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional, Tuple, Callable, List, Union
import re

# enabled(기존) 헤더: [SYM] 👀 ma_thr(0.50%) ...
_HEADER_MA_RE = re.compile(
    r"^\[(?P<sym>[A-Z0-9]+)\]\s+(?P<emoji>[📈📉👀—])\s+ma_thr\(\s*(?P<thr>[0-9.]+)\s*%\s*\)"
)

# disabled 헤더: [SYM] 🚫 disabled (...)
_HEADER_DISABLED_RE = re.compile(
    r"^\[(?P<sym>[A-Z0-9]+)\]\s+🚫\s+disabled(?:\s+\(thr\((?P<thr>[0-9.]+)\%\))?"
)

KST = timezone(timedelta(hours=9))

_POS_RE = re.compile(
    r"^\s*-\s*포지션:\s*(?P<side>LONG|SHORT)\s*\(\s*(?P<qty>\d+(?:\.\d+)?)\s*,\s*[^,]+,\s*(?P<pct>[+\-]?\d+\.\d+)%"
)


def make_status_line(
    symbol: str,
    jump_state: Dict[str, Dict[str, Any]],
    ma_threshold: Dict[str, Optional[float]],
    now_ma100: Dict[str, Optional[float]],
    get_price: Callable[[str], Optional[float]],
    ma_check_enabled: Optional[Dict[str, bool]] = None,
    min_ma_threshold: Optional[Union[Dict[str, Optional[float]], float]] = None,
) -> str:
    js = (jump_state or {}).get(symbol, {})
    state = js.get("state")
    min_dt = js.get("min_dt")
    max_dt = js.get("max_dt")

    price = get_price(symbol)
    ma = now_ma100.get(symbol)
    diff_pct = (
        (price - ma) / ma * 100.0
        if (price is not None and ma not in (None, 0))
        else None
    )

    emoji = "📈" if state == "UP" else ("📉" if state == "DOWN" else "👀")

    thr = ma_threshold.get(symbol)  # None 유지
    thr_pct = (float(thr) * 100.0) if (thr is not None) else None

    enabled = True
    if ma_check_enabled is not None:
        enabled = bool(ma_check_enabled.get(symbol, True))

    # min_thr
    min_thr = None
    if min_ma_threshold is not None:
        min_thr = (
            min_ma_threshold.get(symbol)
            if isinstance(min_ma_threshold, dict)
            else float(min_ma_threshold)
        )
    min_thr_pct = (float(min_thr) * 100.0) if (min_thr is not None) else None

    parts: List[str] = []

    if not enabled:
        if thr is None:
            if min_thr_pct is not None:
                parts.append(f"🚫 disabled (thr(None) < min({min_thr_pct:.2f}%))")
            else:
                parts.append("🚫 disabled")
        else:
            if min_thr_pct is not None:
                parts.append(
                    f"🚫 disabled (thr({thr_pct:.2f}%) < min({min_thr_pct:.2f}%))"
                )
            else:
                parts.append(f"🚫 disabled (thr({thr_pct:.2f}%))")
    else:
        if thr_pct is not None:
            parts.append(f"{emoji} ma_thr({thr_pct:.2f}%)")
        else:
            parts.append(f"{emoji} ma_thr(-)")

    if price is not None:
        parts.append(f"P={price:.2f}")
    if ma is not None:
        parts.append(f"MA100={ma:.2f}")
    if diff_pct is not None:
        parts.append(f"ΔP/MA={diff_pct:+.2f}%")
    if min_dt is not None and max_dt is not None:
        parts.append(f"Δt={min_dt:.3f}~{max_dt:.3f}s")

    return f"[{symbol}] " + " ".join(parts)


def build_market_status_log(
    symbols: List[str],
    jump_state: Dict[str, Dict[str, Any]],
    ma_threshold: Dict[str, Optional[float]],
    now_ma100: Dict[str, Optional[float]],
    get_price: Callable[[str], Optional[float]],
    ma_check_enabled: Optional[Dict[str, bool]] = None,
    min_ma_threshold: Optional[Union[Dict[str, Optional[float]], float]] = None,
) -> str:
    lines: List[str] = ["\n📡 MARKET STATUS"]
    for sym in symbols:
        lines.append(
            make_status_line(
                sym,
                jump_state,
                ma_threshold,
                now_ma100,
                get_price,
                ma_check_enabled=ma_check_enabled,
                min_ma_threshold=min_ma_threshold,
            )
        )
    return "\n".join(lines).rstrip()

def extract_market_status_summary(
    text: str,
    fallback_ma_threshold_pct: Optional[Union[Dict[str, Optional[float]], float]] = None,
) -> Dict[str, Any]:
    summary: Dict[str, Any] = {}
    cur_sym: Optional[str] = None

    for raw in text.splitlines():
        line = raw.strip()

        md = _HEADER_DISABLED_RE.match(line)
        if md:
            cur_sym = md.group("sym")
            summary.setdefault(cur_sym, {"jump": "—", "enabled": None, "ma_thr": None})
            summary[cur_sym]["enabled"] = False
            continue

        m = _HEADER_MA_RE.match(line)
        if m:
            cur_sym = m.group("sym")
            summary.setdefault(cur_sym, {"jump": m.group("emoji"), "enabled": None, "ma_thr": None})
            summary[cur_sym]["enabled"] = True
            summary[cur_sym]["jump"] = m.group("emoji")
            try:
                summary[cur_sym]["ma_thr"] = float(m.group("thr"))
            except Exception:
                summary[cur_sym]["ma_thr"] = None
            continue

    return summary


def should_log_update_market(
    old_summary: Optional[Dict[str, Any]],
    new_summary: Dict[str, Any],
) -> Tuple[bool, Optional[str]]:
    if old_summary is None:
        return True, "initial snapshot"

    def _as_float(x: Any) -> Optional[float]:
        try:
            return None if x is None else float(x)
        except Exception:
            return None

    symbols = set(new_summary.keys()) | set(old_summary.keys())
    for sym in symbols:
        n = new_summary.get(sym, {"jump": "—", "enabled": None, "ma_thr": None})
        o = old_summary.get(sym, {"jump": "—", "enabled": None, "ma_thr": None})

        ne, oe = n.get("enabled"), o.get("enabled")
        if (ne is None) != (oe is None) or (ne is not None and oe is not None and bool(ne) != bool(oe)):
            return True, f"{sym} MA check {'ENABLED' if ne else 'DISABLED'}"

        nth, oth = _as_float(n.get("ma_thr")), _as_float(o.get("ma_thr"))
        if (nth is None) != (oth is None) or (nth is not None and oth is not None and nth != oth):
            return True, f"{sym} MA threshold Δ=({oth}→{nth}%)"

        if n.get("jump") != o.get("jump"):
            return True, f"{sym} jump {o.get('jump')}→{n.get('jump')}"

    return False, None
