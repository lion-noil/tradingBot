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
    def __init__(self, system_logger=None):
        super().__init__(system_logger=system_logger)

        # 트레이딩 공통 설정
        self.leverage = 50
        self.TAKER_FEE_RATE = 0.00055  # 0.055%