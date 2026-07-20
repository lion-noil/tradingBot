# ---------- 유니버스 (2026-07-15 확정): 텔레그램 '전체 N'의 계좌·자산군 단위 ----------
# 1=crypto(Bybit 크립토 전체) / 2=mt5(MT5 비환율 — 지수·금속·원유·크립토CFD) / 3=fx(환율 7종)
# ⚠️ 의존성 無 순수 모듈로 유지할 것 — utils.logger가 redis 없는 환경에서도 임포트한다.
FX_SYMBOLS = {"USDJPY", "EURUSD", "GBPUSD", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD"}
CRYPTO_NAMESPACES = {"s11", "s22", "bybit", "s1", "s2"}   # Bybit 쪽 네임스페이스
UNIVERSE_NAMESPACES = {
    "crypto": ["s11", "s22", "bybit"],          # 1분책+4h책+일봉(cryptod)+레거시 드레인(ns 공유)
    "mt5":    ["s11m", "s22m", "mt5"],          # 1분 확장+4h 확장(예정)+레거시·일봉(ns 공유)
    "fx":     ["fxd", "s11m", "s22m", "mt5"],   # 일봉 FX + (s11m·s22m·mt5 안의 FX 심볼들)
}


def universe_of(namespace: str, symbol: str) -> str:
    """(네임스페이스, 심볼) → 유니버스. ns가 계좌를 가르고, MT5 쪽은 심볼로 FX 분리."""
    ns = (namespace or "").strip().lower()
    if ns in CRYPTO_NAMESPACES:
        return "crypto"
    if (symbol or "").upper() in FX_SYMBOLS:
        return "fx"
    return "mt5"
