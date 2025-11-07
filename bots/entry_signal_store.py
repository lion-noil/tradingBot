# bots/entry_signal_store.py
from typing import Dict, Optional

class EntrySignalStore:
    """
    LONG/SHORT 엔트리 '신호 발생 시각'(체결과 무관)을
    메모리 + Redis 해시에 동기화해 관리.
    """
    def __init__(self, redis_client, symbols):
        self.redis = redis_client
        self._cache: Dict[str, Dict[str, Optional[int]]] = {s: {"LONG": None, "SHORT": None} for s in symbols}
        self._load_from_redis()

    def _load_from_redis(self) -> None:
        try:
            vals = self.redis.hgetall("trading:last_entry_signal_ts") or {}
            for k, v in vals.items():
                key = k.decode() if isinstance(k, (bytes, bytearray)) else k
                val = v.decode() if isinstance(v, (bytes, bytearray)) else v
                try:
                    sym, side = key.split("|", 1)
                except ValueError:
                    continue
                if sym in self._cache and side in ("LONG", "SHORT") and val and str(val).isdigit():
                    self._cache[sym][side] = int(val)
        except Exception:
            # 로드 실패는 치명적이지 않으므로 조용히 무시
            pass

    def get(self, symbol: str, side: str) -> Optional[int]:
        return (self._cache.get(symbol) or {}).get(side)

    def set(self, symbol: str, side: str, ts_ms: Optional[int]) -> None:
        # 메모리 갱신
        if symbol not in self._cache:
            self._cache[symbol] = {"LONG": None, "SHORT": None}
        self._cache[symbol][side] = ts_ms

        # Redis 반영
        try:
            key = f"{symbol}|{side}"
            if ts_ms is None:
                self.redis.hdel("trading:last_entry_signal_ts", key)
            else:
                self.redis.hset("trading:last_entry_signal_ts", key, str(int(ts_ms)))
        except Exception:
            pass

    def clear(self, symbol: str, side: str) -> None:
        self.set(symbol, side, None)
