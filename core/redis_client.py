# core/redis_client.py
import os
from pathlib import Path

import redis
from dotenv import load_dotenv

# ✅ 프로젝트 루트(.env) 로드: app/config.py와 동일한 위치를 보도록
BASE_DIR = Path(__file__).resolve().parent.parent  # core/.. == project root (app과 같은 레벨 가정)
load_dotenv(BASE_DIR / ".env")


def _optional(name: str, default=None):
    v = os.getenv(name)
    return v if (v is not None and v != "") else default


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


# ✅ 1순위: REDIS_URL (있으면 이걸로 끝)
REDIS_URL = _optional("REDIS_URL")


# SSL 강제 여부(선택): env로 오버라이드 가능
# - 일반적으로 REDIS_URL이 rediss:// 이면 SSL
# - 또는 ENABLE_REDIS_SSL=1 같은 플래그로 켤 수도 있음
ENABLE_REDIS_SSL = _truthy(_optional("ENABLE_REDIS_SSL", "0"))


def _make_redis_client():
    # URL 방식이 있으면 URL 우선
    if REDIS_URL:
        # redis.from_url은 rediss:// 스킴이면 SSL로 처리
        return redis.from_url(
            REDIS_URL,
            decode_responses=False,  # 기존 redis.Redis 기본과 동일
        )

redis_client = _make_redis_client()


def redis_ping() -> bool:
    """간단 헬스체크용. __main__/테스트에서 호출하면 좋음."""
    try:
        return bool(redis_client.ping())
    except Exception:
        return False


if __name__ == "__main__":
    # 단독 점검용
    ok = redis_ping()
    print("redis ping:", ok)
    if ok:
        try:
            redis_client.set("redis_test_key", "1", ex=10)
            print("set/get:", redis_client.get("redis_test_key"))
        except Exception as e:
            print("basic set/get failed:", e)
