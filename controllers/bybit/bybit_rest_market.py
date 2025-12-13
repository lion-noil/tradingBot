# controllers/bybit/bybit_rest_market.py

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
    # 포지션 조회 (거래용)
    # -------------------------
    def get_positions(self, symbol=None, category="linear"):
        endpoint = "/v5/position/list"
        params_pairs = [("category", category), ("symbol", symbol)]
        # ✅ 거래용 base_url 사용 (_request_with_resync는 self.base_url 사용)
        resp = self._request_with_resync(
            "GET", endpoint, params_pairs=params_pairs, body_dict=None, timeout=5
        )
        return resp.json()

    # -------------------------
    # 캔들 업데이트 (가격용, 메인넷)
    # -------------------------
    def update_candles(self, candles, symbol=None, count=None):
        try:
            symbol = symbol
            # ✅ 가격용 REST URL (메인넷)
            url = f"{self.price_base_url}/v5/market/kline"

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
                    raise RuntimeError(
                        f"bybit error retCode={ret_code}, retMsg={data.get('retMsg')}"
                    )

                result = data.get("result", {})
                raw_list = result.get("list") or []
                if not raw_list:
                    break

                raw_list = raw_list[::-1]

                chunk = []
                for c in raw_list:
                    try:
                        if not isinstance(c, (list, tuple)) or len(c) < 5:
                            continue
                        chunk.append(
                            {
                                "start": _safe_int(c[0]),
                                "open": float(c[1]),
                                "high": float(c[2]),
                                "low": float(c[3]),
                                "close": float(c[4]),
                            }
                        )
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
    # 레버리지 설정 (거래용)
    # -------------------------
    def set_leverage(self, symbol="BTCUSDT", leverage=10, category="linear"):
        try:
            endpoint = "/v5/position/set-leverage"
            url = self.trade_base_url + endpoint
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

            response = requests.post(url, headers=headers, data=body, timeout=5)

            if response.status_code == 200:
                data = response.json()
                ret_code = data.get("retCode")
                if ret_code in (0, 110043):
                    if getattr(self, "system_logger", None):
                        self.system_logger.debug(
                            f"✅ 레버리지 {leverage}x 설정 완료 | 심볼: {symbol}"
                        )
                    return True
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
    # 주문 체결 대기 (거래용)
    # -------------------------
    def wait_order_fill(self, symbol, order_id, max_retries=10, sleep_sec=1):
        endpoint = "/v5/order/realtime"
        base = self.trade_base_url + endpoint

        from urllib.parse import urlencode

        params_pairs = [
            ("category", "linear"),
            ("symbol", symbol),
            ("orderId", order_id),
        ]
        query_string = urlencode(params_pairs, doseq=False)
        url = f"{base}?{query_string}"

        for i in range(max_retries):
            headers = self._get_headers("GET", endpoint, params=query_string, body="")
            r = requests.get(url, headers=headers, timeout=5)

            try:
                data = r.json()
            except Exception:
                data = {}

            orders = data.get("result", {}).get("list", [])
            if orders:
                o = orders[0]
                status = (o.get("orderStatus") or "").upper()
                if status == "FILLED":
                    return o
                if status in ("CANCELLED", "REJECTED"):
                    return o

            if getattr(self, "system_logger", None):
                self.system_logger.debug(
                    f"⌛ 주문 체결 대기중... ({i+1}/{max_retries}) | {symbol}"
                )
            time.sleep(sleep_sec)

        return {"orderId": order_id, "orderStatus": "TIMEOUT"}
