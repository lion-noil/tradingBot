# bots/trade_config.py
from dataclasses import dataclass, asdict
from typing import Any, Dict
from datetime import datetime, timezone
import json

REDIS_KEY_CFG = "trading:config"                  # 전체 공용 설정 해시
REDIS_KEY_CFG_EXIT_MA = "trading:config:exit_ma"  # 심볼별 청산 스레시홀드 해시
REDIS_CH_CFG = "trading:config:update"            # 변경 브로드캐스트 채널(옵션)

@dataclass
class TradeConfig:
    # 실행/네트워크
    ws_stale_sec: float = 30.0
    ws_global_stale_sec: float = 60.0

    # 레버리지/진입
    leverage: int = 50
    entry_percent: float = 3
    max_effective_leverage: float = 30.0   # 보유노션/지갑 최대 배수 (가드)

    # 인디케이터
    indicator_min_thr: float = 0.004
    indicator_max_thr: float = 0.04
    target_cross: int = 10

    # 슬라이딩 윈도우(캔들 개수)
    candles_num: int = 10080  # (예: 1분봉 7일치)

    # 기본 청산 스레시홀드(심볼별 커스텀은 별도 해시)
    default_exit_ma_threshold: float = -0.0005

    def to_redis(self, redis_client, publish: bool = True) -> None:
        d = self.as_dict()
        pipe = redis_client.pipeline()
        for k, v in d.items():
            pipe.hset(REDIS_KEY_CFG, k, str(v))
        pipe.execute()
        if publish:
            payload = json.dumps(
                {"ts": datetime.now(timezone.utc).isoformat(), "config": d},
                ensure_ascii=False,
            )
            redis_client.publish(REDIS_CH_CFG, payload)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def normalized(self) -> "TradeConfig":
        self.ws_stale_sec = max(1.0, float(self.ws_stale_sec))
        self.ws_global_stale_sec = max(5.0, float(self.ws_global_stale_sec))
        self.leverage = max(1, int(self.leverage))
        self.entry_percent = max(0.01, float(self.entry_percent))
        self.max_effective_leverage = max(0.0, float(self.max_effective_leverage))
        self.indicator_min_thr = max(0.0, float(self.indicator_min_thr))
        self.indicator_max_thr = max(self.indicator_min_thr, float(self.indicator_max_thr))
        self.target_cross = max(1, int(self.target_cross))
        self.candles_num = max(1, int(self.candles_num))  # << 추가 가드
        return self
