# controllers/mt5/mt5_rest_controller.py

from __future__ import annotations


import MetaTrader5 as mt5

from controllers.mt5.mt5_rest_base import Mt5RestBase
from controllers.mt5.mt5_rest_market import Mt5RestMarketMixin

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

    def __init__(
            self,
            system_logger=None,
            *,
            trade_base_url: str | None = None,
            price_base_url: str | None = None,
            api_key: str | None = None,
            api_secret: str | None = None,
            leverage: int = 50,
            taker_fee_rate: float = 0.00055,
    ):
        super().__init__(
            system_logger=system_logger,
            trade_base_url=trade_base_url,
            price_base_url=price_base_url,
            api_key=api_key,
            api_secret=api_secret,
        )
        self.leverage = int(leverage)
        self.TAKER_FEE_RATE = float(taker_fee_rate)