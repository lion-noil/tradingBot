# controllers/mt5/mt5_rest_controller.py

from __future__ import annotations

from pprint import pprint
from datetime import datetime, timedelta
import time


import MetaTrader5 as mt5

from controllers.mt5.mt5_rest_base import Mt5RestBase
from controllers.mt5.mt5_rest_market import Mt5RestMarketMixin

# ✅ 터미널 기반 기능들(REST 아님)
from controllers.mt5.mt5_rest_account import Mt5RestAccountMixin
from controllers.mt5.mt5_rest_trade import Mt5RestTradeMixin


class Mt5RestController(
    Mt5RestBase,
    Mt5RestMarketMixin,    # ✅ price REST 사용 (use="price")
    Mt5RestAccountMixin,   # ✅ MT5 터미널 API
    Mt5RestTradeMixin,     # ✅ MT5 터미널 API로 주문
):
    """
    MT5 최종 컨트롤러 (가격=REST, 거래=터미널)

    - Mt5RestBase: price_base_url만 필수로 유지
    - Mt5RestMarketMixin: 캔들/시세는 REST(ONLINE)로 호출 (_request(use="price"))
    - Mt5RestAccount/Orders/Trade: MT5 터미널 API로 처리 (MetaTrader5)
    """

    def __init__(self, system_logger=None):
        super().__init__(
            system_logger=system_logger,
        )

        # 트레이딩 공통 설정
        self.leverage = 50
        self.TAKER_FEE_RATE = 0.00055  # 0.055%


def _pos_snapshot(symbol: str):
    sym = symbol.upper()
    poss = mt5.positions_get(symbol=sym) or []
    rows = []
    for p in poss:
        rows.append({
            "ticket": int(getattr(p, "ticket", 0) or 0),
            "type": int(getattr(p, "type", -1)),
            "volume": float(getattr(p, "volume", 0.0) or 0.0),
            "price_open": float(getattr(p, "price_open", 0.0) or 0.0),
        })
    return rows


