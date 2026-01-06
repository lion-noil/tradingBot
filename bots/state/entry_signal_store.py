# bots/entry_signal_store.py
from typing import Dict, Optional


class EntrySignalStore:
    """
    LONG/SHORT 엔트리 '신호 발생 시각'(체결과 무관)을
    메모리 + Redis 해시에 동기화해 관리.

    name(예: "bybit", "mt5_signal") 별로 서로 다른 Redis 키를 사용해서
    플랫폼 간 충돌이 나지 않도록 분리.
    """

    def __init__(self, redis_client, symbols, name: str = "bybit"):
        self.redis = redis_client
        self.name = name
        self.redis_key = f"trading:{self.name}:last_entry_signal_ts"

        self._cache: Dict[str, Dict[str, Optional[int]]] = {
            s: {"LONG": None, "SHORT": None} for s in symbols
        }
        self._load_from_redis()

    def _load_from_redis(self) -> None:
        """
        Redis 해시(trading:{name}:last_entry_signal_ts)에서 기존 값들을 한 번만 로드.
        필드 형식: "<SYMBOL>|<SIDE>" → "<ts_ms>"
        예: "BTCUSDT|LONG" → "1733980000123"
        """
        try:
            vals = self.redis.hgetall(self.redis_key) or {}

            # Upstash/redis-py 양쪽 다 대응 (bytes / str)
            for k, v in vals.items():
                key = (
                    k.decode()
                    if isinstance(k, (bytes, bytearray))
                    else str(k)
                )
                val = (
                    v.decode()
                    if isinstance(v, (bytes, bytearray))
                    else str(v)
                )

                try:
                    sym, side = key.split("|", 1)
                except ValueError:
                    # 형식 안 맞으면 스킵
                    continue

                if side not in ("LONG", "SHORT"):
                    continue

                if sym not in self._cache:
                    self._cache[sym] = {"LONG": None, "SHORT": None}

                if val and str(val).isdigit():
                    self._cache[sym][side] = int(val)
        except Exception:
            # 로드 실패는 치명적이지 않으므로 조용히 무시
            pass

    def get(self, symbol: str, side: str) -> Optional[int]:
        """
        해당 심볼/사이드의 마지막 엔트리 시그널 발생 시각(ts_ms) 반환.
        없으면 None.
        """
        return (self._cache.get(symbol) or {}).get(side)

    def set(self, symbol: str, side: str, ts_ms: Optional[int]) -> None:
        """
        메모리 + Redis 모두 갱신.
        ts_ms = None 이면 해당 필드를 삭제(clear).
        """
        if side not in ("LONG", "SHORT"):
            return

        # 메모리 캐시 갱신
        if symbol not in self._cache:
            self._cache[symbol] = {"LONG": None, "SHORT": None}
        self._cache[symbol][side] = ts_ms

        # Redis 반영
        try:
            field = f"{symbol}|{side}"
            if ts_ms is None:
                # 삭제
                self.redis.hdel(self.redis_key, field)
            else:
                self.redis.hset(self.redis_key, field, str(int(ts_ms)))
        except Exception:
            # Redis 장애로 인한 실패는 치명적이지 않으므로 조용히 무시
            pass

    def clear(self, symbol: str, side: str) -> None:
        """
        편의 함수: 해당 심볼/사이드 엔트리 시각 제거.
        """
        self.set(symbol, side, None)
