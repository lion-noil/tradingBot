# bots/state/bot_state.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class BotState:
    symbols: List[str]
    default_exit_ma_threshold: float

    # 자산/포지션
    asset: Dict[str, Any] = field(default_factory=dict)

    # 인디케이터 상태들
    ma100s: Dict[str, List[Optional[float]]] = field(default_factory=dict)
    now_ma100: Dict[str, Optional[float]] = field(default_factory=dict)
    ma_threshold: Dict[str, Optional[float]] = field(default_factory=dict)
    momentum_threshold: Dict[str, Optional[float]] = field(default_factory=dict)
    exit_ma_threshold: Dict[str, Optional[float]] = field(default_factory=dict)
    thr_quantized: Dict[str, Optional[float]] = field(default_factory=dict)
    prev3_candle: Dict[str, Optional[dict]] = field(default_factory=dict)

    def init_defaults(self) -> None:
        # asset 기본
        if not self.asset:
            self.asset = {
                "wallet": {"USDT": 0.0},
                "positions": {s: {} for s in self.symbols},
            }

        # dict들 기본
        for s in self.symbols:
            self.ma100s.setdefault(s, [])
            self.now_ma100.setdefault(s, None)
            self.ma_threshold.setdefault(s, None)
            self.momentum_threshold.setdefault(s, None)
            self.exit_ma_threshold.setdefault(s, self.default_exit_ma_threshold)
            self.thr_quantized.setdefault(s, None)
            self.prev3_candle.setdefault(s, None)

    def get_exit_thr(self, symbol: str) -> float:
        v = self.exit_ma_threshold.get(symbol)
        return v if v is not None else self.default_exit_ma_threshold
