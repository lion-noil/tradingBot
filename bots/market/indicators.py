# bots/market/indicators.py
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP
import json
from typing import Dict, Optional, List, Tuple, Callable
from dataclasses import dataclass

KST = timezone(timedelta(hours=9))

# ── 시간/표시 ──────────────────────────────────────
def kst_now_str() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S %z")

# ── 임계값 양자화 ───────────────────────────────────
def quantize_thr(thr: Optional[float], lo: float = 0.005, hi: float = 0.07) -> Optional[float]:
    if thr is None:
        return None
    v = Decimal(str(max(lo, min(hi, float(thr)))))
    return float(v.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def arrow(prev: Optional[float], new: Optional[float]) -> str:
    if prev is None or new is None:
        return "→"
    return "↑" if new > prev else ("↓" if new < prev else "→")



def fmt_pct(v: Optional[float]) -> str:
    return "—" if v is None else f"{float(v) * 100:.3f}%"


# ── Redis 스트림 로깅(xadd) ────────────────────────
def xadd_pct_log(
    redis_client,
    symbol: str,
    name: str,
    prev: Optional[float],
    new: Optional[float],
    arrow_mark: str,
    msg: str,
    *,
    namespace: Optional[str] = None,
    stream_key: Optional[str] = None,
    cross_times: Optional[List[Tuple[str, str, float, float, float]]] = None,
    cross_times_max: int = 20,  # 너무 크면 최근 N개만
) -> None:
    """
    - 기본 키: "OpenPctLog"
    - namespace가 있으면 기본 키: "trading:{namespace}:OpenPctLog"
    - stream_key를 직접 넘기면 그 값을 그대로 사용
    """
    if redis_client is None:
        return

    # 최종 스트림 키 결정
    if stream_key is None:
        if namespace:
            stream_key = f"trading:{namespace}:OpenPctLog"
        else:
            stream_key = "OpenPctLog"

    def _fmt(x):
        return "" if x is None else f"{float(x):.10f}"

    # 필요시 최근 N개만 유지
    if cross_times:
        trimmed = cross_times[-cross_times_max:]
        ct_dicts = [
            {
                "dir": d,
                "time": t,
                "price": float(p),
                "bid": float(b),
                "ask": float(a),
            }
            for (d, t, p, b, a) in trimmed
        ]
        ct_json = json.dumps(ct_dicts, ensure_ascii=False)
    else:
        ct_json = ""

    fields = {
        "ts": kst_now_str(),
        "sym": symbol,
        "name": name,
        "prev": _fmt(prev),
        "new": _fmt(new),
        "arrow": arrow_mark,
        "msg": msg,
        "cross_times": ct_json,
    }
    redis_client.xadd(stream_key, fields, maxlen=300, approximate=False)

# 2-1) 지표 계산 (순수)
def compute_indicators_for_symbol(candle_engine, indicator_engine, symbol: str):
    candles = candle_engine.get_candles(symbol)
    cross_times, q_thr, ma100s = indicator_engine.compute_all(candles)

    prev3_candle = None
    if len(candles) >= 4:  # 3분 전 봉을 확실히 잡으려면 보통 최소 4개 필요(아래 설명)
        c = candles[-4]
        if all(c.get(k) is not None for k in ("open", "high", "low", "close")):
            prev3_candle = {k: float(c[k]) for k in ("open", "high", "low", "close")}

    return {
        "cross_times": cross_times,
        "q_thr": q_thr,
        "ma100s": ma100s,
        "prev3_candle": prev3_candle,
    }



# 2-2) 임계값 파생치 & 로깅 메시지 준비 (순수)
def derive_thresholds_and_log(prev_q: Optional[float], thr_raw: Optional[float]):
    q = quantize_thr(thr_raw)
    mom_thr = (q / 2.0) if q is not None else None # momentum 기준
    # 로깅용 문자열(있을 때만)
    log = None
    if q != prev_q:
        arr = arrow(prev_q, q)
        log = {
            "msg": f"🔧 MA threshold: {fmt_pct(prev_q)} {arr} {fmt_pct(q)}",
            "arrow": arr,
            "prev_q": prev_q,
            "new_q": q,
        }
    return q, mom_thr, log


def refresh_indicators_for_symbol(
    candle_engine,
    indicator_engine,
    symbol: str,
    *,
    ma100s: Dict[str, List[Optional[float]]],
    now_ma100_map: Dict[str, Optional[float]],
    ma_threshold_map: Dict[str, Optional[float]],
    thr_quantized_map: Dict[str, Optional[float]],
    momentum_threshold_map: Dict[str, Optional[float]],
    prev3_candle_map: Dict[str, Optional[dict]],
    min_ma_threshold: float,   # ✅ 추가
    ma_check_enabled_map: Dict[str, bool],
    system_logger=None,
    redis_client=None,
    namespace: Optional[str] = None,
) -> None:
    """
    한 심볼에 대해:
    - 인디케이터 계산
    - MA threshold / momentum threshold / prev_close_3 반영
    - MA threshold 변경시 xadd_pct_log 로 로그 남김 (네임스페이스 포함 가능)
    """
    res = compute_indicators_for_symbol(candle_engine, indicator_engine, symbol)

    prev_q = thr_quantized_map.get(symbol)

    # 상태 반영
    ma100s[symbol] = res.get("ma100s") or []

    arr = ma100s[symbol]
    now = None
    for v in reversed(arr):
        if v is not None:
            now = float(v)
            break
    now_ma100_map[symbol] = now

    raw_thr = res["q_thr"]
    q, mom_thr, log = derive_thresholds_and_log(prev_q, raw_thr)

    thr_quantized_map[symbol] = q
    ma_threshold_map[symbol] = q
    momentum_threshold_map[symbol] = mom_thr

    # ✅ 체크 상태 전환 감지: (q가 min 이상이면 enabled)
    prev_enabled = bool(ma_check_enabled_map.get(symbol, False))
    now_enabled = (q is not None) and (float(q) >= float(min_ma_threshold))

    if prev_enabled != now_enabled:
        ma_check_enabled_map[symbol] = now_enabled

        # 로그 메시지
        state_msg = "✅ MA check ENABLED" if now_enabled else "⛔ MA check DISABLED"
        detail = f"(thr={fmt_pct(q)} / min={fmt_pct(min_ma_threshold)})"
        msg = f"[{symbol}] {state_msg} {detail}"

        if system_logger:
            system_logger.debug(msg)
    else:
        # 상태가 변하지 않으면 업데이트는 하지 않아도 되지만,
        # 초기 None 케이스/기본값 세팅 원하면 아래 한 줄은 유지해도 됨.
        ma_check_enabled_map.setdefault(symbol, now_enabled)

    # MA threshold 변경 시 내부 debug 로그만. OpenPctLog redis 발행 제거(2026-06-29):
    # basic(MA100) 은퇴 + 프론트 터치/임계 위젯 제거 → 아무도 안 씀. (계산은 상태맵 표시용으로 유지)
    if log and system_logger:
        system_logger.debug(f"[{symbol}] {log['msg']}")

    prev3_candle_map[symbol] = res.get("prev3_candle")  # None도 포함

@dataclass
class IndicatorState:
    """
    refresh_indicators_for_symbol에 넘기던 map들을 한 곳에 묶는 컨테이너
    """
    ma100s: Dict[str, List[Optional[float]]]
    now_ma100_map: Dict[str, Optional[float]]
    ma_threshold_map: Dict[str, Optional[float]]
    thr_quantized_map: Dict[str, Optional[float]]
    momentum_threshold_map: Dict[str, Optional[float]]
    prev3_candle_map: Dict[str, Optional[dict]]  # ✅ dict(open/high/low/close) 형태로 맞추기
    ma_check_enabled_map: Dict[str, bool]
    min_ma_threshold: float   # ✅ 이거 추가

def refresh_symbol_indicators(
    candle_engine,
    indicator_engine,
    symbol: str,
    state: IndicatorState,
    *,
    system_logger=None,
    redis_client=None,
    namespace: Optional[str] = None,
) -> None:
    """
    ✅ 상태(state)만 넘기면 되는 새 API
    """
    return refresh_indicators_for_symbol(
        candle_engine,
        indicator_engine,
        symbol,
        ma100s=state.ma100s,
        now_ma100_map=state.now_ma100_map,
        ma_threshold_map=state.ma_threshold_map,
        thr_quantized_map=state.thr_quantized_map,
        momentum_threshold_map=state.momentum_threshold_map,
        prev3_candle_map=state.prev3_candle_map,
        ma_check_enabled_map=state.ma_check_enabled_map,   # ✅ 추가
        min_ma_threshold=state.min_ma_threshold,
        system_logger=system_logger,
        redis_client=redis_client,
        namespace=namespace,
    )


def bind_refresher(
    candle_engine,
    indicator_engine,
    state: IndicatorState,
    *,
    system_logger=None,
    redis_client=None,
    namespace: Optional[str] = None,
) -> Callable[[str], None]:
    """
    ✅ (symbol) -> None 형태의 바인딩된 refresher 반환
    TradeBot에서 self._refresh_indicators(symbol)로 쓰기 좋게 만듦.
    """
    def _refresh(symbol: str) -> None:
        refresh_symbol_indicators(
            candle_engine,
            indicator_engine,
            symbol,
            state,
            system_logger=system_logger,
            redis_client=redis_client,
            namespace=namespace,
        )
    return _refresh