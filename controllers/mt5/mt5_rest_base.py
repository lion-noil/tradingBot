# controllers/mt5/mt5_rest_base.py
import os
import json
from typing import Any, Dict, Optional

import requests
from dotenv import load_dotenv

load_dotenv()


class Mt5RestBase:
    """
    MT5용 REST Base
    - BybitRestBase 와 비슷한 역할이지만,
      MT5 서버는 HMAC 서명/타임싱크 대신 단순 API KEY 헤더만 사용한다고 가정.
    """

    def __init__(self, system_logger=None, base_url: str | None = None):
        self.system_logger = system_logger
        # 기본 베이스 URL: 환경변수 없으면 api.hyeongeonnoil.com 사용
        self.base_url = base_url or os.getenv("MT5_API_BASE_URL", "https://api.hyeongeonnoil.com")
        self.api_key = os.getenv("MT5_API_KEY")

        if self.system_logger:
            self.system_logger.debug(
                f"[MT5 REST] init base_url={self.base_url}, api_key={'set' if self.api_key else 'none'}"
            )

    # -------------------------
    # URL / 헤더 빌더
    # -------------------------
    def _build_url(self, endpoint: str) -> str:
        """
        endpoint 예: "/v5/market/candles/with-gaps"
        """
        return self.base_url.rstrip("/") + endpoint

    def _get_headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
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
    ) -> Dict[str, Any]:
        """
        - method: "GET" / "POST"
        - endpoint: "/v5/market/candles/with-gaps" 같은 path
        - params: 쿼리스트링 dict
        - body_dict: JSON body dict
        - return: resp.json() (dict)
        """
        url = self._build_url(endpoint)

        try:
            if method.upper() == "GET":
                resp = requests.get(
                    url,
                    headers=self._get_headers(),
                    params=params,
                    timeout=timeout,
                )
            else:
                # POST 등
                body = json.dumps(body_dict or {}, separators=(",", ":"))
                resp = requests.post(
                    url,
                    headers=self._get_headers(),
                    params=params,
                    data=body,
                    timeout=timeout,
                )
        except requests.RequestException as e:
            if self.system_logger:
                self.system_logger.error(f"[MT5 REST] 네트워크 예외: {e}")
            raise

        if resp.status_code != 200:
            if self.system_logger:
                self.system_logger.error(
                    f"[MT5 REST] HTTP {resp.status_code} {resp.text[:200]}"
                )
            resp.raise_for_status()

        try:
            data = resp.json()
        except Exception:
            if self.system_logger:
                self.system_logger.error(f"[MT5 REST] JSON 파싱 실패: {resp.text[:200]}")
            raise

        return data
