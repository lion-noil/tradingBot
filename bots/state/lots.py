# bots/state/lots.py
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from decimal import Decimal, ROUND_DOWN
from core.redis_client import redis_client


# ----------------------------- keys -----------------------------

def _now_ms() -> int:
    return int(time.time() * 1000)


def _ns(namespace: str) -> str:
    n = (namespace or "bybit").strip().lower()
    return f"trading:{n}"


def _lot_key(namespace: str, lot_id: str) -> str:
    return f"{_ns(namespace)}:lot:{lot_id}"


def _open_zset_key(namespace: str, symbol: str, side: str) -> str:
    # ✅ lots로 시작 + OPEN 인덱스
    return f"{_ns(namespace)}:lots:{symbol}:{side}:OPEN"


def _by_signal_hash_key(namespace: str) -> str:
    # ✅ entry_signal_id -> lot_id 매핑을 hash 1개로 통합
    return f"{_ns(namespace)}:lots:by_signal:OPEN"


# ----------------------------- Redis store (source of truth) -----------------------------
def _safe_num_str(x: float, max_decimals: int = 12) -> str:
    """
    float -> Decimal(str(float))로 변환 후 소수 자릿수 제한(버림)하여
    Redis에 저장할 '깨끗한' 문자열 생성.
    """
    try:
        d = Decimal(str(float(x)))
    except Exception:
        return "0"

    if d.is_nan() or d.is_infinite():
        return "0"

    q = Decimal("1").scaleb(-max_decimals)  # 10^-max_decimals
    d = d.quantize(q, rounding=ROUND_DOWN)

    s = format(d.normalize(), "f")
    if s == "-0":
        s = "0"
    return s


def open_lot(
    *,
    namespace: str = "bybit",
    symbol: str,
    side: str,  # "LONG" | "SHORT"
    entry_ts_ms: int,
    entry_price: float,
    qty_total: float,
    entry_signal_id: Optional[str] = None,
    ex_lot_id: Optional[int] = None,   # ✅ 추가
) -> str:
    """
    ✅ 체결 확정 후에만 호출해야 함.
    - lot hash 저장
    - open lots zset 인덱싱
    - entry_signal_id -> lot_id 매핑은 HASH 1개에 저장
    """
    lot_id = uuid.uuid4().hex
    hkey = _lot_key(namespace, lot_id)
    zkey = _open_zset_key(namespace, symbol, side)
    mkey = _by_signal_hash_key(namespace)

    body = {
        "lot_id": lot_id,
        "symbol": symbol,
        "side": side,
        "entry_ts_ms": str(int(entry_ts_ms)),
        "entry_price": _safe_num_str(entry_price, max_decimals=8),
        "qty_total": _safe_num_str(qty_total, max_decimals=12),
        "entry_signal_id": entry_signal_id or "",
        "ex_lot_id": str(int(ex_lot_id or 0)),      # ✅ 추가
        "created_ts_ms": str(_now_ms()),
    }

    pipe = redis_client.pipeline()
    pipe.hset(hkey, mapping=body)
    pipe.zadd(zkey, {lot_id: float(entry_ts_ms)})

    if entry_signal_id:
        pipe.hset(mkey, entry_signal_id, lot_id)

    pipe.execute()
    return lot_id


def close_lot_full(
    *,
    namespace: str = "bybit",
    lot_id: str,
) -> bool:
    """
    ✅ 청산 주문 체결 확정 후에만 호출해야 함.
    - open zset에서 제거
    - lot hash 삭제
    - by_signal hash에서도 제거(같이 정리)
    """
    hkey = _lot_key(namespace, lot_id)
    if not redis_client.exists(hkey):
        return False

    symbol_b = redis_client.hget(hkey, "symbol")
    side_b = redis_client.hget(hkey, "side")
    entry_signal_b = redis_client.hget(hkey, "entry_signal_id")

    symbol = symbol_b.decode() if symbol_b else ""
    side = side_b.decode() if side_b else ""
    entry_signal_id = entry_signal_b.decode() if entry_signal_b else ""

    pipe = redis_client.pipeline()

    if symbol and side:
        pipe.zrem(_open_zset_key(namespace, symbol, side), lot_id)

    # lot 본문 삭제
    pipe.delete(hkey)

    # ✅ entry_signal_id -> lot_id 매핑도 같이 삭제
    if entry_signal_id:
        pipe.hdel(_by_signal_hash_key(namespace), entry_signal_id)

    pipe.execute()
    return True


def pick_open_lot_ids(
    *,
    namespace: str = "bybit",
    symbol: str,
    side: str,
    policy: str,           # "LIFO" | "FIFO"
    limit: Optional[int],  # 1이면 1개, None이면 전부
) -> List[str]:
    """
    open lots zset에서 OPEN lot ids 선택.
    - LIFO: 최신 entry_ts_ms부터
    - FIFO: 오래된 entry_ts_ms부터
    """
    zkey = _open_zset_key(namespace, symbol, side)
    pol = (policy or "LIFO").upper()

    if limit is None:
        ids = redis_client.zrevrange(zkey, 0, -1) if pol == "LIFO" else redis_client.zrange(zkey, 0, -1)
        return [x.decode() for x in ids]

    n = max(0, int(limit))
    if n == 0:
        return []

    ids = redis_client.zrevrange(zkey, 0, n - 1) if pol == "LIFO" else redis_client.zrange(zkey, 0, n - 1)
    return [x.decode() for x in ids]


def get_lot_qty_total(*, namespace: str = "bybit", lot_id: str) -> Optional[float]:
    v = redis_client.hget(_lot_key(namespace, lot_id), "qty_total")
    if not v:
        return None
    try:
        return float(v.decode())
    except Exception:
        return None

def get_lot_ex_lot_id(*, namespace: str = "bybit", lot_id: str) -> Optional[int]:
    v = redis_client.hget(_lot_key(namespace, lot_id), "ex_lot_id")
    if not v:
        return None
    try:
        x = int(float(v.decode()))
        return x if x > 0 else None
    except Exception:
        return None

# ----------------------------- LotsIndex (in-memory cache) -----------------------------

@dataclass
class LotCacheItem:
    lot_id: str
    entry_ts_ms: int
    qty_total: float
    entry_price: float
    entry_signal_id: str = ""
    ex_lot_id: int = 0          # ✅ 추가


class LotsIndex:
    """
    In-memory cache.
    목표:
    - 시작 시 Redis로부터 동기화(load_from_redis)
    - 체결 확정 후 on_open/on_close로만 갱신
    - entry_signal_id -> lot_id lookup을 메모리에서 처리
    """

    def __init__(self, *, namespace: str, redis_cli=redis_client) -> None:
        self.namespace = namespace
        self.redis = redis_cli

        self._items: Dict[Tuple[str, str], List[LotCacheItem]] = {}  # (symbol, side) -> newest-first
        self._rev: Dict[str, Tuple[str, str]] = {}                   # lot_id -> (symbol, side)

        self._by_entry_signal: Dict[Tuple[str, str, str], str] = {}  # (symbol, side, entry_signal_id) -> lot_id
        self._entry_by_lot: Dict[str, str] = {}                      # lot_id -> entry_signal_id

    def load_from_redis(self, *, symbols: List[str]) -> None:
        self._items.clear()
        self._rev.clear()
        self._by_entry_signal.clear()
        self._entry_by_lot.clear()

        namespace = self.namespace
        r = self.redis

        for sym in symbols:
            for side in ("LONG", "SHORT"):
                lot_ids = pick_open_lot_ids(
                    namespace=namespace,
                    symbol=sym,
                    side=side,
                    policy="LIFO",
                    limit=None,
                )
                if not lot_ids:
                    continue

                arr: List[LotCacheItem] = []
                for lot_id in lot_ids:
                    h = r.hgetall(_lot_key(namespace, lot_id))
                    if not h:
                        continue

                    def _get(k: str) -> str:
                        b = h.get(k.encode())
                        return b.decode() if b else ""

                    try:
                        entry_ts_ms = int(float(_get("entry_ts_ms") or "0"))
                        qty_total = float(_get("qty_total") or "0")
                        entry_price = float(_get("entry_price") or "0")
                        entry_signal_id = _get("entry_signal_id") or ""
                        ex_lot_id = int(float(_get("ex_lot_id") or "0"))   # ✅ 추가
                    except Exception:
                        continue

                    item = LotCacheItem(
                        lot_id=lot_id,
                        entry_ts_ms=entry_ts_ms,
                        qty_total=qty_total,
                        entry_price=entry_price,
                        entry_signal_id=entry_signal_id,
                        ex_lot_id=ex_lot_id if ex_lot_id > 0 else 0,      # ✅ 추가
                    )
                    arr.append(item)
                    self._rev[lot_id] = (sym, side)

                    if entry_signal_id:
                        self._by_entry_signal[(sym, side, entry_signal_id)] = lot_id
                        self._entry_by_lot[lot_id] = entry_signal_id

                arr.sort(key=lambda x: x.entry_ts_ms, reverse=True)
                if arr:
                    self._items[(sym, side)] = arr

    def find_open_lot_id_by_entry_signal_id(self, symbol: str, side: str, entry_signal_id: str) -> Optional[str]:
        if not entry_signal_id:
            return None
        return self._by_entry_signal.get((symbol, side, entry_signal_id))

    def on_open(
        self,
        symbol: str,
        side: str,
        lot_id: str,
        *,
        entry_ts_ms: int,
        qty_total: float,
        entry_price: float,
        entry_signal_id: str = "",
        ex_lot_id: int = 0,     # ✅ 추가
    ) -> None:
        k = (symbol, side)
        arr = list(self._items.get(k) or [])

        arr = [x for x in arr if x.lot_id != lot_id]

        item = LotCacheItem(
            lot_id=lot_id,
            entry_ts_ms=int(entry_ts_ms),
            qty_total=float(qty_total),
            entry_price=float(entry_price),
            entry_signal_id=entry_signal_id or "",
            ex_lot_id=int(ex_lot_id or 0),     # ✅ 추가
        )
        arr.insert(0, item)

        self._items[k] = arr
        self._rev[lot_id] = k

        if entry_signal_id:
            self._by_entry_signal[(symbol, side, entry_signal_id)] = lot_id
            self._entry_by_lot[lot_id] = entry_signal_id

    def on_close(self, symbol: str, side: str, lot_id: str) -> None:
        k = (symbol, side)
        arr = self._items.get(k) or []
        arr = [x for x in arr if x.lot_id != lot_id]
        if arr:
            self._items[k] = arr
        else:
            self._items.pop(k, None)

        self._rev.pop(lot_id, None)

        entry_signal_id = self._entry_by_lot.pop(lot_id, "")
        if entry_signal_id:
            self._by_entry_signal.pop((symbol, side, entry_signal_id), None)