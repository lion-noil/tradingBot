# controllers/bybit/bybit_rest_controller.py
from .bybit_rest_base import BybitRestBase
from .bybit_rest_orders import BybitRestOrdersMixin
from .bybit_rest_account import BybitRestAccountMixin
from .bybit_rest_market import BybitRestMarketMixin
from controllers.bybit.bybit_rest_trade import BybitRestTradeMixin


class BybitRestController(
    BybitRestBase,
    BybitRestOrdersMixin,
    BybitRestAccountMixin,
    BybitRestMarketMixin,
    BybitRestTradeMixin,
):
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