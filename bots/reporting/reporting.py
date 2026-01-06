# bots/reporting/reporting.py
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional, Tuple, Callable, List, Union
import re

_HEADER_MA_RE = re.compile(
    r"^\[(?P<sym>[A-Z0-9]+)\]\s+(?P<emoji>[📈📉👀—])\s+ma_thr\(\s*(?P<thr>[0-9.]+)\s*%\s*\)"
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
) -> str:
    js = (jump_state or {}).get(symbol, {})
    state = js.get("state")
    min_dt = js.get("min_dt")
    max_dt = js.get("max_dt")
    thr_pct = (ma_threshold.get(symbol) or 0) * 100

    price = get_price(symbol)
    ma = now_ma100.get(symbol)
    diff_pct = (
        (price - ma) / ma * 100.0
        if (price is not None and ma not in (None, 0))
        else None
    )
    emoji = "📈" if state == "UP" else ("📉" if state == "DOWN" else "👀")

    parts = [f"{emoji} ma_thr({thr_pct:.2f}%)"]
    if price is not None:
        parts.append(f"P={price:.2f}")
    if ma is not None:
        parts.append(f"MA100={ma:.2f}")
    if diff_pct is not None:
        parts.append(f"ΔP/MA={diff_pct:+.2f}%")
    if min_dt is not None and max_dt is not None:
        parts.append(f"Δt={min_dt:.3f}~{max_dt:.3f}s")

    return f"[{symbol}] " + " ".join(parts)


def format_position_lines(
    get_price: Callable[[str], Optional[float]],
    taker_fee_rate: float,
    positions_for_symbol: Dict[str, Any],
    symbol: str,
) -> str:
    price = get_price(symbol)
    if price is None:
        return "  - 시세 없음"

    def _fmt_one(side_name: str, rec: Optional[Dict[str, Any]]) -> Optional[str]:
        if not rec:
            return None
        qty = float(rec.get("qty", 0.0) or 0.0)
        entry = float(rec.get("avg_price", 0.0) or 0.0)
        if qty <= 0 or entry <= 0:
            return None

        if side_name == "LONG":
            profit_rate = (price - entry) / entry * 100.0
            gross_profit = (price - entry) * qty
        else:
            profit_rate = (entry - price) / entry * 100.0
            gross_profit = (entry - price) * qty

        position_value = qty * entry
        fee_total = position_value * taker_fee_rate * 2  # 왕복
        net_profit = gross_profit - fee_total

        s = [
            f"  - 포지션: {side_name} ({qty}, {entry:.1f}, {profit_rate:+.3f}%, {net_profit:+.1f})"
        ]
        entries = rec.get("entries") or []
        for i, e in enumerate(entries, start=1):
            q = float(e.get("qty", 0.0) or 0.0)
            signed_qty = (-q) if side_name == "SHORT" else q
            t_str = e.get("ts_str") or e.get("ts")
            if isinstance(t_str, (int, float)):
                t_str = datetime.fromtimestamp(
                    int(t_str) / 1000, tz=KST
                ).strftime("%Y-%m-%d %H:%M:%S")
            price_e = float(e.get("price", 0.0) or 0.0)
            s.append(
                f"     └#{i} {signed_qty:+.3f} : {t_str or '-'}, {price_e:.1f}"
            )
        return "\n".join(s)

    pos = positions_for_symbol or {}
    long_line = _fmt_one("LONG", pos.get("LONG"))
    short_line = _fmt_one("SHORT", pos.get("SHORT"))

    if not long_line and not short_line:
        return "  - 포지션 없음"
    return "\n".join([x for x in (long_line, short_line) if x])

def build_full_status_log(
    total_usdt: float,
    currency: str,   # ✅ 추가
    symbols: List[str],
    jump_state: Dict[str, Dict[str, Any]],
    ma_threshold: Dict[str, Optional[float]],
    now_ma100: Dict[str, Optional[float]],
    get_price: Callable[[str], Optional[float]],
    positions_by_symbol: Dict[str, Dict[str, Any]],
    taker_fee_rate: float,
) -> str:
    lines: List[str] = [f"\n💰 총 자산: {total_usdt:.2f} {currency}"]


    for sym in symbols:
        # 1) 심볼 상태 한 줄
        lines.append(
            make_status_line(
                sym, jump_state, ma_threshold, now_ma100, get_price
            )
        )

        # 2) 그 심볼 포지션 라인(바로 아래)
        pos_lines = format_position_lines(
            get_price=get_price,
            taker_fee_rate=taker_fee_rate,
            positions_for_symbol=(positions_by_symbol or {}).get(sym, {}),
            symbol=sym,
        ).rstrip("\n")  # 끝 개행 정리

        # 보기 좋게 들여쓰기/하이픈 형식 맞추려면 format_position_lines 출력도 조정 가능
        lines.append(pos_lines)

    return "\n".join(lines).rstrip()
def extract_status_summary(
    text: str,
    fallback_ma_threshold_pct: Optional[Union[Dict[str, Optional[float]], float]] = None
) -> Dict[str, Any]:
    summary: Dict[str, Any] = {}
    cur_sym: Optional[str] = None
    for raw in text.splitlines():
        line = raw.strip()
        m = _HEADER_MA_RE.match(line)
        if m:
            cur_sym = m.group("sym")
            summary.setdefault(
                cur_sym, {"jump": m.group("emoji"), "ma_thr": None, "LONG": None, "SHORT": None}
            )
            try:
                summary[cur_sym]["ma_thr"] = float(m.group("thr"))
            except Exception:
                summary[cur_sym]["ma_thr"] = None
            continue

        if cur_sym:
            pm = _POS_RE.match(line)
            if pm:
                side = pm.group("side")
                qty = float(pm.group("qty") or 0.0)
                pct = float(pm.group("pct") or 0.0)
                summary[cur_sym][side] = {"q": round(qty, 6), "pr": round(pct, 1)}

    if fallback_ma_threshold_pct:
        for sym, v in summary.items():
            if v.get("ma_thr") is None:
                raw_val = fallback_ma_threshold_pct.get(sym)
                if raw_val is not None:
                    v["ma_thr"] = float(raw_val) * 1.0
    return summary
def should_log_update(
    old_summary: Optional[Dict[str, Any]],
    new_summary: Dict[str, Any],
    qty_thr: float = 0.0001,
    rate_thr: float = 1.0,
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
        n = new_summary.get(sym, {"jump": "—", "ma_thr": None, "LONG": None, "SHORT": None})
        o = old_summary.get(sym, {"jump": "—", "ma_thr": None, "LONG": None, "SHORT": None})

        # MA threshold 값 변화(단위: %)
        nth, oth = _as_float(n.get("ma_thr")), _as_float(o.get("ma_thr"))
        if (nth is None) != (oth is None) or (nth is not None and oth is not None and nth != oth):
            return True, f"{sym} MA threshold Δ=({oth}→{nth}%)"

        # jump emoji 변화
        if n.get("jump") != o.get("jump"):
            return True, f"{sym} jump {o.get('jump')}→{n.get('jump')}"

        # 포지션 등장/소멸
        for side in ("LONG", "SHORT"):
            if (n.get(side) is None) != (o.get(side) is None):
                mode = "appeared" if n.get(side) else "disappeared"
                return True, f"{sym} {side} position {mode}"

        # qty / 수익률(%) 변화
        for side in ("LONG", "SHORT"):
            npos, opos = n.get(side), o.get(side)
            if not npos or not opos:
                continue
            nq, npr = npos.get("q"), npos.get("pr")
            oq, opr = opos.get("q"), opos.get("pr")
            if nq is not None and oq is not None and abs(float(nq) - float(oq)) >= qty_thr:
                return True, f"{sym} {side} qty Δ=({oq}→{nq})"
            if npr is not None and opr is not None and abs(float(npr) - float(opr)) >= rate_thr:
                return True, f"{sym} {side} PnL Δ=({opr}%→{npr}%)"

    return False, None



