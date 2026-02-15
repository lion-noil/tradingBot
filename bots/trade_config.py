# bots/trade_config.py

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
import json
from dataclasses import replace
from typing import Any, Dict, List, Optional, Tuple
import os
from pathlib import Path
from dotenv import load_dotenv
REDIS_KEY_CFG = "trading:{name}:config"                  # 전체 공용 설정 해시
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

@dataclass
class TradeConfig:
    # 어떤 용도/엔진인지 구분용 (예: "bybit", "mt5_signal")
    name: str = "default"
    min_ma_threshold: float = 0.005

    # 청산(보유시간/근접윈도우)
    position_max_hold_sec: int = 7 * 24 * 3600  # ✅ 7일 기본
    near_touch_window_sec: int = 60 * 60  # ✅ 60분 기본

    # 이 설정이 다루는 심볼 목록 (프론트/봇에서 공통으로 사용)
    symbols: List[str] = field(default_factory=list)

    # 실행/네트워크
    ws_stale_sec: float = 30.0
    ws_global_stale_sec: float = 60.0

    # 레버리지/진입
    leverage: int = 50
    entry_percent: float = 3  # leverage * entry_percent 가 한번 진입 퍼센트: 50 x 3 = 150% 진입
    max_effective_leverage: float = 10.0   # 보유노션/지갑 최대 배수 (가드)

    # ✅ 심볼별 진입 퍼센트 (없으면 entry_percent 사용)
    entry_percent_by_symbol: Dict[str, float] = field(default_factory=dict)

    # 인디케이터
    indicator_min_thr: float = 0.005
    indicator_max_thr: float = 0.05
    target_cross: int = 10

    # 슬라이딩 윈도우(캔들 개수)
    candles_num: int = 10080  # (예: 1분봉 7일치)


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
        return replace(
            self,
            ws_stale_sec=max(1.0, float(self.ws_stale_sec)),
            ws_global_stale_sec=max(5.0, float(self.ws_global_stale_sec)),
            leverage=max(1, int(self.leverage)),
            entry_percent=max(0.001, float(self.entry_percent)),
            entry_percent_by_symbol={
                str(k).upper(): max(0.01, float(v))
                for k, v in (self.entry_percent_by_symbol or {}).items()
            },
            max_effective_leverage=max(0.0, float(self.max_effective_leverage)),
            indicator_min_thr=max(0.0, float(self.indicator_min_thr)),
            indicator_max_thr=max(max(0.0, float(self.indicator_min_thr)), float(self.indicator_max_thr)),
            target_cross=max(1, int(self.target_cross)),
            candles_num=max(1, int(self.candles_num)),
            signal_only=bool(self.signal_only),
            position_max_hold_sec=max(600, int(self.position_max_hold_sec)),
            near_touch_window_sec=max(0, int(self.near_touch_window_sec)),
            min_ma_threshold=max(0.0, float(self.min_ma_threshold)),
            symbols=list(self.symbols),
        )


def _parse_symbols(v: str | None) -> list[str] | None:
    if not v:
        return None
    # 콤마/공백/개행 모두 허용
    raw = v.replace("\n", ",").replace(" ", ",")
    items = [s.strip().upper() for s in raw.split(",") if s.strip()]
    return items or None



def make_bybit_config(
    *,
    # 인디케이터 기본값 (기존 TradeConfig 기본값과 동일)
    indicator_min_thr: float = 0.004,
    indicator_max_thr: float = 0.05,
    target_cross: int = 14,
    candles_num: int = 10080,

    # 실행/네트워크
    ws_stale_sec: float = 30.0,
    ws_global_stale_sec: float = 60.0,

    # 레버리지/진입 관련 (기존 Bybit 기본값)
    leverage: int = 50,
    entry_percent: float = 3.0,
    max_effective_leverage: float = 10.0,

    # Bybit는 기본적으로 주문까지 수행하므로 기본 False
    signal_only: bool = False,

    # 이 설정이 다루는 심볼 목록
    symbols: list[str] | tuple[str, ...] | None = None,
    min_ma_threshold: float = 0.0055,

    # ✅ 추가: 심볼별 entry% 맵
    entry_percent_by_symbol: dict[str, float] | None = None,

) -> "TradeConfig":
    """
    Bybit용 기본 트레이딩 설정 팩토리.
    - 기존 TradeConfig 기본값을 그대로 사용하면서, 필요시 인자만 살짝 바꿔서 재사용.
    """

    _load_dotenv_once()
    symbols = _parse_symbols(os.getenv("BYBIT_SYMBOLS"))

    if entry_percent_by_symbol is None:
        entry_percent_by_symbol = {
            "ETHUSDT": 2.0,
            "SOLUSDT": 1.0,
            "XRPUSDT": 1.0,
            "XAUTUSDT": 1.0,
        }

    cfg = TradeConfig(
        name="bybit",               # 🔹 Bybit용 네임스페이스
        symbols=list(symbols),

        ws_stale_sec=ws_stale_sec,
        ws_global_stale_sec=ws_global_stale_sec,

        leverage=leverage,
        entry_percent=entry_percent,
        entry_percent_by_symbol=entry_percent_by_symbol,
        max_effective_leverage=max_effective_leverage,


        indicator_min_thr=indicator_min_thr,
        indicator_max_thr=indicator_max_thr,
        target_cross=target_cross,

        candles_num=candles_num,

        min_ma_threshold=min_ma_threshold,
        signal_only=signal_only,
    )
    return cfg.normalized()


def make_mt5_signal_config(
    *,
    indicator_min_thr: float = 0.005,
    indicator_max_thr: float = 0.07,
    target_cross: int = 14,
    candles_num: int = 10080,
    symbols: list[str] | tuple[str, ...] | None = None,
    min_ma_threshold: float = 0.0055,

    # ✅ 추가: 심볼별 entry% 맵
    entry_percent_by_symbol: dict[str, float] | None = None,
) -> "TradeConfig":
    """
    MT5 시그널 전용 기본 설정 팩토리.
    - 주문(레버리지, 진입비율)은 사용하지 않으므로 최소값으로 고정
    """

    _load_dotenv_once()

    symbols = _parse_symbols(os.getenv("MT5_SYMBOLS"))
    entry_percent = 5.0
    # leverage 50이므로 1은 50% 진입(=50 x 3%)
    """if entry_percent_by_symbol is None:
        entry_percent_by_symbol = {
            "XAUUSD":0.5,
            "XAGUSD":0.5,
            "BTCUSD": 0.5,
            "ETHUSD": 0.5,
            "WTI": 0.5,
            "XNGUSD": 0.5,
        }"""

    cfg = TradeConfig(
        name="mt5",
        symbols=list(symbols),

        ws_stale_sec=30.0,
        ws_global_stale_sec=60.0,

        # 주문 관련 값은 의미 없으므로 안전하게 최소로
        leverage=50,
        entry_percent=entry_percent,
        entry_percent_by_symbol=entry_percent_by_symbol,

        max_effective_leverage=20.0,

        # 인디케이터 관련
        indicator_min_thr=indicator_min_thr,
        indicator_max_thr=indicator_max_thr,
        target_cross=target_cross,

        candles_num=candles_num,

        min_ma_threshold=min_ma_threshold,
        signal_only=False,
    )
    return cfg.normalized()
