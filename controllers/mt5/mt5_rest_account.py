# controllers/mt5/mt5_rest_account.py

import json
import MetaTrader5 as mt5


class Mt5RestAccountMixin:
    """
    BybitRestAccountMixin의 역할을 MT5용으로 옮긴 버전.

    - get_account_balance(): MT5 계좌 balance/equity/free_margin 등 조회
    - get_positions(symbol): 특정 심볼 포지션 조회
    """

    REDIS_ASSET_KEY = "trading:mt5_signal:asset"

    def _asset_key(self):
        return self.REDIS_ASSET_KEY

    # -------------------------
    # 내부: MT5 연결 보장
    # -------------------------
    def _ensure_mt5(self):
        if mt5.initialize():
            return True
        if getattr(self, "system_logger", None):
            self.system_logger.error(f"[ERROR] MT5 initialize failed: {mt5.last_error()}")
        return False

    def _json_or_empty_list(self, inpobj):
        if inpobj is None:
            return "[]"
        return json.dumps(inpobj, separators=(",", ":"), ensure_ascii=False)

    # -------------------------
    # 계좌(자산) 조회
    # -------------------------
    def get_account_balance(self):
        try:
            if not self._ensure_mt5():
                return None

            acc = mt5.account_info()
            if acc is None:
                if getattr(self, "system_logger", None):
                    self.system_logger.error(f"[ERROR] account_info() failed: {mt5.last_error()}")
                return None

            return {
                "currency": getattr(acc, "currency", None),
                "balance": float(getattr(acc, "balance", 0.0) or 0.0),
                "equity": float(getattr(acc, "equity", 0.0) or 0.0),
                "margin": float(getattr(acc, "margin", 0.0) or 0.0),
                "free_margin": float(getattr(acc, "margin_free", 0.0) or 0.0),
                "leverage": int(getattr(acc, "leverage", 0) or 0),
            }

        except Exception as e:
            if getattr(self, "system_logger", None):
                self.system_logger.error(f"[ERROR] 계좌 조회 실패: {e}")
            return None

    # -------------------------
    # 특정 심볼 포지션 조회
    # -------------------------
    def get_positions(self, symbol: str = None):
        try:
            if not self._ensure_mt5():
                return []

            if symbol:
                rows = mt5.positions_get(symbol=symbol) or []
            else:
                rows = mt5.positions_get() or []

            return list(rows)

        except Exception as e:
            if getattr(self, "system_logger", None):
                self.system_logger.error(f"[ERROR] positions_get 실패: {e}")
            return []

    def build_asset(self, asset: dict | None = None, symbol: str | None = None) -> dict:
        asset = dict(asset or {})
        wallet = dict(asset.get("wallet") or {})
        positions = dict(asset.get("positions") or {})

        # ---- 1) wallet ----
        result = self.get_account_balance()
        if result:
            try:
                ccy = result.get("currency") or "ACC"
                wallet[ccy] = float(result.get("balance") or 0.0)
            except Exception:
                pass

        asset["wallet"] = wallet
        asset["positions"] = positions

        if not symbol:
            return asset

        sym = str(symbol).upper().strip()
        positions.setdefault(sym, {"LONG": None, "SHORT": None})

        # ---- 2) positions ----
        rows = self.get_positions(symbol=sym) or []

        long_pos, short_pos = None, None

        for p in rows:
            volume = float(getattr(p, "volume", 0.0) or 0.0)
            if volume == 0:
                continue

            price_open = float(getattr(p, "price_open", 0.0) or 0.0)
            ptype = int(getattr(p, "type", -1))

            if ptype == mt5.POSITION_TYPE_BUY:
                if long_pos is None:
                    long_pos = {"qty": volume, "avg_price": price_open}
                else:
                    tot = long_pos["qty"] + volume
                    if tot > 0:
                        long_pos["avg_price"] = (long_pos["avg_price"] * long_pos["qty"] + price_open * volume) / tot
                    long_pos["qty"] = tot

            elif ptype == mt5.POSITION_TYPE_SELL:
                if short_pos is None:
                    short_pos = {"qty": volume, "avg_price": price_open}
                else:
                    tot = short_pos["qty"] + volume
                    if tot > 0:
                        short_pos["avg_price"] = (short_pos["avg_price"] * short_pos["qty"] + price_open * volume) / tot
                    short_pos["qty"] = tot

        # ---- 3) entries build (기존 방식 유지) ----
        local_orders = []
        can_build_entries = hasattr(self, "load_orders") and hasattr(self, "_build_entries_from_orders")

        if can_build_entries:
            try:
                local_orders = self.load_orders(sym) or []
            except Exception:
                local_orders = []

        if long_pos is not None:
            if can_build_entries:
                try:
                    long_pos["entries"] = self._build_entries_from_orders(local_orders, sym, "LONG", long_pos["qty"])
                except Exception:
                    long_pos["entries"] = []
            else:
                long_pos["entries"] = []

        if short_pos is not None:
            if can_build_entries:
                try:
                    short_pos["entries"] = self._build_entries_from_orders(local_orders, sym, "SHORT", short_pos["qty"])
                except Exception:
                    short_pos["entries"] = []
            else:
                short_pos["entries"] = []

        positions[sym]["LONG"] = long_pos
        positions[sym]["SHORT"] = short_pos
        asset["positions"] = positions

        return asset

