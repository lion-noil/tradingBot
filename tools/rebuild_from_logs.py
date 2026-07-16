# -*- coding: utf-8 -*-
"""Upstash 잠금 사고(2026-07-16) — signals jsonl 로그 리플레이로 로컬 Redis에 신호 장부 재구성.
ENTRY 수집(signal_id, 전체 payload: tp/sl/game_id/전략) → EXIT(open_signal_id)로 상쇄 → 순 오픈 셋.
기록: open zset + signal hash(payload_json) + stream(쿨다운 워밍업용 — (ns,sym,side,tag)별 최신 ENTRY 포함).

실행: docker exec -i tradingbot-signal-s11-1 python - < tools/rebuild_from_logs.py -- [--dry-run]
  (컨테이너의 /app/logs 마운트 사용, 대상 Redis = MIGRATE_DST 또는 redis://host.docker.internal:6379)"""
import sys, os, json, glob, time
import redis as redislib

DRY = "--dry-run" in sys.argv
DST = redislib.from_url(os.getenv("MIGRATE_DST", "redis://host.docker.internal:6379/0"))
assert DST.ping()
NOW = int(time.time() * 1000)
KEEP_MS = 35 * 86400 * 1000

entries = {}   # sid -> (ns, sym, side, ts_ms, price, payload)
exits = set()  # open_signal_id
lines = 0
for path in sorted(glob.glob("/app/logs/signals*.jsonl")):
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line.startswith("SIG "):
                    continue
                lines += 1
                try:
                    d = json.loads(line[4:])
                except Exception:
                    continue
                ns = (d.get("engine") or "").lower()
                sym = (d.get("symbol") or "").upper()
                side = (d.get("side") or "").upper()
                sid = d.get("signal_id")
                ts = int(d.get("ts_ms") or 0)
                if not (ns and sym and side and sid and ts):
                    continue
                if d.get("kind") == "ENTRY":
                    entries[sid] = (ns, sym, side, ts, d.get("price"), d)
                elif d.get("kind") == "EXIT":
                    oid = d.get("open_signal_id")
                    if oid:
                        exits.add(oid)
    except FileNotFoundError:
        continue

open_sids = {sid: v for sid, v in entries.items() if sid not in exits}
# 보유상한 지난 것 제외(로그에 EXIT 유실 대비): max_hold_sec 초과 + 7d 여유 → 이미 청산됐다고 간주하지 않고 포함하되 표기
print(f"라인 {lines:,} | ENTRY {len(entries)} | EXIT {len(exits)} | 순 오픈 {len(open_sids)}")

by_bucket = {}
for sid, (ns, sym, side, ts, price, d) in sorted(open_sids.items(), key=lambda x: x[1][3]):
    by_bucket.setdefault((ns, sym, side), []).append((sid, ts, price, d))

pipe = DST.pipeline()
nkeys = 0
last_entry_all = {}   # (ns,sym,side,tag) -> 최신 ENTRY (쿨다운용, 오픈 여부 무관)
for sid, (ns, sym, side, ts, price, d) in entries.items():
    tag = (d.get("reasons") or [d.get("strategy") or ""])[0]
    k = (ns, sym, side, tag)
    if k not in last_entry_all or ts > last_entry_all[k][1]:
        last_entry_all[k] = (sid, ts, price, d)

for (ns, sym, side), items in sorted(by_bucket.items()):
    zkey = f"trading:{ns}:signals:{sym}:{side}:ENTRY"
    for sid, ts, price, d in items:
        hkey = f"trading:{ns}:signal:{sid}"
        print(f"  {ns:5} {sym:9} {side:5} {(d.get('reasons') or ['?'])[0]:4} ts={ts} px={price} "
              f"tp={d.get('tp_price')}")
        if DRY:
            continue
        pipe.zadd(zkey, {sid: float(ts)})
        pipe.hset(hkey, mapping={
            "signal_id": sid, "ts_ms": str(ts), "symbol": sym, "side": side, "kind": "ENTRY",
            "price": "" if price is None else str(float(price)),
            "payload_json": json.dumps(d, ensure_ascii=False),
            "created_ts_ms": str(NOW),
        })
        pipe.pexpire(hkey, KEEP_MS)
        nkeys += 1

# 쿨다운 워밍업용 스트림: (ns)별로 최근 ENTRY들(오픈 무관, 태그별 최신) 시간순 xadd
if not DRY:
    by_ns_stream = {}
    for (ns, sym, side, tag), (sid, ts, price, d) in last_entry_all.items():
        by_ns_stream.setdefault(ns, []).append((ts, sid, sym, side, tag, price))
    for ns, rows in by_ns_stream.items():
        skey = f"trading:{ns}:signals"
        seq = {}
        for ts, sid, sym, side, tag, price in sorted(rows):
            sq = seq.get(ts, 0); seq[ts] = sq + 1
            try:
                pipe.xadd(skey, {
                    "signal_id": sid, "ts_ms": str(ts), "symbol": sym, "side": side,
                    "kind": "ENTRY", "price": "" if price is None else str(float(price)),
                    "reasons_json": json.dumps([tag], ensure_ascii=False),
                }, id=f"{ts}-{sq}")
            except Exception as e:
                print("stream skip:", ns, sym, str(e)[:60])
    pipe.execute()
print(f'{"[DRY] " if DRY else ""}오픈 신호 {nkeys}개 기록 완료 | dbsize={DST.dbsize()}')
