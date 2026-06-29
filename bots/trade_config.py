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
    near_touch_window_sec: int = 60 * 30  # ✅ 30분 기본

    # 이 설정이 다루는 심볼 목록 (프론트/봇에서 공통으로 사용)
    symbols: List[str] = field(default_factory=list)

    # 실행/네트워크
    ws_stale_sec: float = 30.0
    ws_global_stale_sec: float = 60.0
    # 피드 게이트(장 마감 판정) 임계 — ws_stale_sec보다 길게 둬서 저유동성 심볼
    # (예: ETHUSD)의 간헐적 틱공백으로 stale↔fresh 플래핑하는 걸 방지.
    feed_gate_stale_sec: float = 120.0

    # 레버리지/진입
    leverage: int = 50
    entry_percent: float = 0.5  # leverage * entry_percent 가 한번 진입 퍼센트: 50 x 2 = 100% 진입
    max_effective_leverage: float = 5.0   # 보유노션/지갑 최대 배수 (가드)

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

    # ✅ 전략 선택: "basic"(기존 MA100 리버전) | "s1"(σ-복귀 롱)
    strategy: str = "basic"
    # basic 전략의 롱/숏 진입 on/off. 롱=S1, 숏=S2로 분리하면 둘 다 False(basic 은퇴). True=종전대로.
    basic_long_enabled: bool = True
    basic_short_enabled: bool = True
    # S1(σ-복귀) 파라미터 — strategy="s1"일 때만 사용. 백테스트 검증값.
    s1_win: int = 10080          # MA/σ 창(1분봉 7일). 고정(검증값)
    s1_k1: float = 2.5           # 진입 z 임계 (z <= -k1)
    s1_b: float = 2.0            # TP 복귀밴드 (b < k1 필수)
    s1_cooldown_sec: int = 12 * 3600
    # ✅ S1 v2: 심볼별 파라미터 맵 {SYM: {k1,b,cooldown_sec,max_concurrent}}. 비면 위 전역값 사용.
    s1_params_by_symbol: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # ✅ S1 v2: 최대보유(초). 초과 시 시장가 강제청산. 기본 14일.
    s1_max_hold_sec: int = 14 * 24 * 3600
    # ✅ 추매(평단↓): True면 신호 재발생 시 새 포지션 대신 기존에 1회 추매(재앵커). S2 역추세 전용.
    avg_down: bool = False
    # ✅ 캔들 타임프레임: "1"(분, 기존) | "D"(일봉채널). MarketSync가 이 값으로 분기.
    candle_interval: str = "1"

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
    target_cross: int = 5,
    candles_num: int = 10080,

    # 실행/네트워크
    ws_stale_sec: float = 30.0,
    ws_global_stale_sec: float = 60.0,

    # 레버리지/진입 관련. entry_notional = bal × (entry_percent/100) × leverage.
    #   0.1/100 × 50 = 0.05 = 1진입 5% notional. max_eff_lev 10 = 총 10배(=200랏 @5%).
    leverage: int = 50,
    entry_percent: float = 0.1,
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
        '''entry_percent_by_symbol = {
            "ETHUSDT": 1.0,
            "SOLUSDT": 1.0,
            "XRPUSDT": 1.0,
            "XAUTUSDT": 1.0,
        }'''
        entry_percent_by_symbol = {}

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
        basic_long_enabled=False,   # 🔴 롱=S1, 숏=S2로 분리 → basic 은퇴
        basic_short_enabled=False,
    )
    return cfg.normalized()


def make_s1_config(
    *,
    candles_num: int = 10160,            # win(10080) + 여유 (여유는 진입 준비 전용, 청산엔 무관)
    ws_stale_sec: float = 30.0,
    ws_global_stale_sec: float = 60.0,
    leverage: int = 50,
    entry_percent: float = 0.5,          # 실제 주문 사이징은 실행기(executor)가 담당 → 여기선 표시용
    max_effective_leverage: float = 5.0,
    signal_only: bool = True,   # ✅ S1 미검증 → 기본 신호만(실주문 X). 백테스트 검증 후 False로 승격.
    symbols: list[str] | tuple[str, ...] | None = None,
    name: str = "bybit",        # ✅ 네임스페이스/엔진 ("bybit" | "mt5")
    params_by_symbol: dict | None = None,  # ✅ 심볼별 v2 파라미터(없으면 name으로 기본맵 선택)
    strategy: str = "s1",       # ✅ "s1"(추세) | "s2"(역추세) — 동일 엔진, 방향만 다름
    avg_down: bool = False,     # ✅ 추매(S2 역추세 전용)
    s1_win: int = 10080,        # ✅ MA/σ 창. 1분봉=10080(7일). 일봉채널=90(90일).
    candle_interval: str = "1",  # ✅ "1"(분) | "D"(일봉채널)
    s1_max_hold_sec: int = 14 * 24 * 3600,  # ✅ 최대보유. 1분=14일, 일봉=30일.
) -> "TradeConfig":
    """S1(σ-복귀 롱) / S2(추세 숏) 신호 설정. namespace=name, strategy 분기.
    - 심볼: .env BYBIT_S1_SYMBOLS
    - S1 파라미터: .env S1_K1 / S1_B / S1_COOLDOWN_H (없으면 백테스트 검증 기본값)
    큰틀(TradeBot/실행기)은 그대로, strategy 분기만 타는 표준 전략 인스턴스.
    """
    _load_dotenv_once()

    # ✅ S1 = 추세(trend) — portfolio_sim picks 전체. 롱=z≥+K1(과열지속), 숏=z≤-K1(급락지속).
    #   maxc=MC(=200, 비구속)로 두고 포트폴리오 캡(max_effective_leverage=10 → 200랏)이 실제 제한.
    _H = 3600
    MC = 200
    TREND_BYBIT: dict[str, dict] = {
        "BTCUSDT": {"long": {"k1": 3.2,  "b": -2.0, "cooldown_sec": int(3.0 * _H),  "max_concurrent": MC}},
        "ETHUSDT": {"long": {"k1": 2.35, "b": 1.2,  "cooldown_sec": int(2.5 * _H),  "max_concurrent": MC},
                    "short": {"k1": 3.45,"b": -1.8, "cooldown_sec": int(3.0 * _H),  "max_concurrent": MC}},
        "SOLUSDT": {"long": {"k1": 3.4,  "b": -2.0, "cooldown_sec": int(3.0 * _H),  "max_concurrent": MC},
                    "short": {"k1": 3.4, "b": -2.0, "cooldown_sec": int(1.5 * _H),  "max_concurrent": MC}},
        "XRPUSDT": {"long": {"k1": 2.55, "b": -0.4, "cooldown_sec": int(3.0 * _H),  "max_concurrent": MC}},
    }
    TREND_MT5: dict[str, dict] = {
        "US100":  {"long": {"k1": 2.8,  "b": -1.8, "cooldown_sec": int(2.75 * _H), "max_concurrent": MC}},
        "JP225":  {"long": {"k1": 3.35, "b": -2.0, "cooldown_sec": int(2.0 * _H),  "max_concurrent": MC},
                   "short": {"k1": 3.25,"b": 0.8,  "cooldown_sec": int(1.25 * _H), "max_concurrent": MC}},
        "HK50":   {"long": {"k1": 2.05, "b": 0.6,  "cooldown_sec": int(3.0 * _H),  "max_concurrent": MC}},
        "GER40":  {"long": {"k1": 2.75, "b": -1.8, "cooldown_sec": int(3.0 * _H),  "max_concurrent": MC}},
        "UK100":  {"long": {"k1": 3.25, "b": -1.2, "cooldown_sec": int(1.75 * _H), "max_concurrent": MC},
                   "short": {"k1": 3.5, "b": 0.8,  "cooldown_sec": int(1.0 * _H),  "max_concurrent": MC}},
        "XAUUSD": {"long": {"k1": 3.45, "b": -1.8, "cooldown_sec": int(1.25 * _H), "max_concurrent": MC},
                   "short": {"k1": 3.2, "b": 0.2,  "cooldown_sec": int(1.0 * _H),  "max_concurrent": MC}},
        "XAGUSD": {"long": {"k1": 2.75, "b": -1.2, "cooldown_sec": int(3.0 * _H),  "max_concurrent": MC},
                   "short": {"k1": 2.65,"b": 1.2,  "cooldown_sec": int(2.0 * _H),  "max_concurrent": MC}},
        "WTI":    {"long": {"k1": 2.9,  "b": -1.4, "cooldown_sec": int(2.5 * _H),  "max_concurrent": MC},
                   "short": {"k1": 3.2, "b": 1.8,  "cooldown_sec": int(1.0 * _H),  "max_concurrent": MC}},
        # ── FX 메이저 (HFM, HANDOFF_FX_majors 2026-06-27 추가; 에러 시 이 블록만 제거) ──
        #   S1추세롱=trendlong(z≥+K1) / S1추세숏=trend(z≤−K1). 기대값 작음(지수/크립토의 1/3~1/5).
        "EURUSD": {"long": {"k1": 3.8,  "b": -1.0, "cooldown_sec": int(1.0 * _H),  "max_concurrent": MC}},
        "AUDUSD": {"long": {"k1": 3.7,  "b": 2.0,  "cooldown_sec": int(0.5 * _H),  "max_concurrent": MC}},
        "GBPUSD": {"long": {"k1": 3.4,  "b": 0.4,  "cooldown_sec": int(1.0 * _H),  "max_concurrent": MC}},
        "USDCHF": {"short": {"k1": 4.1, "b": -2.0, "cooldown_sec": int(0.75 * _H), "max_concurrent": MC}},
        "USDJPY": {"short": {"k1": 3.0, "b": -1.4, "cooldown_sec": int(1.5 * _H),  "max_concurrent": MC}},
    }
    pbs = params_by_symbol if params_by_symbol is not None \
        else (TREND_MT5 if name == "mt5" else TREND_BYBIT)
    symbols = list(pbs.keys())

    def _f(key: str, d: float) -> float:
        try:
            return float(os.getenv(key) or d)
        except Exception:
            return d

    s1_k1 = _f("S1_K1", 2.5)
    s1_b = _f("S1_B", 2.0)
    s1_cooldown_sec = int(_f("S1_COOLDOWN_H", 12.0) * 3600)

    cfg = TradeConfig(
        name=name,                # 🔹 basic과 통일된 네임스페이스(bybit/mt5). 전략 tag로 구분
        strategy=strategy,
        symbols=list(symbols or []),

        ws_stale_sec=ws_stale_sec,
        ws_global_stale_sec=ws_global_stale_sec,

        leverage=leverage,
        entry_percent=entry_percent,
        max_effective_leverage=max_effective_leverage,

        candles_num=candles_num,
        signal_only=signal_only,

        s1_win=s1_win,
        s1_k1=s1_k1,
        s1_b=s1_b,
        s1_cooldown_sec=s1_cooldown_sec,
        s1_params_by_symbol=pbs,           # ✅ v2 심볼별 파라미터
        s1_max_hold_sec=s1_max_hold_sec,   # ✅ 최대보유(1분=14일/일봉=30일)
        avg_down=avg_down,                 # ✅ 추매(S2 전용)
        candle_interval=candle_interval,   # ✅ 캔들 타임프레임("1"/"D")
    )
    return cfg.normalized()


def make_s1_mt5_config(*, signal_only: bool = True, **kw) -> "TradeConfig":
    """S1 v2 MT5용 — make_s1_config(name='mt5', S1_V2_MT5 맵). MT5 심볼/별칭은 컨트롤러가 매핑."""
    return make_s1_config(name="mt5", signal_only=signal_only, **kw)


def make_s2_config(*, signal_only: bool = True, **kw) -> "TradeConfig":
    """S2 = 역추세(reversion) Bybit (portfolio_sim picks). 롱=z≤-K1/숏=z≥+K1."""
    _H = 3600
    MC = 200
    REV_BYBIT = {
        "BTCUSDT": {"long": {"k1": 3.3, "b": -2.0, "cooldown_sec": int(3.0 * _H),  "max_concurrent": MC},
                    "short": {"k1": 4.6,"b": -0.4, "cooldown_sec": int(0.5 * _H),  "max_concurrent": MC}},
        "ETHUSDT": {"long": {"k1": 3.15,"b": -1.2, "cooldown_sec": int(2.0 * _H),  "max_concurrent": MC},
                    "short": {"k1": 3.3,"b": -1.2, "cooldown_sec": int(3.0 * _H),  "max_concurrent": MC}},
        "SOLUSDT": {"long": {"k1": 3.3, "b": 1.8,  "cooldown_sec": int(3.0 * _H),  "max_concurrent": MC}},
        "XRPUSDT": {"long": {"k1": 3.5, "b": -0.4, "cooldown_sec": int(2.25 * _H), "max_concurrent": MC},
                    "short": {"k1": 5.0,"b": -2.0, "cooldown_sec": int(0.5 * _H),  "max_concurrent": MC}},
    }
    return make_s1_config(name="bybit", params_by_symbol=REV_BYBIT, strategy="s2",
                          avg_down=True, signal_only=signal_only, **kw)


def make_s2_mt5_config(*, signal_only: bool = True, **kw) -> "TradeConfig":
    """S2 = 역추세(reversion) MT5 (portfolio_sim picks)."""
    _H = 3600
    MC = 200
    REV_MT5 = {
        "US100":  {"long": {"k1": 3.25,"b": -0.8, "cooldown_sec": int(1.5 * _H), "max_concurrent": MC}},
        "JP225":  {"long": {"k1": 2.7, "b": -2.0, "cooldown_sec": int(3.0 * _H),  "max_concurrent": MC},
                   "short": {"k1": 3.8,"b": 1.0,  "cooldown_sec": int(0.75 * _H), "max_concurrent": MC}},
        "HK50":   {"long": {"k1": 2.6, "b": -2.0, "cooldown_sec": int(2.0 * _H), "max_concurrent": MC},
                   "short": {"k1": 3.0,"b": 1.6,  "cooldown_sec": int(1.0 * _H), "max_concurrent": MC}},
        "GER40":  {"long": {"k1": 3.5, "b": -2.0, "cooldown_sec": int(1.25 * _H), "max_concurrent": MC}},
        "UK100":  {"long": {"k1": 3.35,"b": -2.0, "cooldown_sec": int(1.5 * _H),  "max_concurrent": MC},
                   "short": {"k1": 3.8,"b": 1.8,  "cooldown_sec": int(0.5 * _H),  "max_concurrent": MC}},
        "XAUUSD": {"long": {"k1": 2.35,"b": -1.8, "cooldown_sec": int(3.0 * _H),  "max_concurrent": MC}},
        "XAGUSD": {"long": {"k1": 2.85,"b": -1.8, "cooldown_sec": int(3.0 * _H),  "max_concurrent": MC},
                   "short": {"k1": 3.8,"b": -2.0, "cooldown_sec": int(1.0 * _H),  "max_concurrent": MC}},
        "WTI":    {"long": {"k1": 2.9, "b": -2.0, "cooldown_sec": int(3.0 * _H), "max_concurrent": MC},
                   "short": {"k1": 3.4,"b": 0.6,  "cooldown_sec": int(1.0 * _H), "max_concurrent": MC}},
        # ── FX 메이저 (HFM, HANDOFF_FX_majors 2026-06-27 추가; 에러 시 이 블록만 제거) ──
        #   S2역추세롱=long(z≤−K1) / S2역추세숏=short(z≥+K1). AUD·NZD가 평균회귀형 주력.
        "AUDUSD": {"long": {"k1": 3.5, "b": -2.0, "cooldown_sec": int(1.25 * _H), "max_concurrent": MC},
                   "short": {"k1": 2.8,"b": -2.0, "cooldown_sec": int(2.25 * _H), "max_concurrent": MC}},
        "NZDUSD": {"long": {"k1": 3.6, "b": -2.0, "cooldown_sec": int(1.0 * _H),  "max_concurrent": MC},
                   "short": {"k1": 3.1,"b": -1.0, "cooldown_sec": int(2.25 * _H), "max_concurrent": MC}},
        "GBPUSD": {"long": {"k1": 3.5, "b": -0.4, "cooldown_sec": int(1.0 * _H),  "max_concurrent": MC}},
        "EURUSD": {"long": {"k1": 3.7, "b": -0.6, "cooldown_sec": int(0.75 * _H), "max_concurrent": MC}},
        "USDJPY": {"short": {"k1": 3.3,"b": -0.6, "cooldown_sec": int(1.75 * _H), "max_concurrent": MC}},
        "USDCHF": {"short": {"k1": 3.7,"b": -1.6, "cooldown_sec": int(0.75 * _H), "max_concurrent": MC}},
        "USDCAD": {"short": {"k1": 3.6,"b": -2.0, "cooldown_sec": int(0.75 * _H), "max_concurrent": MC}},
    }
    return make_s1_config(name="mt5", params_by_symbol=REV_MT5, strategy="s2",
                          avg_down=True, signal_only=signal_only, **kw)


# ─────────────────────────────────────────────────────────────────────────────
# 일봉(D1) FX 채널 — HANDOFF_DAILY_FX. namespace "fxd"(1분 mt5와 별개 채널),
#   win=90일, 쿨다운 일(日), 최대보유 30일, candle_interval="D". executor-a2(MT5) 공유.
#   §3 🟢(쓸만) 픽만. avg_down 미사용(일봉 핸드오프 무관). maxc=핸드오프 §3 값.
# ─────────────────────────────────────────────────────────────────────────────
_D = 86400  # 1일(초)


def make_fx_daily_trend_config(*, signal_only: bool = True, **kw) -> "TradeConfig":
    """일봉 FX 추세(S1). 롱=trendlong(z≥+K1) / 숏=trend(z≤−K1). §3 🟢 추세픽."""
    FXD_TREND = {
        # 추세롱(z≥+K1) — §3 🟢 4종 (추세숏은 🟢 없음 → 제외)
        "EURUSD": {"long": {"k1": 2.1, "b": -1.4, "cooldown_sec": 3 * _D, "max_concurrent": 8}},
        "USDJPY": {"long": {"k1": 2.5, "b": -2.0, "cooldown_sec": 2 * _D, "max_concurrent": 10}},
        "USDCAD": {"long": {"k1": 2.7, "b": 0.2,  "cooldown_sec": 1 * _D, "max_concurrent": 10}},
        "NZDUSD": {"long": {"k1": 2.5, "b": 0.8,  "cooldown_sec": 1 * _D, "max_concurrent": 10}},
    }
    return make_s1_config(name="fxd", params_by_symbol=FXD_TREND, strategy="s3",  # s3=일봉 추세
                          avg_down=False, signal_only=signal_only,
                          s1_win=90, candle_interval="D", candles_num=250,
                          s1_max_hold_sec=30 * _D, **kw)


def make_fx_daily_rev_config(*, signal_only: bool = True, **kw) -> "TradeConfig":
    """일봉 FX 역추세(S2). 롱=z≤−K1 / 숏=z≥+K1. §3 🟢 역추세픽. 추매 미사용."""
    FXD_REV = {
        # 역추세롱(z≤−K1) — §3 🟢 7종
        "EURUSD": {"long": {"k1": 2.0, "b": -1.2, "cooldown_sec": 3 * _D, "max_concurrent": 9}},
        "GBPUSD": {"long": {"k1": 2.8, "b": 1.4,  "cooldown_sec": 1 * _D, "max_concurrent": 10},
                   "short": {"k1": 2.0,"b": -1.8, "cooldown_sec": 3 * _D, "max_concurrent": 7}},
        "USDJPY": {"long": {"k1": 2.3, "b": -0.2, "cooldown_sec": 3 * _D, "max_concurrent": 6}},
        "AUDUSD": {"long": {"k1": 2.6, "b": -0.2, "cooldown_sec": 1 * _D, "max_concurrent": 10},
                   "short": {"k1": 1.9,"b": -1.6, "cooldown_sec": 7 * _D, "max_concurrent": 5}},
        "USDCAD": {"long": {"k1": 1.8, "b": -1.6, "cooldown_sec": 3 * _D, "max_concurrent": 9}},
        "USDCHF": {"long": {"k1": 2.8, "b": -1.2, "cooldown_sec": 1 * _D, "max_concurrent": 10},
                   "short": {"k1": 2.2,"b": 0.0,  "cooldown_sec": 3 * _D, "max_concurrent": 7}},
        "NZDUSD": {"long": {"k1": 2.3, "b": 1.4,  "cooldown_sec": 3 * _D, "max_concurrent": 5},
                   "short": {"k1": 1.0,"b": -2.0, "cooldown_sec": 10 * _D, "max_concurrent": 3}},
    }
    return make_s1_config(name="fxd", params_by_symbol=FXD_REV, strategy="s4",  # s4=일봉 역추세
                          avg_down=False, signal_only=signal_only,
                          s1_win=90, candle_interval="D", candles_num=250,
                          s1_max_hold_sec=30 * _D, **kw)


def make_mt5_signal_config(
    *,
    indicator_min_thr: float = 0.005,
    indicator_max_thr: float = 0.07,
    target_cross: int = 5,
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
    entry_percent = 0.1   # 0.1/100 × leverage(50) = 5% notional/진입
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

        max_effective_leverage=10.0,

        # 인디케이터 관련
        indicator_min_thr=indicator_min_thr,
        indicator_max_thr=indicator_max_thr,
        target_cross=target_cross,

        candles_num=candles_num,

        min_ma_threshold=min_ma_threshold,
        signal_only=False,
        basic_long_enabled=False,   # 🔴 롱=S1, 숏=S2로 분리 → basic 은퇴
        basic_short_enabled=False,
    )
    return cfg.normalized()
