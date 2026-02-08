# controllers/bybit/bybit_rest_trade.py
import math
import requests
import time
from urllib.parse import urlencode


class BybitRestTradeMixin:

    def fetch_symbol_rules(self, symbol: str, category: str = "linear") -> dict:
        url = f"{self.price_base_url}/v5/market/instruments-info"
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
        if rules["qtyStep"] <= 0:
            rules["qtyStep"] = 0.001
        if rules["minOrderQty"] <= 0:
            rules["minOrderQty"] = rules["qtyStep"]

        self._symbol_rules[symbol] = rules
        return rules

    def get_symbol_rules(self, symbol: str) -> dict:
        return self._symbol_rules.get(symbol) or self.fetch_symbol_rules(symbol)
    # -------------------------
    # 주문 생성/청산 래퍼
    # -------------------------
    def submit_market_order(self, symbol, order_side, qty, position_idx, reduce_only=False):
        """
        Market 주문 생성.
        반환: result dict (bybit 응답의 result)
        """
        endpoint = "/v5/order/create"
        body = {
            "category": "linear",
            "symbol": symbol,
            "side": order_side,          # "Buy" / "Sell"
            "orderType": "Market",
            "qty": str(qty),
            "positionIdx": position_idx, # 1=LONG, 2=SHORT
            "reduceOnly": bool(reduce_only),
            "timeInForce": "IOC",
        }
        resp = self._request_with_resync(
            "POST", endpoint, params_pairs=None, body_dict=body, timeout=5
        )
        if resp is None:
            return None

        if getattr(resp, "status_code", None) != 200:
            if getattr(self, "system_logger", None):
                self.system_logger.error(f"❌ HTTP 오류: {resp.status_code} {getattr(resp, 'text', '')}")
            return None

        data = resp.json()
        if data.get("retCode") == 0:
            return data.get("result", {}) or {}

        if getattr(self, "system_logger", None):
            self.system_logger.error(
                f"❌ 주문 실패: {data.get('retMsg')} (코드 {data.get('retCode')})"
            )
        return None

    def open_market(self, symbol, side, qty, **kwargs):
        qty = float(qty or 0.0)

        # 1. qty가 유효한지 체크 (이미 Executor에서 정규화 했겠지만 안전장치)
        if qty <= 0:
            if getattr(self, "system_logger", None):
                self.system_logger.error(f"❌ open_market 수량 오류: {qty}")
            return None

        # 2. Side 매핑
        if side.lower() == "long":
            order_side, position_idx = "Buy", 1
        elif side.lower() == "short":
            order_side, position_idx = "Sell", 2
        else:
            if getattr(self, "system_logger", None):
                self.system_logger.error(f"❌ 알 수 없는 side 값: {side}")
            return None

        # 3. 로그
        if getattr(self, "system_logger", None):
            self.system_logger.debug(
                f"📥 {side.upper()} 진입 주문 전송 | qty={qty} ({symbol})"
            )

        # 4. 주문 전송
        res = self.submit_market_order(symbol, order_side, qty, position_idx, reduce_only=False)

        if res and isinstance(res, dict):
            res["qty"] = float(qty)

        return res


    def close_market(self, symbol, side, qty, **kwargs):
        """
        보유 포지션 청산(시장가 reduceOnly).
        side: "LONG" / "SHORT"
        """
        qty = float(qty)
        qty = self.normalize_qty(symbol, qty, mode="floor")  # 청산은 floor가 안전
        if qty <= 0:
            if getattr(self, "system_logger", None):
                self.system_logger.warning("❗ 청산 수량이 최소단위 미만입니다. 중단.")
            return None

        if side.upper() == "LONG":
            order_side, position_idx = "Sell", 1
        elif side.upper() == "SHORT":
            order_side, position_idx = "Buy", 2
        else:
            if getattr(self, "system_logger", None):
                self.system_logger.error(f"❌ 알 수 없는 side 값: {side}")
            return None

        if getattr(self, "system_logger", None):
            self.system_logger.debug(
                f"📤 {side.upper()} 포지션 청산 시도 | qty={qty:.8f} ({symbol})"
            )

        # ✅ 수정: 바로 return 하지 않고 결과를 받아서 qty를 넣어줌
        res = self.submit_market_order(symbol, order_side, qty, position_idx, reduce_only=True)

        if res and isinstance(res, dict):
            res["qty"] = float(qty)  # <-- 핵심: 내가 요청한 수량을 결과에 명시

        return res

    # -------------------------
    # 주문 취소
    # -------------------------
    def cancel_order(self, symbol, order_id):
        import json as _json

        endpoint = "/v5/order/cancel"
        url = self.trade_base_url + endpoint
        method = "POST"
        payload = {"category": "linear", "symbol": symbol, "orderId": order_id}

        body = _json.dumps(payload, separators=(",", ":"), sort_keys=True)
        headers = self._get_headers(method, endpoint, body=body)
        headers["Content-Type"] = "application/json"

        r = requests.post(url, headers=headers, data=body, timeout=5)
        return r.json()

    # -------------------------
    # 수량 정규화
    # -------------------------
    def _round_step(self, value: float, step: float, mode: str = "floor") -> float:
        """
        step 단위로 라운딩. mode: floor/ceil/round
        """
        if step <= 0:
            return float(value)
        n = float(value) / step
        if mode == "ceil":
            n = math.ceil(n - 1e-12)
        elif mode == "round":
            n = round(n)
        else:
            n = math.floor(n + 1e-12)
        return float(f"{n * step:.8f}")

    def normalize_qty(self, symbol: str, qty: float, mode: str = "floor") -> float:
        """
        심볼 규칙(qtyStep/minOrderQty)에 맞춰 수량 정규화.
        """
        rules = self.get_symbol_rules(symbol)
        step = rules.get("qtyStep", 0.001) or 0.001
        min_qty = rules.get("minOrderQty", step) or step

        q = max(0.0, float(qty))
        q = self._round_step(q, step, mode=mode)
        if q < min_qty:
            return 0.0
        return q
