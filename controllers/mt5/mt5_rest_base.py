# controllers/mt5/mt5_rest_base.py
import json
from typing import Any, Dict, Optional
import requests


class Mt5RestBase:
    """
    MT5 REST Base (Bybit 스타일 유지)

    - price_base_url: ONLINE (시세/캔들)  ✅ 필수
    - trade_base_url: LOCAL  (주문/계좌 REST)  ✅ 옵션 (지금은 터미널 API로 거래하니 없어도 됨)
    - base_url: 기존 코드 호환용 (기본은 price로 두는 걸 추천)
    """

    def __init__(
            self,
            system_logger=None,
            *,
            trade_base_url: str | None = None,
            price_base_url: str | None = None,
            api_key: str | None = None,
            api_secret: str | None = None,
    ):
        self.system_logger = system_logger
        self.price_base_url = price_base_url
        self.trade_base_url = trade_base_url
        self.api_key = api_key
        self._symbol_rules: dict[str, dict] = {}


    # -------------------------
    # URL / 헤더 빌더
    # -------------------------
    def _build_url(self, endpoint: str, *, use: str = "price") -> str:
        """
        use: "price" | "trade"
        """
        if use not in ("trade", "price"):
            raise ValueError("use must be 'trade' or 'price'")

        if use == "price":
            base = self.price_base_url
        else:
            if not self.trade_base_url:
                raise RuntimeError("MT5_TRADE_REST_URL is not configured (trade REST disabled)")
            base = self.trade_base_url

        return base.rstrip("/") + endpoint

    def _get_headers(self, *, use: str = "price") -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        # ✅ 키는 trade에만 (있을 때만) 붙임
        if use == "trade" and self.api_key:
            headers["X-API-KEY"] = self.api_key
        return headers

    # -------------------------
    # 공통 요청
    # -------------------------
    def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        body_dict: Optional[Dict[str, Any]] = None,
        timeout: float = 5.0,
        *,
        use: str = "price",
    ) -> Dict[str, Any]:
        """
        use="price" : 시세/캔들(ONLINE)
        use="trade" : 주문/계좌(LOCAL REST)  (지금은 거의 안 쓸 예정)
        """
        url = self._build_url(endpoint, use=use)

        try:
            if method.upper() == "GET":
                resp = requests.get(
                    url,
                    headers=self._get_headers(use=use),
                    params=params,
                    timeout=timeout,
                )
            else:
                body = json.dumps(body_dict or {}, separators=(",", ":"))
                resp = requests.post(
                    url,
                    headers=self._get_headers(use=use),
                    params=params,
                    data=body,
                    timeout=timeout,
                )
        except requests.RequestException as e:
            if self.system_logger:
                self.system_logger.error(f"[MT5 REST] 네트워크 예외(use={use}): {e}")
            raise

        if resp.status_code != 200:
            if self.system_logger:
                self.system_logger.error(
                    f"[MT5 REST] HTTP {resp.status_code} use={use} {resp.text[:200]}"
                )
            resp.raise_for_status()

        try:
            return resp.json()
        except Exception:
            if self.system_logger:
                self.system_logger.error(
                    f"[MT5 REST] JSON 파싱 실패(use={use}): {resp.text[:200]}"
                )
            raise