# bots/state/signals.py
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional, Tuple
from collections import deque

from core.redis_client import redis_client

DAY_MS = 86_400_000


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
    return f"{_ns(namespace)}:signals:{symbol}:{side}:OPEN"


# ---------- models ----------
@dataclass(frozen=True)
class SignalInfo:
    signal_id: str
    ts_ms: int
    symbol: str
    side: str
    kind: str              # "OPEN" | "CLOSE"
    price: Optional[float]
    payload: Any


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


# ---------- write: signal event ----------
def record_signal_with_ts(
    *,
    namespace: str = "bybit",
    symbol: str,
    side: str,                   # "LONG" | "SHORT"
    kind: str,                   # "OPEN" | "CLOSE"
    price: Optional[float] = None,
    payload: Any = None,
    ts_ms: Optional[int] = None,
    open_policy: str = "LIFO",   # CLOSE시 open_pop 정책
    keep_days: int = 10,         # 보관 기간(일)
    trim_approx: bool = True,    # XTRIM 근사(권장)
) -> Tuple[str, int]:
    """
    신호 발생 시점 기록 (체결/lot과 무관)
    - stream: 10일치 전체 로그 (XTRIM MINID ~ 로 유지)
    - hash: signal_id별 원문 (PEXPIRE로 자동 삭제)
    - open_zset: "열린 상태"만 유지 (OPEN add, CLOSE pop)
    return: (signal_id, ts_ms)
    """
    sid = uuid.uuid4().hex
    ts = int(ts_ms or _now_ms())
    kind_u = (kind or "").upper().strip()
    side_u = (side or "").upper().strip()

    hkey = signal_hash_key(namespace, sid)
    skey = stream_key(namespace)

    body = {
        "signal_id": sid,
        "ts_ms": str(ts),
        "symbol": symbol,
        "side": side_u,
        "kind": kind_u,
        "price": "" if price is None else str(float(price)),
        "payload_json": _json_dumps(payload),
        "created_ts_ms": str(_now_ms()),
    }

    stream_fields = {
        "signal_id": sid,
        "ts_ms": str(ts),
        "symbol": symbol,
        "side": side_u,
        "kind": kind_u,
        "price": "" if price is None else str(float(price)),
    }

    keep_ms = int(keep_days) * DAY_MS
    cutoff_ms = _now_ms() - keep_ms
    zkey = open_zset_key(namespace, symbol, side_u)

    pipe = redis_client.pipeline()

    # 1) hash 저장 + TTL
    pipe.hset(hkey, mapping=body)
    pipe.pexpire(hkey, keep_ms)

    # 2) stream append + time-based trim
    pipe.xadd(skey, fields=stream_fields, id="*")
    pipe.xtrim(skey, minid=f"{cutoff_ms}-0", approximate=bool(trim_approx))

    # 3) open-state zset 갱신
    if kind_u == "OPEN":
        pipe.zadd(zkey, {sid: float(ts)})
        pipe.zremrangebyscore(zkey, "-inf", cutoff_ms)  # 오래된 찌꺼기 방지
    elif kind_u == "CLOSE":
        pol = (open_policy or "LIFO").upper()
        if pol == "FIFO":
            pipe.zpopmin(zkey, 1)
        else:
            pipe.zpopmax(zkey, 1)
        pipe.zremrangebyscore(zkey, "-inf", cutoff_ms)

    pipe.execute()
    return sid, ts


# ---------- open-state queries ----------
def open_peek_id_ts(
    *,
    namespace: str,
    symbol: str,
    side: str,
    policy: str = "LIFO",   # "LIFO" | "FIFO"
) -> Optional[Tuple[str, int]]:
    zkey = open_zset_key(namespace, symbol, side)
    pol = (policy or "LIFO").upper()
    items = redis_client.zrange(zkey, 0, 0, withscores=True) if pol == "FIFO" else redis_client.zrevrange(zkey, 0, 0, withscores=True)
    if not items:
        return None
    sid_b, score = items[0]
    sid = sid_b.decode() if isinstance(sid_b, (bytes, bytearray)) else str(sid_b)
    return sid, int(score)


def get_recent_open_signal_id_ts(
    *,
    namespace: str = "bybit",
    symbol: str,
    side: str,
    policy: str = "LIFO",
) -> Optional[Tuple[str, int]]:
    # 기존 호환용 alias
    return open_peek_id_ts(namespace=namespace, symbol=symbol, side=side, policy=policy)


def open_list_ids_ts(
    *,
    namespace: str,
    symbol: str,
    side: str,
    newest_first: bool = False,
    limit: Optional[int] = None,
) -> List[Tuple[str, int]]:
    zkey = open_zset_key(namespace, symbol, side)
    rows = redis_client.zrevrange(zkey, 0, -1, withscores=True) if newest_first else redis_client.zrange(zkey, 0, -1, withscores=True)
    out: List[Tuple[str, int]] = []
    for sid_b, score in rows:
        sid = sid_b.decode() if isinstance(sid_b, (bytes, bytearray)) else str(sid_b)
        out.append((sid, int(score)))
    if limit is not None:
        out = out[: max(0, int(limit))]
    return out


# ---------- stream queries ----------
def stream_range_since_ms(
    *,
    namespace: str,
    since_ms: int,
    count: int = 1000,
    start_id: Optional[str] = None,
) -> List[Dict[str, str]]:
    skey = stream_key(namespace)
    start = start_id or f"{int(since_ms)}-0"
    rows = redis_client.xrange(skey, min=start, max="+", count=int(count))

    out: List[Dict[str, str]] = []
    for entry_id, fields in rows:
        eid = entry_id.decode() if isinstance(entry_id, (bytes, bytearray)) else str(entry_id)
        d: Dict[str, str] = {}
        for k, v in (fields or {}).items():
            kk = k.decode() if isinstance(k, (bytes, bytearray)) else str(k)
            vv = v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
            d[kk] = vv
        out.append({"id": eid, "fields": d})
    return out


# ---------- read hash ----------
def get_signal_info(*, namespace: str = "bybit", signal_id: str) -> Optional[SignalInfo]:
    hkey = signal_hash_key(namespace, signal_id)
    h = redis_client.hgetall(hkey)
    if not h:
        return None

    def s(field: str) -> str:
        b = h.get(field.encode())
        return b.decode() if b else ""

    try:
        ts = int(float(s("ts_ms"))) if s("ts_ms") else 0
    except Exception:
        ts = 0

    price_s = s("price")
    price: Optional[float] = None
    if price_s:
        try:
            price = float(price_s)
        except Exception:
            price = None

    payload_json = s("payload_json")
    try:
        payload = json.loads(payload_json) if payload_json else None
    except Exception:
        payload = payload_json

    return SignalInfo(
        signal_id=s("signal_id") or signal_id,
        ts_ms=ts,
        symbol=s("symbol"),
        side=s("side"),
        kind=s("kind"),
        price=price,
        payload=payload,
    )


# ---------- local cache (no redis writes!) ----------
@dataclass(frozen=True)
class OpenSignalStats:
    count: int
    oldest_ts_ms: Optional[int]
    newest_ts_ms: Optional[int]

Key = Tuple[str, str, str]          # (namespace, symbol, side)
Item = Tuple[str, int]              # (signal_id, ts_ms)

class OpenSignalsIndex:
    def __init__(self) -> None:
        self._dq: Dict[Key, Deque[Item]] = {}

    def load_from_redis(self, *, namespace: str, symbols: List[str]) -> None:
        for sym in symbols:
            for side in ("LONG", "SHORT"):
                zkey = open_zset_key(namespace, sym, side)
                rows = redis_client.zrange(zkey, 0, -1, withscores=True)  # oldest -> newest
                d: Deque[Item] = deque()
                for sid_b, score in rows:
                    sid = sid_b.decode() if isinstance(sid_b, (bytes, bytearray)) else str(sid_b)
                    d.append((sid, int(score)))
                self._dq[(namespace, sym, side)] = d

    def stats(self, *, namespace: str, symbol: str, side: str) -> OpenSignalStats:
        d = self._dq.get((namespace, symbol, side))
        if not d:
            return OpenSignalStats(count=0, oldest_ts_ms=None, newest_ts_ms=None)
        return OpenSignalStats(count=len(d), oldest_ts_ms=d[0][1], newest_ts_ms=d[-1][1])

    def on_open(self, *, namespace: str, symbol: str, side: str, signal_id: str, ts_ms: int) -> None:
        key = (namespace, symbol, side)
        self._dq.setdefault(key, deque()).append((signal_id, int(ts_ms)))

    def on_close(self, *, namespace: str, symbol: str, side: str, policy: str = "LIFO") -> Optional[Item]:
        key = (namespace, symbol, side)
        d = self._dq.get(key)
        if not d:
            return None
        pol = (policy or "LIFO").upper()
        return d.popleft() if pol == "FIFO" else d.pop()
