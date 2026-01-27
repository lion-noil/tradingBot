# bots/market/bootstrap.py
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional


def bootstrap_trading_state_for_symbol(
        rest_client,
        symbol: str,
        leverage: int,
        asset: Dict[str, Any],
        *,
        save_asset: Optional[Callable[[Dict[str, Any], Optional[str]], None]] = None,
        system_logger=None,
) -> Dict[str, Any]:
    """
    지갑/포지션, 레버리지, 주문 동기화만 담당.
    - 실제 주문 모드에서만 필요.
    """
    # 지갑/포지션 동기화
    try:
        asset = rest_client.build_asset(asset=asset, symbol=symbol)
    except Exception as e:
        if system_logger:
            system_logger.warning(f"[{symbol}] 자산/포지션 동기화 실패: {e}")
        return asset  # ✅ build_asset 실패면 저장/레버리지/주문 sync 의미가 애매하니 여기서 종료

    # ✅ 저장 (redis 등) - executor로 옮긴 정책과 일치
    if callable(save_asset):
        try:
            save_asset(asset, symbol)
        except Exception as e:
            if system_logger:
                system_logger.warning(f"[{symbol}] 자산 저장 실패: {e}")

    # 레버리지 설정 (MT5 환경에서는 no-op일 수도 있음)
    try:
        rest_client.set_leverage(symbol=symbol, leverage=leverage)
    except Exception:
        # set_leverage 미구현 / 불필요한 환경이면 조용히 무시
        pass

    # 주문 동기화 (Bybit 전용일 수 있으므로 방어적으로 호출)
    try:
        sync_orders = getattr(rest_client, "sync_orders_from_bybit", None)
        if callable(sync_orders):
            sync_orders(symbol)
    except Exception as e:
        if system_logger:
            system_logger.warning(f"[{symbol}] 초기 주문 동기화 실패: {e}")

    return asset


def bootstrap_candles_for_symbol(
        rest_client,
        candle_engine,
        refresh_indicators: Callable[[str], None],
        symbol: str,
        candles_num: int,
        system_logger=None,
) -> None:
    """
    과거 캔들 백필 + 인디케이터(MA100 등) 갱신만 담당.
    - 시그널 전용 모드에서도 반드시 필요.
    """
    try:
        rest_client.update_candles(
            candle_engine.get_candles(symbol),
            symbol=symbol,
            count=candles_num,
        )
        refresh_indicators(symbol)
    except Exception as e:
        if system_logger:
            system_logger.warning(f"[{symbol}] 초기 캔들/인디케이터 부트스트랩 실패: {e}")


def bootstrap_all_symbols(
        rest_client,
        candle_engine,
        refresh_indicators: Callable[[str], None],
        symbols: List[str],
        leverage: int,
        asset: Dict[str, Any],
        candles_num: int,
        *,
        save_asset: Optional[Callable[[Dict[str, Any], Optional[str]], None]] = None,
        system_logger=None,
) -> Dict[str, Any]:
    """
    모든 심볼에 대해:
    - 트레이딩 상태(지갑/포지션/레버리지/주문) 부트스트랩
    - 캔들 + 인디케이터 부트스트랩
    을 모두 수행.
    (실제 주문 모드에서 사용)
    """
    for sym in symbols:
        asset = bootstrap_trading_state_for_symbol(
            rest_client=rest_client,
            symbol=sym,
            leverage=leverage,
            asset=asset,
            save_asset=save_asset,  # ✅ 추가
            system_logger=system_logger,
        )
        bootstrap_candles_for_symbol(
            rest_client=rest_client,
            candle_engine=candle_engine,
            refresh_indicators=refresh_indicators,
            symbol=sym,
            candles_num=candles_num,
            system_logger=system_logger,
        )
    return asset
