# bots/trading/signal_processor.py
from __future__ import annotations
from typing import Tuple
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional
import time
from strategies.s1_reversion import (
    S1Params, S1Position, s1_indicators, s1_cooldown_ok,
    sigma_entry_levels, sigma_exit_on_tick, avgdown_levels, ewz_indicators,
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
    """시그마 엔진(S1~S4) 전용 deps. (basic/MA100 전략은 퇴출됨 → 관련 deps 제거)"""
    # --- logging / signal store ---
    log_signal: Callable[[str, str, str, Optional[float], Dict[str, Any]], tuple[str, int]]

    # --- 시그마(S1=추세/S2=역추세/S3=일봉추세/S4=일봉역추세) 전용 ---
    get_recent_closes: Callable[[str], Optional[List[float]]]
    get_open_s1_positions: Callable[[str, str], List[tuple]]
    # ✅ 유니버스(crypto/mt5/fx 3분류, 심볼 기반) 열린 게임 수 — 텔레그램 '전체 N' 표기용(없으면 생략)
    get_open_universe_count: Optional[Callable[[str], int]] = None
    get_last_exit_ts_ms: Optional[Callable[[str, str], Optional[int]]] = None
    set_last_exit_ts_ms: Optional[Callable[[str, str, int], None]] = None
    # ✅ 진입 기준 쿨다운용 (직전 진입 시각)
    get_last_entry_ts_ms: Optional[Callable[[str, str], Optional[int]]] = None
    set_last_entry_ts_ms: Optional[Callable[[str, str, int], None]] = None


class SignalProcessor:
    """
    시그마 엔진 전용(S1 추세 / S2 역추세 / S3 일봉추세 / S4 일봉역추세).
    - 신호 판단은 "signal/open-state"만 기준으로 가능하도록 분리
    - lot 선택/체결은 executor 책임
    - basic(MA100) 전략은 퇴출됨: 시그마 외 strategy는 아무 신호도 내지 않음(no-op).
    """

    def __init__(self, *, deps: SignalProcessorDeps, system_logger=None,
                 strategy: str = "s1", s1_params: Optional[S1Params] = None,
                 s1_params_by_symbol: Optional[Dict[str, S1Params]] = None,
                 s1_maxc_by_symbol: Optional[Dict[str, int]] = None,
                 s1_max_hold_sec: int = 14 * 24 * 3600,
                 avg_down: bool = False,
                 entries_disabled: bool = False):
        self.deps = deps
        self.system_logger = system_logger
        self.strategy = (strategy or "s1").lower()
        # ✅ 드레인 모드(구 전략 마이그레이션): 신규 진입(추매 포함) 전면 중지, 청산 관리만 지속
        self.entries_disabled = bool(entries_disabled)
        self.s1_params = s1_params or S1Params()
        # ✅ 시그마 엔진: 심볼별·방향별 파라미터/캡. 중첩 맵.
        #   s1_params_by_symbol = {SYM: {"LONG": S1Params, "SHORT": S1Params}}  (없는 방향은 키 부재)
        #   s1_maxc_by_symbol   = {SYM: {"LONG": int, "SHORT": int}}
        self.s1_params_by_symbol = {str(k).upper(): v for k, v in (s1_params_by_symbol or {}).items()}
        self.s1_maxc_by_symbol = {str(k).upper(): v for k, v in (s1_maxc_by_symbol or {}).items()}
        self.s1_max_hold_sec = int(s1_max_hold_sec or 0)
        # ✅ 추매(평단↓): True(S2 역추세)면 신호 재발생 시 새 포지션 대신 기존에 1회 추매(재앵커).
        #   1포지션 + 최대 1추매(2다리). False(S1 추세)면 종전 maxc 스택.
        self.avg_down = bool(avg_down)

    def _sigma_params_for(self, symbol: str, side: str) -> Optional[S1Params]:
        d = self.s1_params_by_symbol.get((symbol or "").upper())
        return d.get((side or "").upper()) if d else None

    def _sigma_maxc_for(self, symbol: str, side: str) -> int:
        d = self.s1_maxc_by_symbol.get((symbol or "").upper()) or {}
        return int(d.get((side or "").upper(), 1))

    def _universe_n(self, symbol: str) -> Optional[int]:
        """유니버스(crypto/mt5/fx — 심볼로 분류) 현재 열린 게임 수. 진입 직전 값(이 진입 제외).
        dep 미배선/오류 시 None → 텔레그램 '전체 N' 생략."""
        fn = getattr(self.deps, "get_open_universe_count", None)
        if fn is None:
            return None
        try:
            return int(fn(symbol))
        except Exception:
            return None

    def _sigma_mode(self, side: str):
        """(entry_high, position_long). 추세: entry_high==long, 역추세: entry_high!=long."""
        is_long = (side == "LONG")
        # 추세=s1(1분)/s3(일봉)/s11(1분봉책 z추세). 역추세=s2/s4/s12.
        entry_high = is_long if self.strategy in ("s1", "s3", "s11") else (not is_long)
        return entry_high, is_long

    def _hold_sec_for(self, symbol: str, side: str) -> int:
        """셀별 최대보유(S11 페이드 24~72h 등). 미지정(0)이면 채널 기본."""
        p = self._sigma_params_for(symbol, side)
        h = int(getattr(p, "hold_sec", 0) or 0) if p else 0
        return h if h > 0 else self.s1_max_hold_sec

    def _record(self, symbol: str, side: str, kind: str, price: Optional[float], sig: Dict[str, Any]) -> tuple[
        str, int]:
        return self.deps.log_signal(symbol, side, kind, price, sig)

    async def process_symbol(self, symbol: str, price: Optional[float]) -> List[TradeAction]:
        if price is None:
            return []
        if self.strategy in ("s1", "s2", "s3", "s4", "s11", "s12"):  # z-시그마 계열(동일 엔진)
            return self._process_sigma(symbol, price)
        if self.strategy == "s13":  # 급락페이드(1분봉책 신규 패밀리)
            return self._process_fade(symbol, price)
        if self.strategy == "s14":  # ewz 추세(S22 4시간봉책 신규 패밀리) — 시간청산 전용
            return self._process_ewz(symbol, price)
        # basic(MA100) 등 비-시그마 전략은 퇴출 → 신호 없음(config publish/상태표시 노드).
        return []

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
            hold = self._hold_sec_for(symbol, side)
            _sym_games = len(games)            # ✅ 청산 전 이 심볼·방향 게임 수(남은중첩 계산용)
            _uni_now = self._universe_n(symbol)  # ✅ 청산 전 유니버스 게임 수
            _closed = 0
            for gid, legs in games.items():
                first_ts = int(legs[0][1] or 0)
                last_tp, last_sl = float(legs[-1][3]), float(legs[-1][4])
                pos = S1Position(0.0, last_tp, last_sl, first_ts)
                reason = sigma_exit_on_tick(pos, float(price), position_long=is_long)
                if not reason and hold and first_ts and \
                        (now_ms - first_ts) >= hold * 1000:
                    reason = "TIME"
                if not reason:
                    continue
                _closed += 1                    # ✅ 이 게임 청산 → 남은 = 전체 − 누적청산
                _rem_sym = max(0, _sym_games - _closed)
                _rem_uni = max(0, _uni_now - _closed) if _uni_now is not None else None
                for r in legs:   # 그 게임의 전 다리 청산
                    sid, ep = r[0], float(r[2] or 0.0)
                    leg_ts = int(r[1] or 0)
                    pnl_pct = ((price / ep - 1.0) if is_long else (1.0 - price / ep)) * 100.0 if ep else None
                    payload = {
                        "kind": "EXIT", "side": side, "mode": f"{tag}_{reason}", "strategy": tag,
                        "reasons": [f"{tag}_{reason}"], "open_signal_id": sid,
                        "price": price, "entry_price": float(ep), "pnl_pct": pnl_pct,
                        "tp_price": last_tp, "sl_price": last_sl, "game_id": gid,
                        # ✅ 텔레그램 표기용: 실제 보유시간(이 다리) / 최대보유 + 청산 후 남은중첩·유니버스
                        "held_sec": max(0, (now_ms - leg_ts) // 1000) if leg_ts else None,
                        "max_hold_sec": hold or None,
                        "concurrent": _rem_sym, "max_concurrent": self._sigma_maxc_for(symbol, side),
                        "concurrent_universe": _rem_uni,
                    }
                    signal_id, ts_out = self._record(symbol, side, "EXIT", price, payload)
                    actions.append(TradeAction(action="EXIT", symbol=symbol, side=side, price=price,
                                               sig=payload, signal_id=signal_id, close_open_signal_id=sid))
                    if self.deps.set_last_exit_ts_ms:
                        self.deps.set_last_exit_ts_ms(symbol, side, int(ts_out))
            return actions

        # 비-추매(S1 추세 등): 다리별 독립 청산 (다리=게임)
        actions = []
        hold = self._hold_sec_for(symbol, side)
        _sym_games = len(rows)             # ✅ 청산 전 이 심볼·방향 게임 수
        _uni_now = self._universe_n(symbol)  # ✅ 청산 전 유니버스 게임 수
        _closed = 0
        for r in rows:
            sid, ts_ms, ep, tp, sl = r[0], r[1], r[2], r[3], r[4]
            pos = S1Position(float(ep), float(tp), float(sl), int(ts_ms or 0))
            reason = sigma_exit_on_tick(pos, float(price), position_long=is_long)
            if not reason and hold and ts_ms and \
                    (now_ms - int(ts_ms)) >= hold * 1000:
                reason = "TIME"
            if not reason:
                continue
            _closed += 1                    # ✅ 이 다리(=게임) 청산 → 남은 = 전체 − 누적청산
            _rem_sym = max(0, _sym_games - _closed)
            _rem_uni = max(0, _uni_now - _closed) if _uni_now is not None else None
            pnl_pct = ((price / ep - 1.0) if is_long else (1.0 - price / ep)) * 100.0 if ep else None
            payload = {
                "kind": "EXIT", "side": side, "mode": f"{tag}_{reason}", "strategy": tag,
                "reasons": [f"{tag}_{reason}"], "open_signal_id": sid,
                "price": price, "entry_price": float(ep), "pnl_pct": pnl_pct,
                "tp_price": float(tp), "sl_price": float(sl),
                # ✅ 텔레그램 표기용: 실제 보유시간 / 최대보유 + 청산 후 남은중첩·유니버스
                "held_sec": max(0, (now_ms - int(ts_ms)) // 1000) if ts_ms else None,
                "max_hold_sec": hold or None,
                "concurrent": _rem_sym, "max_concurrent": self._sigma_maxc_for(symbol, side),
                "concurrent_universe": _rem_uni,
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
        if self.entries_disabled:   # ✅ 드레인 모드: 신규 진입·추매 전면 중지(청산만 유지)
            return []
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
                    "cooldown_sec": int(p.cooldown_sec),
                    "game_id": gid,
                    # ✅ 텔레그램 표기용: 추매는 다리 수만 늘어남(게임 수 불변) → 현재중첩=n, 유니버스도 불변
                    "concurrent": n, "max_concurrent": self._sigma_maxc_for(symbol, side),
                    "concurrent_universe": self._universe_n(symbol),
                    "max_hold_sec": self._hold_sec_for(symbol, side) or None,
                }
                sigid, _ = self._record(symbol, side, "ENTRY", price, payload)
                actions.append(TradeAction(action="ENTRY", symbol=symbol, side=side, price=price,
                                           sig=payload, signal_id=sigid))

        # ── (b) 새 게임(중첩 유지) — maxc 캡만 적용(계정 200랏은 executor 증거금에서 별도 제한) ──
        if n < self._sigma_maxc_for(symbol, side):
            tp, sl = base_lv
            if getattr(p, "no_sl", False):   # ✅ S11 SL無 셀: SL을 도달불가 레벨로(청산=TP/TIME만)
                sl = price * 1e-9 if is_long else price * 1e9
            payload = {
                "kind": "ENTRY", "side": side, "strategy": tag, "reasons": [tag],
                "price": price, "z": z, "ma": ma, "sd": sd,
                "tp_price": tp, "sl_price": sl, "k1": p.k1, "b": p.b,
                "cooldown_sec": int(p.cooldown_sec),
                # ✅ 텔레그램 표기용: 이 진입 포함 현재중첩 / 최대중첩 / 최대보유 + 유니버스 전체(이 진입 포함)
                "concurrent": n + 1, "max_concurrent": self._sigma_maxc_for(symbol, side),
                "concurrent_universe": (lambda u: (u + 1) if u is not None else None)(self._universe_n(symbol)),
                "max_hold_sec": self._hold_sec_for(symbol, side) or None,
            }
            signal_id, ts_ms_out = self._record(symbol, side, "ENTRY", price, payload)
            if self.deps.set_last_entry_ts_ms:   # 글로벌 쿨다운 = 새 게임 기준(엔진 last)
                self.deps.set_last_entry_ts_ms(symbol, side, int(ts_ms_out))
            actions.append(TradeAction(action="ENTRY", symbol=symbol, side=side, price=price,
                                       sig=payload, signal_id=signal_id))
        return actions

    # ──────────────────────────────────────────────────────────────
    # 급락페이드(s13, HANDOFF_MASTER v4 §2-A′) — z와 독립: M분 원시 수익률 ≤ −drop_pct → 롱.
    #   청산: retr_mult>0 → TP=진입가×(1+retr_mult×|실낙폭|) + 시간캡 / retr_mult=0 → 순수 시간청산.
    #   SL 없음(도달불가 레벨 기록). 청산 스캔은 시그마 공용(_decide_exits_sigma: TP/TIME).
    # ──────────────────────────────────────────────────────────────
    def _process_fade(self, symbol: str, price: float) -> List[TradeAction]:
        exits = self._decide_exits_sigma(symbol, price, "LONG") if \
            self._sigma_params_for(symbol, "LONG") is not None else []
        if exits:
            return exits
        return self._decide_entry_fade(symbol, price)

    def _decide_entry_fade(self, symbol: str, price: float) -> List[TradeAction]:
        if self.entries_disabled:
            return []
        side = "LONG"   # 페이드는 급락 매수만(급등페이드숏은 검증 후 기각)
        p = self._sigma_params_for(symbol, side)
        if p is None or int(getattr(p, "m_min", 0) or 0) <= 0:
            return []
        now_ms = int(time.time() * 1000)
        if self.deps.get_last_entry_ts_ms is not None:
            if not s1_cooldown_ok(self.deps.get_last_entry_ts_ms(symbol, side), now_ms, p):
                return []
        rows = self.deps.get_open_s1_positions(symbol, side) or []
        if len(rows) >= self._sigma_maxc_for(symbol, side):
            return []
        closes = self.deps.get_recent_closes(symbol)
        m = int(p.m_min)
        if not closes or len(closes) <= m:
            return []
        ret = float(price) / float(closes[-1 - m]) - 1.0
        if ret > -float(p.drop_pct):     # 낙폭 미달
            return []
        # TP: 되돌림×배수(BTC) 또는 없음(시간청산 셀). SL 없음.
        if float(getattr(p, "retr_mult", 0.0) or 0.0) > 0:
            tp = price * (1.0 + float(p.retr_mult) * abs(ret))
        else:
            tp = price * 1e9
        sl = price * 1e-9
        payload = {
            "kind": "ENTRY", "side": side, "strategy": "S13", "reasons": ["S13"],
            "price": price, "m_min": m, "drop_pct": round(ret, 5),
            "tp_price": tp, "sl_price": sl,
            "cooldown_sec": int(p.cooldown_sec),
            # ✅ 텔레그램 표기용: 이 진입 포함 현재중첩 / 최대중첩 / 최대보유(페이드 셀별 24~72h) + 유니버스 전체
            "concurrent": len(rows) + 1, "max_concurrent": self._sigma_maxc_for(symbol, side),
            "concurrent_universe": (lambda u: (u + 1) if u is not None else None)(self._universe_n(symbol)),
            "max_hold_sec": self._hold_sec_for(symbol, side) or None,
        }
        signal_id, ts_ms_out = self._record(symbol, side, "ENTRY", price, payload)
        if self.deps.set_last_entry_ts_ms:
            self.deps.set_last_entry_ts_ms(symbol, side, int(ts_ms_out))
        return [TradeAction(action="ENTRY", symbol=symbol, side=side, price=price,
                            sig=payload, signal_id=signal_id)]

    # ──────────────────────────────────────────────────────────────
    # ewz 추세(s14, HANDOFF_S22) — EMA 잔차 z: resid=C−EMA_s, σ=EMA_s(|resid|), ez=resid/σ.
    #   진입: 롱 ez≥+K1 / 숏 ez≤−K1 (방향은 파라미터 키 LONG/SHORT로 지정).
    #   청산: 시간청산 전용(셀별 hold_sec) — TP/SL 없음(도달불가 레벨 기록, 스캔은 시그마 공용 TIME).
    # ──────────────────────────────────────────────────────────────
    def _process_ewz(self, symbol: str, price: float) -> List[TradeAction]:
        exits: List[TradeAction] = []
        for side in ("LONG", "SHORT"):
            if self._sigma_params_for(symbol, side) is not None:
                exits += self._decide_exits_sigma(symbol, price, side)
        if exits:
            return exits
        entries: List[TradeAction] = []
        for side in ("LONG", "SHORT"):
            if self._sigma_params_for(symbol, side) is not None:
                entries += self._decide_entry_ewz(symbol, price, side)
        return entries

    def _decide_entry_ewz(self, symbol: str, price: float, side: str) -> List[TradeAction]:
        if self.entries_disabled:
            return []
        p = self._sigma_params_for(symbol, side)
        if p is None or int(getattr(p, "ewz_s", 0) or 0) <= 0:
            return []
        is_long = (side == "LONG")
        now_ms = int(time.time() * 1000)
        if self.deps.get_last_entry_ts_ms is not None:
            if not s1_cooldown_ok(self.deps.get_last_entry_ts_ms(symbol, side), now_ms, p):
                return []
        rows = self.deps.get_open_s1_positions(symbol, side) or []
        n = len(rows)
        if n >= self._sigma_maxc_for(symbol, side):
            return []
        closes = self.deps.get_recent_closes(symbol)
        s = int(p.ewz_s)
        if not closes or len(closes) <= s:
            return []
        # closes[-1]은 진행중 봉(REST 스냅샷) — 현재가를 그 봉의 종가로 치환해 ez 산출
        ez = ewz_indicators(closes[:-1], s, price)
        if ez is None:
            return []
        if is_long:
            if ez < p.k1:
                return []
        else:
            if ez > -p.k1:
                return []
        # 시간청산 전용: TP/SL 도달불가 레벨(청산 스캔은 TIME만 발동)
        if is_long:
            tp, sl = price * 1e9, price * 1e-9
        else:
            tp, sl = price * 1e-9, price * 1e9
        payload = {
            "kind": "ENTRY", "side": side, "strategy": "S14", "reasons": ["S14"],
            "price": price, "z": round(float(ez), 4), "k1": p.k1, "ewz_s": s,
            "tp_price": tp, "sl_price": sl,
            "cooldown_sec": int(p.cooldown_sec),
            "concurrent": n + 1, "max_concurrent": self._sigma_maxc_for(symbol, side),
            "concurrent_universe": (lambda u: (u + 1) if u is not None else None)(self._universe_n(symbol)),
            "max_hold_sec": self._hold_sec_for(symbol, side) or None,
        }
        signal_id, ts_ms_out = self._record(symbol, side, "ENTRY", price, payload)
        if self.deps.set_last_entry_ts_ms:
            self.deps.set_last_entry_ts_ms(symbol, side, int(ts_ms_out))
        return [TradeAction(action="ENTRY", symbol=symbol, side=side, price=price,
                            sig=payload, signal_id=signal_id)]
