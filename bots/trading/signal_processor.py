# bots/trading/signal_processor.py
from __future__ import annotations
from typing import Tuple
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from strategies.basic_strategy import (
    get_short_entry_signal, get_long_entry_signal, get_exit_signal
)
from bots.state.balances import get_total_balance_usd


@dataclass
class TradeAction:
    action: str  # "ENTRY" | "EXIT"
    symbol: str
    side: str  # "LONG" | "SHORT"
    price: Optional[float] = None

    sig: Optional[Dict[str, Any]] = None
    signal_id: Optional[str] = None  # signals store에 기록된 id

    close_open_signal_id: Optional[str] = None


Item = Tuple[str, int, float]  # (signal_id, ts_ms, entry_price)


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
    get_near_touch_window_sec: Callable[[], int]

    get_open_signal_items: Callable[[str, str], List[Item]]  # (symbol, side) -> items
    # (symbol, side) -> last_scaleout_ts_ms (없으면 None)
    get_last_scaleout_ts_ms: Callable[[str, str], Optional[int]]
    set_last_scaleout_ts_ms: Callable[[str, str, int], None]

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
        if price is None:
            return []

        now_ma100 = self.deps.get_now_ma100(symbol)
        if now_ma100 is None:
            return []

        thr = self.deps.get_ma_threshold(symbol)
        if thr is None:
            return []

        # 1) EXIT 먼저
        exit_actions = self._decide_exits(symbol, price, now_ma100, thr)
        if exit_actions:
            return exit_actions  # ✅ EXIT만 (여러 개 가능)

        # 2) EXIT 없으면 ENTRY
        entry_actions = self._decide_entries(symbol, price, now_ma100, thr)
        if entry_actions:
            return [entry_actions[0]]

        return []

    def _decide_exits(
            self,
            symbol: str,
            price: float,
            now_ma100: float,
            thr: float,
    ) -> List[TradeAction]:
        actions: List[TradeAction] = []

        for side in ("LONG", "SHORT"):
            open_items = self.deps.get_open_signal_items(symbol, side)  # [(sid, ts, entry_price), newest-first]

            if not open_items:
                continue

            sig = get_exit_signal(
                side=side,
                price=price,
                ma100=now_ma100,
                prev3_candle=self.deps.get_prev3_candle(symbol),  # ✅ 추가
                open_items=open_items,  # [(open_signal_id, ts_ms, entry_price), ...]

                ma_threshold=float(thr),
                time_limit_sec=self.deps.get_position_max_hold_sec(),
                near_touch_window_sec=self.deps.get_near_touch_window_sec(),

                momentum_threshold=float(self.deps.get_momentum_threshold(symbol) or 0.0),  # ✅ 추가
                last_scaleout_ts_ms=self.deps.get_last_scaleout_ts_ms(symbol, side),
                scaleout_cooldown_sec=30 * 60,
            )

            if not sig:
                continue

            targets = sig.get("targets") or []
            if not targets:
                continue

            for target_open_id in targets:
                payload = {
                    **sig,
                    "open_signal_id": target_open_id,
                    "price": price,
                    "ma100": now_ma100,
                    "ma_delta_pct": (price - now_ma100) / max(now_ma100, 1e-12) * 100.0,
                }
                signal_id, ts_ms  = self._record(symbol, side, "EXIT", price, payload)


                actions.append(TradeAction(
                    action="EXIT",
                    symbol=symbol,
                    side=side,
                    sig=payload,  # ✅ payload 넣는게 좋음(개별 open_signal_id 포함)
                    signal_id=signal_id,
                    close_open_signal_id=target_open_id,
                ))

                # ✅ SCALE_OUT이면 쿨다운 캐시 갱신
                if payload.get("mode") == "SCALE_OUT":
                    self.deps.set_last_scaleout_ts_ms(symbol, side, int(ts_ms))

        return actions

    def _decide_entries(
            self,
            symbol: str,
            price: float,
            now_ma100: float,
            thr: float,
    ) -> List[TradeAction]:
        asset = self.deps.get_asset()
        signal_only = self.deps.is_signal_only()

        wallet = (asset.get("wallet") or {})
        pos = ((asset.get("positions") or {}).get(symbol) or {})

        total_balance = get_total_balance_usd(wallet)
        max_eff = self.deps.get_max_effective_leverage()

        actions: List[TradeAction] = []

        # --- Short ---
        short_amt = abs(float(((pos.get("SHORT") or {}).get("qty")) or 0.0))
        short_eff_x = (short_amt * price / total_balance) if (total_balance and not signal_only) else 0.0

        if signal_only or short_eff_x < max_eff:
            open_items = self.deps.get_open_signal_items(symbol, "SHORT")  # ✅ 추가

            sig_s = get_short_entry_signal(
                price=price,
                ma100=now_ma100,
                prev3_candle=self.deps.get_prev3_candle(symbol),
                open_items=open_items,  # ✅ 추가
                ma_threshold=float(thr),  # ✅ 원본 thr 그대로
                momentum_threshold=self.deps.get_momentum_threshold(symbol),
                reentry_cooldown_sec=30 * 60,  # ✅ 30분
            )
            if sig_s:
                signal_id, _ = self._record(symbol, "SHORT", "ENTRY", price, sig_s)
                actions.append(TradeAction(
                    action="ENTRY",
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
            open_items = self.deps.get_open_signal_items(symbol, "LONG")  # ✅ 추가

            sig_l = get_long_entry_signal(
                price=price,
                ma100=now_ma100,
                prev3_candle=self.deps.get_prev3_candle(symbol),
                open_items=open_items,  # ✅ 추가
                ma_threshold=float(thr),  # ✅ 원본 thr 그대로
                momentum_threshold=self.deps.get_momentum_threshold(symbol),
                reentry_cooldown_sec=30 * 60,  # ✅ 30분
            )
            if sig_l:
                signal_id, _ = self._record(symbol, "LONG", "ENTRY", price, sig_l)
                actions.append(TradeAction(
                    action="ENTRY",
                    symbol=symbol,
                    side="LONG",
                    price=price,
                    sig=sig_l,
                    signal_id=signal_id,
                ))

        return actions
