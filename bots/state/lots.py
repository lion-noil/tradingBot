# bots/state/lots.py
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

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
    return f"{_ns(namespace)}:open_lots:{symbol}:{side}"


def _open_lot_by_signal_key(namespace: str, entry_signal_id: str) -> str:
    return f"{_ns(namespace)}:open_lot_by_signal:{entry_signal_id}"


# ----------------------------- Redis store (source of truth) -----------------------------


def open_lot(
    *,
    namespace: str = "bybit",
    symbol: str,
    side: str,  # "LONG" | "SHORT"
    entry_ts_ms: int,
    entry_price: float,
    qty_total: float,
    entry_signal_id: Optional[str] = None,
) -> str:
    """
    ✅ 체결 확정 후에만 호출해야 함.
    - OPEN lot 생성 + open_lots zset 인덱싱
    - signal_id ↔ lot_id 매핑 저장(옵션)
    """
    lot_id = uuid.uuid4().hex
    hkey = _lot_key(namespace, lot_id)
    zkey = _open_zset_key(namespace, symbol, side)

    body = {
        "lot_id": lot_id,
        "symbol": symbol,
        "side": side,
        "entry_ts_ms": str(int(entry_ts_ms)),
        "entry_price": str(float(entry_price)),
        "qty_total": str(float(qty_total)),
        "entry_signal_id": entry_signal_id or "",
        "created_ts_ms": str(_now_ms()),
    }

    pipe = redis_client.pipeline()
    pipe.hset(hkey, mapping=body)
    pipe.zadd(zkey, {lot_id: float(entry_ts_ms)})

    if entry_signal_id:
        pipe.set(_open_lot_by_signal_key(namespace, entry_signal_id), lot_id)

    pipe.execute()
    return lot_id


def close_lot_full(
    *,
    namespace: str = "bybit",
    lot_id: str,
) -> bool:
    """
    ✅ 청산 주문 체결 확정 후에만 호출해야 함.

    설계:
    - CLOSED 상태는 별도 저장 안 함(요청사항)
    - open_lots 인덱스에서 제거 + lot hash 삭제
    - signal_id ↔ lot_id 매핑도 제거
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
    pipe.delete(hkey)

    if entry_signal_id:
        pipe.delete(_open_lot_by_signal_key(namespace, entry_signal_id))

    pipe.execute()
    return True


def pick_open_lot_ids(
    *,
    namespace: str = "bybit",
    symbol: str,
    side: str,
    policy: str,          # "LIFO" | "FIFO"
    limit: Optional[int], # 1이면 1개, None이면 전부
) -> List[str]:
    """
    Redis open index에서 OPEN lot ids 선택.
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


def get_open_lot_id_by_entry_signal_id(
    *,
    namespace: str = "bybit",
    entry_signal_id: str,
) -> Optional[str]:
    v = redis_client.get(_open_lot_by_signal_key(namespace, entry_signal_id))
    if not v:
        return None
    try:
        return v.decode()
    except Exception:
        return None


def get_lot_qty_total(*, namespace: str = "bybit", lot_id: str) -> Optional[float]:
    v = redis_client.hget(_lot_key(namespace, lot_id), "qty_total")
    if not v:
        return None
    try:
        return float(v.decode())
    except Exception:
        return None


def get_lot_entry_ts_ms(*, namespace: str = "bybit", lot_id: str) -> Optional[int]:
    v = redis_client.hget(_lot_key(namespace, lot_id), "entry_ts_ms")
    if not v:
        return None
    try:
        return int(float(v.decode()))
    except Exception:
        return None


def get_lot_entry_price(*, namespace: str = "bybit", lot_id: str) -> Optional[float]:
    v = redis_client.hget(_lot_key(namespace, lot_id), "entry_price")
    if not v:
        return None
    try:
        return float(v.decode())
    except Exception:
        return None


# ----------------------------- LotsIndex (in-memory cache) -----------------------------


@dataclass
class LotCacheItem:
    lot_id: str
    entry_ts_ms: int
    qty_total: float
    entry_price: float


class LotsIndex:
    """
    ✅ 로컬 캐시: OPEN lot만 관리 (가속용)
    - source of truth는 Redis lots store
    - 주문 체결 성공 후(OPEN/CLOSE) TradeExecutor에서 on_open/on_close로만 갱신
    - 재시작 시 1회 load_from_redis로 warm-up
    """

    def __init__(self) -> None:
        # (symbol, side) -> [LotCacheItem...]
        # 기본 정렬: entry_ts_ms 내림차순 (LIFO: newest first)
        self._items: Dict[Tuple[str, str], List[LotCacheItem]] = {}
        # lot_id -> (symbol, side) 역인덱스
        self._rev: Dict[str, Tuple[str, str]] = {}

    # ---- warmup ----

    def load_from_redis(self, redis_cli, *, namespace: str, symbols: List[str]) -> None:
        """
        재시작 시 1회 로드용.
        - 각 symbol, side별 open_lots zset에서 lot_id들을 가져오고
        - lot hash에서 필요한 메타(entry_ts_ms/qty_total/entry_price)만 읽어 캐시에 넣는다.
        """
        self._items.clear()
        self._rev.clear()

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
                    hkey = _lot_key(namespace, lot_id)
                    h = redis_cli.hgetall(hkey)
                    if not h:
                        continue

                    def _get(k: str) -> str:
                        b = h.get(k.encode())
                        return b.decode() if b else ""

                    try:
                        entry_ts_ms = int(float(_get("entry_ts_ms") or "0"))
                        qty_total = float(_get("qty_total") or "0")
                        entry_price = float(_get("entry_price") or "0")
                    except Exception:
                        continue

                    item = LotCacheItem(
                        lot_id=lot_id,
                        entry_ts_ms=entry_ts_ms,
                        qty_total=qty_total,
                        entry_price=entry_price,
                    )
                    arr.append(item)
                    self._rev[lot_id] = (sym, side)

                # LIFO 정렬 보장
                arr.sort(key=lambda x: x.entry_ts_ms, reverse=True)
                if arr:
                    self._items[(sym, side)] = arr

    # ---- realtime updates (after fills only) ----

    def on_open(
        self,
        symbol: str,
        side: str,
        lot_id: str,
        *,
        entry_ts_ms: int,
        qty_total: float,
        entry_price: float,
    ) -> None:
        k = (symbol, side)
        arr = self._items.get(k) or []

        # 중복 제거
        arr = [x for x in arr if x.lot_id != lot_id]

        # newest first
        arr.insert(0, LotCacheItem(
            lot_id=lot_id,
            entry_ts_ms=int(entry_ts_ms),
            qty_total=float(qty_total),
            entry_price=float(entry_price),
        ))

        self._items[k] = arr
        self._rev[lot_id] = k

    def on_close(self, symbol: str, side: str, lot_id: str) -> None:
        k = (symbol, side)
        arr = self._items.get(k) or []
        arr = [x for x in arr if x.lot_id != lot_id]
        if arr:
            self._items[k] = arr
        else:
            self._items.pop(k, None)
        self._rev.pop(lot_id, None)

    # ---- reads ----

    def pick_open_lot_ids(self, symbol: str, side: str, *, policy: str, limit: Optional[int]) -> List[str]:
        k = (symbol, side)
        arr = list(self._items.get(k) or [])

        pol = (policy or "LIFO").upper()
        if pol == "FIFO":
            arr = list(reversed(arr))

        ids = [x.lot_id for x in arr]
        if limit is None:
            return ids
        n = max(0, int(limit))
        return ids[:n]

    def get_entry_ts_ms(self, lot_id: str) -> Optional[int]:
        k = self._rev.get(lot_id)
        if not k:
            return None
        arr = self._items.get(k) or []
        for x in arr:
            if x.lot_id == lot_id:
                return x.entry_ts_ms
        return None

    def get_qty_total(self, lot_id: str) -> Optional[float]:
        k = self._rev.get(lot_id)
        if not k:
            return None
        arr = self._items.get(k) or []
        for x in arr:
            if x.lot_id == lot_id:
                return x.qty_total
        return None

    def get_entry_price(self, lot_id: str) -> Optional[float]:
        k = self._rev.get(lot_id)
        if not k:
            return None
        arr = self._items.get(k) or []
        for x in arr:
            if x.lot_id == lot_id:
                return x.entry_price
        return None
