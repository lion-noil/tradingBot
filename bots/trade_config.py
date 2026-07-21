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
    # ✅ (전략,심볼)별 진입 퍼센트 {strategy: {SYM: pct, "_default": pct}}. executor가 액션 strategy로 조회.
    #   1분봉(s1/s2)·일봉(s3/s4)이 같은 심볼이라도 다른 %(일봉 MT5=2% 등). 비면 심볼/전역 fallback.
    entry_percent_by_strategy: Dict[str, Dict[str, float]] = field(default_factory=dict)

    # 인디케이터
    indicator_min_thr: float = 0.005
    indicator_max_thr: float = 0.05
    target_cross: int = 10

    # 슬라이딩 윈도우(캔들 개수)
    candles_num: int = 10080  # (예: 1분봉 7일치)


    # signal_only (True면 시그널만, 실제 주문 X)
    signal_only: bool = False

    # ✅ 전략 선택: "s1"(σ추세)/"s2"(σ역추세)/"s3"(일봉추세)/"s4"(일봉역추세)
    #   /"s11"(1분봉책 z추세)/"s12"(1분봉책 z역추세)/"s13"(급락페이드).
    #   basic(MA100 리버전)은 퇴출됨 — 시그마 파라미터 없는 엔진(bybit/mt5)은 config publish/상태표시 전용(무매매).
    strategy: str = "s1"
    # ✅ 드레인 모드(구 전략 마이그레이션): True면 신규 진입·추매 중지, 오픈 포지션 청산 관리만 지속
    entries_disabled: bool = False
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
        strategy="none",            # 🔹 매매 안 함(basic 퇴출) — config publish/상태표시 전용. 매매는 s1/s2가 담당
        symbols=list(symbols),

        ws_stale_sec=ws_stale_sec,
        ws_global_stale_sec=ws_global_stale_sec,

        leverage=leverage,
        entry_percent=entry_percent,
        entry_percent_by_symbol=entry_percent_by_symbol,
        # ✅ (전략,심볼)별 진입%: 구 1분봉(s1/s2 드레인)·일봉 Bybit 크립토(s3/s4)=2%(0.04).
        #   S11 1분봉책(s11/s12/s13)=5%(0.1) — S11_SYMBOLS.md §5 진입비율 실측(2026-07-12):
        #   5%=CAGR 41.7%/MTM 낙폭 53.3%(2022 스트레스 상한)/청산 불가능 수준.
        #   S22 4시간봉책(s14=ewz 포함, HANDOFF_S22)=5% — z/페이드 셀은 s11~s13 태그 공유(동률 5%).
        #   ⚠️ S11+S22 동시 5%는 2022형 이벤트 합산낙폭 62%(청산은 38배 여유) — HANDOFF_S22 §3.
        #   (0.04/100 × 레버50 = 2% notional, 0.1/100 × 레버50 = 5% notional)
        entry_percent_by_strategy={
            **{s: {"_default": 0.04} for s in ("s1", "s2", "s3", "s4")},
            **{s: {"_default": 0.1} for s in ("s11", "s12", "s13", "s14")},
        },
        max_effective_leverage=max_effective_leverage,


        indicator_min_thr=indicator_min_thr,
        indicator_max_thr=indicator_max_thr,
        target_cross=target_cross,

        candles_num=candles_num,

        min_ma_threshold=min_ma_threshold,
        signal_only=signal_only,
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
    strategy: str = "s1",       # ✅ "s1"(추세) | "s2"(역추세) | "s11"/"s12"/"s13"(1분봉책) — 동일 엔진 계열
    avg_down: bool = False,     # ✅ 추매(S2 역추세 전용)
    s1_win: int = 10080,        # ✅ MA/σ 창. 1분봉=10080(7일). 일봉채널=90(90일).
    candle_interval: str = "1",  # ✅ "1"(분) | "D"(일봉채널)
    s1_max_hold_sec: int = 14 * 24 * 3600,  # ✅ 최대보유. 1분=14일, 일봉=30일.
    entries_disabled: bool = False,  # ✅ 드레인 모드(신규진입 중지, 청산만)
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
        # XAUTUSDT(테더골드) — S1추세 양방향 🟢 (HANDOFF_S1_trend). 크립토 추세셋 중 강함.
        "XAUTUSDT": {"long": {"k1": 3.25, "b": -2.0, "cooldown_sec": int(2.0 * _H),  "max_concurrent": MC},
                     "short": {"k1": 3.5, "b": 0.8,  "cooldown_sec": int(0.75 * _H), "max_concurrent": MC}},
    }
    TREND_MT5: dict[str, dict] = {
        # BTCUSD·ETHUSD 1분 S1추세롱 🟢 (HANDOFF_S1_trend). Bybit BTCUSDT와 별개 거래소/계좌.
        "BTCUSD": {"long": {"k1": 3.25, "b": -2.0, "cooldown_sec": int(2.75 * _H), "max_concurrent": MC}},
        "ETHUSD": {"long": {"k1": 3.5,  "b": 1.6,  "cooldown_sec": int(1.75 * _H), "max_concurrent": MC}},
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
        entries_disabled=entries_disabled,  # ✅ 드레인 모드
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
        # XAUTUSDT 역추세롱 🟢 (HANDOFF_S2_reversion). 숏은 ❌ → 롱만.
        "XAUTUSDT": {"long": {"k1": 2.75,"b": -1.8, "cooldown_sec": int(3.0 * _H),  "max_concurrent": MC}},
    }
    return make_s1_config(name="bybit", params_by_symbol=REV_BYBIT, strategy="s2",
                          avg_down=True, signal_only=signal_only, **kw)


def make_s2_mt5_config(*, signal_only: bool = True, **kw) -> "TradeConfig":
    """S2 = 역추세(reversion) MT5 (portfolio_sim picks)."""
    _H = 3600
    MC = 200
    REV_MT5 = {
        # BTCUSD 1분 S2역추세롱 🟢 (HANDOFF_S2_reversion). ETHUSD 역추세는 ⚪ → 추세(TREND_MT5)만.
        "BTCUSD": {"long": {"k1": 3.5, "b": -2.0, "cooldown_sec": int(2.25 * _H), "max_concurrent": MC}},
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
# S11 「1분봉책」 — HANDOFF_MASTER v4 §2-A′ (2026-07-11/12). 구 S1/S2 폐기·대체.
#   3패밀리: s11=z추세 / s12=z역추세 / s13=급락페이드. 창 6~24시간(구 7일 대비 대폭 축소).
#   네임스페이스: Bybit="s11", MT5="s11m" (구 채널과 분리 필수 — open_signals 격리).
#   롱=SL無(no_sl, 크립토 SL유해 4회 재확인) 단 XAUT추세롱·XRP숏은 SL 유지. 보유 14d(페이드는 셀별 24~72h).
#   방법론: 무게이트 베이스자립 + 상장 전기간 연도균형. fee 0.11%(USDJPY 0.02%).
# ─────────────────────────────────────────────────────────────────────────────
_H = 3600   # 1시간(초)


def make_s11_trend_config(*, signal_only: bool = True, **kw) -> "TradeConfig":
    """S11 z추세 (Bybit). 롱=z≥+K1(SL無), XRP만 숏(z≤−K1, SL유지)."""
    # ✅ 개별캡(셀·심볼) 폐지 (2026-07-20 사용자 확정): 책 셀은 전부 maxc=200(비구속 센티널),
    #   리스크는 사이징+executor max_eff_lev(유니버스캡 200게임 상당)로만 관리.
    #   근거(universe_symbolcap.py 스윕): 깊은 중첩=폭락 클러스터 매수가 복리수익의 절반(캡12면 39배→21배),
    #   임의 캡은 성과 훼손(구 페이드캡 12는 -27%였음 — cap_sim.py). 실측 참고치: z셀 최대 6~11,
    #   페이드 최대 28(HK50)·24(SOL), 심볼합산 최대 SOL 27/HK50 40/JP225 34.
    S11_TREND = {
        "BTCUSDT": {"long": {"win": 1440, "k1": 6.0, "b": 0.0,  "cooldown_sec": 3 * _H, "max_concurrent": 200, "no_sl": True}},
        "ETHUSDT": {"long": {"win": 720,  "k1": 6.0, "b": -3.0, "cooldown_sec": 3 * _H, "max_concurrent": 200, "no_sl": True}},
        "SOLUSDT": {"long": {"win": 1440, "k1": 5.5, "b": 2.5,  "cooldown_sec": 3 * _H, "max_concurrent": 200, "no_sl": True}},
        "XAUTUSDT": {"long": {"win": 1440, "k1": 4.0, "b": -2.5, "cooldown_sec": 1 * _H, "max_concurrent": 200}},  # 금: SL 무해→유지
        "XRPUSDT": {"short": {"win": 720, "k1": 5.0, "b": -0.5, "cooldown_sec": 1 * _H, "max_concurrent": 200}},   # 유일 숏: SL유
    }
    return make_s1_config(name="s11", params_by_symbol=S11_TREND, strategy="s11",
                          avg_down=False, signal_only=signal_only,
                          s1_win=1440, candle_interval="1", candles_num=2000,
                          s1_max_hold_sec=14 * _D, **kw)


def make_s11_rev_config(*, signal_only: bool = True, **kw) -> "TradeConfig":
    """S11 z역추세 (Bybit). 롱=z≤−K1, SL無. 추매 없음(구 S2와 다름)."""
    S11_REV = {
        "BTCUSDT": {"long": {"win": 1320, "k1": 5.0,  "b": -1.0, "cooldown_sec": 1 * _H, "max_concurrent": 200, "no_sl": True}},
        "XAUTUSDT": {"long": {"win": 1440, "k1": 4.25, "b": -3.0, "cooldown_sec": 3 * _H, "max_concurrent": 200, "no_sl": True}},
    }
    return make_s1_config(name="s11", params_by_symbol=S11_REV, strategy="s12",
                          avg_down=False, signal_only=signal_only,
                          s1_win=1440, candle_interval="1", candles_num=2000,
                          s1_max_hold_sec=14 * _D, **kw)


def make_s11_fade_config(*, signal_only: bool = True, **kw) -> "TradeConfig":
    """S11 급락페이드 (Bybit). M분 수익률≤−X% 롱 / BTC=되돌림×1.5 익절+캡48h, 나머지=시간청산 24h. SL無."""
    # ✅ 페이드 캡 12 폐지→200 (2026-07-20): 백테스트는 캡 없이 검증 — 캡 12는 폭락 클러스터에서
    #   진입을 잘라 6.3y 최종자산 -27%였음(전부 SOL, 드롭 12건에 r합 +808%p — cap_sim.py).
    S11_FADE = {
        "BTCUSDT": {"long": {"m_min": 60, "drop_pct": 0.04, "retr_mult": 1.5, "hold_sec": 48 * _H,
                             "cooldown_sec": 1800, "max_concurrent": 200}},
        "ETHUSDT": {"long": {"m_min": 30, "drop_pct": 0.04, "hold_sec": 24 * _H,
                             "cooldown_sec": 1800, "max_concurrent": 200}},
        "SOLUSDT": {"long": {"m_min": 15, "drop_pct": 0.05, "hold_sec": 24 * _H,
                             "cooldown_sec": 1800, "max_concurrent": 200}},  # ⚠️꼬리 -55% — 저사이징 전제
        "XRPUSDT": {"long": {"m_min": 30, "drop_pct": 0.05, "hold_sec": 24 * _H,
                             "cooldown_sec": 1800, "max_concurrent": 200}},
    }
    return make_s1_config(name="s11", params_by_symbol=S11_FADE, strategy="s13",
                          avg_down=False, signal_only=signal_only,
                          s1_win=1440, candle_interval="1", candles_num=2000,
                          s1_max_hold_sec=48 * _H, **kw)


def make_s11_mt5_trend_config(*, signal_only: bool = True, **kw) -> "TradeConfig":
    """S11 확장판 z추세롱 (MT5·FX, 2026-07-12). ⚠️데이터 3.5년(2022 미검증) → 보수 사이징 등급. 전셀 SL無."""
    S11M_TREND = {
        "JP225":  {"long": {"win": 1440, "k1": 4.0,  "b": -1.5, "cooldown_sec": 3 * _H, "max_concurrent": 200, "no_sl": True}},
        # US100: 1m 백필(6.3y) 재스크리닝 교체(2026-07-16) — 구 w720 K5.25 B-2.5 cd1h는 확장데이터 +0.28% 탈락.
        #   신규 w1440 K5.5 B-3 cd3h: 이웃격자 중앙값 +0.34/+0.09 견고.
        "US100":  {"long": {"win": 1440, "k1": 5.5,  "b": -3.0, "cooldown_sec": 3 * _H, "max_concurrent": 200, "no_sl": True}},
        "GER40":  {"long": {"win": 1440, "k1": 3.75, "b": -3.0, "cooldown_sec": 3 * _H, "max_concurrent": 200, "no_sl": True}},
        "UK100":  {"long": {"win": 1440, "k1": 3.75, "b": -3.0, "cooldown_sec": 3 * _H, "max_concurrent": 200, "no_sl": True}},
        # HK50 추세롱(구 w360 K5.75): 1m 백필 재스크리닝에서 -0.13% 기각 → 제거(2026-07-16, 오픈 없음 확인).
        #   HK50은 역추롱(S11M_REV w2880)으로 대체.
        "XAGUSD": {"long": {"win": 1320, "k1": 4.75, "b": -2.5, "cooldown_sec": 1 * _H, "max_concurrent": 200, "no_sl": True}},
        "WTI":    {"long": {"win": 720,  "k1": 4.5,  "b": -2.0, "cooldown_sec": 3 * _H, "max_concurrent": 200, "no_sl": True}},
        "USDJPY": {"long": {"win": 1440, "k1": 4.5,  "b": -2.5, "cooldown_sec": 1 * _H, "max_concurrent": 200, "no_sl": True}},
        # ── 크립토 CFD (2026-07-15 추가) — Bybit S11 검증 파라미터 그대로(교차검증 일치). ──
        #   별도 계좌 유니버스 논리로 채택. HFM 실비용 실측: 스왑 연−8%(일−0.022%)+스프레드
        #   BTC 0.096%/ETH 0.26% → 14d 보유 실질 +0.34~0.48%p 초과비용 반영해도 기대 +1.1~1.2% 생존.
        #   BTC z역추롱(+1.01→+0.67%)은 마진 얇아 제외. ⚠️Bybit s11과 동일 신호 — 계좌 합산 노출 인지.
        "BTCUSD": {"long": {"win": 1440, "k1": 6.0, "b": 0.0,  "cooldown_sec": 3 * _H, "max_concurrent": 200, "no_sl": True}},
        "ETHUSD": {"long": {"win": 720,  "k1": 6.0, "b": -3.0, "cooldown_sec": 3 * _H, "max_concurrent": 200, "no_sl": True}},
    }
    return make_s1_config(name="s11m", params_by_symbol=S11M_TREND, strategy="s11",
                          avg_down=False, signal_only=signal_only,
                          # ⚠️ candles_num: 책 모드 캔들 스토어는 primary(이 config)의 값 사용 —
                          #    S11M_REV HK50 w2880 커버 위해 2000→3200 (trade_bot.py CandleEngine 참조)
                          s1_win=1440, candle_interval="1", candles_num=3200,
                          s1_max_hold_sec=14 * _D, **kw)


def make_s11_mt5_rev_config(*, signal_only: bool = True, **kw) -> "TradeConfig":
    """S11 확장판 z역추세롱 (MT5, 2026-07-15 추가). BTCUSD=Bybit S11 역추롱 그대로
    (실보유 평균 5.0일(중앙 2.2일) → 스왑 실비용 반영 기대 +0.92%).
    HK50=1m 백필(6.3y) 재스크리닝 신규(2026-07-16, 이웃 +0.26 견고) — 기각된 추세롱 대체."""
    S11M_REV = {
        "BTCUSD": {"long": {"win": 1320, "k1": 5.0, "b": -1.0, "cooldown_sec": 1 * _H, "max_concurrent": 200, "no_sl": True}},
        "HK50":   {"long": {"win": 2880, "k1": 3.5, "b": 2.0,  "cooldown_sec": 1 * _H, "max_concurrent": 200, "no_sl": True}},
    }
    return make_s1_config(name="s11m", params_by_symbol=S11M_REV, strategy="s12",
                          avg_down=False, signal_only=signal_only,
                          s1_win=1440, candle_interval="1", candles_num=3200,  # HK50 w2880 커버
                          s1_max_hold_sec=14 * _D, **kw)


def make_s11_mt5_fade_config(*, signal_only: bool = True, **kw) -> "TradeConfig":
    """S11 확장판 급락페이드 (MT5·FX). JP225 4h−3%(T48h)·HK50 2h−2%(T72h)·USDJPY 2h−1%(T48h). 시간청산·SL無."""
    # ✅ 페이드 캡 12 폐지→200 (2026-07-20): 캡 12는 HK50×21+JP225×13 진입 드롭(-1.6%) — S11_FADE 참조.
    S11M_FADE = {
        "JP225":  {"long": {"m_min": 240, "drop_pct": 0.03, "hold_sec": 48 * _H,
                            "cooldown_sec": 1800, "max_concurrent": 200}},
        "HK50":   {"long": {"m_min": 120, "drop_pct": 0.02, "hold_sec": 72 * _H,
                            "cooldown_sec": 1800, "max_concurrent": 200}},
        "USDJPY": {"long": {"m_min": 120, "drop_pct": 0.01, "hold_sec": 48 * _H,
                            "cooldown_sec": 1800, "max_concurrent": 200}},
        # ── 크립토 CFD (2026-07-15 추가) — Bybit S11 페이드 그대로. 보유 짧아 스왑 영향 미미. ──
        "BTCUSD": {"long": {"m_min": 60, "drop_pct": 0.04, "retr_mult": 1.5, "hold_sec": 48 * _H,
                            "cooldown_sec": 1800, "max_concurrent": 200}},
        "ETHUSD": {"long": {"m_min": 30, "drop_pct": 0.04, "hold_sec": 24 * _H,
                            "cooldown_sec": 1800, "max_concurrent": 200}},
    }
    return make_s1_config(name="s11m", params_by_symbol=S11M_FADE, strategy="s13",
                          avg_down=False, signal_only=signal_only,
                          s1_win=1440, candle_interval="1", candles_num=2000,
                          s1_max_hold_sec=72 * _H, **kw)


# ─────────────────────────────────────────────────────────────────────────────
# 일봉(D1) FX 채널 — 🟡 드레인 (2026-07-21 S33M 통합, 사용자 확정 "통일성 측면 합치는 게 맞음").
#   활성 FX 셀은 전부 MT5D_TREND/REV(ns "mt5")로 이동 — 유니버스(환율 11.5%)·사이징은 심볼 기반이라 불변.
#   이 fxd 채널은 오픈 포지션(USDCHF 역숏 2건, ≤7/28 만기) 청산 관리만 남김 → 소진 후 s33f 컨테이너 제거.
#   ✅ 일봉 maxc도 200 센티널 (2026-07-20 개별캡 전면 폐지): 핸드오프 §3 캡은 쿨다운×보유15일의
#   이론상 천장 기록값이라 최근 5y 시뮬에서 캡 유/무 결과 완전 동일(1건도 미발동) — universe_symbolcap.py NOCAP33.
# ─────────────────────────────────────────────────────────────────────────────
_D = 86400  # 1일(초)


def make_fx_daily_trend_config(*, signal_only: bool = True, **kw) -> "TradeConfig":
    """일봉 FX 추세(S3). HANDOFF_MASTER v2(2026-07-09, 창 재배정): FX 추세는 USDJPY W120만 엣지.
    (EURUSD/USDCAD/NZDUSD 추세롱은 전 창 탐색 후 엣지 없음 확정 → 제외; 제외 시점 오픈 S3포지션 없음 확인)"""
    FXD_TREND = {
        "USDJPY": {"long": {"win": 120, "k1": 2.9, "b": -1.4, "cooldown_sec": 5 * _D, "max_concurrent": 200}},  # 13.5y 구제(bad4→worst-3)
    }
    return make_s1_config(name="fxd", params_by_symbol=FXD_TREND, strategy="s3",  # s3=일봉 추세
                          avg_down=False, signal_only=signal_only,
                          s1_win=250, candle_interval="D", candles_num=350,
                          s1_max_hold_sec=15 * _D, **kw)


def make_fx_daily_rev_config(*, signal_only: bool = True, **kw) -> "TradeConfig":
    """일봉 FX 역추세(S4). HANDOFF_MASTER v2 §3-B: 심볼×방향별 win(90~200). 추매 미사용."""
    FXD_REV = {
        # ✅ 13.5y(D13) 재검증 2026-07-15: 역롱 4종 파라미터 교체(구제), USDCAD·USDCHF 역숏 기각 제거.
        "EURUSD": {"long": {"win": 120, "k1": 2.9, "b": -0.6, "cooldown_sec": 1 * _D, "max_concurrent": 200}},  # ⚠️2020 편중
        "GBPUSD": {"long": {"win": 150, "k1": 2.4, "b": 1.6,  "cooldown_sec": 1 * _D, "max_concurrent": 200},
                   "short": {"win": 150, "k1": 2.1, "b": 0.6, "cooldown_sec": 1 * _D, "max_concurrent": 200}},
        "USDJPY": {"long": {"win": 150, "k1": 2.1, "b": -3.0, "cooldown_sec": 1 * _D, "max_concurrent": 200}},
        "AUDUSD": {"long": {"win": 200, "k1": 2.6, "b": -3.0, "cooldown_sec": 3 * _D, "max_concurrent": 200},
                   "short": {"win": 90,  "k1": 1.8, "b": 0.2, "cooldown_sec": 3 * _D, "max_concurrent": 200}},
        "USDCAD": {"long": {"win": 200, "k1": 2.3, "b": 1.8,  "cooldown_sec": 1 * _D, "max_concurrent": 200}},   # 역숏(15년 -71) 기각 제거
        "USDCHF": {"long": {"win": 200, "k1": 2.3, "b": -0.6, "cooldown_sec": 1 * _D, "max_concurrent": 200},
                   "short": {"win": 200, "k1": 99.0, "b": -0.2, "cooldown_sec": 3 * _D, "max_concurrent": 200}},  # 역숏 기각 — 오픈 2개 드레인(k1=99)
        "NZDUSD": {"long": {"win": 250, "k1": 2.9, "b": -3.0, "cooldown_sec": 1 * _D, "max_concurrent": 200},
                   "short": {"win": 150, "k1": 1.4, "b": -1.2, "cooldown_sec": 2 * _D, "max_concurrent": 200}},
    }
    return make_s1_config(name="fxd", params_by_symbol=FXD_REV, strategy="s4",  # s4=일봉 역추세
                          avg_down=False, signal_only=signal_only,
                          s1_win=250, candle_interval="D", candles_num=350,
                          s1_max_hold_sec=15 * _D, **kw)


# ─────────────────────────────────────────────────────────────────────────────
# 일봉(D1) 크립토(Bybit) 채널 — HANDOFF_DAILY_MT5 §3b. namespace "bybit" 공유(1분 s1/s2와 동일,
#   전략 태그 s3/s4로만 구분). basic(MA100) 퇴출로 청산 충돌 없음 — 남은 엔진은 전부 list_open_s1(tag)로
#   자기 전략 포지션만 관리. win=90일, 쿨다운 일(日), 최대보유 15일, candle_interval="D".
#   거래는 executor-a1(Bybit)이 s3/s4 태그로 2% 사이징.
# ─────────────────────────────────────────────────────────────────────────────
def make_crypto_daily_trend_config(*, signal_only: bool = True, **kw) -> "TradeConfig":
    """일봉 크립토 추세(S3). HANDOFF_MASTER v2 §3-C: 창 재배정(ETHUSDT롱 W60, 나머지 W90)."""
    CRYPTOD_TREND = {
        "BTCUSDT": {"long": {"win": 90, "k1": 2.5, "b": -2.0, "cooldown_sec": 2 * _D, "max_concurrent": 200},
                    "short": {"win": 90, "k1": 2.4, "b": 0.2,  "cooldown_sec": 1 * _D, "max_concurrent": 200}},
        "ETHUSDT": {"long": {"win": 60, "k1": 2.5, "b": -3.0, "cooldown_sec": 1 * _D, "max_concurrent": 200},
                    "short": {"win": 90, "k1": 1.6, "b": -0.8, "cooldown_sec": 5 * _D, "max_concurrent": 200}},
        "SOLUSDT": {"long": {"win": 90, "k1": 2.9, "b": -3.0, "cooldown_sec": 1 * _D, "max_concurrent": 200},
                    "short": {"win": 90, "k1": 1.7, "b": 0.2,  "cooldown_sec": 2 * _D, "max_concurrent": 200}},
        "XRPUSDT": {"long": {"win": 90, "k1": 3.1, "b": -3.0, "cooldown_sec": 1 * _D, "max_concurrent": 200},
                    "short": {"win": 90, "k1": 1.6, "b": -1.4, "cooldown_sec": 5 * _D, "max_concurrent": 200}},
    }
    return make_s1_config(name="bybit", params_by_symbol=CRYPTOD_TREND, strategy="s3",  # bybit 네임스페이스 공유(basic 퇴출로 충돌 없음), 태그 s3로 구분
                          avg_down=False, signal_only=signal_only,
                          s1_win=250, candle_interval="D", candles_num=350,
                          s1_max_hold_sec=15 * _D, **kw)


def make_crypto_daily_rev_config(*, signal_only: bool = True, **kw) -> "TradeConfig":
    """일봉 크립토 역추세(S4). HANDOFF_MASTER v2 §3-C: 창 150~200 상향, 🆕ETHUSDT 양방향 채택."""
    CRYPTOD_REV = {
        "BTCUSDT": {"long": {"win": 150, "k1": 1.1, "b": 0.8,  "cooldown_sec": 10 * _D, "max_concurrent": 200}},
        "ETHUSDT": {"long": {"win": 200, "k1": 1.9, "b": -0.4, "cooldown_sec": 2 * _D, "max_concurrent": 200},
                    "short": {"win": 200, "k1": 2.6, "b": 2.2,  "cooldown_sec": 1 * _D, "max_concurrent": 200}},
        "SOLUSDT": {"long": {"win": 200, "k1": 1.8, "b": 0.4,  "cooldown_sec": 1 * _D, "max_concurrent": 200}},
        "XRPUSDT": {"long": {"win": 200, "k1": 1.6, "b": -1.4, "cooldown_sec": 2 * _D, "max_concurrent": 200},
                    "short": {"win": 200, "k1": 1.1, "b": 0.2,  "cooldown_sec": 7 * _D, "max_concurrent": 200}},
    }
    return make_s1_config(name="bybit", params_by_symbol=CRYPTOD_REV, strategy="s4",  # bybit 네임스페이스 공유(basic 퇴출로 충돌 없음), 태그 s4로 구분
                          avg_down=False, signal_only=signal_only,
                          s1_win=250, candle_interval="D", candles_num=350,
                          s1_max_hold_sec=15 * _D, **kw)


# ─────────────────────────────────────────────────────────────────────────────
# 일봉(D1) MT5 채널 — HANDOFF_DAILY_MT5 §3 + FX(구 fxd, 2026-07-21 통합). namespace "mt5" 공유
#   (1분 s1/s2와 동일, 태그 s3/s4로만 구분). basic 퇴출로 충돌 없음. 쿨다운 일(日), 최대보유 15일,
#   candle_interval="D". 사이징은 executor entry_percent_by_strategy 심볼 기반(비환율 7%·FX 11.5%) —
#   ns 무관이라 통합해도 불변. 유니버스 분류도 심볼 기반(universe_of)이라 환율 회계 유지.
# ─────────────────────────────────────────────────────────────────────────────
def make_mt5_daily_trend_config(*, signal_only: bool = True, **kw) -> "TradeConfig":
    """일봉 MT5 비환율 추세(S3). HANDOFF_MASTER v2 §3-A: 창 재배정(ETHUSD W60, XAGUSD·JP225 W200)."""
    MT5D_TREND = {
        # ── FX (2026-07-21 fxd 채널 통합 — 구 FXD_TREND 그대로, 유니버스=환율·일봉 11.5% 사이징) ──
        "USDJPY": {"long": {"win": 120, "k1": 2.9, "b": -1.4, "cooldown_sec": 5 * _D, "max_concurrent": 200}},  # 13.5y 구제
        "BTCUSD": {"long": {"win": 90, "k1": 2.5, "b": -2.0, "cooldown_sec": 2 * _D, "max_concurrent": 200},
                   "short": {"win": 90, "k1": 2.4, "b": -0.2, "cooldown_sec": 1 * _D, "max_concurrent": 200}},
        "ETHUSD": {"long": {"win": 60, "k1": 2.8, "b": -3.0, "cooldown_sec": 1 * _D, "max_concurrent": 200},
                   "short": {"win": 60, "k1": 2.0, "b": 0.0,  "cooldown_sec": 2 * _D, "max_concurrent": 200}},
        # ✅ 13.5y(D13) 재검증 2026-07-15: XAGUSD·XAUUSD 추롱 파라미터 교체(구제 스윕),
        #   WTI 추롱(bad4)·US100 추롱(bad3) 기각 제거. WTI 추숏·JP225 추롱은 13.5y 통과 유지.
        "XAGUSD": {"long": {"win": 250, "k1": 2.9, "b": 1.0,  "cooldown_sec": 5 * _D, "max_concurrent": 200}},
        "XAUUSD": {"long": {"win": 150, "k1": 2.6, "b": 0.2,  "cooldown_sec": 3 * _D, "max_concurrent": 200}},
        "WTI":    {"short": {"win": 90, "k1": 2.3, "b": 2.0,  "cooldown_sec": 1 * _D, "max_concurrent": 200}},
        "JP225":  {"long": {"win": 200, "k1": 2.7, "b": 1.4,  "cooldown_sec": 1 * _D, "max_concurrent": 200}},
    }
    return make_s1_config(name="mt5", params_by_symbol=MT5D_TREND, strategy="s3",  # mt5 네임스페이스 공유(basic 퇴출로 충돌 없음), 태그 s3로 구분
                          avg_down=False, signal_only=signal_only,
                          s1_win=250, candle_interval="D", candles_num=350,
                          s1_max_hold_sec=15 * _D, **kw)


def make_mt5_daily_rev_config(*, signal_only: bool = True, **kw) -> "TradeConfig":
    """일봉 MT5 비환율 역추세(S4). HANDOFF_MASTER v2 §3-A: 창 150~200 상향, 🆕XAUUSD·UK100·HK50 숏 채택."""
    MT5D_REV = {
        # ── FX (2026-07-21 fxd 채널 통합 — 구 FXD_REV 활성 셀 그대로. 기각·드레인 셀(USDCHF 역숏 k1=99)은
        #    fxd 채널에 남겨 오픈 소진만 관리) ──
        "EURUSD": {"long": {"win": 120, "k1": 2.9, "b": -0.6, "cooldown_sec": 1 * _D, "max_concurrent": 200}},  # ⚠️2020 편중
        "GBPUSD": {"long": {"win": 150, "k1": 2.4, "b": 1.6,  "cooldown_sec": 1 * _D, "max_concurrent": 200},
                   "short": {"win": 150, "k1": 2.1, "b": 0.6, "cooldown_sec": 1 * _D, "max_concurrent": 200}},
        "USDJPY": {"long": {"win": 150, "k1": 2.1, "b": -3.0, "cooldown_sec": 1 * _D, "max_concurrent": 200}},
        "AUDUSD": {"long": {"win": 200, "k1": 2.6, "b": -3.0, "cooldown_sec": 3 * _D, "max_concurrent": 200},
                   "short": {"win": 90,  "k1": 1.8, "b": 0.2, "cooldown_sec": 3 * _D, "max_concurrent": 200}},
        "USDCAD": {"long": {"win": 200, "k1": 2.3, "b": 1.8,  "cooldown_sec": 1 * _D, "max_concurrent": 200}},   # 역숏(15년 -71) 기각
        "USDCHF": {"long": {"win": 200, "k1": 2.3, "b": -0.6, "cooldown_sec": 1 * _D, "max_concurrent": 200}},   # 역숏 기각(fxd 드레인)
        "NZDUSD": {"long": {"win": 250, "k1": 2.9, "b": -3.0, "cooldown_sec": 1 * _D, "max_concurrent": 200},
                   "short": {"win": 150, "k1": 1.4, "b": -1.2, "cooldown_sec": 2 * _D, "max_concurrent": 200}},
        "BTCUSD": {"long": {"win": 200, "k1": 1.0, "b": -3.0, "cooldown_sec": 5 * _D, "max_concurrent": 200}},
        "ETHUSD": {"long": {"win": 200, "k1": 1.9, "b": -0.4, "cooldown_sec": 1 * _D, "max_concurrent": 200}},
        # ✅ 13.5y 재검증: XAGUSD 역롱(18년 -74)·XAUUSD 역롱(bad3)·WTI 역롱(2014 -176, 구제 실패) 기각.
        #   🟡 XAGUSD·WTI 역롱은 오픈 포지션 있어 k1=99 드레인(진입 불가·청산만) — ≤15d 소진 후 삭제.
        "XAGUSD": {"long": {"win": 90, "k1": 99.0, "b": -1.0, "cooldown_sec": 1 * _D, "max_concurrent": 200}},
        "XAUUSD": {"short": {"win": 200, "k1": 2.8, "b": 1.2,  "cooldown_sec": 1 * _D, "max_concurrent": 200}},
        "WTI":    {"long": {"win": 200, "k1": 99.0, "b": 0.8,  "cooldown_sec": 5 * _D, "max_concurrent": 200}},
        "US100":  {"long": {"win": 150, "k1": 2.0, "b": -3.0, "cooldown_sec": 1 * _D, "max_concurrent": 200}},
        "JP225":  {"long": {"win": 200, "k1": 2.0, "b": -3.0, "cooldown_sec": 1 * _D, "max_concurrent": 200}},
        "GER40":  {"long": {"win": 90, "k1": 2.6, "b": -3.0, "cooldown_sec": 1 * _D, "max_concurrent": 200}},  # 13.5y 구제(구 w200: 18년 -41)
        "UK100":  {"long": {"win": 90, "k1": 1.7, "b": -0.8, "cooldown_sec": 2 * _D, "max_concurrent": 200},
                   "short": {"win": 250, "k1": 2.3, "b": 1.0,  "cooldown_sec": 5 * _D, "max_concurrent": 200}},  # 13.5y 구제
        "HK50":   {"long": {"win": 200, "k1": 1.8, "b": -3.0, "cooldown_sec": 3 * _D, "max_concurrent": 200},
                   "short": {"win": 150, "k1": 2.2, "b": 2.0,  "cooldown_sec": 1 * _D, "max_concurrent": 200}},
    }
    return make_s1_config(name="mt5", params_by_symbol=MT5D_REV, strategy="s4",  # mt5 네임스페이스 공유(basic 퇴출로 충돌 없음), 태그 s4로 구분
                          avg_down=False, signal_only=signal_only,
                          s1_win=250, candle_interval="D", candles_num=350,
                          s1_max_hold_sec=15 * _D, **kw)


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
        strategy="none",            # 🔹 매매 안 함(basic 퇴출) — config publish/상태표시 전용. 매매는 s1/s2가 담당
        symbols=list(symbols),

        ws_stale_sec=30.0,
        ws_global_stale_sec=60.0,

        # 주문 관련 값은 의미 없으므로 안전하게 최소로
        leverage=50,
        entry_percent=entry_percent,
        entry_percent_by_symbol=entry_percent_by_symbol,
        # ✅ (전략,심볼)별 진입% — 2026-07-16 사이징 상향(최저증거금 25% 기준, universe_scale.py 스윕):
        #   U2(비환율) ×3.5: 책 1%→3.5%(0.07), 일봉 2%→7%(0.14), WTI 4h페이드 0.5%→1.75%(0.035)
        #   U3(환율7종) ×2.3: 책 1%→2.3%(0.046), 일봉 5%→11.5%(0.23)
        #   근거: U2 예상 낙폭 ~21%/증거금 ~25%, U3 낙폭 ~6.5%/증거금 ~25% (UNIVERSE_REPORT.md)
        #   ⚠️ 두 유니버스 합산 계좌라 동시 최악(2025Q2형) 노출 ~770% = 실효레버 7.7배 < max_eff 10
        #   (환산: 값 × 레버50 = notional% → 1%=0.02)
        entry_percent_by_strategy={
            **{s: {"_default": 0.04} for s in ("s1", "s2")},  # 1분봉(드레인 중) 2% 유지
            **{s: {"_default": 0.07,
                   "USDJPY": 0.046, "EURUSD": 0.046, "GBPUSD": 0.046, "AUDUSD": 0.046,
                   "USDCAD": 0.046, "USDCHF": 0.046, "NZDUSD": 0.046}
               for s in ("s11", "s12", "s14", "s15")},
            "s13": {"_default": 0.07, "WTI": 0.035,   # WTI 4h페이드=이벤트 몰빵 → 절반 규칙 유지
                    "USDJPY": 0.046, "EURUSD": 0.046, "GBPUSD": 0.046, "AUDUSD": 0.046,
                    "USDCAD": 0.046, "USDCHF": 0.046, "NZDUSD": 0.046},
            **{s: {"_default": 0.23,  # 일봉 FX 11.5%
                   "BTCUSD": 0.14, "ETHUSD": 0.14, "XAUUSD": 0.14, "XAGUSD": 0.14, "WTI": 0.14,
                   "US100": 0.14, "JP225": 0.14, "GER40": 0.14, "UK100": 0.14, "HK50": 0.14}
               for s in ("s3", "s4")},  # 일봉 비환율 7%
        },

        max_effective_leverage=10.0,

        # 인디케이터 관련
        indicator_min_thr=indicator_min_thr,
        indicator_max_thr=indicator_max_thr,
        target_cross=target_cross,

        candles_num=candles_num,

        min_ma_threshold=min_ma_threshold,
        signal_only=False,
    )
    return cfg.normalized()


# ─────────────────────────────────────────────────────────────────────────────
# S22 「4시간봉책」 — HANDOFF_S22 (2026-07-13). Bybit 라이브 v1 = 8셀, 진입 5%/자산.
#   namespace "s22" (S11/일봉과 open_signals 분리 필수). candle_interval="240"(4h, UTC 정렬).
#   패밀리: z추세(s11)/z역추세(s12)/급락페이드(s13)=기존 엔진 재사용, ewz추세(s14)=신규.
#   공통: 봉마감 진입(틱 근사), SL 없음(숏 포함 — 숏 손절 대체=시간청산), 보유≤15d.
#   워밍업 600봉(win240+EMA 안정화) → candles_num=700.
#   검증: 상장 전기간·연도균형·이웃격자 견고성. 계획기대=이웃 중앙값(보수치).
# ─────────────────────────────────────────────────────────────────────────────
_H4 = 4 * _H   # 4시간봉 1개(초)


def make_s22_trend_config(*, signal_only: bool = True, **kw) -> "TradeConfig":
    """S22 z추세롱 (Bybit 4h). #1 BTC K3.25/B-2.5 cd24h / #6 XRP K4.0/B-3.0 cd12h.
    TP=진입가 대비 밴드거리 미러(엔진 entry_high pct=1-(MA+Bσ)/price, B<0=오버슛), 고가터치≈틱."""
    S22_TREND = {
        "BTCUSDT": {"long": {"win": 240, "k1": 3.25, "b": -2.5, "cooldown_sec": 24 * _H, "max_concurrent": 200, "no_sl": True}},
        "XRPUSDT": {"long": {"win": 240, "k1": 4.0,  "b": -3.0, "cooldown_sec": 12 * _H, "max_concurrent": 200, "no_sl": True}},
    }
    return make_s1_config(name="s22", params_by_symbol=S22_TREND, strategy="s11",
                          avg_down=False, signal_only=signal_only,
                          s1_win=240, candle_interval="240", candles_num=700,
                          s1_max_hold_sec=15 * _D, **kw)


def make_s22_rev_config(*, signal_only: bool = True, **kw) -> "TradeConfig":
    """S22 z역추세롱 (Bybit 4h). #5 SOL K2.75/TP=MA+1.0σ(B=-1.0) cd24h / #7 XRP K3.0/TP=MA-1.5σ(B=+1.5) cd12h.
    엔진 entry_low pct=(MA-Bσ)/price-1 → TP=MA-Bσ. 추매 없음."""
    S22_REV = {
        "SOLUSDT": {"long": {"win": 240, "k1": 2.75, "b": -1.0, "cooldown_sec": 24 * _H, "max_concurrent": 200, "no_sl": True}},
        "XRPUSDT": {"long": {"win": 240, "k1": 3.0,  "b": 1.5,  "cooldown_sec": 12 * _H, "max_concurrent": 200, "no_sl": True}},
    }
    return make_s1_config(name="s22", params_by_symbol=S22_REV, strategy="s12",
                          avg_down=False, signal_only=signal_only,
                          s1_win=240, candle_interval="240", candles_num=700,
                          s1_max_hold_sec=15 * _D, **kw)


def make_s22_ewz_config(*, signal_only: bool = True, **kw) -> "TradeConfig":
    """S22 ewz추세 (Bybit 4h, 신규 패밀리 s14). resid=C-EMA_s, σ=EMA_s(|resid|), ez=resid/σ.
    #2 ETH롱 s200 k3.0 T90봉(15d) / #3 ETH숏 s50 k2.5 T18봉(3d, 22년 헤지) / #4 SOL롱 s20 k3.0 T60봉(10d).
    청산=시간청산 전용(hold_sec), 쿨다운 24h(진입기준 6봉)."""
    S22_EWZ = {
        "ETHUSDT": {"long":  {"ewz_s": 200, "k1": 3.0, "cooldown_sec": 24 * _H, "hold_sec": 90 * _H4, "max_concurrent": 200},
                    "short": {"ewz_s": 50,  "k1": 2.5, "cooldown_sec": 24 * _H, "hold_sec": 18 * _H4, "max_concurrent": 200}},
        "SOLUSDT": {"long":  {"ewz_s": 20,  "k1": 3.0, "cooldown_sec": 24 * _H, "hold_sec": 60 * _H4, "max_concurrent": 200}},
    }
    return make_s1_config(name="s22", params_by_symbol=S22_EWZ, strategy="s14",
                          avg_down=False, signal_only=signal_only,
                          s1_win=240, candle_interval="240", candles_num=700,
                          s1_max_hold_sec=15 * _D, **kw)


def make_s22_fade_config(*, signal_only: bool = True, **kw) -> "TradeConfig":
    """S22 급락페이드 (Bybit 4h). #8 XRP: 12봉(48h) 수익률 ≤-15% → TP=진입가×(1+1.5×|낙폭|), 캡 60봉(10d).
    ⚠️ m_min은 '봉 수'(엔진이 closes 인덱스로 사용) — 4h 채널에서 12=48h."""
    S22_FADE = {
        "XRPUSDT": {"long": {"m_min": 12, "drop_pct": 0.15, "retr_mult": 1.5, "hold_sec": 60 * _H4,
                             "cooldown_sec": 24 * _H, "max_concurrent": 200}},
    }
    return make_s1_config(name="s22", params_by_symbol=S22_FADE, strategy="s13",
                          avg_down=False, signal_only=signal_only,
                          s1_win=240, candle_interval="240", candles_num=700,
                          s1_max_hold_sec=10 * _D, **kw)

# ─────────────────────────────────────────────────────────────────────────────
# S22 확장판 「4시간봉책 MT5·FX」 — HANDOFF_S22 §6 (2026-07-15 확정, 13.5년 검증).
#   namespace "s22m", candle_interval="240", 유니버스: 비FX=U2 / FX심볼=U3(분류는 심볼 기반).
#   사이징: mt5 executor entry_percent_by_strategy s11~s15 = 0.02(=1%), WTI s13만 절반.
#   z셀 SL: 롱=無(no_sl) / 역추숏=미러 SL 유지(백테스트 동일). 계획기대=이웃 중앙값(§6).
# ─────────────────────────────────────────────────────────────────────────────
def make_s22m_trend_config(*, signal_only: bool = True, **kw) -> "TradeConfig":
    """S22m z추세롱: JP225 w120봉 K3.0 B1 / HK50 w180봉 K3.0 B-2 (⚠️HK50 2022 데이터 공백 검증)."""
    S22M_TREND = {
        "JP225": {"long": {"win": 120, "k1": 3.0, "b": 1.0,  "cooldown_sec": 12 * _H, "max_concurrent": 200, "no_sl": True}},
        "HK50":  {"long": {"win": 180, "k1": 3.0, "b": -2.0, "cooldown_sec": 12 * _H, "max_concurrent": 200, "no_sl": True}},
    }
    return make_s1_config(name="s22m", params_by_symbol=S22M_TREND, strategy="s11",
                          avg_down=False, signal_only=signal_only,
                          s1_win=240, candle_interval="240", candles_num=700,
                          s1_max_hold_sec=15 * _D, **kw)


def make_s22m_rev_config(*, signal_only: bool = True, **kw) -> "TradeConfig":
    """S22m z역추세: US100·HK50·USDJPY 롱(SL無) + AUDUSD·NZDUSD 숏(미러SL 유지)."""
    S22M_REV = {
        "US100":  {"long": {"win": 180, "k1": 3.0, "b": 1.0,  "cooldown_sec": 12 * _H, "max_concurrent": 200, "no_sl": True}},
        "HK50":   {"long": {"win": 180, "k1": 3.0, "b": 1.0,  "cooldown_sec": 12 * _H, "max_concurrent": 200, "no_sl": True}},
        "USDJPY": {"long": {"win": 120, "k1": 2.5, "b": 1.0,  "cooldown_sec": 12 * _H, "max_concurrent": 200, "no_sl": True}},
        "AUDUSD": {"short": {"win": 30, "k1": 3.0, "b": -2.0, "cooldown_sec": 12 * _H, "max_concurrent": 200}},
        "NZDUSD": {"short": {"win": 60, "k1": 3.0, "b": -1.0, "cooldown_sec": 24 * _H, "max_concurrent": 200}},
    }
    return make_s1_config(name="s22m", params_by_symbol=S22M_REV, strategy="s12",
                          avg_down=False, signal_only=signal_only,
                          s1_win=240, candle_interval="240", candles_num=700,
                          s1_max_hold_sec=15 * _D, **kw)


def make_s22m_ewz_config(*, signal_only: bool = True, **kw) -> "TradeConfig":
    """S22m ewz(XAGUSD): 역추롱 s200 ez≤-2.5 T30봉(ewz_rev) + 추세숏 s50 ez≤-3.0 T18봉."""
    S22M_EWZ = {
        "XAGUSD": {"long":  {"ewz_s": 200, "k1": 2.5, "ewz_rev": True, "cooldown_sec": 24 * _H,
                             "hold_sec": 30 * _H4, "max_concurrent": 200},
                   "short": {"ewz_s": 50,  "k1": 3.0, "cooldown_sec": 24 * _H,
                             "hold_sec": 18 * _H4, "max_concurrent": 200}},
    }
    return make_s1_config(name="s22m", params_by_symbol=S22M_EWZ, strategy="s14",
                          avg_down=False, signal_only=signal_only,
                          s1_win=240, candle_interval="240", candles_num=700,
                          s1_max_hold_sec=15 * _D, **kw)


def make_s22m_fade_config(*, signal_only: bool = True, **kw) -> "TradeConfig":
    """S22m 급락페이드 5셀. m_min=4h봉 수. US100만 되돌림×1.5, 나머지 시간청산. WTI=절반 사이징(executor)."""
    S22M_FADE = {
        "JP225":  {"long": {"m_min": 18, "drop_pct": 0.05, "hold_sec": 60 * _H4, "cooldown_sec": 24 * _H, "max_concurrent": 200}},
        "US100":  {"long": {"m_min": 30, "drop_pct": 0.07, "retr_mult": 1.5, "hold_sec": 30 * _H4, "cooldown_sec": 24 * _H, "max_concurrent": 200}},
        "UK100":  {"long": {"m_min": 12, "drop_pct": 0.03, "hold_sec": 60 * _H4, "cooldown_sec": 24 * _H, "max_concurrent": 200}},
        "XAUUSD": {"long": {"m_min": 6,  "drop_pct": 0.03, "hold_sec": 60 * _H4, "cooldown_sec": 24 * _H, "max_concurrent": 200}},
        "WTI":    {"long": {"m_min": 6,  "drop_pct": 0.07, "hold_sec": 60 * _H4, "cooldown_sec": 24 * _H, "max_concurrent": 200}},
    }
    return make_s1_config(name="s22m", params_by_symbol=S22M_FADE, strategy="s13",
                          avg_down=False, signal_only=signal_only,
                          s1_win=240, candle_interval="240", candles_num=700,
                          s1_max_hold_sec=10 * _D, **kw)


def make_s22m_sweep_config(*, signal_only: bool = True, **kw) -> "TradeConfig":
    """S22m 유동성스윕(s15 신규): 직전봉 저가<N120봉 저점 & 종가 복귀 → 롱, T60봉(10d). JP225·USDCAD."""
    S22M_SWEEP = {
        "JP225":  {"long": {"sweep_n": 120, "hold_sec": 60 * _H4, "cooldown_sec": 24 * _H, "max_concurrent": 200}},
        "USDCAD": {"long": {"sweep_n": 120, "hold_sec": 60 * _H4, "cooldown_sec": 24 * _H, "max_concurrent": 200}},
    }
    return make_s1_config(name="s22m", params_by_symbol=S22M_SWEEP, strategy="s15",
                          avg_down=False, signal_only=signal_only,
                          s1_win=240, candle_interval="240", candles_num=700,
                          s1_max_hold_sec=10 * _D, **kw)

