# controllers/bybit/bybit_rest_trade.py
import math
import requests


class BybitRestTradeMixin:
    """
    주문 생성/청산/취소 + 수량 정규화 기능.
    (기존 bybit_rest_market.py에 섞여 있던 trade 관련 로직을 분리)

    요구사항:
    - self._request_with_resync(method, endpoint, params_pairs=None, body_dict=None, timeout=5)
    - self._get_headers(method, endpoint, params=None, body="")
    - self.trade_base_url (거래용 base url)
    - self.get_symbol_rules(symbol)  # 심볼 룰 조회 (qtyStep/minOrderQty 등)
    - (선택) self.system_logger
    - (선택) self.leverage
    """

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

    def open_market(self, symbol, side, price, percent, wallet):
        """
        wallet(USDT) + percent 기반으로 qty 계산해서 시장가 진입.
        side: "long" / "short"
        """
        if price is None or wallet is None:
            if getattr(self, "system_logger", None):
                self.system_logger.error("❌ 가격 또는 잔고 정보가 누락되었습니다.")
            return None

        total_balance = wallet.get("USDT", 0)
        leverage = getattr(self, "leverage", 1)

        raw_qty = total_balance * leverage / price * percent / 100.0
        qty = self.normalize_qty(symbol, raw_qty, mode="floor")
        if qty <= 0:
            if getattr(self, "system_logger", None):
                self.system_logger.error(
                    f"❗ 주문 수량이 최소단위 미만입니다. raw={raw_qty:.8f}, norm={qty:.8f} ({symbol})"
                )
            return None

        if side.lower() == "long":
            order_side, position_idx = "Buy", 1
        elif side.lower() == "short":
            order_side, position_idx = "Sell", 2
        else:
            if getattr(self, "system_logger", None):
                self.system_logger.error(f"❌ 알 수 없는 side 값: {side}")
            return None

        if getattr(self, "system_logger", None):
            self.system_logger.debug(
                f"📥 {side.upper()} 진입 시도 | raw_qty={raw_qty:.8f} → qty={qty:.8f} @ {price:.4f} ({symbol})"
            )

        return self.submit_market_order(symbol, order_side, qty, position_idx, reduce_only=False)

    def close_market(self, symbol, side, qty):
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

        return self.submit_market_order(symbol, order_side, qty, position_idx, reduce_only=True)

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
