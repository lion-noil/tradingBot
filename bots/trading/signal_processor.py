# bots/trading/signal_processor.py
from __future__ import annotations
from typing import Tuple
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional
import time
from strategies.basic_entry import get_short_entry_signal, get_long_entry_signal
from strategies.basic_exit import get_exit_signal

# ✅ tag 포함 (signal_id, ts_ms, entry_price, entry_tag)
Item = Tuple[str, int, float, str]


@dataclass
class TradeAction:
    action: str  # "ENTRY" | "EXIT"
    symbol: str
    side: str  # "LONG" | "SHORT"
    price: Optional[float] = None

    sig: Optional[Dict[str, Any]] = None
    signal_id: Optional[str] = None  # signals store에 기록된 id

    close_open_signal_id: Optional[str] = None


@dataclass
class SignalProcessorDeps:
    # --- state getters ---
    get_now_ma100: Callable[[str], Optional[float]]
    get_prev3_candle: Callable[[str], Optional[dict]]
    get_ma_threshold: Callable[[str], Optional[float]]
    get_momentum_threshold: Callable[[str], Optional[float]]

    # --- config getters ---
    get_position_max_hold_sec: Callable[[], int]
    get_near_touch_window_sec: Callable[[], int]

    # ✅ 이제 tag 포함해서 내려줘야 함
    get_open_signal_items: Callable[[str, str], List[Item]]  # (symbol, side) -> [(sid, ts, ep, tag), ...]

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

    def _decide_exits(self, symbol: str, price: float, now_ma100: float, thr: float) -> List[TradeAction]:
        actions: List[TradeAction] = []

        for side in ("LONG", "SHORT"):
            open_items = self.deps.get_open_signal_items(symbol, side)  # [(sid, ts, ep, tag), ...]

            if not open_items:
                continue

            sig = get_exit_signal(
                side=side,
                price=price,
                ma100=now_ma100,
                prev3_candle=self.deps.get_prev3_candle(symbol),
                open_items=open_items,  # ✅ 4튜플 그대로
                ma_threshold=float(thr),
                time_limit_sec=self.deps.get_position_max_hold_sec(),
                near_touch_window_sec=self.deps.get_near_touch_window_sec(),
                momentum_threshold=float(self.deps.get_momentum_threshold(symbol) or 0.0),
                last_scaleout_ts_ms=self.deps.get_last_scaleout_ts_ms(symbol, side),
            )

            if not sig:
                continue

            targets = sig.get("targets") or []
            if not targets:
                continue

            for target_open_id in targets:
                entry_price = 0.0
                for (sid, _ts, ep, _tag) in open_items:
                    if sid == target_open_id:
                        entry_price = float(ep or 0.0)
                        break

                pnl_pct = None
                if entry_price > 0:
                    if side == "LONG":
                        pnl_pct = (price - entry_price) / entry_price * 100.0
                    else:
                        pnl_pct = (entry_price - price) / entry_price * 100.0

                payload = {
                    **sig,
                    "open_signal_id": target_open_id,
                    "price": price,
                    "entry_price": entry_price,
                    "pnl_pct": pnl_pct,
                    "ma100": now_ma100,
                    "ma_delta_pct": (price - now_ma100) / max(now_ma100, 1e-12) * 100.0,
                }
                signal_id, ts_ms = self._record(symbol, side, "EXIT", price, payload)

                actions.append(TradeAction(
                    action="EXIT",
                    symbol=symbol,
                    side=side,
                    sig=payload,
                    signal_id=signal_id,
                    close_open_signal_id=target_open_id,
                ))

                if payload.get("mode") == "SCALE_OUT":
                    self.deps.set_last_scaleout_ts_ms(symbol, side, int(ts_ms))

        return actions

    def _decide_entries(self, symbol: str, price: float, now_ma100: float, thr: float) -> List[TradeAction]:
        actions: List[TradeAction] = []

        prev3 = self.deps.get_prev3_candle(symbol)
        mom_thr = self.deps.get_momentum_threshold(symbol)

        now_ms = int(time.time() * 1000)

        def _has_init(items: List[Item]) -> bool:
            return any((tag == "INIT") for (_sid, _ts, _ep, tag) in (items or []))

        def _init_age_sec(items: List[Item]) -> Optional[int]:
            # INIT이 여러개면 가장 오래된 INIT 기준(보통 1개일 것)
            inits = [(ts, sid) for (sid, ts, _ep, tag) in (items or []) if tag == "INIT"]
            if not inits:
                return None
            init_ts, _ = min(inits, key=lambda x: x[0])
            return max(0, (now_ms - int(init_ts)) // 1000)

        # ---------------- SHORT ----------------
        open_short = self.deps.get_open_signal_items(symbol, "SHORT")  # [(sid, ts, ep, tag), ...]

        # ✅ “포지션 있는 상태에서 추가진입 허용 조건”을 여기서 결정
        # 예: INIT이 없으면 추가진입 금지 (원하면 조건 바꾸면 됨)
        allow_short_add = (not open_short) or _has_init(open_short)

        if allow_short_add:

            sig_s = get_short_entry_signal(
                price=price,
                ma100=now_ma100,
                prev3_candle=prev3,
                open_items=open_short,  # ✅ 4튜플 그대로
                ma_threshold=float(thr),
                momentum_threshold=mom_thr,
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

        # ---------------- LONG ----------------
        open_long = self.deps.get_open_signal_items(symbol, "LONG")

        allow_long_add = (not open_long) or _has_init(open_long)

        if allow_long_add:
            sig_l = get_long_entry_signal(
                price=price,
                ma100=now_ma100,
                prev3_candle=prev3,
                open_items=open_long,  # ✅ 4튜플 그대로
                ma_threshold=float(thr),
                momentum_threshold=mom_thr,
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
