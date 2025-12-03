# controllers/mt5/mt5_rest_controller.py
from .mt5_rest_base import Mt5RestBase
from .mt5_rest_market import Mt5RestMarketMixin


class Mt5RestController(
    Mt5RestBase,
    Mt5RestMarketMixin,
):
    """
    BybitRestController 와 같은 역할의 최종 MT5 REST 컨트롤러.

    - Mt5RestBase: 공통 HTTP 요청/헤더/베이스 URL
    - Mt5RestMarketMixin: 캔들/시장 관련 기능 (현재는 update_candles)
    - 나중에 자산/포지션/주문 관련 기능이 생기면
      Mt5RestAccountMixin, Mt5RestOrdersMixin 같은 걸 추가로 만들어
      여기 상속 목록에 붙이면 됨.
    """

    def __init__(self, system_logger=None, base_url: str | None = None):
        super().__init__(system_logger=system_logger, base_url=base_url)

        # 추후 공통 트레이딩 설정(리스크, 레버리지 등)을 여기에 둘 수 있음
        # 예: self.default_risk_percent = 1.0


if __name__ == "__main__":
    import logging
    import time

    # 간단 로거 설정
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    logger = logging.getLogger("mt5_rest_test")

    # 이 파일 안에 정의된 Mt5RestController 사용
    rest = Mt5RestController(system_logger=logger)

    candles: list[dict] = []

    try:
        while True:
            # US100 1분봉 최신 100개 가져오기
            rest.update_candles(candles, symbol="US100", count=100)

            if candles:
                last = candles[-1]
                logger.info(
                    f"US100 candles: total={len(candles)} | "
                    f"last start={last['start']} "
                    f"OHLC=({last['open']}, {last['high']}, {last['low']}, {last['close']}) "
                    f"vol={last['volume']}"
                )
            else:
                logger.info("US100 candles: 결과 없음")

            time.sleep(5)

    except KeyboardInterrupt:
        logger.info("MT5 REST 테스트 종료 (Ctrl+C)")

