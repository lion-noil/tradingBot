# controllers/bybit/bybit_rest_account.py

import json

from core.redis_client import redis_client


class BybitRestAccountMixin:
    # -------------------------
    # 지갑 잔고 조회 (거래용: private)
    # -------------------------
    def get_usdt_balance(self):
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
                "coin": coin_data.get("coin") or coin,  # 명시적으로 USDT 표기
                "wallet_balance": wallet_balance,
            }

        except Exception as e:
            if getattr(self, "system_logger", None):
                self.system_logger.error(f"[ERROR] 지갑 응답 파싱 실패: {e}")
            return None

    def _json_or_empty_list(self, inpobj):
        if inpobj is None:
            return "[]"
        return json.dumps(inpobj, separators=(",", ":"), ensure_ascii=False)

    # -------------------------
    # 자산 + 포지션/엔트리 구성 & Redis 저장 (거래용)
    # -------------------------
    def getNsav_asset(self, asset, symbol: str = None, save_redis: bool = True):
        # ---- 1) wallet ----
        result = self.get_usdt_balance()

        if result and save_redis:
            try:
                prev = float((asset.get("wallet") or {}).get("USDT") or 0.0)
                newv = float(result.get("wallet_balance") or 0.0)
                if prev != newv:
                    redis_client.hset(
                        "asset",
                        f"wallet.{result['coin']}",
                        f"{newv:.10f}",
                    )
                    asset.setdefault("wallet", {})
                    asset["wallet"]["USDT"] = newv
            except Exception as e:
                if getattr(self, "system_logger", None):
                    self.system_logger.error(f"[WARN] Redis 저장 실패(wallet): {e}")

        # symbol 없으면 포지션/엔트리 구성은 스킵 (호출부 실수 방어)
        if not symbol:
            return asset

        asset.setdefault("positions", {})
        asset["positions"].setdefault(symbol, {"LONG": None, "SHORT": None})

        # ---- 2) positions (private: trade) ----
        try:
            resp = self.get_positions(symbol=symbol)  # BybitRestMarketMixin
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
                # 보험 처리
                side = (r.get("side") or "").upper()
                if side == "BUY":
                    long_pos = {"qty": size, "avg_price": avg_price}
                elif side == "SELL":
                    short_pos = {"qty": size, "avg_price": avg_price}

        # ---- 3) entries build (local orders) ----
        try:
            local_orders = self.load_orders(symbol)  # BybitRestOrdersMixin
        except Exception:
            local_orders = []

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

        # ---- 4) redis save ----
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
