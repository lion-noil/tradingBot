# bots/trading/signal_processor.py
from __future__ import annotations
from typing import Tuple
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional
import time
from strategies.basic_entry import get_short_entry_signal, get_long_entry_signal
from strategies.basic_exit import get_exit_signal
from strategies.s1_reversion import (
    S1Params, S1Position, s1_indicators, s1_entry_levels, s1_cooldown_ok, s1_exit_on_tick,
)

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

    # --- S1 전략 전용 (strategy="s1"일 때만 사용; basic이면 None) ---
    get_recent_closes: Optional[Callable[[str], Optional[List[float]]]] = None
    get_open_s1_positions: Optional[Callable[[str, str], List[tuple]]] = None
    get_last_exit_ts_ms: Optional[Callable[[str, str], Optional[int]]] = None
    set_last_exit_ts_ms: Optional[Callable[[str, str, int], None]] = None
    # ✅ S1 v2: 진입 기준 쿨다운용 (직전 진입 시각)
    get_last_entry_ts_ms: Optional[Callable[[str, str], Optional[int]]] = None
    set_last_entry_ts_ms: Optional[Callable[[str, str, int], None]] = None


class SignalProcessor:
    """
    - 신호 판단은 "signal/open-state"만 기준으로 가능하도록 분리
    - lot 선택/체결은 executor 책임
    """

    def __init__(self, *, deps: SignalProcessorDeps, system_logger=None,
                 strategy: str = "basic", s1_params: Optional[S1Params] = None,
                 basic_long_enabled: bool = True,
                 s1_params_by_symbol: Optional[Dict[str, S1Params]] = None,
                 s1_maxc_by_symbol: Optional[Dict[str, int]] = None,
                 s1_max_hold_sec: int = 14 * 24 * 3600):
        self.deps = deps
        self.system_logger = system_logger
        self.strategy = (strategy or "basic").lower()
        self.s1_params = s1_params or S1Params()
        self.basic_long_enabled = bool(basic_long_enabled)  # False면 basic 롱 진입 안 함(숏만)
        # ✅ S1 v2: 심볼별 파라미터/동시보유캡/최대보유
        self.s1_params_by_symbol = {str(k).upper(): v for k, v in (s1_params_by_symbol or {}).items()}
        self.s1_maxc_by_symbol = {str(k).upper(): int(v) for k, v in (s1_maxc_by_symbol or {}).items()}
        self.s1_max_hold_sec = int(s1_max_hold_sec or 0)
        # (symbol, side, anchor_signal_id) -> BOOST 누적 진입 횟수
        self._boost_attempts_by_anchor: Dict[tuple[str, str, str], int] = {}

    def _s1_params_for(self, symbol: str) -> S1Params:
        return self.s1_params_by_symbol.get((symbol or "").upper(), self.s1_params)

    def _s1_maxc_for(self, symbol: str) -> int:
        return self.s1_maxc_by_symbol.get((symbol or "").upper(), 1)  # 맵에 없으면 단일보유

    def _record(self, symbol: str, side: str, kind: str, price: Optional[float], sig: Dict[str, Any]) -> tuple[
        str, int]:
        return self.deps.log_signal(symbol, side, kind, price, sig)

    def _get_boost_attempts_by_anchor(self, symbol: str, side: str) -> Dict[str, int]:
        out: Dict[str, int] = {}

        for (sym, sd, anchor_id), cnt in self._boost_attempts_by_anchor.items():
            if sym == symbol and sd == side:
                out[str(anchor_id)] = int(cnt)

        return out

    def _remember_boost_attempt(self, symbol: str, side: str, sig: Dict[str, Any]) -> None:
        extra = sig.get("extra") or {}

        if not extra.get("is_boost"):
            return

        anchor_id = extra.get("anchor_signal_id")
        if not anchor_id:
            return

        key = (str(symbol), str(side), str(anchor_id))
        self._boost_attempts_by_anchor[key] = self._boost_attempts_by_anchor.get(key, 0) + 1

    async def process_symbol(self, symbol: str, price: Optional[float]) -> List[TradeAction]:
        if price is None:
            return []

        if self.strategy == "s1":
            return self._process_symbol_s1(symbol, price)

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
                    price=price,  # ✅ 이 줄 추가
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
                open_items=open_short,
                boost_attempts_by_anchor=self._get_boost_attempts_by_anchor(symbol, "SHORT"),
                ma_threshold=float(thr),
                momentum_threshold=mom_thr,
            )
            if sig_s:
                signal_id, _ = self._record(symbol, "SHORT", "ENTRY", price, sig_s)
                self._remember_boost_attempt(symbol, "SHORT", sig_s)
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

        if allow_long_add and self.basic_long_enabled:  # 🔴 basic 롱 비활성 시 진입 안 함(숏만)
            sig_l = get_long_entry_signal(
                price=price,
                ma100=now_ma100,
                prev3_candle=prev3,
                open_items=open_long,
                boost_attempts_by_anchor=self._get_boost_attempts_by_anchor(symbol, "LONG"),
                ma_threshold=float(thr),
                momentum_threshold=mom_thr,
            )
            if sig_l:
                signal_id, _ = self._record(symbol, "LONG", "ENTRY", price, sig_l)
                self._remember_boost_attempt(symbol, "LONG", sig_l)

                actions.append(TradeAction(
                    action="ENTRY",
                    symbol=symbol,
                    side="LONG",
                    price=price,
                    sig=sig_l,
                    signal_id=signal_id,
                ))

        return actions

    # ──────────────────────────────────────────────────────────────
    # S1 (σ-복귀 롱) 경로. 청산은 지표(z/MA) 가용성과 무관하게 최우선 평가.
    # ──────────────────────────────────────────────────────────────
    def _process_symbol_s1(self, symbol: str, price: float) -> List[TradeAction]:
        # 1) EXIT 먼저 — 현재가 + 진입 시 고정된 tp/sl만 사용(지표 워밍업과 무관)
        exits = self._decide_exits_s1(symbol, price)
        if exits:
            return exits
        # 2) ENTRY — z(win창) 계산 가능 + 쿨다운 통과 + 무포지션일 때만
        entry = self._decide_entry_s1(symbol, price)
        return [entry] if entry else []

    def _decide_exits_s1(self, symbol: str, price: float) -> List[TradeAction]:
        get_pos = self.deps.get_open_s1_positions
        if get_pos is None:
            return []
        side = "LONG"  # S1 롱온리
        now_ms = int(time.time() * 1000)
        actions: List[TradeAction] = []
        for row in (get_pos(symbol, side) or []):
            sid, ts_ms, ep, tp, sl = row
            if tp is None or sl is None or not ep:
                continue
            pos = S1Position(entry_price=float(ep), tp_price=float(tp),
                             sl_price=float(sl), entry_ts_ms=int(ts_ms or 0))
            reason = s1_exit_on_tick(pos, float(price))  # "SL"/"TP"/None (손절 우선)
            # ✅ v2: 14일 최대보유 초과 → 시장가 강제청산(TIME)
            if not reason and self.s1_max_hold_sec and ts_ms and \
                    (now_ms - int(ts_ms)) >= self.s1_max_hold_sec * 1000:
                reason = "TIME"
            if not reason:
                continue
            pnl_pct = (price / ep - 1.0) * 100.0 if ep else None
            payload = {
                "kind": "EXIT", "side": side, "mode": f"S1_{reason}", "strategy": "S1",
                "reasons": [f"S1_{reason}"], "open_signal_id": sid,
                "price": price, "entry_price": float(ep), "pnl_pct": pnl_pct,
                "tp_price": float(tp), "sl_price": float(sl),
            }
            signal_id, ts_out = self._record(symbol, side, "EXIT", price, payload)
            actions.append(TradeAction(
                action="EXIT", symbol=symbol, side=side, price=price,
                sig=payload, signal_id=signal_id, close_open_signal_id=sid,
            ))
            if self.deps.set_last_exit_ts_ms:
                self.deps.set_last_exit_ts_ms(symbol, side, int(ts_out))
        return actions

    def _decide_entry_s1(self, symbol: str, price: float) -> Optional[TradeAction]:
        side = "LONG"
        p = self._s1_params_for(symbol)       # ✅ v2: 심볼별 파라미터
        maxc = self._s1_maxc_for(symbol)      # ✅ v2: 동시보유 캡
        get_pos = self.deps.get_open_s1_positions
        # ✅ v2: 동시보유 허용 — open 수가 maxc 미만일 때만 신규 진입(쌓기)
        open_n = len(get_pos(symbol, side) or []) if get_pos is not None else 0
        if open_n >= maxc:
            return None
        # ✅ v2: 진입 기준 쿨다운 (직전 '진입' 시각 + cooldown)
        if self.deps.get_last_entry_ts_ms is not None:
            last_entry = self.deps.get_last_entry_ts_ms(symbol, side)
            if not s1_cooldown_ok(last_entry, int(time.time() * 1000), p):
                return None
        # 지표 (win창 종가 필요)
        if self.deps.get_recent_closes is None:
            return None
        closes = self.deps.get_recent_closes(symbol)
        if not closes:
            return None
        ma, sd, z = s1_indicators(closes, p.win, price)
        if z is None or ma is None or sd is None:
            return None
        lv = s1_entry_levels(z, ma, sd, float(price), p)
        if not lv:
            return None
        tp, sl = lv
        payload = {
            "kind": "ENTRY", "side": side, "strategy": "S1", "reasons": ["S1"],
            "price": price, "z": z, "ma": ma, "sd": sd,
            "tp_price": tp, "sl_price": sl, "k1": p.k1, "b": p.b,
        }
        signal_id, ts_ms_out = self._record(symbol, side, "ENTRY", price, payload)
        # ✅ v2: 진입 ts 기록(진입기준 쿨다운용)
        if self.deps.set_last_entry_ts_ms:
            self.deps.set_last_entry_ts_ms(symbol, side, int(ts_ms_out))
        return TradeAction(action="ENTRY", symbol=symbol, side=side, price=price,
                           sig=payload, signal_id=signal_id)
