# bots/state/signals.py
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from typing import Deque, Dict, Optional, Tuple, List
from core.redis_client import redis_client

from collections import deque

DAY_MS = 86_400_000


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

def record_signal_with_ts(
    *,
    namespace: str = "bybit",
    symbol: str,
    side: str,                   # "LONG" | "SHORT"
    kind: str,                   # "OPEN" | "CLOSE"
    price: Optional[float] = None,
    payload: Any = None,
    ts_ms: Optional[int] = None,
    open_policy: str = "LIFO",   # ✅ CLOSE시 open_pop 정책
) -> Tuple[str, int]:
    """
    신호 발생 시점 기록 (체결/lot과 무관)
    - stream: 10일치 전체 로그
    - hash: signal_id별 원문
    - open_zset: "열린 상태"만 유지 (OPEN push, CLOSE pop)
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

    pipe = redis_client.pipeline()
    pipe.hset(hkey, mapping=body)
    pipe.xadd(skey, fields=stream_fields, id="*")

    # ✅ open-state zset 갱신도 같은 트랜잭션에 포함
    if kind_u == "OPEN":
        zkey = open_zset_key(namespace, symbol, side_u)
        pipe.zadd(zkey, {sid: float(ts)})

    elif kind_u == "CLOSE":
        # CLOSE는 "열린 OPEN 1개"를 제거 (LIFO/FIFO)
        # redis-py에서 파이프라인으로 zpopmax/min 지원됨
        zkey = open_zset_key(namespace, symbol, side_u)
        pol = (open_policy or "LIFO").upper()
        if pol == "FIFO":
            pipe.zpopmin(zkey, 1)
        else:
            pipe.zpopmax(zkey, 1)

    pipe.execute()
    return sid, ts

def get_recent_open_signal_id_ts(
    *,
    namespace: str = "bybit",
    symbol: str,
    side: str,
    policy: str = "LIFO",
) -> Optional[Tuple[str, int]]:
    # 기존 이름 호환: open_peek_id_ts 래핑
    return open_peek_id_ts(namespace=namespace, symbol=symbol, side=side, policy=policy)

# ---------- model ----------
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

class OpenSignalsIndex:
    """
    (symbol, side)별 OPEN 신호들을 로컬에 전부 들고 있는 캐시.
    - 시작 시 Redis open zset을 한번 로드
    - OPEN 신호 발생: on_open() 호출 (로컬 + redis)
    - CLOSE 신호 발생: on_close() 호출 (로컬 + redis)
    """
    def __init__(self) -> None:
        self._dq: Dict[Tuple[str, str], Deque[Tuple[str, int]]] = {}

    def load_from_redis(self, *, namespace: str, symbols: List[str]) -> None:
        for sym in symbols:
            for side in ("LONG", "SHORT"):
                zkey = open_zset_key(namespace, sym, side)
                # 오래된 -> 최신 순으로 로드 (deque의 왼쪽이 oldest)
                rows = redis_client.zrange(zkey, 0, -1, withscores=True)
                d: Deque[Tuple[str, int]] = deque()
                for sid_b, score in rows:
                    sid = sid_b.decode() if isinstance(sid_b, (bytes, bytearray)) else str(sid_b)
                    d.append((sid, int(score)))
                self._dq[(sym, side)] = d

    def stats(self, *, symbol: str, side: str) -> OpenSignalStats:
        d = self._dq.get((symbol, side))
        if not d:
            return OpenSignalStats(count=0, oldest_ts_ms=None, newest_ts_ms=None)
        return OpenSignalStats(count=len(d), oldest_ts_ms=d[0][1], newest_ts_ms=d[-1][1])

    def on_open(self, *, namespace: str, symbol: str, side: str, signal_id: str, ts_ms: int) -> None:
        key = (symbol, side)
        if key not in self._dq:
            self._dq[key] = deque()
        self._dq[key].append((signal_id, int(ts_ms)))   # newest가 오른쪽
        open_push(namespace=namespace, symbol=symbol, side=side, signal_id=signal_id, ts_ms=int(ts_ms))

    def on_close(self, *, namespace: str, symbol: str, side: str, policy: str = "LIFO") -> Optional[Tuple[str, int]]:
        """
        CLOSE 신호가 발생하면 open 상태에서 1개 제거.
        - LIFO면 newest 제거, FIFO면 oldest 제거
        """
        key = (symbol, side)
        d = self._dq.get(key)
        if not d:
            # 로컬 없으면 Redis에서도 pop 시도(복구성)
            return open_pop(namespace=namespace, symbol=symbol, side=side, policy=policy)

        pol = (policy or "LIFO").upper()
        if pol == "FIFO":
            sid, ts = d.popleft()
        else:
            sid, ts = d.pop()

        # Redis에서도 동일하게 pop(정합성)
        open_pop(namespace=namespace, symbol=symbol, side=side, policy=policy)
        return sid, ts


# ---------- helpers ----------
def _json_dumps(payload: Any) -> str:
    try:
        return json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        return json.dumps(str(payload), ensure_ascii=False)


def record_signal_with_ts(
    *,
    namespace: str = "bybit",
    symbol: str,
    side: str,                   # "LONG" | "SHORT"
    kind: str,                   # "OPEN" | "CLOSE"
    price: Optional[float] = None,
    payload: Any = None,
    ts_ms: Optional[int] = None,
) -> Tuple[str, int]:
    """
    신호 발생 시점 기록 (체결/lot과 무관)
    - stream: 10일치 전체 로그
    - hash: signal_id별 원문
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

    # stream fields는 string이어야 안전
    stream_fields = {
        "signal_id": sid,
        "ts_ms": str(ts),
        "symbol": symbol,
        "side": side_u,
        "kind": kind_u,
        "price": "" if price is None else str(float(price)),
    }

    pipe = redis_client.pipeline()
    pipe.hset(hkey, mapping=body)
    # ID는 '*'로 넣으면 Redis가 "현재ms-seq"로 자동 생성
    pipe.xadd(skey, fields=stream_fields, id="*")
    pipe.execute()

    return sid, ts


# ---------- open-state zset ----------
def open_count(*, namespace: str, symbol: str, side: str) -> int:
    return int(redis_client.zcard(open_zset_key(namespace, symbol, side)) or 0)


def open_peek_id_ts(
    *,
    namespace: str,
    symbol: str,
    side: str,
    policy: str = "LIFO",   # "LIFO" | "FIFO"
) -> Optional[Tuple[str, int]]:
    """
    열린 OPEN 중 대표 1개 조회(삭제 X)
    - LIFO: 가장 최근 OPEN
    - FIFO: 가장 오래된 OPEN
    """
    zkey = open_zset_key(namespace, symbol, side)
    pol = (policy or "LIFO").upper()
    if pol == "FIFO":
        items = redis_client.zrange(zkey, 0, 0, withscores=True)
    else:
        items = redis_client.zrevrange(zkey, 0, 0, withscores=True)

    if not items:
        return None

    sid_b, score = items[0]
    try:
        sid = sid_b.decode() if isinstance(sid_b, (bytes, bytearray)) else str(sid_b)
        return sid, int(score)
    except Exception:
        return None


def open_push(
    *,
    namespace: str,
    symbol: str,
    side: str,
    signal_id: str,
    ts_ms: int,
) -> None:
    """
    OPEN 신호가 발생하면 open 상태에 추가
    """
    zkey = open_zset_key(namespace, symbol, side)
    redis_client.zadd(zkey, {signal_id: float(int(ts_ms))})


def open_pop(
    *,
    namespace: str,
    symbol: str,
    side: str,
    policy: str = "LIFO",  # "LIFO" | "FIFO"
) -> Optional[Tuple[str, int]]:
    """
    CLOSE 신호가 발생하면 open 상태에서 1개 제거
    - LIFO: 가장 최근 OPEN 제거
    - FIFO: 가장 오래된 OPEN 제거
    return: (popped_signal_id, popped_ts_ms)
    """
    zkey = open_zset_key(namespace, symbol, side)
    pol = (policy or "LIFO").upper()

    # Redis 5+ : ZPOPMIN / ZPOPMAX
    try:
        if pol == "FIFO":
            popped = redis_client.zpopmin(zkey, 1)
        else:
            popped = redis_client.zpopmax(zkey, 1)
    except Exception:
        popped = []

    if not popped:
        return None

    sid_b, score = popped[0]
    sid = sid_b.decode() if isinstance(sid_b, (bytes, bytearray)) else str(sid_b)
    return sid, int(score)


def open_remove(
    *,
    namespace: str,
    symbol: str,
    side: str,
    signal_id: str,
) -> int:
    """
    특정 id를 open 상태에서 제거하고 싶을 때(복구/정리용)
    return: removed count
    """
    zkey = open_zset_key(namespace, symbol, side)
    return int(redis_client.zrem(zkey, signal_id) or 0)


def open_trim_older_than_days(
    *,
    namespace: str,
    symbol: str,
    side: str,
    days: int = 10,
) -> int:
    """
    open_zset에 너무 오래된(윈도우 밖) OPEN이 남아있는 경우 정리.
    (신호 상태 TTL)
    """
    cutoff = _now_ms() - int(days) * DAY_MS
    zkey = open_zset_key(namespace, symbol, side)
    return int(redis_client.zremrangebyscore(zkey, "-inf", cutoff) or 0)


# ---------- queries (stream/time) ----------
def stream_range_since_ms(
    *,
    namespace: str,
    since_ms: int,
    count: int = 1000,
    start_id: Optional[str] = None,
) -> List[Dict[str, str]]:
    """
    Stream에서 since_ms 이후 이벤트를 가져옴 (페이지네이션 가능)
    - start_id 없으면 "{since_ms}-0"부터 시작
    - 반환은 [{id: "...", fields: {...}}]
    """
    skey = stream_key(namespace)
    start = start_id or f"{int(since_ms)}-0"
    end = "+"

    rows = redis_client.xrange(skey, min=start, max=end, count=int(count))
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


def stream_trim_older_than_days(
    *,
    namespace: str,
    days: int = 10,
) -> None:
    """
    Stream을 time-based로 10일 유지하고 싶을 때 호출.
    (봇 틱에서 하루 1번 정도 호출해도 충분)
    """
    cutoff_ms = _now_ms() - int(days) * DAY_MS
    skey = stream_key(namespace)
    # Redis 6.2+ : XTRIM MINID ~
    # "~"는 근사 trim (성능↑)
    redis_client.xtrim(skey, minid=f"{cutoff_ms}-0", approximate=True)


# ---------- read hash ----------
def get_signal_info(*, namespace: str = "bybit", signal_id: str) -> Optional[SignalInfo]:
    hkey = signal_hash_key(namespace, signal_id)
    h = redis_client.hgetall(hkey)
    if not h:
        return None

    def s(field: str) -> str:
        b = h.get(field.encode())
        return b.decode() if b else ""

    ts_s = s("ts_ms")
    try:
        ts = int(float(ts_s)) if ts_s else 0
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

def open_list_ids_ts(
    *,
    namespace: str,
    symbol: str,
    side: str,
    newest_first: bool = False,
    limit: Optional[int] = None,
) -> List[Tuple[str, int]]:
    zkey = open_zset_key(namespace, symbol, side)
    # FIFO 기준으로 로컬 deque를 만들려면 "오래된->새로운" 순서가 편함
    if newest_first:
        rows = redis_client.zrevrange(zkey, 0, -1, withscores=True)
    else:
        rows = redis_client.zrange(zkey, 0, -1, withscores=True)

    out: List[Tuple[str, int]] = []
    for sid_b, score in rows:
        sid = sid_b.decode() if isinstance(sid_b, (bytes, bytearray)) else str(sid_b)
        out.append((sid, int(score)))

    if limit is not None:
        out = out[: max(0, int(limit))]
    return out
