# bots/trading/signal_processor.py
from __future__ import annotations
from typing import Tuple
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from bots.state.signals import OpenSignalStats
from strategies.basic_strategy import (
    get_short_entry_signal, get_long_entry_signal, get_exit_signal
)
from bots.state.balances import get_total_balance_usd


@dataclass
class TradeAction:
    action: str  # "OPEN" | "CLOSE"
    symbol: str
    side: str  # "LONG" | "SHORT"
    price: Optional[float] = None

    sig: Optional[Dict[str, Any]] = None
    signal_id: Optional[str] = None  # signals store에 기록된 id

    close_open_signal_id: Optional[str] = None

Item = Tuple[str, int]   # (signal_id, ts_ms)


@dataclass
class SignalProcessorDeps:
    # --- state getters ---
    get_asset: Callable[[], Dict[str, Any]]
    get_now_ma100: Callable[[str], Optional[float]]
    get_prev3_candle: Callable[[str], Optional[dict]]
    get_ma_threshold: Callable[[str], Optional[float]]
    get_momentum_threshold: Callable[[str], Optional[float]]

    # --- config getters ---
    is_signal_only: Callable[[], bool]
    get_max_effective_leverage: Callable[[], float]
    get_position_max_hold_sec: Callable[[], int]
    get_ma_easing: Callable[[str], float]
    get_near_touch_window_sec: Callable[[], int]
    get_min_ma_threshold: Callable[[], Optional[float]]

    # ✅ open-state stats (from in-memory cache; redis fallback은 index가 알아서)
    get_open_signal_stats: Callable[[str, str], OpenSignalStats]  # (symbol, side) -> stats

    get_open_signal_items: Callable[[str, str], List[Item]]  # (symbol, side) -> items

    # --- logging / signal store ---
    log_signal: Callable[[str, str, str, Optional[float], Dict[str, Any]], tuple[str, int]]



class SignalProcessor:
    """
    - 신호 판단은 "signal/open-state"만 기준으로 가능하도록 분리
    - lot 선택/체결은 executor 책임
    """

    def __init__(self, *, deps: SignalProcessorDeps, system_logger=None):
        self.deps = deps
        self.system_logger = system_logger

    def _record(self, symbol: str, side: str, kind: str, price: Optional[float], sig: Dict[str, Any]) -> tuple[
        str, int]:
        return self.deps.log_signal(symbol, side, kind, price, sig)

    async def process_symbol(self, symbol: str, price: Optional[float]) -> List[TradeAction]:
        actions: List[TradeAction] = []

        now_ma100 = self.deps.get_now_ma100(symbol)
        if price is None or now_ma100 is None:
            return actions

        thr = self.deps.get_ma_threshold(symbol)
        if thr is None:
            return actions

        easing = float(self.deps.get_ma_easing(symbol) or 0.0)

        actions.extend(self._decide_exits(symbol, price, now_ma100, thr, easing))
        actions.extend(self._decide_entries(symbol, price, now_ma100, thr, easing))
        return actions

    def _decide_exits(
            self,
            symbol: str,
            price: float,
            now_ma100: float,
            thr: float,
            easing: float,
    ) -> List[TradeAction]:
        actions: List[TradeAction] = []
        exit_easing = easing

        for side in ("LONG", "SHORT"):
            open_items = self.deps.get_open_signal_items(symbol, side)  # [(sid, ts), newest-first]
            if not open_items:
                continue

            sig = get_exit_signal(
                side=side,
                price=price,
                ma100=now_ma100,
                open_items=open_items,  # [(open_signal_id, ts_ms), ...]
                ma_threshold=float(thr),
                exit_easing=float(exit_easing),
                time_limit_sec=self.deps.get_position_max_hold_sec(),
                near_touch_window_sec=self.deps.get_near_touch_window_sec(),
            )
            
            if not sig:
                continue

            targets = sig.get("targets") or []
            if not targets:
                continue

            for target_open_id in targets:
                payload = {**sig, "open_signal_id": target_open_id}  # 개별 close 이벤트용
                signal_id, _ = self._record(symbol, side, "CLOSE", price, payload)

                actions.append(TradeAction(
                    action="CLOSE",
                    symbol=symbol,
                    side=side,
                    sig=payload,  # ✅ payload 넣는게 좋음(개별 open_signal_id 포함)
                    signal_id=signal_id,
                    close_open_signal_id=target_open_id,
                ))

        return actions

    def _decide_entries(
            self,
            symbol: str,
            price: float,
            now_ma100: float,
            thr: float,
            easing: float,
    ) -> List[TradeAction]:
        asset = self.deps.get_asset()
        signal_only = self.deps.is_signal_only()

        wallet = (asset.get("wallet") or {})
        pos = ((asset.get("positions") or {}).get(symbol) or {})

        entry_ma_thr = max(0.0, float(thr) - float(easing))

        total_balance = get_total_balance_usd(wallet)
        max_eff = self.deps.get_max_effective_leverage()

        actions: List[TradeAction] = []

        # --- Short ---
        short_amt = abs(float(((pos.get("SHORT") or {}).get("qty")) or 0.0))
        short_eff_x = (short_amt * price / total_balance) if (total_balance and not signal_only) else 0.0

        if signal_only or short_eff_x < max_eff:
            stats = self.deps.get_open_signal_stats(symbol, "SHORT")
            recent_entry_ts = stats.newest_ts_ms  # ✅ 쿨다운 기준

            sig_s = get_short_entry_signal(
                price=price,
                ma100=now_ma100,
                prev3_candle=self.deps.get_prev3_candle(symbol),
                ma_threshold=entry_ma_thr,
                momentum_threshold=self.deps.get_momentum_threshold(symbol),
                recent_entry_time=recent_entry_ts,
                reentry_cooldown_sec=60 * 60,
            )
            if sig_s:
                signal_id, _ = self._record(symbol, "SHORT", "OPEN", price, sig_s)
                actions.append(TradeAction(
                    action="OPEN",
                    symbol=symbol,
                    side="SHORT",
                    price=price,
                    sig=sig_s,
                    signal_id=signal_id,
                ))

        # --- Long ---
        long_amt = abs(float(((pos.get("LONG") or {}).get("qty")) or 0.0))
        long_eff_x = (long_amt * price / total_balance) if (total_balance and not signal_only) else 0.0

        if signal_only or long_eff_x < max_eff:
            stats = self.deps.get_open_signal_stats(symbol, "LONG")
            recent_entry_ts = stats.newest_ts_ms

            sig_l = get_long_entry_signal(
                price=price,
                ma100=now_ma100,
                prev3_candle=self.deps.get_prev3_candle(symbol),
                ma_threshold=entry_ma_thr,
                momentum_threshold=self.deps.get_momentum_threshold(symbol),
                recent_entry_time=recent_entry_ts,
                reentry_cooldown_sec=60 * 60,
            )
            if sig_l:
                signal_id, _ = self._record(symbol, "LONG", "OPEN", price, sig_l)
                actions.append(TradeAction(
                    action="OPEN",
                    symbol=symbol,
                    side="LONG",
                    price=price,
                    sig=sig_l,
                    signal_id=signal_id,
                ))

        return actions
