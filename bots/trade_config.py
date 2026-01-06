# bots/trade_config.py

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
import json

from typing import Any, Dict, List, Optional, Tuple
import os
from pathlib import Path
from dotenv import load_dotenv
# 네임스페이스(name)에 따라 서로 다른 키를 쓰도록 템플릿으로 정의
REDIS_KEY_CFG = "trading:{name}:config"                  # 전체 공용 설정 해시
REDIS_KEY_CFG_EXIT_MA = "trading:{name}:config:exit_ma"  # 심볼별 청산 스레시홀드 해시
REDIS_CH_CFG = "trading:{name}:config:update"            # 변경 브로드캐스트 채널(옵션)

_ENV_LOADED = False

def _load_dotenv_once(dotenv_path: str | None = None) -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return

    if dotenv_path:
        load_dotenv(dotenv_path, override=False)
        _ENV_LOADED = True
        return

    # 기본: 프로젝트 루트(.git 있는 곳) 또는 현재 작업폴더 기준 상위에서 .env 탐색
    # trade_config.py 위치: <root>/bots/trade_config.py 라는 전제
    root = Path(__file__).resolve().parents[1]  # bots/ 의 상위 = 프로젝트 루트
    load_dotenv(root / ".env", override=False)
    _ENV_LOADED = True


def _optional(name: str, default=None):
    v = os.getenv(name)
    return v if (v is not None and v != "") else default

def _required(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"❌ Missing required env var: {name}")
    return v

def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


@dataclass(frozen=True)
class RedisConfig:
    url: Optional[str] = None
    host: Optional[str] = None
    port: int = 6379
    password: Optional[str] = None

    @staticmethod
    def from_env() -> "RedisConfig":
        _load_dotenv_once()


        return RedisConfig(
            url=_optional("REDIS_URL"),
            host=_optional("REDIS_HOST"),
            port=int(_optional("REDIS_PORT", "6379")),
            password=_optional("REDIS_PASSWORD"),
        )

_SECRET_CACHE: "SecretsConfig | None" = None
@dataclass(frozen=True)
class SecretsConfig:
    enable_bybit: bool = True
    enable_mt5: bool = True

    bybit_price_ws_url: Optional[str] = None
    bybit_price_rest_url: Optional[str] = None
    bybit_trade_rest_url: Optional[str] = None
    bybit_trade_api_key: Optional[str] = None
    bybit_trade_api_secret: Optional[str] = None

    mt5_price_rest_url: Optional[str] = None
    mt5_trade_rest_url: Optional[str] = None
    mt5_price_ws_url: Optional[str] = None
    mt5_trade_api_key: Optional[str] = None

    @staticmethod
    def from_env() -> "SecretsConfig":
        """
        .env / 환경변수에서 1회 로드 후 캐싱.
        - 여기서는 'optional'로만 읽는다. (필요한 강제는 require_*에서 수행)
        """
        global _SECRET_CACHE
        if _SECRET_CACHE is not None:
            return _SECRET_CACHE

        _load_dotenv_once()

        cfg = SecretsConfig(
            enable_bybit=_truthy(_optional("ENABLE_BYBIT", "1")),
            enable_mt5=_truthy(_optional("ENABLE_MT5", "1")),

            bybit_price_ws_url=_optional("BYBIT_PRICE_WS_URL"),
            bybit_price_rest_url=_optional("BYBIT_PRICE_REST_URL"),
            bybit_trade_rest_url=_optional("BYBIT_TRADE_REST_URL"),
            bybit_trade_api_key=_optional("BYBIT_TRADE_API_KEY"),
            bybit_trade_api_secret=_optional("BYBIT_TRADE_API_SECRET"),

            mt5_price_rest_url=_optional("MT5_PRICE_REST_URL"),
            mt5_trade_rest_url=_optional("MT5_TRADE_REST_URL"),
            mt5_price_ws_url=_optional("MT5_PRICE_WS_URL"),
            mt5_trade_api_key=_optional("MT5_TRADE_API_KEY"),
        )

        _SECRET_CACHE = cfg
        return cfg

    def require_bybit_public(self) -> "SecretsConfig":
        if not self.enable_bybit:
            raise RuntimeError("ENABLE_BYBIT=0 인데 Bybit public 설정을 요구했습니다.")
        if not self.bybit_price_ws_url:
            raise RuntimeError("❌ Missing BYBIT_PRICE_WS_URL")
        if not self.bybit_price_rest_url:
            raise RuntimeError("❌ Missing BYBIT_PRICE_REST_URL")
        return self

    def require_bybit_trade(self) -> "SecretsConfig":
        self.require_bybit_public()
        if not self.bybit_trade_rest_url:
            raise RuntimeError("❌ Missing BYBIT_TRADE_REST_URL")
        if not self.bybit_trade_api_key:
            raise RuntimeError("❌ Missing BYBIT_TRADE_API_KEY")
        if not self.bybit_trade_api_secret:
            raise RuntimeError("❌ Missing BYBIT_TRADE_API_SECRET")
        return self

    def require_mt5_public(self) -> "SecretsConfig":
        if not self.enable_mt5:
            raise RuntimeError("ENABLE_MT5=0 인데 MT5 public 설정을 요구했습니다.")
        if not self.mt5_price_rest_url:
            raise RuntimeError("❌ Missing MT5_PRICE_REST_URL")
        return self

    def require_mt5_trade(self) -> "SecretsConfig":
        self.require_mt5_public()
        if not self.mt5_trade_rest_url:
            raise RuntimeError("❌ Missing MT5_TRADE_REST_URL")
        # trade api key를 필수로 만들고 싶으면 아래 주석 해제
        # if not self.mt5_trade_api_key:
        #     raise RuntimeError("❌ Missing MT5_TRADE_API_KEY")
        return self


@dataclass
class TradeConfig:
    # 어떤 용도/엔진인지 구분용 (예: "bybit", "mt5_signal")
    name: str = "default"
    min_ma_threshold: float = 0.005

    # 청산(보유시간/근접윈도우)
    position_max_hold_sec: int = 7 * 24 * 3600  # ✅ 7일 기본
    near_touch_window_sec: int = 60 * 60  # ✅ 1시간 기본

    # 이 설정이 다루는 심볼 목록 (프론트/봇에서 공통으로 사용)
    symbols: List[str] = field(default_factory=list)

    # 실행/네트워크
    ws_stale_sec: float = 30.0
    ws_global_stale_sec: float = 60.0

    # 레버리지/진입
    leverage: int = 50
    entry_percent: float = 3  # leverage * entry_percent 가 한번 진입 퍼센트: 50 x 3 = 150% 진입
    max_effective_leverage: float = 30.0   # 보유노션/지갑 최대 배수 (가드)

    # 인디케이터
    indicator_min_thr: float = 0.004
    indicator_max_thr: float = 0.04
    target_cross: int = 10

    # 슬라이딩 윈도우(캔들 개수)
    candles_num: int = 10080  # (예: 1분봉 7일치)

    # 기본 청산 스레시홀드(심볼별 커스텀은 별도 해시)
    default_exit_ma_threshold: float = -0.0005

    # signal_only (True면 시그널만, 실제 주문 X)
    signal_only: bool = False

    def to_redis(self, redis_client, publish: bool = True) -> None:
        """
        현재 설정을 Redis 해시에 저장하고, 옵션에 따라 브로드캐스트 채널로도 publish.
        name에 따라 서로 다른 키를 사용하므로, bybit / mt5 설정이 서로 덮어쓰지 않음.
        """
        d = self.as_dict()

        key_cfg = REDIS_KEY_CFG.format(name=self.name)
        ch_cfg = REDIS_CH_CFG.format(name=self.name)

        pipe = redis_client.pipeline()
        for k, v in d.items():
            # 타입 보존을 위해 JSON 문자열로 저장
            pipe.hset(key_cfg, k, json.dumps(v, ensure_ascii=False))
        pipe.execute()

        if publish:
            payload = json.dumps(
                {"ts": datetime.now(timezone.utc).isoformat(), "config": d},
                ensure_ascii=False,
            )
            redis_client.publish(ch_cfg, payload)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def normalized(self) -> "TradeConfig":
        """
        각 필드에 대해 최소/형변환 등을 적용해서 안전한 값으로 정규화.
        """
        self.ws_stale_sec = max(1.0, float(self.ws_stale_sec))
        self.ws_global_stale_sec = max(5.0, float(self.ws_global_stale_sec))
        self.leverage = max(1, int(self.leverage))
        self.entry_percent = max(0.01, float(self.entry_percent))
        self.max_effective_leverage = max(0.0, float(self.max_effective_leverage))
        self.indicator_min_thr = max(0.0, float(self.indicator_min_thr))
        self.indicator_max_thr = max(self.indicator_min_thr, float(self.indicator_max_thr))
        self.target_cross = max(1, int(self.target_cross))
        self.candles_num = max(1, int(self.candles_num))
        self.signal_only = bool(self.signal_only)
        self.position_max_hold_sec = max(600, int(self.position_max_hold_sec))  # 최소 60초
        self.near_touch_window_sec = max(0, int(self.near_touch_window_sec))  # 0 허용
        self.min_ma_threshold = max(0.0, float(self.min_ma_threshold))

        # symbols 는 항상 리스트로
        self.symbols = list(self.symbols)
        return self


def make_mt5_signal_config(
    *,
    indicator_min_thr: float = 0.004,
    indicator_max_thr: float = 0.04,
    target_cross: int = 5,
    candles_num: int = 10080,
    symbols: list[str] | tuple[str, ...] | None = None,
    min_ma_threshold: float =  0.0051,
) -> "TradeConfig":
    """
    MT5 시그널 전용 기본 설정 팩토리.
    - 주문(레버리지, 진입비율)은 사용하지 않으므로 최소값으로 고정
    """
    if symbols is None:
        symbols = ("US100", "JP225","XAUUSD","WTI","XNGUSD","XAGUSD","BTCUSD","ETHUSD","HK50","CHINA50","GER40","UK100")

    cfg = TradeConfig(
        name="mt5_signal",
        symbols=list(symbols),

        ws_stale_sec=30.0,
        ws_global_stale_sec=60.0,

        # 주문 관련 값은 의미 없으므로 안전하게 최소로
        leverage=50,
        entry_percent=3.0,
        max_effective_leverage=30.0,

        # 인디케이터 관련
        indicator_min_thr=indicator_min_thr,
        indicator_max_thr=indicator_max_thr,
        target_cross=target_cross,

        candles_num=candles_num,
        default_exit_ma_threshold=-0.0005,

        min_ma_threshold=min_ma_threshold,
        signal_only=False,
    )
    return cfg.normalized()


def make_bybit_config(
    *,
    # 인디케이터 기본값 (기존 TradeConfig 기본값과 동일)
    indicator_min_thr: float = 0.004,
    indicator_max_thr: float = 0.04,
    target_cross: int = 10,
    candles_num: int = 10080,

    # 실행/네트워크
    ws_stale_sec: float = 30.0,
    ws_global_stale_sec: float = 60.0,

    # 레버리지/진입 관련 (기존 Bybit 기본값)
    leverage: int = 50,
    entry_percent: float = 3.0,
    max_effective_leverage: float = 30.0,

    # 기본 청산 스레시홀드
    default_exit_ma_threshold: float = -0.0005,

    # Bybit는 기본적으로 주문까지 수행하므로 기본 False
    signal_only: bool = False,

    # 이 설정이 다루는 심볼 목록
    symbols: list[str] | tuple[str, ...] | None = None,
    min_ma_threshold: float = 0.0051,
) -> "TradeConfig":
    """
    Bybit용 기본 트레이딩 설정 팩토리.
    - 기존 TradeConfig 기본값을 그대로 사용하면서, 필요시 인자만 살짝 바꿔서 재사용.
    """
    if symbols is None:
        symbols = ("BTCUSDT", "ETHUSDT","SOLUSDT","XRPUSDT")

    cfg = TradeConfig(
        name="bybit",               # 🔹 Bybit용 네임스페이스
        symbols=list(symbols),

        ws_stale_sec=ws_stale_sec,
        ws_global_stale_sec=ws_global_stale_sec,

        leverage=leverage,
        entry_percent=entry_percent,
        max_effective_leverage=max_effective_leverage,

        indicator_min_thr=indicator_min_thr,
        indicator_max_thr=indicator_max_thr,
        target_cross=target_cross,

        candles_num=candles_num,
        default_exit_ma_threshold=default_exit_ma_threshold,

        min_ma_threshold=min_ma_threshold,
        signal_only=signal_only,
    )
    return cfg.normalized()

if __name__ == "__main__":
    from pprint import pprint

    print("[DEBUG] dotenv loaded:", _ENV_LOADED)
    print("[DEBUG] cwd:", Path.cwd())
    root = Path(__file__).resolve().parents[1]
    print("[DEBUG] expected .env:", root / ".env")
    print("[DEBUG] .env exists:", (root / ".env").exists())

    s = SecretsConfig.from_env()
    pprint({
        "enable_bybit": s.enable_bybit,
        "enable_mt5": s.enable_mt5,
        "bybit_price_ws_url": s.bybit_price_ws_url,
        "bybit_price_rest_url": s.bybit_price_rest_url,
        "bybit_trade_rest_url": s.bybit_trade_rest_url,
        "bybit_trade_api_key_set": bool(s.bybit_trade_api_key),
        "bybit_trade_api_secret_set": bool(s.bybit_trade_api_secret),
    })

    # 역할별 검증(원하는 것만)
    try:
        s.require_bybit_public()
        print("✅ require_bybit_public OK")
    except Exception as e:
        print("❌ require_bybit_public FAIL:", e)

    try:
        s.require_bybit_trade()
        print("✅ require_bybit_trade OK")
    except Exception as e:
        print("❌ require_bybit_trade FAIL:", e)
