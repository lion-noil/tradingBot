# controllers/bybit/bybit_rest_account.py

import requests
class BybitRestAccountMixin:

    def get_positions(self, symbol=None, category="linear"):
        endpoint = "/v5/position/list"
        params_pairs = [("category", category), ("symbol", symbol)]
        # ✅ 거래용 base_url 사용 (_request_with_resync는 self.base_url 사용)
        resp = self._request_with_resync(
            "GET", endpoint, params_pairs=params_pairs, body_dict=None, timeout=5
        )
        return resp.json()

    def get_position_qty_sum(self, symbol: str, side: str) -> float:
        """
        심볼과 방향(LONG/SHORT)을 주면 현재 보유 수량을 반환하는 경량 함수
        """
        sym = (symbol or "").upper()
        target_side = (side or "").upper()  # "LONG" or "SHORT"

        # 1. API 호출 (positions만 조회)
        try:
            resp = self.get_positions(symbol=sym)
            rows = (resp.get("result") or {}).get("list") or []
        except Exception as e:
            if getattr(self, "system_logger", None):
                self.system_logger.debug(f"[get_position_qty_sum] 조회 실패: {e}")
            return 0.0

        # 2. 파싱 (build_asset 로직의 경량화 버전)
        for r in rows:
            size = float(r.get("size", 0) or 0)
            if size == 0:
                continue

            idx = int(r.get("positionIdx", 0) or 0)

            # 방향 판별
            current_side = ""
            if idx == 1:
                current_side = "LONG"
            elif idx == 2:
                current_side = "SHORT"
            else:
                # One-Way Mode (idx=0)
                raw_side = (r.get("side") or "").upper()
                if raw_side == "BUY":
                    current_side = "LONG"
                elif raw_side == "SELL":
                    current_side = "SHORT"

            if current_side == target_side:
                return size

        return 0.0

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

    def get_account_balance(self):
        method = "GET"
        endpoint = "/v5/account/wallet-balance"
        coin = "USDT"
        params_pairs = [("accountType", "UNIFIED"), ("coin", coin)]

        try:
            # ✅ _request_with_resync는 trade_base_url 기반 (BybitRestBase에서 trade로 고정)
            resp = self._request_with_resync(
                method, endpoint, params_pairs=params_pairs, body_dict=None, timeout=5
            )
            data = resp.json()
        except Exception as e:
            if getattr(self, "system_logger", None):
                self.system_logger.error(f"[ERROR] 지갑 조회 실패 (API): {e}")
            return None

        # API 에러 처리
        if not isinstance(data, dict) or data.get("retCode") != 0:
            if getattr(self, "system_logger", None):
                msg = data.get("retMsg") if isinstance(data, dict) else "Unknown error"
                self.system_logger.error(f"[ERROR] 잔고 조회 실패: {msg}")
            return None

        try:
            account = (data.get("result", {}).get("list") or [{}])[0]
            coin_list = account.get("coin", []) or []
            coin_data = next((c for c in coin_list if c.get("coin") == coin), {}) or {}

            # 1순위: 코인 레벨 walletBalance
            wb_raw = coin_data.get("walletBalance")

            # 값이 없거나 빈 문자열이면 계정 레벨 totalWalletBalance로 폴백
            if wb_raw in (None, "", "null"):
                wb_raw = account.get("totalWalletBalance", 0)

            wallet_balance = float(wb_raw or 0)

            return {
                "currency": coin_data.get("coin") or coin,  # ✅ coin -> currency
                "wallet_balance": float(wb_raw or 0.0),
            }

        except Exception as e:
            if getattr(self, "system_logger", None):
                self.system_logger.error(f"[ERROR] 지갑 응답 파싱 실패: {e}")
            return None

