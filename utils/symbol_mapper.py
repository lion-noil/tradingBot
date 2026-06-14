# utils/symbol_mapper.py
from __future__ import annotations
import os


class SymbolAliasMap:
    """
    Canonical (bot-internal) ↔ Broker (MT5) symbol name translation.
    Configure: MT5_SYMBOL_MAP=NAS100:US100.F,BTCUSD:#BTCUSD,WTI:USOIL
    """

    def __init__(self, mapping: dict[str, str] | None = None):
        self._to_broker: dict[str, str] = {}
        self._to_canonical: dict[str, str] = {}
        for canonical, broker in (mapping or {}).items():
            c = canonical.upper().strip()
            b = broker.upper().strip()
            if c and b and c != b:
                self._to_broker[c] = b
                self._to_canonical[b] = c

    @classmethod
    def from_env(cls, env_key: str = "MT5_SYMBOL_MAP") -> "SymbolAliasMap":
        raw = (os.getenv(env_key) or "").strip()
        mapping: dict[str, str] = {}
        for part in raw.split(","):
            part = part.strip()
            if ":" in part:
                canonical, broker = part.split(":", 1)
                mapping[canonical.strip()] = broker.strip()
        return cls(mapping)

    def to_broker(self, symbol: str) -> str:
        s = (symbol or "").upper().strip()
        return self._to_broker.get(s, s)

    def to_canonical(self, symbol: str) -> str:
        s = (symbol or "").upper().strip()
        return self._to_canonical.get(s, s)

    def __bool__(self) -> bool:
        return bool(self._to_broker)

    def __repr__(self) -> str:
        return f"SymbolAliasMap({self._to_broker})"
