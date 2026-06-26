"""
S1 · σ-복귀 역추세 롱 — 라이브용 자족(self-contained) 시그널 로직.

reversion_research 백테스트(bt_z_tppct_sl)와 동일 규칙을 라이브 봇에서 쓰도록 분리.
순수 함수 + 작은 상태 객체. 거래소/주문/레디스 의존성 없음(상위에서 주입).

규칙 (롱온리):
  - 지표: MA = win분 이동평균(종가), SD = 같은 창 표준편차(모집단). z=(price-MA)/SD
  - 진입: 포지션 없음 + 쿨다운 경과 + z <= -K1
  - 청산레벨(진입 시 고정): TP_price = MA - B*SD  (B < K1 필수, pct>0 가드)
           pct = TP_price/entry - 1,  SL_price = entry*(1 - pct)  (대칭)
  - 청산: 가격 >= TP_price(익절) / <= SL_price(손절). (동시면 손절 우선 — 호출측 봉단위 처리)
  - 쿨다운: 청산 후 cooldown_sec 동안 신규 진입 금지.

라이브 적용:
  from strategies.s1_reversion import S1Params, s1_indicators, s1_entry_levels, s1_exit_on_tick
  매 1분봉 확정마다 s1_indicators(closes)로 (ma,sd,z) 갱신,
  flat이면 s1_entry_levels(...)로 진입 판단, 보유면 s1_exit_on_tick(...)로 청산 판단.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple
import math


@dataclass(frozen=True)
class S1Params:
    win: int = 10080            # MA/σ 창 (1분봉 7일)
    k1: float = 2.5             # 진입 z 임계 (z <= -k1 이면 롱)
    b: float = 2.0              # TP 복귀 밴드 (z=-b). b < k1 필수
    cooldown_sec: int = 12 * 3600
    fee_roundtrip: float = 0.0011  # 리포팅용(체결가엔 미반영)

    def validate(self) -> None:
        # B<0 허용(v2): b<0이면 TP가 평균 위(오버슈팅까지). b<k1만 필수(TP가 진입가보다 위 보장은 levels에서 가드).
        if not (self.b < self.k1):
            raise ValueError(f"S1: b({self.b}) < k1({self.k1}) 위반")


@dataclass
class S1Position:
    entry_price: float
    tp_price: float
    sl_price: float
    entry_ts_ms: int


def s1_indicators(closes: Sequence[float], win: int, price: Optional[float] = None
                  ) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """최근 win개 종가로 (MA, SD, z) 산출. 표본 부족이면 (None,None,None).
    price 미지정 시 마지막 종가로 z 계산."""
    n = len(closes)
    if n < win:
        return None, None, None
    w = closes[n - win:]
    m = sum(w) / win
    var = sum((x - m) * (x - m) for x in w) / win
    sd = math.sqrt(var) if var > 0 else 0.0
    if sd <= 0:
        return m, sd, None
    px = float(price) if price is not None else float(w[-1])
    return m, sd, (px - m) / sd


def s1_entry_levels(z: Optional[float], ma: Optional[float], sd: Optional[float],
                    price: float, p: S1Params) -> Optional[Tuple[float, float]]:
    """flat & 쿨다운 통과 가정. 진입 조건 충족 시 (tp_price, sl_price) 반환, 아니면 None.
    호출측에서 쿨다운/flat 여부는 먼저 확인할 것."""
    if z is None or ma is None or sd is None or sd <= 0:
        return None
    if z > -p.k1:
        return None
    tp_price = ma - p.b * sd
    if tp_price <= price:        # 가드: pct>0 (B<K1이면 보통 성립, 얕은 진입 방지)
        return None
    pct = tp_price / price - 1.0
    sl_price = price * (1.0 - pct)
    return tp_price, sl_price


def s1_cooldown_ok(last_exit_ts_ms: Optional[int], now_ms: int, p: S1Params) -> bool:
    if last_exit_ts_ms is None:
        return True
    return (now_ms - int(last_exit_ts_ms)) >= p.cooldown_sec * 1000


def s1_exit_on_tick(pos: S1Position, price: float) -> Optional[str]:
    """라이브 틱 가격으로 청산 판단. 'SL' / 'TP' / None. 손절 우선."""
    if price <= pos.sl_price:
        return "SL"
    if price >= pos.tp_price:
        return "TP"
    return None


# ─────────────────────────────────────────────────────────────────────────────
# S2 (추세추종 숏) — 진입 신호는 S1과 동일(z ≤ -K1)이나 방향만 숏. TP/SL 미러.
#   pct = (MA - B·σ)/진입가 - 1 (pct>0 필수). TP=진입가×(1-pct)[아래], SL=진입가×(1+pct)[위].
# ─────────────────────────────────────────────────────────────────────────────
def s2_entry_levels(z: Optional[float], ma: Optional[float], sd: Optional[float],
                    price: float, p: S1Params) -> Optional[Tuple[float, float]]:
    """S2 추세숏 진입 레벨. 충족 시 (tp_price, sl_price) 반환(tp<진입가<sl), 아니면 None."""
    if z is None or ma is None or sd is None or sd <= 0:
        return None
    if z > -p.k1:
        return None
    ref = ma - p.b * sd
    pct = ref / price - 1.0
    if pct <= 0:                 # 가드: 거리 pct>0 (S1과 동일 산식)
        return None
    tp_price = price * (1.0 - pct)   # 아래(숏 익절)
    sl_price = price * (1.0 + pct)   # 위(숏 손절)
    return tp_price, sl_price


def s2_exit_on_tick(pos: S1Position, price: float) -> Optional[str]:
    """S2 숏 청산: 위(sl) 닿으면 손절(우선), 아래(tp) 닿으면 익절."""
    if price >= pos.sl_price:
        return "SL"
    if price <= pos.tp_price:
        return "TP"
    return None


def s1_exit_on_candle(pos: S1Position, high: float, low: float) -> Optional[str]:
    """봉(고가/저가) 기준 청산 — 백테스트와 동일(손절 우선)."""
    if low <= pos.sl_price:
        return "SL"
    if high >= pos.tp_price:
        return "TP"
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 자체 검증: 캐시 3년 데이터로 백테스트(bt_z_tppct_sl)와 일치하는지 확인.
#   python strategies/s1_reversion.py    (reversion_research 캐시 필요)
# ─────────────────────────────────────────────────────────────────────────────
def _selftest(symbol="BTCUSDT", k1=2.5, b=2.0, cd_sec=12*3600):
    import os, sys
    import numpy as np
    rr = os.path.join(os.path.dirname(__file__), "..", "..", "reversion_research")
    sys.path.insert(0, os.path.abspath(rr))
    from reversion_calibrator import _load_cache

    p = S1Params(win=10080, k1=k1, b=b, cooldown_sec=cd_sec)
    p.validate()
    m1 = _load_cache(symbol, "1")
    C = np.array([c["close"] for c in m1], float)
    H = np.array([c["high"] for c in m1], float)
    L = np.array([c["low"] for c in m1], float)
    ts = np.array([c["start"] for c in m1], np.int64)
    n = len(C)
    # 빠른 롤링 ma/sd (검증용 — 라이브는 s1_indicators 사용)
    cs = np.concatenate([[0.0], np.cumsum(C)]); cs2 = np.concatenate([[0.0], np.cumsum(C*C)])
    w = p.win
    ma = np.full(n, np.nan); sd = np.full(n, np.nan)
    s = cs[w:]-cs[:-w]; s2 = cs2[w:]-cs2[:-w]
    mm = s/w; var = np.maximum(s2/w-mm*mm, 0.0)
    ma[w-1:] = mm; sd[w-1:] = np.sqrt(var)
    z = np.where(sd > 0, (C-ma)/sd, np.nan)

    fee2 = p.fee_roundtrip
    pos: Optional[S1Position] = None
    last_exit = None
    rets = []
    for i in range(w, n):
        if not np.isfinite(z[i]):
            continue
        now = int(ts[i])
        if pos is None:
            if not s1_cooldown_ok(last_exit, now, p):
                continue
            lv = s1_entry_levels(float(z[i]), float(ma[i]), float(sd[i]), float(C[i]), p)
            if lv:
                tp, sl = lv
                pos = S1Position(float(C[i]), tp, sl, now)
        else:
            r = s1_exit_on_candle(pos, float(H[i]), float(L[i]))
            if r == "SL":
                rets.append(pos.sl_price/pos.entry_price - 1 - fee2); pos = None; last_exit = now
            elif r == "TP":
                rets.append(pos.tp_price/pos.entry_price - 1 - fee2); pos = None; last_exit = now
    import numpy as np
    r = np.array(rets)
    pf = r[r > 0].sum()/(-r[r < 0].sum()) if (r < 0).any() else float("inf")
    print(f"[S1 selftest] {symbol} K1={k1} B={b} CD={cd_sec//3600}h")
    print(f"  거래 {len(r)} | 승률 {(r>0).mean()*100:.0f}% | 누적 {(np.prod(1+r)-1)*100:+.1f}% | PF {pf:.2f}")
    print(f"  (기대: bt_z_tppct_sl와 동일 / BTC 2.5/2.0/12h = 145거래 승률55% +31.4% PF1.36)")


if __name__ == "__main__":
    _selftest()
