# bots/market/bootstrap.py
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional


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
