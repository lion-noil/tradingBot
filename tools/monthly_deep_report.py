# -*- coding: utf-8 -*-
"""월간 심층 보고서 데이터·차트 생성기 (2026-09-02) — 체결별 MFE/MAE 분석.
ENTRY↔EXIT를 lot_id로 페어링, 보유구간 고저로 MFE(최대순행)·MAE(최대역행)·capture를 계산해
"놓친 청산 포인트"와 "진입 질"을 정량화. 심볼별 가격+마커 차트 PNG 생성.
데이터: Supabase trade_records(REST 직접) + Bybit 공개 kline(60m) + mt5-server(240m).
usage: py -3 tools/monthly_deep_report.py [YYYY-MM]   (기본 2026-08)
출력: reports/{month}/ 에 PNG들 + 콘솔에 분석 표(md 조립용)
"""
import json
import os
import sys
import urllib.request
import urllib.parse
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

plt.rcParams["font.family"] = "Malgun Gothic"
plt.rcParams["axes.unicode_minus"] = False
plt.rcParams["figure.facecolor"] = "#16161c"
plt.rcParams["axes.facecolor"] = "#16161c"
plt.rcParams["axes.edgecolor"] = "#444"
plt.rcParams["axes.labelcolor"] = "#ccc"
plt.rcParams["text.color"] = "#ccc"
plt.rcParams["xtick.color"] = "#999"
plt.rcParams["ytick.color"] = "#999"

KST = timezone(timedelta(hours=9))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MONTH = sys.argv[1] if len(sys.argv) > 1 else "2026-08"
Y, M = map(int, MONTH.split("-"))
M_START = datetime(Y, M, 1, tzinfo=KST)
M_END = datetime(Y + (M == 12), (M % 12) + 1, 1, tzinfo=KST)
FETCH_START = M_START - timedelta(days=45)   # 이월 진입 페어링용
OUT = os.path.join(ROOT, "reports", MONTH)
os.makedirs(OUT, exist_ok=True)


def env_val(path, key):
    for line in open(path, encoding="utf-8-sig"):
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    return ""


SB_URL = env_val(r"C:\Users\Hyeongeon\PycharmProjects\News_scrap\app\.env", "SUPABASE_URL")
SB_KEY = (env_val(r"C:\Users\Hyeongeon\PycharmProjects\News_scrap\app\.env", "SUPABASE_SECRET_KEY")
          or env_val(r"C:\Users\Hyeongeon\PycharmProjects\News_scrap\app\.env", "SUPABASE_KEY"))
MT5_KEY = env_val(r"C:\Users\Hyeongeon\PycharmProjects\mt5_server\.env", "API_KEY")


def http_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    return json.load(urllib.request.urlopen(req, timeout=30))


def fetch_records():
    rows, off = [], 0
    base = (f"{SB_URL}/rest/v1/trade_records?select=id,day,symbol,side,kind,signal,pnl,raw_json"
            f"&day=gte.{FETCH_START:%Y-%m-%d}&day=lt.{M_END:%Y-%m-%d}&order=id")
    hdr = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"}
    while True:
        d = http_json(base + f"&limit=1000&offset={off}", hdr)
        rows.extend(d)
        if len(d) < 1000:
            break
        off += 1000
    return rows


BROKER_MT5 = {"US100": "US100", "US500": "US500", "JP225": "JP225"}  # canonical로 요청(서버가 매핑)


def fetch_candles(symbol, account):
    """[(ts_ms, o, h, l, c)] 오름차순 — FETCH_START~현재."""
    out = []
    if account == "BYBIT":
        end = None
        for _ in range(3):   # 1000×60m ≈ 41일 × 3 ≈ 124일
            url = (f"https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}"
                   f"&interval=60&limit=1000" + (f"&end={end}" if end else ""))
            rows = http_json(url).get("result", {}).get("list", [])
            if not rows:
                break
            for r in rows:
                out.append((int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])))
            end = min(int(r[0]) for r in rows) - 1
            if end < int(FETCH_START.timestamp() * 1000):
                break
    else:
        url = (f"http://127.0.0.1:9000/v5/market/candles/with-gaps?symbol={symbol}"
               f"&interval=240&limit=1000")
        rows = http_json(url, {"X-API-Key": MT5_KEY}).get("result", {}).get("list", [])
        for r in rows:
            out.append((int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])))
    out = sorted(set(out))
    return out


def mfe_mae(candles, side, entry_px, t0, t1):
    """보유구간 [t0,t1] 고저 → (MFE%, MAE%) 방향보정. 데이터 없으면 (None, None)."""
    hs = [h for ts, o, h, l, c in candles if t0 <= ts <= t1]
    ls = [l for ts, o, h, l, c in candles if t0 <= ts <= t1]
    if not hs or entry_px <= 0:
        return None, None
    if side == "LONG":
        return (max(hs) / entry_px - 1) * 100, (min(ls) / entry_px - 1) * 100
    return (1 - min(ls) / entry_px) * 100, (1 - max(hs) / entry_px) * 100


def book_of(ns):
    n = (ns or "").lower()
    if n.startswith("s11"):
        return "1분"
    if n.startswith("s22"):
        return "4h"
    if n in ("bybit", "mt5", "fxd", "mt5d", "cryptod"):
        return "일봉"
    return "?"


rows = fetch_records()
print(f"[data] trade_records {len(rows)}행 ({FETCH_START:%m-%d}~)")

entries_by_lot = {}
for r in rows:
    if str(r.get("kind")).upper() == "ENTRY":
        raw = r.get("raw_json") or {}
        lid = str(raw.get("lot_id") or "")
        if lid:
            entries_by_lot[lid] = r

trades = []       # 이달 청산 완결 거래
open_aug = []     # 이달 진입, 미청산
m0, m1 = int(M_START.timestamp() * 1000), int(M_END.timestamp() * 1000)
exit_lots = set()
symbols = {}

for r in rows:
    raw = r.get("raw_json") or {}
    account = str(raw.get("account") or "BYBIT")
    kind = str(r.get("kind")).upper()
    ts = int(raw.get("ts_ms") or 0)
    if kind == "EXIT" and m0 <= ts < m1:
        lid = str(raw.get("lot_id") or "")
        exit_lots.add(lid)
        ent = entries_by_lot.get(lid)
        ent_raw = (ent or {}).get("raw_json") or {}
        t_in = int(ent_raw.get("ts_ms") or 0) or None
        tag = str(raw.get("strategy_tag") or ent_raw.get("strategy_tag")
                  or str(r.get("signal") or "").split("_")[0] or "?").upper()
        ns = raw.get("signal_ns") or ent_raw.get("signal_ns") or ""
        side = str(r.get("side") or "").upper()
        ep = float(raw.get("entry_price") or 0)
        xp = float(raw.get("exit_price") or 0)
        realized = ((xp / ep - 1) if side == "LONG" else (1 - xp / ep)) * 100 - 0.11 if ep and xp else None
        trades.append(dict(account=account, symbol=r["symbol"], side=side, tag=tag,
                           book=book_of(ns), ep=ep, xp=xp, t_in=t_in, t_out=ts,
                           realized=realized, sig=str(r.get("signal") or "")))
        symbols[(r["symbol"], account)] = True

for r in rows:
    raw = r.get("raw_json") or {}
    kind = str(r.get("kind")).upper()
    ts = int(raw.get("ts_ms") or 0)
    if kind == "ENTRY" and m0 <= ts < m1:
        lid = str(raw.get("lot_id") or "")
        if lid and lid not in exit_lots:
            account = str(raw.get("account") or "BYBIT")
            open_aug.append(dict(account=account, symbol=r["symbol"],
                                 side=str(r.get("side") or "").upper(),
                                 tag=str(raw.get("strategy_tag") or "?").upper(),
                                 book=book_of(raw.get("signal_ns")),
                                 ep=float(raw.get("entry_price") or 0), t_in=ts))
            symbols[(r["symbol"], account)] = True

print(f"[pair] 8월 청산 {len(trades)}건 (진입시각 매칭 {sum(1 for t in trades if t['t_in'])}건) · "
      f"8월 진입 미청산 {len(open_aug)}건")

candles = {}
for (sym, acc) in symbols:
    try:
        candles[(sym, acc)] = fetch_candles(sym, acc)
    except Exception as e:
        print(f"[candles] {sym}/{acc} 실패: {e}")

for t in trades:
    cs = candles.get((t["symbol"], t["account"]), [])
    if t["t_in"]:
        t["mfe"], t["mae"] = mfe_mae(cs, t["side"], t["ep"], t["t_in"], t["t_out"])
    else:
        t["mfe"] = t["mae"] = None
    t["missed"] = (t["mfe"] - t["realized"]) if (t["mfe"] is not None and t["realized"] is not None) else None

now_ms = int(datetime.now(KST).timestamp() * 1000)
for t in open_aug:
    cs = candles.get((t["symbol"], t["account"]), [])
    t["mfe"], t["mae"] = mfe_mae(cs, t["side"], t["ep"], t["t_in"], now_ms)
    t["cur"] = cs[-1][4] if cs else None
    t["unreal"] = ((t["cur"] / t["ep"] - 1) if t["side"] == "LONG" else (1 - t["cur"] / t["ep"])) * 100 \
        if (t["cur"] and t["ep"]) else None

# ── 콘솔 분석 표 (md 조립용) ──
def fm(v, d=2):
    return "-" if v is None else f"{v:+.{d}f}"

print("\n== 셀별 MFE/MAE (청산 완결, 진입매칭분) ==")
agg = defaultdict(list)
for t in trades:
    if t["mfe"] is not None:
        agg[(t["account"], t["symbol"], t["book"], t["tag"])].append(t)
for k in sorted(agg, key=lambda k: -sum(x["realized"] or 0 for x in agg[k])):
    ts_ = agg[k]
    n = len(ts_)
    rl = sum(x["realized"] for x in ts_) / n
    mfe = sum(x["mfe"] for x in ts_) / n
    mae = sum(x["mae"] for x in ts_) / n
    cap = (sum(x["realized"] for x in ts_) / sum(x["mfe"] for x in ts_) * 100) \
        if sum(x["mfe"] for x in ts_) > 0 else None
    print(f"{k[0]:5} {k[1]:8} {k[2]:2} {k[3]:3} n{n:>2} 실현{rl:+6.2f}% MFE{mfe:+6.2f}% "
          f"MAE{mae:+6.2f}% capture{'' if cap is None else f'{cap:4.0f}%'}")

print("\n== 놓친 청산 top10 (MFE−실현 큰 순) ==")
for t in sorted([t for t in trades if t["missed"] is not None], key=lambda x: -x["missed"])[:10]:
    print(f"{t['account']:5} {t['symbol']:8} {t['book']:2} {t['tag']:3} {t['sig']:8} "
          f"in {datetime.fromtimestamp(t['t_in']/1000, KST):%m-%d %H:%M} → out {datetime.fromtimestamp(t['t_out']/1000, KST):%m-%d %H:%M} "
          f"실현 {fm(t['realized'])}% / MFE {fm(t['mfe'])}% → 반납 {fm(t['missed'])}%p")

print("\n== 진입 질 나쁜 top10 (MAE 깊은 순) ==")
for t in sorted([t for t in trades if t["mae"] is not None], key=lambda x: x["mae"])[:10]:
    print(f"{t['account']:5} {t['symbol']:8} {t['book']:2} {t['tag']:3} "
          f"in {datetime.fromtimestamp(t['t_in']/1000, KST):%m-%d %H:%M} MAE {fm(t['mae'])}% → 최종 {fm(t['realized'])}%")

print("\n== 8월 진입 미청산 (보유중) ==")
for t in sorted(open_aug, key=lambda x: -(x["unreal"] or 0)):
    print(f"{t['account']:5} {t['symbol']:8} {t['book']:2} {t['tag']:3} "
          f"in {datetime.fromtimestamp(t['t_in']/1000, KST):%m-%d} @{t['ep']} 평가 {fm(t['unreal'])}% "
          f"(MFE {fm(t['mfe'])}/MAE {fm(t['mae'])})")

# ── 차트 ──
def dtx(ms):
    return datetime.fromtimestamp(ms / 1000, KST)

# 1) 심볼별 가격 + 진입/청산 마커
for (sym, acc), cs in candles.items():
    cs_m = [x for x in cs if m0 - 12 * 86400000 <= x[0] < min(m1, now_ms) + 86400000]
    if len(cs_m) < 10:
        continue
    fig, ax = plt.subplots(figsize=(11, 4.2), dpi=110)
    ax.plot([dtx(x[0]) for x in cs_m], [x[4] for x in cs_m], lw=0.9, color="#5aa6e0")
    tin = [t for t in trades if t["symbol"] == sym and t["account"] == acc and t["t_in"]]
    ax.scatter([dtx(t["t_in"]) for t in tin], [t["ep"] for t in tin], marker="^", s=52,
               color="#2fe08d", zorder=5, label="진입")
    ax.scatter([dtx(t["t_out"]) for t in tin], [t["xp"] for t in tin], marker="x", s=58,
               color=["#7ee787" if (t["realized"] or 0) > 0 else "#ff6b6b" for t in tin],
               zorder=5, linewidths=2, label="청산(초록=익절)")
    op = [t for t in open_aug if t["symbol"] == sym and t["account"] == acc]
    if op:
        ax.scatter([dtx(t["t_in"]) for t in op], [t["ep"] for t in op], marker="^", s=64,
                   facecolors="none", edgecolors="#ffd166", zorder=5, label="진입(보유중)")
    ax.set_title(f"{sym} ({acc}) — {MONTH} 진입·청산", fontsize=11)
    ax.legend(fontsize=8, loc="best", facecolor="#202027", edgecolor="#444")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d", tz=KST))
    ax.grid(alpha=0.15)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, f"sym_{acc}_{sym}.png"))
    plt.close(fig)

# 2) MFE vs 실현 산점도 (capture 시각화)
ok = [t for t in trades if t["mfe"] is not None and t["realized"] is not None]
if ok:
    fig, ax = plt.subplots(figsize=(6.4, 6), dpi=110)
    colors = {"1분": "#ffb86c", "4h": "#4cc9f0", "일봉": "#ffd166", "?": "#8a8a8a"}
    for bk in colors:
        pts = [t for t in ok if t["book"] == bk]
        if pts:
            ax.scatter([t["mfe"] for t in pts], [t["realized"] for t in pts], s=26,
                       color=colors[bk], alpha=0.85, label=f"{bk}책 ({len(pts)})")
    lim = max(max(abs(t["mfe"]) for t in ok), max(abs(t["realized"]) for t in ok)) * 1.1
    ax.plot([0, lim], [0, lim], ls="--", lw=0.8, color="#666")
    ax.axhline(0, lw=0.6, color="#555")
    ax.set_xlabel("MFE 최대순행 % (보유중 닿았던 최대이익)")
    ax.set_ylabel("실현 %")
    ax.set_title(f"{MONTH} 체결별 MFE vs 실현 — 대각선 아래 거리 = 반납분", fontsize=10.5)
    ax.legend(fontsize=8.5, facecolor="#202027", edgecolor="#444")
    ax.grid(alpha=0.15)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "mfe_capture.png"))
    plt.close(fig)

# 3) 셀별 합계 바
cellsum = defaultdict(float)
for t in trades:
    if t["realized"] is not None:
        cellsum[f"{t['account'][:2]} {t['symbol']} {t['book']} {t['tag']}"] += t["realized"]
if cellsum:
    ks = sorted(cellsum, key=lambda k: cellsum[k])
    fig, ax = plt.subplots(figsize=(8, max(3, 0.32 * len(ks))), dpi=110)
    ax.barh(ks, [cellsum[k] for k in ks],
            color=["#2fe08d" if cellsum[k] > 0 else "#ff6b6b" for k in ks])
    ax.set_title(f"{MONTH} 셀별 실현 합계 (%p)", fontsize=11)
    ax.grid(alpha=0.15, axis="x")
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "cells_sum.png"))
    plt.close(fig)

print(f"\n[charts] {OUT} 에 PNG {len([f for f in os.listdir(OUT) if f.endswith('.png')])}개 저장")
