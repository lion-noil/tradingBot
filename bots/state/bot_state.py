# bots/state/bot_state.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class BotState:
    symbols: List[str]

    min_ma_threshold: float

    # 인디케이터 상태들
    ma100s: Dict[str, List[Optional[float]]] = field(default_factory=dict)
    now_ma100: Dict[str, Optional[float]] = field(default_factory=dict)
    ma_threshold: Dict[str, Optional[float]] = field(default_factory=dict)
    momentum_threshold: Dict[str, Optional[float]] = field(default_factory=dict)
    thr_quantized: Dict[str, Optional[float]] = field(default_factory=dict)
    prev3_candle: Dict[str, Optional[dict]] = field(default_factory=dict)
    ma_check_enabled: Dict[str, bool] = field(default_factory=dict)

    def init_defaults(self) -> None:
        for s in self.symbols:
            self.ma100s.setdefault(s, [])
            self.now_ma100.setdefault(s, None)
            self.ma_threshold.setdefault(s, None)
            self.momentum_threshold.setdefault(s, None)
            self.thr_quantized.setdefault(s, None)
            self.prev3_candle.setdefault(s, None)
            self.ma_check_enabled.setdefault(s, False)

