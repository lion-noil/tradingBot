# controllers/mt5/mt5_rest_controller.py

from __future__ import annotations

from app.config import MT5_PRICE_REST_URL

from .mt5_rest_base import Mt5RestBase
from .mt5_rest_market import Mt5RestMarketMixin

# ✅ 터미널 기반 기능들(REST 아님)
from .mt5_rest_account import Mt5RestAccountMixin
from .mt5_rest_orders import Mt5RestOrdersMixin
from .mt5_rest_trade import Mt5RestTradeMixin


class Mt5RestController(
    Mt5RestBase,
    Mt5RestMarketMixin,    # ✅ price REST 사용 (use="price")
    Mt5RestAccountMixin,   # ✅ MT5 터미널 API
    Mt5RestOrdersMixin,    # ✅ 로컬 파일 + (필요시) 히스토리 동기화
    Mt5RestTradeMixin,     # ✅ MT5 터미널 API로 주문
):
    """
    MT5 최종 컨트롤러 (가격=REST, 거래=터미널)

    - Mt5RestBase: price_base_url만 필수로 유지
    - Mt5RestMarketMixin: 캔들/시세는 REST(ONLINE)로 호출 (_request(use="price"))
    - Mt5RestAccount/Orders/Trade: MT5 터미널 API로 처리 (MetaTrader5)
    """

    def __init__(self, system_logger=None, price_base_url: str | None = None):
        super().__init__(
            system_logger=system_logger,
            price_base_url=(price_base_url or MT5_PRICE_REST_URL),
            # trade_base_url / api_key는 이제 옵션이므로 여기서 안 넣어도 됨
            base_url=(price_base_url or MT5_PRICE_REST_URL),  # 호환용 base_url도 price로
        )

        # 트레이딩 공통 설정
        self.leverage = 50
        self.TAKER_FEE_RATE = 0.00055  # 0.055%