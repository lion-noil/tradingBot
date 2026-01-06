# bots/market/jump_reporting.py
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, Optional


def log_jump(system_logger, symbol, state, min_dt, max_dt):
    if not system_logger or not state:
        return

    try:
        if state == "UP":
            system_logger.info(f"({symbol}) 📈 급등 감지! (Δ {min_dt:.3f}~{max_dt:.3f}s)")
        elif state == "DOWN":
            system_logger.info(f"({symbol}) 📉 급락 감지! (Δ {min_dt:.3f}~{max_dt:.3f}s)")
    except Exception:
        # 포맷팅 실패 등 로그 때문에 봇이 죽지 않게
        system_logger.info(f"({symbol}) jump={state} min_dt={min_dt} max_dt={max_dt}")


@dataclass
class JumpState:
    state: Optional[str]
    min_dt: Optional[float]
    max_dt: Optional[float]
    ts: Optional[float]


class JumpService:
    """
    JumpDetector 결과를 상태로 누적하고, 필요 시 log_jump까지 호출.
    TradeBot의 _updown_test + jump_state 업데이트를 대체.
    """

    def __init__(self, jump_detector: Any, symbols, *, system_logger=None):
        self.jump = jump_detector
        self.system_logger = system_logger

        self.state_by_symbol: Dict[str, JumpState] = {
            s: JumpState(state=None, min_dt=None, max_dt=None, ts=None) for s in symbols
        }

    def ensure_symbol(self, symbol: str) -> None:
        if symbol not in self.state_by_symbol:
            self.state_by_symbol[symbol] = JumpState(state=None, min_dt=None, max_dt=None, ts=None)

    def get_state_map(self) -> Dict[str, Dict[str, Any]]:
        """
        기존 build_full_status_log이 기대하는 dict 형태로 변환
        """
        out: Dict[str, Dict[str, Any]] = {}
        for sym, st in self.state_by_symbol.items():
            out[sym] = {"state": st.state, "min_dt": st.min_dt, "max_dt": st.max_dt, "ts": st.ts}
        return out

    def update(self, symbol: str, ma_threshold: Optional[float]) -> JumpState:
        """
        - jump.check_jump 호출
        - state_by_symbol 갱신
        - log_jump 호출
        """
        self.ensure_symbol(symbol)

        state, min_dt, max_dt = self.jump.check_jump(symbol, ma_threshold)

        prev_ts = self.state_by_symbol[symbol].ts
        new_ts = time.time() if state else prev_ts

        st = JumpState(state=state, min_dt=min_dt, max_dt=max_dt, ts=new_ts)
        self.state_by_symbol[symbol] = st

        # 로깅은 state 있을 때만 log_jump 내부에서 처리됨
        log_jump(self.system_logger, symbol, state, min_dt, max_dt)

        return st
