# -*- coding: utf-8 -*-
"""고아 포지션 복구 (2026-07-14 사고) — hash TTL(keep_days=10) 만료로 TP/SL 레벨이 소실돼
청산 스캐너가 못 보는 열린 포지션 14건을 lots 장부(진입가)로 복원한다.

복원 방식: trading:{ns}:signal:{sid} hash 재생성 — 진입가=lots 장부, TP/SL=도달불가 레벨
(원본 소실) → 청산은 각 채널의 최대보유 시간청산(TIME)으로만 발동. 진입시각은 zset score
그대로(보유시간 기산 보존). reasons[0]=태그로 담당 채널 지정.

실행: docker exec tradingbot-signal-s11-1 python tools/restore_orphan_signals.py
      (--dry-run 으로 미리보기)
전제: bots/state/signals.py keep_days=35 반영된 이미지로 전 채널 재기동 예정
      (구 코드의 zset 10일 청소가 복구본을 다시 지우는 것 방지)"""
import sys, json, time
from core.redis_client import redis_client as r

DRY = "--dry-run" in sys.argv
now = int(time.time() * 1000)

BASES = [
    'trading:agent:CopyZannavi:u7c9f14d2a1:BYBIT',
    'trading:agent:CopyZannaviMT5:u8f3a9c1e7b:MT5',
]
# (ns, sym, side) → 담당 채널 태그 (그 셀을 params에 가진 채널)
TAG = {
    ('bybit', 'ETHUSDT', 'SHORT'): 'S1',   # 레거시 s1 추세숏 (드레인, 14d)
    ('bybit', 'XAUTUSDT', 'LONG'): 'S1',   # 레거시 s1 추세롱 (드레인, 14d)
    ('bybit', 'XRPUSDT', 'LONG'): 'S1',    # 레거시 s1 (드레인, 14d)
    ('bybit', 'XRPUSDT', 'SHORT'): 'S2',   # 레거시 s2 역추숏 (드레인, 14d)
    ('mt5', 'HK50', 'LONG'): 'S1',         # 레거시 s1mt5 (드레인, 14d)
    ('fxd', 'USDJPY', 'LONG'): 'S3',       # 일봉 FX 추세롱 (30d)
    ('fxd', 'EURUSD', 'LONG'): 'S4',       # 일봉 FX 역추롱 (30d)
}

sig2lot = {}
for base in BASES:
    for k, v in r.hgetall(f'{base}:lots:by_signal:OPEN').items():
        sig2lot[k.decode()] = (base, v.decode())

fixed = 0
for ns in ['bybit', 'mt5', 'fxd']:
    for key in r.scan_iter(f'trading:{ns}:signals:*:ENTRY'):
        k = key.decode()
        parts = k.split(':')
        sym, side = parts[3], parts[4]
        for sid_b, score in r.zrange(k, 0, -1, withscores=True):
            sid = sid_b.decode()
            hk = f'trading:{ns}:signal:{sid}'
            if r.exists(hk):
                continue
            m = sig2lot.get(sid)
            if not m:
                print(f'skip(lot없음): {ns} {sym} {side} {sid[:10]}')
                continue
            base, lot = m
            d = {a.decode(): b.decode() for a, b in r.hgetall(f'{base}:lot:{lot}').items()}
            ep = float(d.get('entry_price') or 0)
            tag = TAG.get((ns, sym, side))
            if ep <= 0 or not tag:
                print(f'skip(ep/태그): {ns} {sym} {side} {sid[:10]} ep={ep} tag={tag}')
                continue
            long = (side == 'LONG')
            tp = ep * 1e9 if long else ep * 1e-9   # 도달불가 → 시간청산 전용
            sl = ep * 1e-9 if long else ep * 1e9
            held_d = (now - score) / 86400000
            print(f'{"[DRY] " if DRY else ""}복구: {ns} {sym} {side} tag={tag} ep={ep} 보유 {held_d:.1f}d')
            if DRY:
                continue
            payload = {'kind': 'ENTRY', 'side': side, 'strategy': tag, 'reasons': [tag, 'RESTORED'],
                       'price': ep, 'tp_price': tp, 'sl_price': sl, 'restored_ts_ms': now,
                       'note': 'hash TTL 만료 고아 복구(2026-07-14) — 진입가=lots, 청산=최대보유 TIME'}
            r.hset(hk, mapping={'signal_id': sid, 'ts_ms': str(int(score)), 'symbol': sym,
                                'side': side, 'kind': 'ENTRY', 'price': str(ep),
                                'payload_json': json.dumps(payload, ensure_ascii=False),
                                'created_ts_ms': str(now)})
            r.pexpire(hk, 40 * 86400 * 1000)
            fixed += 1
print(f'{"[DRY] " if DRY else ""}복구 합계 {fixed}')
