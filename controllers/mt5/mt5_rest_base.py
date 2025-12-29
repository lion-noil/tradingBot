# controllers/mt5/mt5_rest_base.py
import json
from typing import Any, Dict, Optional
from bots.trade_config import SecretsConfig
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
        system_logger=None
    ):
        self.system_logger = system_logger
        cfg_secret = SecretsConfig.from_env().require_mt5_trade()

        self.price_base_url = cfg_secret.mt5_price_rest_url
        self.trade_base_url = cfg_secret.mt5_trade_rest_url
        self.api_key = cfg_secret.mt5_trade_api_key
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

if __name__ == "__main__":
    from pprint import pprint
    import time

    print("\n[0] SecretsConfig MT5 env load test")
    try:
        sec = SecretsConfig.from_env()
        pprint({
            "enable_mt5": sec.enable_mt5,
            "mt5_price_rest_url": sec.mt5_price_rest_url,
            "mt5_trade_rest_url": sec.mt5_trade_rest_url,
            "mt5_trade_api_key_set": bool(sec.mt5_trade_api_key),
        })

        # 너 코드가 require_mt5_trade()를 쓰고 있으니 그대로 검증
        sec.require_mt5_trade()
        print("✅ require_mt5_trade OK")
    except Exception as e:
        print("❌ MT5 env/config load failed:", e)
        raise

    print("\n[1] Mt5RestBase init test")
    try:
        b = Mt5RestBase(system_logger=None)
        print("price_base_url:", b.price_base_url)
        print("trade_base_url:", b.trade_base_url)
        print("api_key_set:", bool(b.api_key))
    except Exception as e:
        print("❌ Mt5RestBase init failed:", e)
        raise

    print("\n[2] Simple GET test to price server")
    # 어떤 endpoint가 있는지 프로젝트마다 달라서, 아래는 '연결 확인용'으로만 구성함.
    # 흔한 health 엔드포인트 후보들을 순서대로 시도하고, 하나라도 성공하면 OK 처리.
    candidates = ["/health", "/ping", "/v1/health", "/api/health", "/time", "/v5/market/time"]

    ok = False
    last_err = None

    for ep in candidates:
        try:
            print(f" - trying GET {ep} (use=price)")
            out = b._request("GET", ep, use="price", timeout=5.0)
            print("   ✅ success:", type(out).__name__)
            # 너무 길면 일부만
            if isinstance(out, dict):
                pprint({k: out[k] for k in list(out.keys())[:10]})
            else:
                pprint(out)
            ok = True
            break
        except Exception as e:
            last_err = e
            print(f"   ❌ failed: {e}")

    if not ok:
        print("\n[WARN] No candidate endpoint succeeded.")
        print("This usually means: base URL is wrong OR server has different endpoints.")
        if last_err:
            print("Last error:", last_err)
        raise SystemExit(1)

    print("\nDONE ✅")

