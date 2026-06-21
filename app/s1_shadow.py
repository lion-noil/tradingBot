"""
S1 섀도우(paper) 러너 — 거래소 키/레디스 없이 실시간 S1 시그널만 기록.

Bybit 공개 kline(인증 불필요)으로 최근 1분봉을 받아 S1 판단을 굴리고,
진입/청산 의도를 로그+상태파일(JSON)에만 남긴다. 실주문 없음(signal_only).
실거래 전 "라이브 시그널이 백테스트와 같은 타이밍에 나오나"를 며칠 검증하는 용도.

사용:
  python app/s1_shadow.py --symbols BTCUSDT --once         # 현재 상태 1회 점검
  python app/s1_shadow.py --symbols BTCUSDT,SOLUSDT --loop 60   # 60초마다
"""
from __future__ import annotations
import argparse, json, os, time, sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from strategies.s1_reversion import (
    S1Params, S1Position, s1_indicators, s1_entry_levels, s1_cooldown_ok, s1_exit_on_tick,
)

try:
    import requests
except ImportError:
    requests = None

STATE_DIR = os.path.join(os.path.dirname(__file__), "..", "s1_state")
BASE = "https://api.bybit.com"


def fetch_1m(symbol: str, need: int) -> list[dict]:
    """최근 need개 1분봉(오래된→최신). 공개 API, 페이지네이션."""
    if requests is None:
        raise RuntimeError("requests 미설치")
    out: list[dict] = []
    end = None
    while len(out) < need:
        params = {"category": "linear", "symbol": symbol, "interval": "1",
                  "limit": min(1000, need - len(out))}
        if end is not None:
            params["end"] = end
        r = requests.get(f"{BASE}/v5/market/kline", params=params, timeout=20)
        r.raise_for_status()
        lst = ((r.json().get("result") or {}).get("list")) or []
        if not lst:
            break
        lst = lst[::-1]
        chunk = [{"start": int(c[0]), "open": float(c[1]), "high": float(c[2]),
                  "low": float(c[3]), "close": float(c[4])} for c in lst]
        out = chunk + out
        end = int(lst[0][0]) - 1
        if len(lst) < params["limit"]:
            break
        time.sleep(0.2)
    return out[-need:]


def load_state(symbol: str) -> dict:
    p = os.path.join(STATE_DIR, f"{symbol}.json")
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return {"position": None, "last_exit_ts_ms": None, "trades": []}


def save_state(symbol: str, st: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, f"{symbol}.json"), "w") as f:
        json.dump(st, f, indent=2)


def check_symbol(symbol: str, p: S1Params, log=print) -> None:
    candles = fetch_1m(symbol, p.win + 5)
    if len(candles) < p.win:
        log(f"[{symbol}] 데이터 부족 {len(candles)}/{p.win}"); return
    closes = [c["close"] for c in candles]
    last = candles[-1]
    price = last["close"]; now_ms = int(time.time() * 1000)
    ma, sd, z = s1_indicators(closes, p.win, price)
    st = load_state(symbol)
    pos = st.get("position")
    dts = datetime.fromtimestamp(last["start"] / 1000, tz=timezone.utc)

    zstr = f"{z:+.2f}" if z is not None else "—"
    head = f"[{symbol} {dts:%m-%d %H:%M}] price={price:.4f} z={zstr}"

    if pos is None:
        if not s1_cooldown_ok(st.get("last_exit_ts_ms"), now_ms, p):
            log(f"{head} | flat (쿨다운 중)"); save_state(symbol, st); return
        lv = s1_entry_levels(z, ma, sd, price, p)
        if lv:
            tp, sl = lv
            st["position"] = {"entry_price": price, "tp_price": tp, "sl_price": sl,
                              "entry_ts_ms": now_ms}
            log(f"{head} | ▲ ENTER LONG @ {price:.4f}  TP {tp:.4f}(+{(tp/price-1)*100:.2f}%) "
                f"SL {sl:.4f}(-{(1-sl/price)*100:.2f}%)  [SIGNAL ONLY]")
        else:
            log(f"{head} | flat (진입조건 미충족, z>{-p.k1})")
    else:
        P = S1Position(pos["entry_price"], pos["tp_price"], pos["sl_price"], pos["entry_ts_ms"])
        reason = s1_exit_on_tick(P, price)
        if reason:
            ret = (price / P.entry_price - 1) - p.fee_roundtrip
            st["trades"].append({"entry": P.entry_price, "exit": price, "reason": reason,
                                 "ret_pct": ret * 100, "exit_ts_ms": now_ms})
            st["position"] = None; st["last_exit_ts_ms"] = now_ms
            log(f"{head} | ⊖ EXIT {reason} @ {price:.4f}  진입 {P.entry_price:.4f}  "
                f"손익 {ret*100:+.2f}%  [SIGNAL ONLY]")
        else:
            log(f"{head} | 보유중 진입 {P.entry_price:.4f} / TP {P.tp_price:.4f} / SL {P.sl_price:.4f}")
    save_state(symbol, st)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="BTCUSDT")
    ap.add_argument("--k1", type=float, default=2.5)
    ap.add_argument("--b", type=float, default=2.0)
    ap.add_argument("--cooldown-h", type=float, default=12.0)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", type=int, default=0, help="N초 간격 반복")
    args = ap.parse_args()
    p = S1Params(win=10080, k1=args.k1, b=args.b, cooldown_sec=int(args.cooldown_h * 3600))
    p.validate()
    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    print(f"S1 섀도우 (signal-only) | {syms} | K1={p.k1} B={p.b} 쿨다운{args.cooldown_h}h | 상태→{STATE_DIR}")

    def tick():
        for s in syms:
            try:
                check_symbol(s, p)
            except Exception as e:
                print(f"[{s}] 오류: {e}")

    if args.loop and not args.once:
        while True:
            tick(); time.sleep(args.loop)
    else:
        tick()


if __name__ == "__main__":
    main()
