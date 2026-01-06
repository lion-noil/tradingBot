# bots/market/ws_freshness.py
from __future__ import annotations

import time
from typing import Any, Optional


def _to_sec_epoch(ts: Optional[float]) -> Optional[float]:
    """
    epoch ts를 sec로 정규화 (ms->sec).
    주의: monotonic 값에는 절대 쓰면 안 됨.
    """
    if ts is None:
        return None
    t = float(ts)
    return t / 1000.0 if t > 1e12 else t


def ws_is_fresh(ws: Any, symbol: str, ws_stale_sec: float, ws_global_stale_sec: float) -> bool:
    """
    WS freshness 판단 (권장/최종).

    ✅ 우선순위:
    1) get_last_recv_time(None/symbol)  [monotonic]  ← heartbeat 포함이라 저유동 심볼에도 안정적
    2) (fallback) get_last_frame_time() [monotonic]
    3) (fallback) get_last_exchange_ts(symbol) [epoch]  ← 최후의 수단

    ws_stale_sec: symbol별 허용 지연 (monotonic 기준)
    ws_global_stale_sec: 전역 허용 지연 (monotonic 기준)
    """
    now_mono = time.monotonic()

    # 1) ✅ recv 기반 (best)
    get_recv = getattr(ws, "get_last_recv_time", None)
    if callable(get_recv):
        # 1-1) symbol별 recv가 최신이면 fresh
        sym_recv = get_recv(symbol)
        if sym_recv is not None and (now_mono - float(sym_recv)) <= float(ws_stale_sec):
            return True

        # 1-2) 전역 recv가 최신이면 fresh (hb가 여기 걸림)
        global_recv = get_recv(None)
        if global_recv is not None and (now_mono - float(global_recv)) <= float(ws_global_stale_sec):
            return True

        # recv가 있는데 둘 다 오래됐다 = WS 진짜 죽었을 가능성 큼
        return False

    # 2) fallback: frame time (monotonic)
    get_frame = getattr(ws, "get_last_frame_time", None)
    if callable(get_frame):
        frame_mono = get_frame()
        if frame_mono is not None and (now_mono - float(frame_mono)) <= float(ws_global_stale_sec):
            return True
        if frame_mono is not None:
            return False

    # 3) 최후 fallback: exchange ts (epoch)
    now_epoch = time.time()
    get_ex = getattr(ws, "get_last_exchange_ts", None)
    if callable(get_ex):
        sym_ts = _to_sec_epoch(get_ex(symbol))
        if sym_ts is None:
            return False
        return (now_epoch - sym_ts) <= float(ws_stale_sec)

    return False
