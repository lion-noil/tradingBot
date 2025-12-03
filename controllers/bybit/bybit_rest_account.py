# controllers/bybit/bybit_rest_account.py
import json

from core.redis_client import redis_client


class BybitRestAccountMixin:
    # -------------------------
    # 지갑 잔고 조회
    # -------------------------
    def get_usdt_balance(self):
        method = "GET"
        endpoint = "/v5/account/wallet-balance"
        coin = "USDT"
        params_pairs = [("accountType", "UNIFIED"), ("coin", coin)]

        try:
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
                self.system_logger.error(
                    f"[ERROR] 잔고 조회 실패: {data.get('retMsg') if isinstance(data, dict) else 'Unknown error'}"
                )
            return None

        try:
            account = (data.get("result", {}).get("list") or [{}])[0]
            coin_list = account.get("coin", [])
            coin_data = next((c for c in coin_list if c.get("coin") == coin), {})

            # 1순위: 코인 레벨 walletBalance
            wb_raw = coin_data.get("walletBalance")

            # 값이 없거나 빈 문자열이면 계정 레벨 totalWalletBalance로 폴백
            if wb_raw in (None, "", "null"):
                wb_raw = account.get("totalWalletBalance", 0)

            wallet_balance = float(wb_raw or 0)

            result = {
                "coin": coin_data.get("coin") or coin,  # 명시적으로 USDT 표기
                "wallet_balance": wallet_balance,
            }

        except Exception as e:
            if getattr(self, "system_logger", None):
                self.system_logger.error(f"[ERROR] 지갑 응답 파싱 실패: {e}")
            return None

        return result

    def _json_or_empty_list(self, inpobj):
        # 포지션 없으면 [] 그대로, 있으면 compact JSON
        if inpobj is None:
            return "[]"
        return json.dumps(inpobj, separators=(",", ":"), ensure_ascii=False)

    # -------------------------
    # 자산 + 포지션/엔트리 구성 & Redis 저장
    # -------------------------
    def getNsav_asset(self, asset, symbol: str = None, save_redis: bool = True):
        result = self.get_usdt_balance()

        if result and asset["wallet"]["USDT"] != result["wallet_balance"] and save_redis:
            try:
                redis_client.hset(
                    "asset",
                    f"wallet.{result['coin']}",
                    f"{result['wallet_balance']:.10f}",
                )
                asset["wallet"]["USDT"] = result["wallet_balance"]
            except Exception as e:
                if getattr(self, "system_logger", None):
                    self.system_logger.error(f"[WARN] Redis 저장 실패: {e}")

        try:
            # BybitRestMarketMixin에서 제공하는 get_positions 사용
            resp = self.get_positions(symbol=symbol)
            rows = (resp.get("result") or {}).get("list") or []
        except Exception:
            rows = []

        long_pos, short_pos = None, None

        for r in rows:
            size = float(r.get("size", 0) or 0)
            if size == 0:
                continue

            avg_price = float(r.get("avgPrice", 0) or 0)
            idx = r.get("positionIdx")

            if idx == 1:
                long_pos = {"qty": size, "avg_price": avg_price}
            elif idx == 2:
                short_pos = {"qty": size, "avg_price": avg_price}
            else:
                side = r.get("side", "").upper()
                if side == "BUY":
                    long_pos = {"qty": size, "avg_price": avg_price}
                elif side == "SELL":
                    short_pos = {"qty": size, "avg_price": avg_price}

        local_orders = self.load_orders(symbol)

        if long_pos is not None:
            long_pos["entries"] = self._build_entries_from_orders(
                local_orders, symbol, "LONG", long_pos["qty"]
            )
        if short_pos is not None:
            short_pos["entries"] = self._build_entries_from_orders(
                local_orders, symbol, "SHORT", short_pos["qty"]
            )
        asset["positions"][symbol]["LONG"] = long_pos
        asset["positions"][symbol]["SHORT"] = short_pos

        if save_redis:
            try:
                redis_client.hset(
                    "asset",
                    f"positions.{symbol}",
                    self._json_or_empty_list(asset["positions"][symbol]),
                )
            except Exception as e:
                if getattr(self, "system_logger", None):
                    self.system_logger.error(f"[WARN] Redis 저장 실패({symbol}): {e}")

        return asset
