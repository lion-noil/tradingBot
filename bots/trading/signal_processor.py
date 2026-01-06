# bots/trading/signal_processor.py
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from strategies.basic_strategy import (
    get_short_entry_signal, get_long_entry_signal, get_exit_signal
)
from bots.state.balances import get_total_balance_usd


# 실행 요청(=의사결정 결과)
@dataclass
class TradeAction:
    action: str                 # "OPEN" | "CLOSE"
    symbol: str
    side: str                   # "LONG" | "SHORT"
    price: Optional[float] = None
    qty: Optional[float] = None
    sig: Optional[Any] = None   # 원본 시그널(로그/디버깅용)


@dataclass
class SignalProcessorDeps:
    # --- state getters ---
    get_asset: Callable[[], Dict[str, Any]]
    get_now_ma100: Callable[[str], Optional[float]]
    get_prev3_candle: Callable[[str], Optional[dict]]
    get_ma_threshold: Callable[[str], Optional[float]]
    get_momentum_threshold: Callable[[str], Optional[float]]
    get_exit_ma_threshold: Callable[[str], float]

    # --- config getters ---
    is_signal_only: Callable[[], bool]
    get_max_effective_leverage: Callable[[], float]
    get_position_max_hold_sec: Callable[[], int]
    get_near_touch_window_sec: Callable[[], int]

    # ✅ (미래 대비) 최소 ma_threshold 게이트. 지금 당장은 None 반환해도 됨.
    get_min_ma_threshold: Callable[[], Optional[float]]

    # --- entry store ---
    entry_store_get: Callable[[str, str], Optional[int]]
    entry_store_set: Callable[[str, str, Optional[int]], None]

    # --- logging (signal 발생 시점에만) ---
    log_signal: Callable[[str, Any], None]  # (symbol, sig)


class SignalProcessor:
    """
    ✅ 책임: 신호 판단 + 상태 업데이트(entry_store 등) + 액션 리스트 반환
    ❌ 책임 아님: 주문 실행/자산 동기화 (TradeExecutor가 담당)
    """

    def __init__(self, *, deps: SignalProcessorDeps, system_logger=None):
        self.deps = deps
        self.system_logger = system_logger

    async def process_symbol(self, symbol: str, price: Optional[float]) -> List[TradeAction]:
        actions: List[TradeAction] = []

        now_ma100 = self.deps.get_now_ma100(symbol)
        if price is None or now_ma100 is None:
            return actions

        # ✅ (미래) ma_threshold 게이트: 너무 낮으면 신호 자체를 생성하지 않음
        min_thr = self.deps.get_min_ma_threshold()  # 예: 0.005
        thr = self.deps.get_ma_threshold(symbol)
        if (min_thr is not None) and (thr is not None) and (thr < min_thr):
            return actions

        actions.extend(self._decide_exits(symbol, price))
        actions.extend(self._decide_entries(symbol, price))
        return actions

    def _decide_exits(self, symbol: str, price: float) -> List[TradeAction]:
        asset = self.deps.get_asset()
        actions: List[TradeAction] = []

        for side in ("LONG", "SHORT"):
            recent_time = self.deps.entry_store_get(symbol, side)
            if not recent_time:
                continue

            sig = get_exit_signal(
                side,
                price,
                self.deps.get_now_ma100(symbol),
                recent_entry_time=recent_time,
                ma_threshold=self.deps.get_ma_threshold(symbol),
                exit_ma_threshold=self.deps.get_exit_ma_threshold(symbol),
                time_limit_sec=self.deps.get_position_max_hold_sec(),
                near_touch_window_sec=self.deps.get_near_touch_window_sec(),
            )
            if not sig:
                continue

            # 엔트리 기록 제거 + 로그(신호 발생 시점)
            self.deps.entry_store_set(symbol, side, None)
            self.deps.log_signal(symbol, sig)

            pos_amt = abs(float(
                (((asset.get("positions") or {}).get(symbol) or {}).get(side) or {}).get("qty") or 0
            ))
            if pos_amt == 0:
                if self.system_logger:
                    self.system_logger.info(f"({symbol}) EXIT 신호지만 {side} qty=0 → 스킵")
                continue

            actions.append(TradeAction(
                action="CLOSE",
                symbol=symbol,
                side=side,
                qty=pos_amt,
                sig=sig,
            ))

        return actions

    def _decide_entries(self, symbol: str, price: float) -> List[TradeAction]:
        asset = self.deps.get_asset()
        signal_only = self.deps.is_signal_only()

        wallet = (asset.get("wallet") or {})
        pos = ((asset.get("positions") or {}).get(symbol) or {})

        total_balance = get_total_balance_usd(wallet)
        max_eff = self.deps.get_max_effective_leverage()

        actions: List[TradeAction] = []

        # --- Short ---
        recent_short_time = self.deps.entry_store_get(symbol, "SHORT")
        short_amt = abs(float(((pos.get("SHORT") or {}).get("qty")) or 0.0))
        short_eff_x = (short_amt * price / total_balance) if (total_balance and not signal_only) else 0.0

        if signal_only or short_eff_x < max_eff:
            sig_s = get_short_entry_signal(
                price=price,
                ma100=self.deps.get_now_ma100(symbol),
                prev3_candle=self.deps.get_prev3_candle(symbol),
                ma_threshold=self.deps.get_ma_threshold(symbol),
                momentum_threshold=self.deps.get_momentum_threshold(symbol),
                recent_entry_time=recent_short_time,
                reentry_cooldown_sec=60 * 60,
            )
            if sig_s:
                now_ms = int(time.time() * 1000)
                self.deps.log_signal(symbol, sig_s)
                self.deps.entry_store_set(symbol, "SHORT", now_ms)

                actions.append(TradeAction(
                    action="OPEN",
                    symbol=symbol,
                    side="SHORT",
                    price=price,
                    sig=sig_s,
                ))

        # --- Long ---
        recent_long_time = self.deps.entry_store_get(symbol, "LONG")
        long_amt = abs(float(((pos.get("LONG") or {}).get("qty")) or 0.0))
        long_eff_x = (long_amt * price / total_balance) if (total_balance and not signal_only) else 0.0

        if signal_only or long_eff_x < max_eff:
            sig_l = get_long_entry_signal(
                price=price,
                ma100=self.deps.get_now_ma100(symbol),
                prev3_candle=self.deps.get_prev3_candle(symbol),
                ma_threshold=self.deps.get_ma_threshold(symbol),
                momentum_threshold=self.deps.get_momentum_threshold(symbol),
                recent_entry_time=recent_long_time,
                reentry_cooldown_sec=60 * 60,
            )
            if sig_l:
                now_ms = int(time.time() * 1000)
                self.deps.log_signal(symbol, sig_l)
                self.deps.entry_store_set(symbol, "LONG", now_ms)

                actions.append(TradeAction(
                    action="OPEN",
                    symbol=symbol,
                    side="LONG",
                    price=price,
                    sig=sig_l,
                ))

        return actions
