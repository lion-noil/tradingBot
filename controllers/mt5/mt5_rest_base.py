# controllers/mt5/mt5_rest_base.py
import json
from typing import Any, Dict, Optional

import requests

from app.config import (
    MT5_PRICE_REST_URL,
    MT5_TRADE_REST_URL,
    MT5_TRADE_API_KEY,
)


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
        price_base_url: str | None = None,
        trade_base_url: str | None = None,
        base_url: str | None = None,
    ):
        self.system_logger = system_logger

        # ✅ PRICE는 필수
        self.price_base_url = (price_base_url or MT5_PRICE_REST_URL or "").strip()
        if not self.price_base_url:
            raise RuntimeError("MT5_PRICE_REST_URL is missing (.env)")

        # ✅ TRADE REST는 옵션 (없어도 OK)
        self.trade_base_url = (trade_base_url or MT5_TRADE_REST_URL or "").strip()

        # ✅ API KEY도 trade REST에서만 필요 (없어도 OK)
        self.api_key = (MT5_TRADE_API_KEY or "").strip()

        # ✅ 호환용 base_url: 기본은 price로 (캔들이 더 자주 호출되고 실수 방지)
        #    만약 기존 코드가 base_url로 'trade'를 호출하는 케이스가 남아있으면,
        #    해당 호출부를 use="trade"로 바꾸는 게 정답.
        self.base_url = (base_url or self.price_base_url).strip()

        if self.system_logger:
            self.system_logger.info(
                f"[MT5 REST] price={self.price_base_url} trade={self.trade_base_url or '(disabled)'} base={self.base_url}"
            )

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
