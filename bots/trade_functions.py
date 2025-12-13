# bots/trade_functions.py
import json
import hashlib
import time
import re
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone, timedelta
from typing import Callable, Dict, Any, Optional, Tuple, List

KST = timezone(timedelta(hours=9))

# ── 시간/표시 ──────────────────────────────────────
def kst_now_str() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S %z")


def arrow(prev: Optional[float], new: Optional[float]) -> str:
    if prev is None or new is None:
        return "→"
    return "↑" if new > prev else ("↓" if new < prev else "→")


def fmt_pct(v: Optional[float]) -> str:
    return "—" if v is None else f"{float(v) * 100:.3f}%"


# ── 임계값 양자화 ───────────────────────────────────
def quantize_thr(thr: Optional[float], lo: float = 0.005, hi: float = 0.03) -> Optional[float]:
    if thr is None:
        return None
    v = Decimal(str(max(lo, min(hi, float(thr)))))
    return float(v.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


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
    cross_times_max: int = 10,  # 너무 크면 최근 N개만
) -> None:
    """
    - 기본 키: "OpenPctLog"
    - namespace가 있으면 기본 키: "trading:{namespace}:OpenPctLog"
    - stream_key를 직접 넘기면 그 값을 그대로 사용
    """

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
    redis_client.xadd(stream_key, fields, maxlen=30, approximate=False)


# ── 트레이딩 시그널 업로드 ─────────────────────────
def upload_signal(redis_client, sig: Dict[str, Any], namespace: Optional[str] = None) -> None:
    """
    시그널은:
    - 기존 글로벌 키 "trading:signal" 에는 계속 저장 (기존 프론트/툴 호환용)
    - namespace 가 있으면 "trading:{namespace}:signal" 에도 추가로 저장
    """
    symbol = sig["symbol"]
    ts_iso = sig["ts"]
    day = ts_iso[:10]
    sid = hashlib.sha1(f"{symbol}|{ts_iso}".encode("utf-8")).hexdigest()
    field = f"{day}|{sid}"

    extra = sig.get("extra") or {}
    if "ts_ms" not in extra:
        extra["ts_ms"] = int(time.time() * 1000)
        sig["extra"] = extra

    value = json.dumps(sig, ensure_ascii=False, separators=(",", ":"))

    redis_client.hset("trading:signal", field, value)

    # 2) 네임스페이스별 키(플랫폼별 분리용)
    if namespace:
        key_ns = f"trading:{namespace}:signal"
        redis_client.hset(key_ns, field, value)


# ── 자산/포지션 문자열 빌더(순수 함수) ────────────────
def format_position_lines(
    get_price: Callable[[str], Optional[float]],
    taker_fee_rate: float,
    positions_for_symbol: Dict[str, Any],
    symbol: str,
) -> str:
    price = get_price(symbol)
    if price is None:
        return "  - 시세 없음\n"

    def _fmt_one(side_name: str, rec: Optional[Dict[str, Any]]) -> Optional[str]:
        if not rec:
            return None
        qty = float(rec.get("qty", 0.0) or 0.0)
        entry = float(rec.get("avg_price", 0.0) or 0.0)
        if qty <= 0 or entry <= 0:
            return None

        if side_name == "LONG":
            profit_rate = (price - entry) / entry * 100.0
            gross_profit = (price - entry) * qty
        else:
            profit_rate = (entry - price) / entry * 100.0
            gross_profit = (entry - price) * qty

        position_value = qty * entry
        fee_total = position_value * taker_fee_rate * 2  # 왕복
        net_profit = gross_profit - fee_total

        s = [
            f"  - 포지션: {side_name} ({qty}, {entry:.1f}, {profit_rate:+.3f}%, {net_profit:+.1f})"
        ]
        entries = rec.get("entries") or []
        for i, e in enumerate(entries, start=1):
            q = float(e.get("qty", 0.0) or 0.0)
            signed_qty = (-q) if side_name == "SHORT" else q
            t_str = e.get("ts_str") or e.get("ts")
            if isinstance(t_str, (int, float)):
                t_str = datetime.fromtimestamp(
                    int(t_str) / 1000, tz=KST
                ).strftime("%Y-%m-%d %H:%M:%S")
            price_e = float(e.get("price", 0.0) or 0.0)
            s.append(
                f"     └#{i} {signed_qty:+.3f} : {t_str or '-'}, {price_e:.1f} "
            )
        return "\n".join(s)

    pos = positions_for_symbol or {}
    long_line = _fmt_one("LONG", pos.get("LONG"))
    short_line = _fmt_one("SHORT", pos.get("SHORT"))

    if not long_line and not short_line:
        return "  - 포지션 없음\n"
    return "\n".join([x for x in (long_line, short_line) if x]) + "\n"


# ── 상태 로그(점프/MA/괴리율) 빌더 ────────────────────
def make_status_log_msg(
    total_usdt: float,
    symbols: List[str],
    jump_state: Dict[str, Dict[str, Any]],
    ma_threshold: Dict[str, Optional[float]],
    now_ma100: Dict[str, Optional[float]],
    get_price: Callable[[str], Optional[float]],
) -> str:
    log_msg = f"\n💰 총 자산: {total_usdt:.2f} USDT\n"
    for symbol in symbols:
        js = (jump_state or {}).get(symbol, {})
        state = js.get("state")
        min_dt = js.get("min_dt")
        max_dt = js.get("max_dt")
        thr_pct = (ma_threshold.get(symbol) or 0) * 100

        price = get_price(symbol)
        ma = now_ma100.get(symbol)
        diff_pct = (
            (price - ma) / ma * 100.0
            if (price is not None and ma not in (None, 0))
            else None
        )
        emoji = "📈" if state == "UP" else ("📉" if state == "DOWN" else "👀")

        parts = [f"{emoji} ma_thr({thr_pct:.2f}%)"]
        if price is not None:
            parts.append(f"P={price:.2f}")
        if ma is not None:
            parts.append(f"MA100={ma:.2f}")
        if diff_pct is not None:
            parts.append(f"ΔP/MA={diff_pct:+.2f}%")
        if min_dt is not None and max_dt is not None:
            parts.append(f"Δt={min_dt:.3f}~{max_dt:.3f}s")

        log_msg += f"[{symbol}] " + " ".join(parts) + "\n"
    return log_msg.rstrip()


# ── 상태 요약 파싱/변화 감지 ─────────────────────────
_HEADER_MA_RE = re.compile(
    r"^\[(?P<sym>[A-Z0-9]+)\]\s+(?P<emoji>[📈📉👀—])\s+ma_thr\(\s*(?P<thr>[0-9.]+)\s*%\s*\)"
)
_POS_RE = re.compile(
    r"^\s*-\s*포지션:\s*(?P<side>LONG|SHORT)\s*\(\s*(?P<qty>\d+(?:\.\d+)?)\s*,\s*[^,]+,\s*(?P<pct>[+\-]?\d+\.\d+)%"
)


def extract_status_summary(
    text: str,
    fallback_ma_threshold_pct: Optional[Dict[str, Optional[float]]] = None,
) -> Dict[str, Any]:
    summary: Dict[str, Any] = {}
    cur_sym: Optional[str] = None
    for raw in text.splitlines():
        line = raw.strip()
        m = _HEADER_MA_RE.match(line)
        if m:
            cur_sym = m.group("sym")
            summary.setdefault(
                cur_sym, {"jump": m.group("emoji"), "ma_thr": None, "LONG": None, "SHORT": None}
            )
            try:
                summary[cur_sym]["ma_thr"] = float(m.group("thr"))
            except Exception:
                summary[cur_sym]["ma_thr"] = None
            continue

        if cur_sym:
            pm = _POS_RE.match(line)
            if pm:
                side = pm.group("side")
                qty = float(pm.group("qty") or 0.0)
                pct = float(pm.group("pct") or 0.0)
                summary[cur_sym][side] = {"q": round(qty, 6), "pr": round(pct, 1)}

    if fallback_ma_threshold_pct:
        for sym, v in summary.items():
            if v.get("ma_thr") is None:
                raw_val = fallback_ma_threshold_pct.get(sym)
                if raw_val is not None:
                    v["ma_thr"] = float(raw_val) * 1.0
    return summary


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
    mom_thr = (q / 4.0) if q is not None else None
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


def build_full_status_log(
    total_usdt: float,
    symbols: List[str],
    jump_state: Dict[str, Dict[str, Any]],
    ma_threshold: Dict[str, Optional[float]],
    now_ma100: Dict[str, Optional[float]],
    get_price: Callable[[str], Optional[float]],
    positions_by_symbol: Dict[str, Dict[str, Any]],
    taker_fee_rate: float,
) -> str:
    head = make_status_log_msg(
        total_usdt=total_usdt,
        symbols=symbols,
        jump_state=jump_state,
        ma_threshold=ma_threshold,
        now_ma100=now_ma100,
        get_price=get_price,
    )
    tails: List[str] = []
    for sym in symbols:
        tails.append(
            format_position_lines(
                get_price=get_price,
                taker_fee_rate=taker_fee_rate,
                positions_for_symbol=(positions_by_symbol or {}).get(sym, {}),
                symbol=sym,
            )
        )
    return (head + "\n" + "".join(tails)).rstrip()


def bootstrap_trading_state_for_symbol(
    rest_client,
    symbol: str,
    leverage: int,
    asset: Dict[str, Any],
    system_logger=None,
) -> Dict[str, Any]:
    """
    지갑/포지션, 레버리지, 주문 동기화만 담당.
    - 실제 주문 모드에서만 필요.
    """
    # 지갑/포지션 동기화
    try:
        asset = rest_client.getNsav_asset(asset=asset, symbol=symbol, save_redis=True)
    except Exception as e:
        if system_logger:
            system_logger.warning(f"[{symbol}] 자산/포지션 동기화 실패: {e}")

    # 레버리지 설정 (MT5 환경에서는 no-op일 수도 있음)
    try:
        rest_client.set_leverage(symbol=symbol, leverage=leverage)
    except Exception:
        # set_leverage 미구현 / 불필요한 환경이면 조용히 무시
        pass

    # 주문 동기화 (Bybit 전용일 수 있으므로 방어적으로 호출)
    try:
        sync_orders = getattr(rest_client, "sync_orders_from_bybit", None)
        if callable(sync_orders):
            sync_orders(symbol)
    except Exception as e:
        if system_logger:
            system_logger.warning(f"[{symbol}] 초기 주문 동기화 실패: {e}")

    return asset


def bootstrap_candles_for_symbol(
    rest_client,
    candle_engine,
    refresh_indicators: Callable[[str], None],
    symbol: str,
    candles_num: int,
    system_logger=None,
) -> None:
    """
    과거 캔들 백필 + 인디케이터(MA100 등) 갱신만 담당.
    - 시그널 전용 모드에서도 반드시 필요.
    """
    try:
        rest_client.update_candles(
            candle_engine.get_candles(symbol),
            symbol=symbol,
            count=candles_num,
        )
        refresh_indicators(symbol)
    except Exception as e:
        if system_logger:
            system_logger.warning(f"[{symbol}] 초기 캔들/인디케이터 부트스트랩 실패: {e}")


def bootstrap_all_symbols(
    rest_client,
    candle_engine,
    refresh_indicators: Callable[[str], None],
    symbols: List[str],
    leverage: int,
    asset: Dict[str, Any],
    candles_num: int,
    system_logger=None,
) -> Dict[str, Any]:
    """
    모든 심볼에 대해:
    - 트레이딩 상태(지갑/포지션/레버리지/주문) 부트스트랩
    - 캔들 + 인디케이터 부트스트랩
    을 모두 수행.
    (실제 주문 모드에서 사용)
    """
    for sym in symbols:
        asset = bootstrap_trading_state_for_symbol(
            rest_client=rest_client,
            symbol=sym,
            leverage=leverage,
            asset=asset,
            system_logger=system_logger,
        )
        bootstrap_candles_for_symbol(
            rest_client=rest_client,
            candle_engine=candle_engine,
            refresh_indicators=refresh_indicators,
            symbol=sym,
            candles_num=candles_num,
            system_logger=system_logger,
        )
    return asset


def position_ratio(position_value: float, total_balance: float) -> float:
    return (position_value / total_balance) if total_balance else 0.0


def log_jump(system_logger, symbol, state, min_dt, max_dt):
    if not system_logger or not state:
        return
    if state == "UP":
        system_logger.info(f"({symbol}) 📈 급등 감지! (Δ {min_dt:.3f}~{max_dt:.3f}s)")
    elif state == "DOWN":
        system_logger.info(f"({symbol}) 📉 급락 감지! (Δ {min_dt:.3f}~{max_dt:.3f}s)")

def momentum_vs_ohlc(price: float, candle: dict) -> Optional[float]:
    if price is None or candle is None:
        return None
    vals = []
    for k in ("open", "high", "low", "close"):
        v = candle.get(k)
        if v is None:
            continue
        v = float(v)
        if v <= 0:
            continue
        vals.append((price - v) / v)
    return max(vals, key=lambda x: abs(x)) if vals else None

def refresh_indicators_for_symbol(
    candle_engine,
    indicator_engine,
    symbol: str,
    *,
    ma100s: Dict[str, Any],
    now_ma100_map: Dict[str, Optional[float]],
    ma_threshold_map: Dict[str, Optional[float]],
    thr_quantized_map: Dict[str, Optional[float]],
    momentum_threshold_map: Dict[str, Optional[float]],
    prev3_candle_map: Dict[str, Optional[float]],
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
    now_ma100_map[symbol] = ma100s[symbol][-1] if ma100s[symbol] else None
    ma_threshold_map[symbol] = res["q_thr"]
    q, mom_thr, log = derive_thresholds_and_log(prev_q, res["q_thr"])
    thr_quantized_map[symbol] = q
    momentum_threshold_map[symbol] = mom_thr
    # 로깅(있을 때만)
    if log and redis_client is not None:
        msg = f"[{symbol}] {log['msg']}"
        if system_logger:
            system_logger.debug(msg)
        xadd_pct_log(
            redis_client,
            symbol,
            "MA threshold",
            log["prev_q"],
            log["new_q"],
            log["arrow"],
            msg,
            namespace=namespace,
            cross_times=res["cross_times"],
        )

    prev3_candle_map[symbol] = res.get("prev3_candle")  # None도 포함


def log_threshold_change(
    system_logger,
    redis_client,
    symbol: str,
    log_payload: Optional[Dict[str, Any]],
    namespace: Optional[str] = None,
):
    if not log_payload:
        return
    msg = f"[{symbol}] {log_payload['msg']}"
    if system_logger:
        system_logger.debug(msg)
    xadd_pct_log(
        redis_client,
        symbol,
        "MA threshold",
        log_payload["prev_q"],
        log_payload["new_q"],
        log_payload["arrow"],
        msg,
        namespace=namespace,
    )


def ws_is_fresh(ws, symbol: str, stale_sec: float, global_stale_sec: float) -> bool:
    get_last_tick = getattr(ws, "get_last_tick_time", None)
    get_last_frame = getattr(ws, "get_last_frame_time", None)
    now_m = time.monotonic()
    if callable(get_last_tick):
        lt = get_last_tick(symbol)
        if lt and (now_m - lt) < stale_sec:
            return True
    if callable(get_last_frame):
        lf = get_last_frame()
        if lf and (now_m - lf) < global_stale_sec:
            return True
    return False


def should_log_update(
    old_summary: Optional[Dict[str, Any]],
    new_summary: Dict[str, Any],
    qty_thr: float = 0.0001,
    rate_thr: float = 1.0,
) -> Tuple[bool, Optional[str]]:
    if old_summary is None:
        return True, "initial snapshot"

    def _as_float(x: Any) -> Optional[float]:
        try:
            return None if x is None else float(x)
        except Exception:
            return None

    symbols = set(new_summary.keys()) | set(old_summary.keys())
    for sym in symbols:
        n = new_summary.get(sym, {"jump": "—", "ma_thr": None, "LONG": None, "SHORT": None})
        o = old_summary.get(sym, {"jump": "—", "ma_thr": None, "LONG": None, "SHORT": None})

        # MA threshold 값 변화(단위: %)
        nth, oth = _as_float(n.get("ma_thr")), _as_float(o.get("ma_thr"))
        if (nth is None) != (oth is None) or (nth is not None and oth is not None and nth != oth):
            return True, f"{sym} MA threshold Δ=({oth}→{nth}%)"

        # jump emoji 변화
        if n.get("jump") != o.get("jump"):
            return True, f"{sym} jump {o.get('jump')}→{n.get('jump')}"

        # 포지션 등장/소멸
        for side in ("LONG", "SHORT"):
            if (n.get(side) is None) != (o.get(side) is None):
                mode = "appeared" if n.get(side) else "disappeared"
                return True, f"{sym} {side} position {mode}"

        # qty / 수익률(%) 변화
        for side in ("LONG", "SHORT"):
            npos, opos = n.get(side), o.get(side)
            if not npos or not opos:
                continue
            nq, npr = npos.get("q"), npos.get("pr")
            oq, opr = opos.get("q"), opos.get("pr")
            if nq is not None and oq is not None and abs(float(nq) - float(oq)) >= qty_thr:
                return True, f"{sym} {side} qty Δ=({oq}→{nq})"
            if npr is not None and opr is not None and abs(float(npr) - float(opr)) >= rate_thr:
                return True, f"{sym} {side} PnL Δ=({opr}%→{npr}%)"

    return False, None
