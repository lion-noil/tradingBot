# bots/trading/signal_processor.py
from __future__ import annotations
from typing import Tuple
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional
import time
from strategies.basic_entry import get_short_entry_signal, get_long_entry_signal
from strategies.basic_exit import get_exit_signal
from strategies.s1_reversion import (
    S1Params, S1Position, s1_indicators, s1_cooldown_ok,
    sigma_entry_levels, sigma_exit_on_tick, avgdown_levels,
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
                 basic_short_enabled: bool = True,
                 s1_params_by_symbol: Optional[Dict[str, S1Params]] = None,
                 s1_maxc_by_symbol: Optional[Dict[str, int]] = None,
                 s1_max_hold_sec: int = 14 * 24 * 3600,
                 avg_down: bool = False):
        self.deps = deps
        self.system_logger = system_logger
        self.strategy = (strategy or "basic").lower()
        self.s1_params = s1_params or S1Params()
        self.basic_long_enabled = bool(basic_long_enabled)   # False면 basic 롱 진입 안 함
        self.basic_short_enabled = bool(basic_short_enabled)  # False면 basic 숏 진입 안 함
        # ✅ 시그마 엔진(s1=추세/s2=역추세): 심볼별·방향별 파라미터/캡. 중첩 맵.
        #   s1_params_by_symbol = {SYM: {"LONG": S1Params, "SHORT": S1Params}}  (없는 방향은 키 부재)
        #   s1_maxc_by_symbol   = {SYM: {"LONG": int, "SHORT": int}}
        self.s1_params_by_symbol = {str(k).upper(): v for k, v in (s1_params_by_symbol or {}).items()}
        self.s1_maxc_by_symbol = {str(k).upper(): v for k, v in (s1_maxc_by_symbol or {}).items()}
        self.s1_max_hold_sec = int(s1_max_hold_sec or 0)
        # ✅ 추매(평단↓): True(S2 역추세)면 신호 재발생 시 새 포지션 대신 기존에 1회 추매(재앵커).
        #   1포지션 + 최대 1추매(2다리). False(S1 추세)면 종전 maxc 스택.
        self.avg_down = bool(avg_down)
        # (symbol, side, anchor_signal_id) -> BOOST 누적 진입 횟수
        self._boost_attempts_by_anchor: Dict[tuple[str, str, str], int] = {}

    def _sigma_params_for(self, symbol: str, side: str) -> Optional[S1Params]:
        d = self.s1_params_by_symbol.get((symbol or "").upper())
        return d.get((side or "").upper()) if d else None

    def _sigma_maxc_for(self, symbol: str, side: str) -> int:
        d = self.s1_maxc_by_symbol.get((symbol or "").upper()) or {}
        return int(d.get((side or "").upper(), 1))

    def _sigma_mode(self, side: str):
        """(entry_high, position_long). 추세(s1): entry_high==long, 역추세(s2): entry_high!=long."""
        is_long = (side == "LONG")
        entry_high = is_long if self.strategy == "s1" else (not is_long)
        return entry_high, is_long

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

        if self.strategy in ("s1", "s2"):
            return self._process_sigma(symbol, price)

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

        if allow_short_add and self.basic_short_enabled:  # 🔴 basic 숏 비활성 시 진입 안 함

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
    # 시그마 엔진 (s1=추세 / s2=역추세). 각 심볼 롱+숏(설정된 방향만). 청산 최우선.
    #   진입/청산 방향·z부호는 _sigma_mode(side)로 결정.
    #   같은 namespace에 두 전략 공존 가능 → 포지션은 strategy 태그로 분리(list_open_s1 tag).
    # ──────────────────────────────────────────────────────────────
    def _process_sigma(self, symbol: str, price: float) -> List[TradeAction]:
        exits: List[TradeAction] = []
        for side in ("LONG", "SHORT"):
            if self._sigma_params_for(symbol, side) is not None:
                exits += self._decide_exits_sigma(symbol, price, side)
        if exits:
            return exits
        entries: List[TradeAction] = []
        for side in ("LONG", "SHORT"):
            if self._sigma_params_for(symbol, side) is not None:
                entries += self._decide_entry_sigma(symbol, price, side)
        return entries

    @staticmethod
    def _group_games(rows: List[tuple]) -> "Dict[str, List[tuple]]":
        """오픈 레그들을 game_id(r[5])로 묶어 게임 단위로. 각 게임 레그는 ts 오름차순.
        한 게임 = 첫 진입 + (추매 다리). game.adds = len(legs)-1."""
        games: Dict[str, List[tuple]] = {}
        for r in rows:
            gid = (r[5] if len(r) > 5 and r[5] else r[0])
            games.setdefault(str(gid), []).append(r)
        for gid in games:
            games[gid].sort(key=lambda r: int(r[1] or 0))
        return games

    def _decide_exits_sigma(self, symbol: str, price: float, side: str) -> List[TradeAction]:
        get_pos = self.deps.get_open_s1_positions
        if get_pos is None:
            return []
        _, is_long = self._sigma_mode(side)
        tag = self.strategy.upper()  # "S1"(추세) / "S2"(역추세)
        now_ms = int(time.time() * 1000)
        rows = [r for r in (get_pos(symbol, side) or []) if r[3] is not None and r[4] is not None and r[2]]
        if not rows:
            return []

        if self.avg_down:
            # 중첩(ontop): 한 심볼·방향에 여러 게임 동시보유. 게임=game_id로 묶인 레그들.
            # 게임마다 독립 청산: 유효 tp/sl=그 게임 최신 다리(재앵커 반영), 14일=그 게임 첫 다리.
            # 트리거 시 그 게임의 전 다리만 동시청산(다른 게임은 유지).
            games = self._group_games(rows)
            actions: List[TradeAction] = []
            for gid, legs in games.items():
                first_ts = int(legs[0][1] or 0)
                last_tp, last_sl = float(legs[-1][3]), float(legs[-1][4])
                pos = S1Position(0.0, last_tp, last_sl, first_ts)
                reason = sigma_exit_on_tick(pos, float(price), position_long=is_long)
                if not reason and self.s1_max_hold_sec and first_ts and \
                        (now_ms - first_ts) >= self.s1_max_hold_sec * 1000:
                    reason = "TIME"
                if not reason:
                    continue
                for r in legs:   # 그 게임의 전 다리 청산
                    sid, ep = r[0], float(r[2] or 0.0)
                    pnl_pct = ((price / ep - 1.0) if is_long else (1.0 - price / ep)) * 100.0 if ep else None
                    payload = {
                        "kind": "EXIT", "side": side, "mode": f"{tag}_{reason}", "strategy": tag,
                        "reasons": [f"{tag}_{reason}"], "open_signal_id": sid,
                        "price": price, "entry_price": float(ep), "pnl_pct": pnl_pct,
                        "tp_price": last_tp, "sl_price": last_sl, "game_id": gid,
                    }
                    signal_id, ts_out = self._record(symbol, side, "EXIT", price, payload)
                    actions.append(TradeAction(action="EXIT", symbol=symbol, side=side, price=price,
                                               sig=payload, signal_id=signal_id, close_open_signal_id=sid))
                    if self.deps.set_last_exit_ts_ms:
                        self.deps.set_last_exit_ts_ms(symbol, side, int(ts_out))
            return actions

        # 비-추매(S1 추세 등): 다리별 독립 청산
        actions = []
        for r in rows:
            sid, ts_ms, ep, tp, sl = r[0], r[1], r[2], r[3], r[4]
            pos = S1Position(float(ep), float(tp), float(sl), int(ts_ms or 0))
            reason = sigma_exit_on_tick(pos, float(price), position_long=is_long)
            if not reason and self.s1_max_hold_sec and ts_ms and \
                    (now_ms - int(ts_ms)) >= self.s1_max_hold_sec * 1000:
                reason = "TIME"
            if not reason:
                continue
            pnl_pct = ((price / ep - 1.0) if is_long else (1.0 - price / ep)) * 100.0 if ep else None
            payload = {
                "kind": "EXIT", "side": side, "mode": f"{tag}_{reason}", "strategy": tag,
                "reasons": [f"{tag}_{reason}"], "open_signal_id": sid,
                "price": price, "entry_price": float(ep), "pnl_pct": pnl_pct,
                "tp_price": float(tp), "sl_price": float(sl),
            }
            signal_id, ts_out = self._record(symbol, side, "EXIT", price, payload)
            actions.append(TradeAction(action="EXIT", symbol=symbol, side=side, price=price,
                                       sig=payload, signal_id=signal_id, close_open_signal_id=sid))
            if self.deps.set_last_exit_ts_ms:
                self.deps.set_last_exit_ts_ms(symbol, side, int(ts_out))
        return actions

    def _decide_entry_sigma(self, symbol: str, price: float, side: str) -> List[TradeAction]:
        """정본(중첩/ontop): 유효 신호+쿨다운 통과 시 — (a) 열린 각 게임에 추매 1회(역추세 전용),
        (b) 새 게임 오픈(중첩 유지). 비-추매(S1 추세)는 (b)만 = maxc 스택."""
        p = self._sigma_params_for(symbol, side)
        if p is None:
            return []
        entry_high, is_long = self._sigma_mode(side)
        tag = self.strategy.upper()
        get_pos = self.deps.get_open_s1_positions
        rows = sorted((get_pos(symbol, side) or []), key=lambda r: int(r[1] or 0))
        n = len(rows)
        now_ms = int(time.time() * 1000)
        # 글로벌 쿨다운(새 게임 간격). 핸드오프 §4: 통과 못하면 추매·신규 둘 다 스킵.
        if self.deps.get_last_entry_ts_ms is not None:
            if not s1_cooldown_ok(self.deps.get_last_entry_ts_ms(symbol, side), now_ms, p):
                return []
        if self.deps.get_recent_closes is None:
            return []
        closes = self.deps.get_recent_closes(symbol)
        if not closes:
            return []
        ma, sd, z = s1_indicators(closes, p.win, price)
        if z is None or ma is None or sd is None:
            return []
        # 진입신호(z) 충족 여부 = sigma_entry_levels None 아님
        base_lv = sigma_entry_levels(z, ma, sd, float(price), p,
                                     entry_high=entry_high, position_long=is_long)
        if not base_lv:
            return []

        actions: List[TradeAction] = []
        cd_ms = int(p.cooldown_sec) * 1000

        # ── (a) 추매: 열린 각 게임에 1회(레그<2 & 그 게임 쿨다운 경과). 역추세 전용. ──
        if self.avg_down:
            for gid, legs in self._group_games(rows).items():
                if len(legs) >= 2:                       # 이미 추매됨(max_adds=1)
                    continue
                first_ts = int(legs[0][1] or 0)
                if first_ts and (now_ms - first_ts) < cd_ms:   # 그 게임 쿨다운 미경과
                    continue
                epx = [float(l[2] or 0.0) for l in legs if (l[2] or 0) > 0]
                if not epx:
                    continue
                avg = (sum(epx) + float(price)) / (len(epx) + 1)   # 1:1 균등 → 단순평균
                lv = avgdown_levels(ma, sd, avg, p, position_long=is_long)
                if not lv:                               # 재앵커 무효면 이 게임은 추매 안 함
                    continue
                a_tp, a_sl = lv
                payload = {
                    "kind": "ENTRY", "side": side, "strategy": tag, "reasons": [tag, "ADD"],
                    "price": price, "z": z, "ma": ma, "sd": sd,
                    "tp_price": a_tp, "sl_price": a_sl, "k1": p.k1, "b": p.b,
                    "game_id": gid,
                }
                sigid, _ = self._record(symbol, side, "ENTRY", price, payload)
                actions.append(TradeAction(action="ENTRY", symbol=symbol, side=side, price=price,
                                           sig=payload, signal_id=sigid))

        # ── (b) 새 게임(중첩 유지) — maxc 캡만 적용(계정 200랏은 executor 증거금에서 별도 제한) ──
        if n < self._sigma_maxc_for(symbol, side):
            tp, sl = base_lv
            payload = {
                "kind": "ENTRY", "side": side, "strategy": tag, "reasons": [tag],
                "price": price, "z": z, "ma": ma, "sd": sd,
                "tp_price": tp, "sl_price": sl, "k1": p.k1, "b": p.b,
            }
            signal_id, ts_ms_out = self._record(symbol, side, "ENTRY", price, payload)
            if self.deps.set_last_entry_ts_ms:   # 글로벌 쿨다운 = 새 게임 기준(엔진 last)
                self.deps.set_last_entry_ts_ms(symbol, side, int(ts_ms_out))
            actions.append(TradeAction(action="ENTRY", symbol=symbol, side=side, price=price,
                                       sig=payload, signal_id=signal_id))
        return actions
