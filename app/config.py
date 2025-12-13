# app/config.py
import os
from dotenv import load_dotenv
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _required(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"❌ Missing required env var: {name}")
    return v


def _optional(name: str, default=None):
    v = os.getenv(name)
    return v if (v is not None and v != "") else default


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


# =============================
# Feature flags (중요)
# =============================
# .env에서 ENABLE_BYBIT=1 / ENABLE_MT5=1 로 켜기
ENABLE_BYBIT = _truthy(_optional("ENABLE_BYBIT", "1"))
ENABLE_MT5 = _truthy(_optional("ENABLE_MT5", "1"))

# =============================
# Redis (둘 중 하나만 있으면 됨)
# =============================
REDIS_URL = _optional("REDIS_URL")  # 있으면 이걸 우선 사용
REDIS_HOST = _optional("REDIS_HOST")
REDIS_PORT = int(_optional("REDIS_PORT", "6379"))
REDIS_PASSWORD = _optional("REDIS_PASSWORD")

# 만약 redis_client가 URL을 쓰는 형태면 REDIS_URL만 있으면 OK.
# host/port 기반이 필요하면 아래처럼 fallback용으로 쓸 수 있음.
if not REDIS_URL and not REDIS_HOST:
    # Redis를 반드시 쓰는 구조면 required로 바꿔도 됨
    # 지금은 "main이 죽지 않게" 기본은 optional 처리
    pass


# =============================
# Bybit (ENABLE_BYBIT일 때만 required)
# =============================
BYBIT_PRICE_WS_URL = None
BYBIT_PRICE_REST_URL = None
BYBIT_TRADE_REST_URL = None
BYBIT_TRADE_API_KEY = None
BYBIT_TRADE_API_SECRET = None

if ENABLE_BYBIT:
    # ===== 가격 / 시세 (항상 메인넷) =====
    BYBIT_PRICE_WS_URL = _required("BYBIT_PRICE_WS_URL")
    BYBIT_PRICE_REST_URL = _required("BYBIT_PRICE_REST_URL")

    # 🚨 보호 가드: 가격용은 demo/test 금지
    if any(x in BYBIT_PRICE_WS_URL.lower() for x in ("demo", "test")):
        raise RuntimeError("❌ BYBIT_PRICE_WS_URL must be MAINNET (no demo/test)")
    if any(x in BYBIT_PRICE_REST_URL.lower() for x in ("demo", "test")):
        raise RuntimeError("❌ BYBIT_PRICE_REST_URL must be MAINNET (no demo/test)")

    # ===== 거래 / 주문 (테스트넷) =====
    BYBIT_TRADE_REST_URL = _required("BYBIT_TRADE_REST_URL")

    # (선택) 거래는 testnet 강제하고 싶다면
    if "demo" not in BYBIT_TRADE_REST_URL.lower():
        raise RuntimeError("❌ BYBIT_TRADE_REST_URL must be TESTNET (api-demo.bybit.com)")

    BYBIT_TRADE_API_KEY = _required("BYBIT_TRADE_API_KEY")
    BYBIT_TRADE_API_SECRET = _required("BYBIT_TRADE_API_SECRET")


# =============================
# MT5 (ENABLE_MT5일 때만 required)
# =============================
MT5_PRICE_REST_URL = None
MT5_TRADE_REST_URL = None
MT5_PRICE_WS_URL = None
MT5_TRADE_API_KEY = None

if ENABLE_MT5:
    MT5_PRICE_REST_URL = _required("MT5_PRICE_REST_URL")
    MT5_TRADE_REST_URL = _required("MT5_TRADE_REST_URL")

    # 선택값
    MT5_PRICE_WS_URL = _optional("MT5_PRICE_WS_URL")
    MT5_TRADE_API_KEY = _optional("MT5_TRADE_API_KEY")
