# controllers/bybit/bybit_rest_controller.py
from .bybit_rest_base import BybitRestBase
from .bybit_rest_orders import BybitRestOrdersMixin
from .bybit_rest_account import BybitRestAccountMixin
from .bybit_rest_market import BybitRestMarketMixin


class BybitRestController(
    BybitRestBase,
    BybitRestOrdersMixin,
    BybitRestAccountMixin,
    BybitRestMarketMixin,
):
    def __init__(self, system_logger=None):
        super().__init__(system_logger=system_logger)
        # 트레이딩 공통 설정
        self.leverage = 50
        self.FEE_RATE = 0.00055  # 0.055%
