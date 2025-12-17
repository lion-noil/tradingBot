# controllers/bybit/bybit_rest_base.py
import time
import hmac
import hashlib
import json
from urllib.parse import urlencode

import requests
from app.config import (
    BYBIT_TRADE_REST_URL,
    BYBIT_TRADE_API_KEY,
    BYBIT_TRADE_API_SECRET,
    BYBIT_PRICE_REST_URL,
)


class BybitRestBase:
    def __init__(self, system_logger=None, base_url: str | None = None):
        self.system_logger = system_logger

        # ✅ 역할 분리
        self.trade_base_url = (base_url or BYBIT_TRADE_REST_URL).rstrip("/")
        self.price_base_url = (BYBIT_PRICE_REST_URL or "").rstrip("/")

        # ✅ 기존 코드 호환: base_url = trade
        self.base_url = self.trade_base_url

        # ✅ 키/시크릿 (trade only)
        self.api_key = BYBIT_TRADE_API_KEY
        if not BYBIT_TRADE_API_SECRET:
            raise RuntimeError("BYBIT_TRADE_API_SECRET is missing")
        self.api_secret = (
            BYBIT_TRADE_API_SECRET.encode()
            if BYBIT_TRADE_API_SECRET
            else None
        )

        if not self.api_key:
            raise RuntimeError("BYBIT_TRADE_API_KEY is missing")
        if not self.trade_base_url:
            raise RuntimeError("BYBIT_TRADE_REST_URL is missing")
        if not self.price_base_url:
            raise RuntimeError("BYBIT_PRICE_REST_URL is missing")

        self.recv_window = "15000"
        self._time_offset_ms = 0
        self._symbol_rules: dict[str, dict] = {}

        # ⏱ 서명 검증은 trade 서버가 하므로 trade 기준으로 동기화
        self.sync_time()

    # -------------------------
    # Query builder
    # -------------------------
    def _build_query(self, params_pairs: list[tuple[str, str]] | None) -> str:
        if not params_pairs:
            return ""
        return urlencode(params_pairs, doseq=False)

    # -------------------------
    # 시간 동기화 (trade 서버 기준)
    # -------------------------
    def sync_time(self):
        t0 = time.time()
        r = requests.get(f"{self.trade_base_url}/v5/market/time", timeout=5)
        t1 = time.time()
        server_ms = int((r.json() or {}).get("time"))
        rtt_ms = (t1 - t0) * 1000.0
        local_est_ms = int(t1 * 1000 - rtt_ms / 2)
        self._time_offset_ms = server_ms - local_est_ms

    def _now_ms(self) -> str:
        return str(int(time.time() * 1000 + self._time_offset_ms - 10))

    # -------------------------
    # 서명/헤더 (trade only)
    # -------------------------
    def _generate_signature(self, timestamp: str, method: str, params: str = "", body: str = "") -> str:
        query_string = params if method == "GET" else body
        payload = f"{timestamp}{self.api_key}{self.recv_window}{query_string}"
        if not self.api_secret:
            raise RuntimeError("Trade API secret is not configured")
        return hmac.new(self.api_secret, payload.encode(), hashlib.sha256).hexdigest()

    def _get_headers(self, method: str, endpoint: str, params: str = "", body: str = "") -> dict:
        timestamp = self._now_ms()
        sign = self._generate_signature(timestamp, method, params=params, body=body)
        return {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": self.recv_window,
            "X-BAPI-SIGN": sign,
        }

    # -------------------------
    # 공통 요청 with 재동기화 (trade only)
    # -------------------------
    def _request_with_resync(
        self,
        method: str,
        endpoint: str,
        params_pairs: list[tuple[str, str]] | None = None,
        body_dict: dict | None = None,
        timeout: float = 5.0,
    ):
        base = self.trade_base_url + endpoint
        query_string = self._build_query(params_pairs)
        url = f"{base}?{query_string}" if query_string else base

        body_str = ""

        def _make_headers():
            nonlocal body_str
            body_str = json.dumps(body_dict, separators=(",", ":"), sort_keys=True) if body_dict is not None else ""
            return self._get_headers(method, endpoint, params=query_string, body=body_str)

        def _send():
            hdrs = _make_headers()
            if method == "GET":
                return requests.get(url, headers=hdrs, timeout=timeout)
            hdrs = {**hdrs, "Content-Type": "application/json"}
            return requests.post(url, headers=hdrs, data=body_str, timeout=timeout)

        resp = _send()

        try:
            j = resp.json()
        except Exception:
            return resp

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

