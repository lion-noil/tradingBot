# controllers/bybit/bybit_rest_base.py
import os
import time
import hmac
import hashlib
import json
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

load_dotenv()


class BybitRestBase:
    def __init__(self, system_logger=None, base_url: str | None = None):
        self.system_logger = system_logger
        self.base_url = base_url or "https://api-demo.bybit.com"
        self.api_key = os.getenv("BYBIT_TEST_API_KEY")
        self.api_secret = os.getenv("BYBIT_TEST_API_SECRET").encode()  # HMAC 서명용
        self.recv_window = "15000"
        self._time_offset_ms = 0

        self._symbol_rules: dict[str, dict] = {}

        # ⏱ 서버-로컬 시간 동기화
        self.sync_time()

    # -------------------------
    # Query builder
    # -------------------------
    def _build_query(self, params_pairs: list[tuple[str, str]] | None) -> str:
        if not params_pairs:
            return ""
        return urlencode(params_pairs, doseq=False)

    # -------------------------
    # 시간 동기화
    # -------------------------
    def sync_time(self):
        t0 = time.time()
        r = requests.get(f"{self.base_url}/v5/market/time", timeout=5)
        t1 = time.time()
        server_ms = int((r.json() or {}).get("time"))
        rtt_ms = (t1 - t0) * 1000.0
        local_est_ms = int(t1 * 1000 - rtt_ms / 2)
        self._time_offset_ms = server_ms - local_est_ms

    def _now_ms(self) -> str:
        # 미래 금지 마진으로 10ms 빼기
        return str(int(time.time() * 1000 + self._time_offset_ms - 10))

    # -------------------------
    # 서명/헤더
    # -------------------------
    def _generate_signature(self, timestamp: str, method: str, params: str = "", body: str = "") -> str:
        query_string = params if method == "GET" else body
        payload = f"{timestamp}{self.api_key}{self.recv_window}{query_string}"
        return hmac.new(self.api_secret, payload.encode(), hashlib.sha256).hexdigest()

    def _get_headers(self, method: str, endpoint: str, params: str = "", body: str = "") -> dict:
        timestamp = self._now_ms()  # ✅ 오프셋 반영 & 미래 방지
        sign = self._generate_signature(timestamp, method, params=params, body=body)
        return {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": self.recv_window,
            "X-BAPI-SIGN": sign,
        }

    # -------------------------
    # 공통 요청 with 재동기화
    # -------------------------
    def _request_with_resync(
        self,
        method: str,
        endpoint: str,
        params_pairs: list[tuple[str, str]] | None = None,
        body_dict: dict | None = None,
        timeout: float = 5.0,
    ):
        base = self.base_url + endpoint
        query_string = self._build_query(params_pairs)
        url = f"{base}?{query_string}" if query_string else base

        body_str = ""

        def _make_headers():
            nonlocal body_str
            if body_dict is not None:
                body_str = json.dumps(body_dict, separators=(",", ":"), sort_keys=True)
            else:
                body_str = ""
            return self._get_headers(method, endpoint, params=query_string, body=body_str)

        def _send():
            hdrs = _make_headers()
            if method == "GET":
                return requests.get(url, headers=hdrs, timeout=timeout)
            else:
                hdrs = {**hdrs, "Content-Type": "application/json"}
                return requests.post(url, headers=hdrs, data=body_str, timeout=timeout)

        # 1차 시도
        resp = _send()
        j = None
        try:
            j = resp.json()
        except Exception:
            # JSON이 아니면 그대로 리턴
            return resp

        # 타임스탬프/윈도우 오류 감지
        ret_code = j.get("retCode")
        ret_msg = (j.get("retMsg") or "").lower()
        needs_resync = (
            ret_code == 10002
            or "timestamp" in ret_msg
            or "recv_window" in ret_msg
            or "check your server timestamp" in ret_msg
        )

        if needs_resync:
            self.sync_time()
            resp = _send()

        return resp

    # -------------------------
    # 심볼 규칙
    # -------------------------
    def fetch_symbol_rules(self, symbol: str, category: str = "linear") -> dict:
        """
        v5/market/instruments-info에서 lotSizeFilter/priceFilter를 읽어 규칙 반환.
        네트워크/응답 이슈시 예외를 올림.
        """
        url = f"{self.base_url}/v5/market/instruments-info"
        params = {"category": category, "symbol": symbol}
        r = requests.get(url, params=params, timeout=5)
        r.raise_for_status()
        j = r.json()
        if j.get("retCode") != 0:
            raise RuntimeError(f"retCode={j.get('retCode')}, retMsg={j.get('retMsg')}")
        lst = (j.get("result") or {}).get("list") or []
        if not lst:
            raise RuntimeError("empty instruments list")
        info = lst[0]
        lot = info.get("lotSizeFilter", {}) or {}
        price = info.get("priceFilter", {}) or {}

        rules = {
            "qtyStep": float(lot.get("qtyStep", 0) or 0),
            "minOrderQty": float(lot.get("minOrderQty", 0) or 0),
            "maxOrderQty": float(lot.get("maxOrderQty", 0) or 0),
            "tickSize": float(price.get("tickSize", 0) or 0),
            "minPrice": float(price.get("minPrice", 0) or 0),
            "maxPrice": float(price.get("maxPrice", 0) or 0),
        }
        # 방어: 기본값 보정
        if rules["qtyStep"] <= 0:
            rules["qtyStep"] = 0.001  # 안전 폴백
        if rules["minOrderQty"] <= 0:
            rules["minOrderQty"] = rules["qtyStep"]

        self._symbol_rules[symbol] = rules
        return rules

    def get_symbol_rules(self, symbol: str) -> dict:
        return self._symbol_rules.get(symbol) or self.fetch_symbol_rules(symbol)
