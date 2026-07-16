# -*- coding: utf-8 -*-
"""Upstash → 로컬 Redis 전체 마이그레이션 (2026-07-16 한도 사고).
SCAN 전체 키 → DUMP/RESTORE(TTL 보존). 실행 전 Upstash 잠금 해제(유료 전환) 필수.

실행(컨테이너 안 — 소스 REDIS_URL은 .env의 Upstash):
  docker exec -i tradingbot-signal-s11-1 python - < tools/migrate_redis.py -- --dry-run
  docker exec -i tradingbot-signal-s11-1 python - < tools/migrate_redis.py
대상(로컬): redis://host.docker.internal:6379 (bridge) — WSL mirrored라 호스트 127.0.0.1:6379.

이후 컷오버: .env REDIS_URL=redis://호스트:6379 로 교체 → 전 컨테이너 재기동."""
import sys, os
import redis as redislib
from core.redis_client import redis_client as SRC

DRY = "--dry-run" in sys.argv
DST_URL = os.getenv("MIGRATE_DST", "redis://host.docker.internal:6379/0")
DST = redislib.from_url(DST_URL)
assert DST.ping(), "로컬 Redis 응답 없음"

total = 0; failed = 0; skipped = 0
for key in SRC.scan_iter("*", count=500):
    total += 1
    try:
        ttl = SRC.pttl(key)          # -1=무기한, -2=없음
        if ttl == -2:
            skipped += 1; continue
        blob = SRC.dump(key)
        if blob is None:
            skipped += 1; continue
        if DRY:
            continue
        DST.restore(key, ttl if ttl > 0 else 0, blob, replace=True)
    except Exception as e:
        failed += 1
        print(f"실패: {key[:60]} — {str(e)[:80]}")
print(f'{"[DRY] " if DRY else ""}총 {total}키 | 실패 {failed} | 스킵 {skipped}')
if not DRY:
    print("검증: 로컬 dbsize =", DST.dbsize())
