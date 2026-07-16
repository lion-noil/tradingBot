# -*- coding: utf-8 -*-
"""Upstash 잠금 사고 복구 v2 — 로그 리플레이 + 거래소 교차대조로 로컬 Redis에 전체 장부 재구성.
① signals jsonl → ENTRY−EXIT = 오픈 후보 (시그마 태그만, 나이 ≤32d)
② MT5: 거래소 포지션(티켓·수량·진입가)과 매칭 — 매칭된 것만 채택 + lots 생성(ex_lot_id=티켓)
③ Bybit: (심볼,방향) 거래소 수량을 오픈 게임들에 균등 배분 → lots 생성
④ 신호 zset/hash/stream(쿨다운) + lots hash/zset/by_signal 기록
입력: /app/tools/mt5_positions.json, /app/logs/bybit_positions.json
실행: docker exec -i -e MIGRATE_DST=redis://redis:6379/0 tradingbot-signal-s11-1 python - < tools/rebuild_v2.py [-- --dry-run]"""
import sys, os, json, glob, time, uuid
import redis as redislib

DRY = "--dry-run" in sys.argv
DST = redislib.from_url(os.getenv("MIGRATE_DST", "redis://redis:6379/0"))
assert DST.ping()
NOW = int(time.time() * 1000)
KEEP_MS = 35 * 86400 * 1000
MAX_AGE = 32 * 86400 * 1000
TAGS = {"S1", "S2", "S3", "S4", "S11", "S12", "S13", "S14", "S15"}
BYBIT_NS = {"bybit", "s1", "s2", "s11", "s22"}
MT5_NS = {"mt5", "s11m", "s22m", "s33m", "fxd", "mt5d"}
ALIAS = {"#BTCUSD": "BTCUSD", "#ETHUSD": "ETHUSD", "USOIL": "WTI"}
ACC_BY = "trading:agent:CopyZannavi:u7c9f14d2a1:BYBIT"
ACC_MT = "trading:agent:CopyZannaviMT5:u8f3a9c1e7b:MT5"

# ── ① 로그 리플레이 ──
entries = {}; exits = set()
for path in sorted(glob.glob("/app/logs/signals*.jsonl")):
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            if not line.startswith("SIG "): continue
            try: d = json.loads(line[4:].strip())
            except Exception: continue
            sid = d.get("signal_id"); ts = int(d.get("ts_ms") or 0)
            if not sid or not ts: continue
            if d.get("kind") == "ENTRY":
                entries[sid] = d
            elif d.get("kind") == "EXIT" and d.get("open_signal_id"):
                exits.add(d["open_signal_id"])
# 🔴 legacy s1 ETH숏 7게임 — 로그 로테이션으로 ENTRY 유실 → 어제(7/15) Upstash 복구 스냅샷 값으로 합성.
#    청산 = 14d TIME (원본 TP 소실 — 어제 복구본과 동일 규칙). 거래소 합산 0.08과 배분 일치.
import uuid as _uuid
from datetime import datetime as _dt, timezone as _tz
_ETHS=[('2026-07-03T00:00:00',1694.42),('2026-07-03T03:00:00',1701.55),('2026-07-03T04:00:00',1701.55),
       ('2026-07-03T07:00:00',1702.44),('2026-07-03T08:00:00',1702.44),('2026-07-03T10:00:00',1713.46),
       ('2026-07-03T11:00:00',1713.46)]
for _i,(_t,_px) in enumerate(_ETHS):
    _ts=int(_dt.fromisoformat(_t).replace(tzinfo=_tz.utc).timestamp()*1000)
    _sid=f"restethsh{_i:02d}"+"0"*22   # 결정적 sid → 멱등
    entries[_sid]={"kind":"ENTRY","side":"SHORT","strategy":"S1","reasons":["S1","RESTORED"],
        "price":_px,"tp_price":_px*1e-9,"sl_price":_px*1e9,"engine":"bybit","symbol":"ETHUSDT",
        "signal_id":_sid,"ts_ms":_ts,
        "note":"legacy ETH숏 합성복구(2026-07-16) — 14d TIME 청산"}

cands = []
for sid, d in entries.items():
    if sid in exits: continue
    tag = (d.get("reasons") or [""])[0]
    if tag not in TAGS: continue
    if NOW - int(d["ts_ms"]) > MAX_AGE: continue
    ns = (d.get("engine") or "").lower()
    # 레거시 엔진명 → 현행 ns (일봉 채널이 bybit/mt5 공유로 이관됨)
    if ns == "mt5d": ns = "mt5"
    if ns == "cryptod": ns = "bybit"
    cands.append((ns, (d.get("symbol") or "").upper(), (d.get("side") or "").upper(), sid, d))
print(f"오픈 후보 {len(cands)} (ENTRY {len(entries)} − EXIT매칭)")

adopted = []   # (ns, sym, side, sid, d, qty, ex_lot_id, account)
# ── ② MT5 거래소 매칭 ──
mt5pos = json.load(open("/app/tools/mt5_positions.json", encoding="utf-8"))
for p in mt5pos: p["symbol"] = ALIAS.get(p["symbol"], p["symbol"])
used = set()
mt5_cands = [c for c in cands if c[0] in MT5_NS]
for p in sorted(mt5pos, key=lambda x: x["ts"]):
    best = None
    for c in mt5_cands:
        ns, sym, side, sid, d = c
        if sym != p["symbol"] or side != p["side"] or sid in used: continue
        ep = float(d.get("price") or 0)
        if ep <= 0: continue
        diff = abs(ep - p["entry"]) / p["entry"]
        if diff > 0.005: continue
        if best is None or diff < best[0]: best = (diff, c)
    if best:
        _, (ns, sym, side, sid, d) = best
        used.add(sid)
        adopted.append((ns, sym, side, sid, d, p["vol"], str(p["ticket"]), ACC_MT))
        print(f"MT5 매칭: {ns} {sym} {side} px={d.get('price')} ↔ 티켓{p['ticket']} vol={p['vol']}")
    else:
        print(f"⚠️ MT5 고아(신호 없음): {p['symbol']} {p['side']} vol={p['vol']} entry={p['entry']} 티켓{p['ticket']}")
for c in mt5_cands:
    if c[3] not in used:
        print(f"  드롭(거래소 무): {c[0]} {c[1]} {c[2]} px={c[4].get('price')} tag={(c[4].get('reasons') or ['?'])[0]}")

# ── ③ Bybit 배분 ──
bypos = json.load(open("/app/logs/bybit_positions.json", encoding="utf-8"))
by_cands = {}
for c in cands:
    if c[0] in BYBIT_NS:
        by_cands.setdefault((c[1], c[2]), []).append(c)
for p in bypos:
    key = (p["symbol"], p["side"])
    cs = sorted(by_cands.get(key, []), key=lambda c: int(c[4]["ts_ms"]))
    if not cs:
        print(f"⚠️ Bybit 고아(신호 없음): {key} qty={p['qty']}")
        continue
    q_each = p["qty"] / len(cs)
    for ns, sym, side, sid, d in cs:
        adopted.append((ns, sym, side, sid, d, q_each, "", ACC_BY))
    print(f"Bybit 배분: {key} qty={p['qty']} → 게임 {len(cs)}개 × {q_each:.6f}")
for key, cs in by_cands.items():
    if not any(p["symbol"] == key[0] and p["side"] == key[1] for p in bypos):
        for c in cs:
            print(f"  드롭(거래소 무): {c[0]} {c[1]} {c[2]} tag={(c[4].get('reasons') or ['?'])[0]}")

print(f"\n채택 합계 {len(adopted)}")
if DRY:
    print("[DRY] 종료"); sys.exit(0)

# ── ④ 기록 ──
# 멱등 재실행: 이 스크립트가 소유한 키만 스코프 정리(신호 ledger + lots) 후 다시 기록
OWN_NS = {c[0] for c in adopted} | {"s11", "s22", "bybit", "mt5", "fxd", "s11m", "s22m", "s1", "s2"}
for ns in OWN_NS:
    for pat in (f"trading:{ns}:signals:*:ENTRY", f"trading:{ns}:signal:*", f"trading:{ns}:signals"):
        for k in DST.scan_iter(pat):
            DST.delete(k)
for acc in (ACC_BY, ACC_MT):
    for pat in (f"{acc}:lot:*", f"{acc}:lots:*"):
        for k in DST.scan_iter(pat):
            DST.delete(k)

pipe = DST.pipeline()
last_by_tag = {}
for ns, sym, side, sid, d, qty, ticket, acc in adopted:
    ts = int(d["ts_ms"])
    pipe.zadd(f"trading:{ns}:signals:{sym}:{side}:ENTRY", {sid: float(ts)})
    hk = f"trading:{ns}:signal:{sid}"
    pipe.hset(hk, mapping={
        "signal_id": sid, "ts_ms": str(ts), "symbol": sym, "side": side, "kind": "ENTRY",
        "price": str(float(d.get("price") or 0)),
        "payload_json": json.dumps(d, ensure_ascii=False),
        "created_ts_ms": str(NOW)})
    pipe.pexpire(hk, KEEP_MS)
    lot_id = sid  # 멱등: lot_id=signal_id
    pipe.hset(f"{acc}:lot:{lot_id}", mapping={
        "lot_id": lot_id, "symbol": sym, "side": side, "entry_ts_ms": str(ts),
        "entry_price": f"{float(d.get('price') or 0):.8f}", "qty_total": f"{qty:.12f}",
        "entry_signal_id": sid, "ex_lot_id": ticket, "created_ts_ms": str(NOW)})
    pipe.zadd(f"{acc}:lots:{sym}:{side}:OPEN", {lot_id: float(ts)})
    pipe.hset(f"{acc}:lots:by_signal:OPEN", sid, lot_id)
# 쿨다운 스트림: 태그별 최신 ENTRY(전체 entries 기준)
for sid, d in entries.items():
    tag = (d.get("reasons") or [""])[0]
    if tag not in TAGS: continue
    ns = (d.get("engine") or "").lower()
    if ns == "mt5d": ns = "mt5"
    k = (ns, (d.get("symbol") or "").upper(), (d.get("side") or "").upper(), tag)
    if k not in last_by_tag or int(d["ts_ms"]) > int(last_by_tag[k]["ts_ms"]):
        last_by_tag[k] = d
by_ns = {}
for (ns, sym, side, tag), d in last_by_tag.items():
    by_ns.setdefault(ns, []).append(d)
for ns, rows in by_ns.items():
    seq = {}
    for d in sorted(rows, key=lambda x: int(x["ts_ms"])):
        ts = int(d["ts_ms"]); sq = seq.get(ts, 0); seq[ts] = sq + 1
        try:
            pipe.xadd(f"trading:{ns}:signals", {
                "signal_id": d["signal_id"], "ts_ms": str(ts),
                "symbol": (d.get("symbol") or "").upper(), "side": (d.get("side") or "").upper(),
                "kind": "ENTRY", "price": str(float(d.get("price") or 0)),
                "reasons_json": json.dumps(d.get("reasons") or [], ensure_ascii=False),
            }, id=f"{ts}-{sq}")
        except Exception as e:
            print("stream skip:", ns, str(e)[:50])
pipe.execute()
print("기록 완료 | dbsize =", DST.dbsize())
