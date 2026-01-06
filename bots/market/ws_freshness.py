# bots/market/ws_freshness.py
from __future__ import annotations
import time
from typing import Any


def ws_is_fresh(ws: Any, symbol: str, ws_stale_sec: float, ws_global_stale_sec: float) -> bool:
    """
    WS ticker가 충분히 최신인지 판단.
    - symbol별 last_exchange_ts가 지금 기준 ws_stale_sec 이내면 fresh
    - 전역(last_any_exchange_ts 같은 게 있다면)도 ws_global_stale_sec로 체크 가능
    """
    now = time.time()

    # symbol별 timestamp
    get_sym_ts = getattr(ws, "get_last_exchange_ts", None)
    sym_ts = get_sym_ts(symbol) if callable(get_sym_ts) else None

    if sym_ts is None:
        return False

    # sym_ts가 ms일 수도 있으니 보정 (1e12 이상이면 ms로 판단)
    sym_ts_sec = float(sym_ts) / 1000.0 if float(sym_ts) > 1e12 else float(sym_ts)
    if now - sym_ts_sec > float(ws_stale_sec):
        return False

    # 전역 timestamp(있으면) 추가 체크
    get_global_ts = getattr(ws, "get_last_any_exchange_ts", None) or getattr(ws, "get_last_exchange_ts_all", None)
    if callable(get_global_ts):
        gts = get_global_ts()
        if gts is not None:
            gts_sec = float(gts) / 1000.0 if float(gts) > 1e12 else float(gts)
            if now - gts_sec > float(ws_global_stale_sec):
                return False

    return True
