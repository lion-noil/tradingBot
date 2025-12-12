# bots/trade_config.py
from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List
from datetime import datetime, timezone
import json

# 네임스페이스(name)에 따라 서로 다른 키를 쓰도록 템플릿으로 정의
REDIS_KEY_CFG = "trading:{name}:config"                  # 전체 공용 설정 해시
REDIS_KEY_CFG_EXIT_MA = "trading:{name}:config:exit_ma"  # 심볼별 청산 스레시홀드 해시
REDIS_CH_CFG = "trading:{name}:config:update"            # 변경 브로드캐스트 채널(옵션)


@dataclass
class TradeConfig:
    # 어떤 용도/엔진인지 구분용 (예: "bybit", "mt5_signal")
    name: str = "default"

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
        # symbols 는 항상 리스트로
        self.symbols = list(self.symbols)
        return self


def make_mt5_signal_config(
    *,
    indicator_min_thr: float = 0.004,
    indicator_max_thr: float = 0.04,
    target_cross: int = 10,
    candles_num: int = 10080,
    symbols: list[str] | tuple[str, ...] | None = None,
) -> "TradeConfig":
    """
    MT5 시그널 전용 기본 설정 팩토리.
    - 주문(레버리지, 진입비율)은 사용하지 않으므로 최소값으로 고정
    - signal_only=True 로 고정
    """
    if symbols is None:
        symbols = ("US100", "JP225","GER40","CHINA50","XAUUSD","WTI","XNGUSD")

    cfg = TradeConfig(
        name="mt5_signal",           # 🔹 MT5 시그널용 네임스페이스
        symbols=list(symbols),

        ws_stale_sec=30.0,
        ws_global_stale_sec=60.0,

        # 주문 관련 값은 의미 없으므로 안전하게 최소로
        leverage=1,
        entry_percent=0.01,
        max_effective_leverage=0.0,

        # 인디케이터 관련
        indicator_min_thr=indicator_min_thr,
        indicator_max_thr=indicator_max_thr,
        target_cross=target_cross,

        candles_num=candles_num,
        default_exit_ma_threshold=-0.0005,

        signal_only=True,
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
) -> "TradeConfig":
    """
    Bybit용 기본 트레이딩 설정 팩토리.
    - 기존 TradeConfig 기본값을 그대로 사용하면서, 필요시 인자만 살짝 바꿔서 재사용.
    """
    if symbols is None:
        symbols = ("BTCUSDT", "ETHUSDT")

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

        signal_only=signal_only,
    )
    return cfg.normalized()
