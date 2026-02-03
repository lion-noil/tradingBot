# bots/state/signals.py
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional, Tuple
from collections import deque

from core.redis_client import redis_client
from zoneinfo import ZoneInfo
from datetime import datetime
DAY_MS = 86_400_000
_TZ = ZoneInfo("Asia/Seoul")


# ---------- base ----------
def _now_ms() -> int:
    return int(time.time() * 1000)


def _ns(namespace: str) -> str:
    n = (namespace or "bybit").strip().lower()
    return f"trading:{n}"


# ---------- keys ----------
def stream_key(namespace: str) -> str:
    # 10일치 전체 로그(OPEN/CLOSE 전부)
    return f"{_ns(namespace)}:signals"


def signal_hash_key(namespace: str, signal_id: str) -> str:
    return f"{_ns(namespace)}:signal:{signal_id}"


def open_zset_key(namespace: str, symbol: str, side: str) -> str:
    # "신호상 열린 상태"만 유지
    return f"{_ns(namespace)}:signals:{symbol}:{side}:ENTRY"


# ---------- models ----------
@dataclass(frozen=True)
class SignalInfo:
    signal_id: str
    ts_ms: int
    symbol: str
    side: str
    kind: str  # "ENTRY" | "EXIT"
    price: Optional[float]
    payload: Optional[Dict[str, Any]]


@dataclass(frozen=True)
class OpenSignalStats:
    count: int
    oldest_ts_ms: Optional[int]
    newest_ts_ms: Optional[int]


# ---------- json ----------
def _json_dumps(payload: Any) -> str:
    try:
        return json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        return json.dumps(str(payload), ensure_ascii=False)


def _extract_open_signal_id(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    v = payload.get("open_signal_id") or payload.get("target_open_signal_id")
    return str(v) if v else None


def _normalize_kind(kind: str) -> str:
    k = (kind or "").upper().strip()
    if k == "OPEN":
        return "ENTRY"
    if k == "CLOSE":
        return "EXIT"
    return k


def record_signal_with_ts(
    *,
    namespace: str = "bybit",
    symbol: str,
    side: str,                    # "LONG" | "SHORT"
    kind: str,                    # "ENTRY" | "EXIT" (또는 OPEN/CLOSE 들어와도 됨)
    price: Optional[float] = None,
    payload: Any = None,
    ts_ms: Optional[int] = None,
    keep_days: int = 10,
    trim_approx: bool = True,
) -> Tuple[str, int]:
    """
    신호 발생 시점 기록 (체결/lot과 무관)
    - stream: 10일치 전체 로그 (XTRIM MINID ~ 로 유지)
    - hash: signal_id별 원문 (PEXPIRE로 자동 삭제)
    - open_zset: "열린 상태(ENTRY만)" 유지 (ENTRY add, EXIT zrem(open_signal_id))
    return: (signal_id, ts_ms)
    """
    sid = uuid.uuid4().hex
    ts = int(ts_ms or _now_ms())

    kind_u = _normalize_kind(kind)
    side_u = (side or "").upper().strip()
    symbol_u = (symbol or "").upper().strip()

    if kind_u not in ("ENTRY", "EXIT"):
        raise ValueError(f"invalid kind: {kind_u} (expected ENTRY/EXIT)")

    # EXIT는 반드시 어떤 ENTRY를 닫는지 명시해야 함
    open_id = _extract_open_signal_id(payload)
    if kind_u == "EXIT" and not open_id:
        raise ValueError("EXIT signal missing open_signal_id")

    # reasons: list[str] -> reasons_json
    reasons: List[str] = []
    if isinstance(payload, dict) and isinstance(payload.get("reasons"), list):
        reasons = [str(x) for x in payload["reasons"]]

    hkey = signal_hash_key(namespace, sid)
    skey = stream_key(namespace)
    zkey = open_zset_key(namespace, symbol_u, side_u)

    keep_ms = int(keep_days) * DAY_MS
    cutoff_ms = _now_ms() - keep_ms

    # hash 원문(프론트에서 굳이 안 읽어도 되지만 디버그용/백필용)
    body = {
        "signal_id": sid,
        "ts_ms": str(ts),
        "symbol": symbol_u,
        "side": side_u,
        "kind": kind_u,
        "price": "" if price is None else str(float(price)),
        "payload_json": _json_dumps(payload),
        "created_ts_ms": str(_now_ms()),
    }

    # stream: "한 번에 읽는 최소/핵심" + reasons + (EXIT면 open_signal_id)
    stream_fields: Dict[str, str] = {
        "signal_id": sid,
        "ts_ms": str(ts),
        "symbol": symbol_u,
        "side": side_u,
        "kind": kind_u,
        "price": "" if price is None else str(float(price)),
        "reasons_json": json.dumps(reasons, ensure_ascii=False),
    }
    if open_id:
        stream_fields["open_signal_id"] = open_id

    pipe = redis_client.pipeline()

    # 1) hash 저장 + TTL
    pipe.hset(hkey, mapping=body)
    pipe.pexpire(hkey, keep_ms)

    # 2) stream append + time-based trim
    pipe.xadd(skey, fields=stream_fields, id="*")
    pipe.xtrim(skey, minid=f"{cutoff_ms}-0", approximate=bool(trim_approx))

    # 3) open-state zset 갱신 (ENTRY add / EXIT remove)
    if kind_u == "ENTRY":
        pipe.zadd(zkey, {sid: float(ts)})
        pipe.zremrangebyscore(zkey, "-inf", cutoff_ms)
    else:
        # kind_u == "EXIT"
        pipe.zrem(zkey, open_id)
        pipe.zremrangebyscore(zkey, "-inf", cutoff_ms)

    pipe.execute()
    return sid, ts

# ---------- local cache (no redis writes!) ----------
Key = Tuple[str, str, str]  # (namespace, symbol, side)
Item = Tuple[str, int, float]  # (signal_id, ts_ms, entry_price)


class OpenSignalsIndex:
    def __init__(self) -> None:
        self._dq: Dict[Key, Deque[Item]] = {}

    def load_from_redis(self, *, namespace: str, symbols: List[str]) -> None:
        for sym in symbols:
            for side in ("LONG", "SHORT"):
                zkey = open_zset_key(namespace, sym, side)
                rows = redis_client.zrange(zkey, 0, -1, withscores=True)  # oldest -> newest

                # sid 목록
                sids: List[str] = []
                ts_list: List[int] = []
                for sid_b, score in rows:
                    sid = sid_b.decode() if isinstance(sid_b, (bytes, bytearray)) else str(sid_b)
                    sids.append(sid)
                    ts_list.append(int(score))

                # ✅ sid별 entry_price는 hash에서 읽어옴 (pipeline)
                prices: List[float] = []
                if sids:
                    pipe = redis_client.pipeline()
                    for sid in sids:
                        hkey = signal_hash_key(namespace, sid)
                        pipe.hget(hkey, "price")
                    raw_prices = pipe.execute()

                    for rp in raw_prices:
                        # record_signal_with_ts에서 price는 "" 또는 "123.45" 문자열로 저장
                        try:
                            p = float(rp) if rp not in (None, b"", "") else 0.0
                        except Exception:
                            p = 0.0
                        prices.append(p)

                d: Deque[Item] = deque()
                for sid, ts, p in zip(sids, ts_list, prices):
                    d.append((sid, ts, float(p)))

                self._dq[(namespace, sym, side)] = d

    def stats(self, *, namespace: str, symbol: str, side: str) -> OpenSignalStats:
        d = self._dq.get((namespace, symbol, side))
        if not d:
            return OpenSignalStats(count=0, oldest_ts_ms=None, newest_ts_ms=None)
        return OpenSignalStats(count=len(d), oldest_ts_ms=d[0][1], newest_ts_ms=d[-1][1])

    def on_open(
        self,
        *,
        namespace: str,
        symbol: str,
        side: str,
        signal_id: str,
        ts_ms: int,
        entry_price: float,
    ) -> None:
        key = (namespace, symbol, side)
        self._dq.setdefault(key, deque()).append((signal_id, int(ts_ms), float(entry_price)))

    def list_open(
        self,
        *,
        namespace: str,
        symbol: str,
        side: str,
        newest_first: bool = True,
        limit: Optional[int] = None,
    ) -> List[Item]:
        d = self._dq.get((namespace, symbol, side))
        if not d:
            return []
        rows = list(d)
        if newest_first:
            rows = list(reversed(rows))
        if limit is not None:
            rows = rows[: max(0, int(limit))]
        return rows

    def on_close_by_id(
        self,
        *,
        namespace: str,
        symbol: str,
        side: str,
        open_signal_id: str,
    ) -> Optional[Item]:
        key = (namespace, symbol, side)
        d = self._dq.get(key)
        if not d:
            return None
        for i, (sid, ts, p) in enumerate(d):
            if sid == open_signal_id:
                d.rotate(-i)
                item = d.popleft()
                d.rotate(i)
                return item
        return None

def record_and_index_signal(
    *,
    namespace: str,
    open_index: "OpenSignalsIndex",
    sym: str,
    side: str,
    kind: str,
    price: Optional[float],
    payload: Any,
    engine: Optional[str] = None,
    system_logger=None,
    trading_logger=None,
) -> Tuple[str, int]:
    # payload dict 보장
    p = payload if isinstance(payload, dict) else {}

    kind_u = _normalize_kind(kind)
    side_u = (side or "").upper().strip()
    sym_u = (sym or "").upper().strip()

    sig_dict = {
        **p,
        "kind": kind_u,
        "side": side_u,
        "symbol": sym_u,
        "ts": datetime.now(_TZ).isoformat(),
        "price": price,
        "engine": engine or namespace,
    }

    sid, ts_ms = record_signal_with_ts(
        namespace=namespace,
        symbol=sym_u,
        side=side_u,
        kind=kind_u,
        price=price,
        payload=sig_dict,
    )

    # ✅ 로컬 캐시도 같이 갱신
    if kind_u == "ENTRY":
        open_index.on_open(
            namespace=namespace,
            symbol=sym_u,
            side=side_u,
            signal_id=sid,
            ts_ms=ts_ms,
            entry_price=float(price or 0.0),
        )
    else:
        open_id = _extract_open_signal_id(sig_dict)  # record_signal_with_ts에서 이미 검증됨
        if open_id:
            open_index.on_close_by_id(
                namespace=namespace,
                symbol=sym_u,
                side=side_u,
                open_signal_id=open_id,
            )

    # ✅ 로그
    if trading_logger:
        try:
            trading_logger.info("SIG " + json.dumps(sig_dict, ensure_ascii=False, default=str))
        except Exception:
            trading_logger.info(f"SIG {sig_dict}")

    return sid, ts_ms