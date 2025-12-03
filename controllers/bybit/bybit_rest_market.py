# controllers/bybit/bybit_rest_market.py
import math
import time
from datetime import timezone, timedelta

import requests

KST = timezone(timedelta(hours=9))


def _safe_int(x):
    try:
        return int(x)
    except Exception:
        return int(float(x))


class BybitRestMarketMixin:
    # -------------------------
    # 포지션 조회
    # -------------------------
    def get_positions(self, symbol=None, category="linear"):
        endpoint = "/v5/position/list"
        params_pairs = [("category", category), ("symbol", symbol)]
        resp = self._request_with_resync(
            "GET", endpoint, params_pairs=params_pairs, body_dict=None, timeout=5
        )
        return resp.json()

    # -------------------------
    # 캔들 업데이트
    # -------------------------
    def update_candles(self, candles, symbol=None, count=None):
        try:
            symbol = symbol
            url = f"{self.base_url}/v5/market/kline"

            target = count if (isinstance(count, int) and count > 0) else 1000
            all_candles = []
            latest_end = None  # ms

            while len(all_candles) < target:
                req_limit = min(1000, target - len(all_candles))
                params = {
                    "category": "linear",
                    "symbol": symbol,
                    "interval": "1",
                    "limit": req_limit,
                }
                if latest_end is not None:
                    params["end"] = latest_end

                res = requests.get(url, params=params, timeout=10)
                res.raise_for_status()

                data = res.json()
                if not isinstance(data, dict):
                    raise RuntimeError(f"unexpected JSON root: {type(data).__name__}")

                ret_code = data.get("retCode", 0)
                if ret_code != 0:
                    ret_msg = data.get("retMsg")
                    raise RuntimeError(
                        f"bybit error retCode={ret_code}, retMsg={ret_msg}"
                    )

                result = data.get("result", {})
                if isinstance(result, dict):
                    raw_list = result.get("list") or []
                elif isinstance(result, list):
                    raw_list = result
                else:
                    raise RuntimeError(
                        f"unexpected 'result' type: {type(result).__name__}"
                    )

                if not isinstance(raw_list, list):
                    raise RuntimeError(f"'list' is {type(raw_list).__name__}, not list")

                if not raw_list:
                    break

                raw_list = raw_list[::-1]

                chunk = []
                for c in raw_list:
                    try:
                        if not isinstance(c, (list, tuple)) or len(c) < 5:
                            continue
                        item = {
                            "start": _safe_int(c[0]),
                            "open": float(c[1]),
                            "high": float(c[2]),
                            "low": float(c[3]),
                            "close": float(c[4]),
                        }
                        # volume/turnover 필요하면 여기 추가
                        chunk.append(item)
                    except Exception:
                        continue

                if chunk:
                    all_candles = chunk + all_candles
                    latest_end = _safe_int(raw_list[0][0]) - 1
                else:
                    break

                if len(raw_list) < req_limit:
                    break

            if isinstance(count, int) and count > 0:
                all_candles = all_candles[-count:]

            candles.clear()
            candles.extend(all_candles)

            last = candles[-1] if candles else None
            if getattr(self, "system_logger", None):
                if last:
                    self.system_logger.debug(
                        f"📊 ({symbol}) 캔들 갱신 완료: {len(candles)}개, "
                        f"last OHLC=({last['open']}, {last['high']}, {last['low']}, {last['close']})"
                    )
                else:
                    self.system_logger.debug(f"📊 ({symbol}) 캔들 갱신: 결과 없음")

        except Exception as e:
            if getattr(self, "system_logger", None):
                self.system_logger.warning(f"❌ ({symbol}) 캔들 요청 실패: {e}")

    # -------------------------
    # 레버리지 설정
    # -------------------------
    def set_leverage(self, symbol="BTCUSDT", leverage=10, category="linear"):
        try:
            endpoint = "/v5/position/set-leverage"
            url = self.base_url + endpoint
            method = "POST"

            payload = {
                "category": category,
                "symbol": symbol,
                "buyLeverage": str(leverage),
                "sellLeverage": str(leverage),
            }

            import json as _json

            body = _json.dumps(payload, separators=(",", ":"), sort_keys=True)
            headers = self._get_headers(method, endpoint, body=body)

            response = requests.post(url, headers=headers, data=body)

            if response.status_code == 200:
                data = response.json()
                ret_code = data.get("retCode")
                if ret_code == 0:
                    if getattr(self, "system_logger", None):
                        self.system_logger.debug(
                            f"✅ 레버리지 {leverage}x 설정 완료 | 심볼: {symbol}"
                        )
                    return True
                elif ret_code == 110043:
                    if getattr(self, "system_logger", None):
                        self.system_logger.debug(
                            f"⚠️ 이미 설정된 레버리지입니다: {leverage}x | 심볼: {symbol}"
                        )
                    return True  # 이건 실패 아님
                else:
                    if getattr(self, "system_logger", None):
                        self.system_logger.error(
                            f"❌ 레버리지 설정 실패: {data.get('retMsg')} (retCode {ret_code})"
                        )
            else:
                if getattr(self, "system_logger", None):
                    self.system_logger.error(
                        f"❌ HTTP 오류: {response.status_code} {response.text}"
                    )
        except Exception as e:
            if getattr(self, "system_logger", None):
                self.system_logger.error(f"❌ 레버리지 설정 중 예외 발생: {e}")

        return False

    # -------------------------
    # 주문 체결 대기
    # -------------------------
    def wait_order_fill(self, symbol, order_id, max_retries=10, sleep_sec=1):
        endpoint = "/v5/order/realtime"
        base = self.base_url + endpoint

        params_pairs = [
            ("category", "linear"),
            ("symbol", symbol),
            ("orderId", order_id),
        ]
        # 동일한 쿼리스트링 생성
        from urllib.parse import urlencode as _urlencode

        query_string = _urlencode(params_pairs, doseq=False)

        # 요청 URL
        url = f"{base}?{query_string}"

        for i in range(max_retries):
            # 이 쿼리스트링으로 서명 생성 (GET은 body 대신 queryString 사용)
            headers = self._get_headers("GET", endpoint, params=query_string, body="")

            r = requests.get(url, headers=headers, timeout=5)
            # retCode 확인 (에러면 디버그 찍고 다음 루프)
            try:
                data = r.json()
            except Exception:
                if getattr(self, "system_logger", None):
                    self.system_logger.debug(f"응답 JSON 파싱 실패: {r.text[:200]}")
                data = {}

            orders = data.get("result", {}).get("list", [])
            if orders:
                o = orders[0]
                status = (o.get("orderStatus") or "").upper()
                # ✅ 가득 체결만 인정
                if status == "FILLED" and str(o.get("cumExecQty")) not in (
                    "0",
                    "0.0",
                    "",
                    None,
                ):
                    return o
                # ❌ 취소/거절이면 즉시 반환 (호출부에서 분기)
                if status in ("CANCELLED", "REJECTED"):
                    return o

                # 그 외(New/PartiallyFilled 등)는 계속 대기
            if getattr(self, "system_logger", None):
                self.system_logger.debug(
                    f"⌛ 주문 체결 대기중... ({i + 1}/{max_retries}) | 심볼: {symbol} | 주문ID: {order_id[-6:]}"
                )
            time.sleep(sleep_sec)

        # ⏰ 타임아웃: 호출부가 분기할 수 있게 '타임아웃 상태' 반환
        return {"orderId": order_id, "orderStatus": "TIMEOUT"}

    # -------------------------
    # 주문 생성/청산 래퍼들
    # -------------------------
    def submit_market_order(self, symbol, order_side, qty, position_idx, reduce_only=False):
        endpoint = "/v5/order/create"
        body = {
            "category": "linear",
            "symbol": symbol,
            "side": order_side,
            "orderType": "Market",
            "qty": str(qty),
            "positionIdx": position_idx,
            "reduceOnly": bool(reduce_only),
            "timeInForce": "IOC",
        }
        resp = self._request_with_resync(
            "POST", endpoint, params_pairs=None, body_dict=body, timeout=5
        )
        if resp.status_code != 200:
            if getattr(self, "system_logger", None):
                self.system_logger.error(f"❌ HTTP 오류: {resp.status_code} {resp.text}")
            return None
        data = resp.json()
        if data.get("retCode") == 0:
            return data.get("result", {})
        if getattr(self, "system_logger", None):
            self.system_logger.error(
                f"❌ 주문 실패: {data.get('retMsg')} (코드 {data.get('retCode')})"
            )
        return None

    def open_market(self, symbol, side, price, percent, wallet):
        if price is None or wallet is None:
            if getattr(self, "system_logger", None):
                self.system_logger.error("❌ 가격 또는 잔고 정보가 누락되었습니다.")
            return None

        total_balance = wallet.get("USDT", 0)
        # self.leverage는 최종 컨트롤러에서 세팅
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
                f"📥 {side.upper()} 진입 시도 | raw_qty={raw_qty:.8f} → qty={qty:.8f} @ {price:.2f} ({symbol})"
            )
        return self.submit_market_order(symbol, order_side, qty, position_idx, reduce_only=False)

    def close_market(self, symbol, side, qty):
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
        url = self.base_url + endpoint
        method = "POST"
        payload = {
            "category": "linear",
            "symbol": symbol,
            "orderId": order_id,
        }
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
        return float(f"{n * step:.8f}")  # 부동소수 잡음 방지

    def normalize_qty(self, symbol: str, qty: float, mode: str = "floor") -> float:
        """
        심볼 규칙(qtyStep/minOrderQty)에 맞춰 수량 정규화.
        - open: 보통 'floor' (과다 주문 방지)
        - close: 보통 'floor' (잔량 남을 수 있으나 초과주문 방지)
        """
        rules = self.get_symbol_rules(symbol)
        step = rules.get("qtyStep", 0.001) or 0.001
        min_qty = rules.get("minOrderQty", step) or step
        q = max(0.0, float(qty))
        q = self._round_step(q, step, mode=mode)
        if q < min_qty:
            return 0.0
        # (옵션) maxOrderQty 적용 원하면 여기에서 min(q, maxOrderQty)
        return q
