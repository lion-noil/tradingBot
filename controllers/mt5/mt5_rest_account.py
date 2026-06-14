# controllers/mt5/mt5_rest_account.py

import json
try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None
from datetime import datetime, timezone, timedelta
from typing import List

KST = timezone(timedelta(hours=9))

class Mt5RestAccountMixin:
    # -------------------------
    # ?대?: MT5 ?곌껐 蹂댁옣
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

    def get_position_entries(self, symbol: str, side: str) -> List[dict]:
        """
        MT5??ticket-level entries 諛섑솚.
        return: [{ "ts": ms, "qty": float, "price": float }, ...]  (ts ?ㅻ쫫李⑥닚 沅뚯옣)
        """
        if not self._ensure_mt5():
            return []

        sym = (symbol or "").upper().strip()
        sd = (side or "").upper().strip()
        if sd not in ("LONG", "SHORT"):
            return []

        rows = self.get_positions(symbol=sym) or []
        out = []

        for p in rows:
            vol = float(getattr(p, "volume", 0.0) or 0.0)
            if vol <= 0:
                continue

            ptype = int(getattr(p, "type", -1))
            if sd == "LONG" and ptype != mt5.POSITION_TYPE_BUY:
                continue
            if sd == "SHORT" and ptype != mt5.POSITION_TYPE_SELL:
                continue

            ts_ms = int(getattr(p, "time_msc", 0) or 0)
            price_open = float(getattr(p, "price_open", 0.0) or 0.0)

            out.append({"ts": ts_ms, "qty": vol, "price": price_open})

        out.sort(key=lambda x: x["ts"])
        return out

    # -------------------------
    # 怨꾩쥖(?먯궛) 議고쉶
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

            currency = (getattr(acc, "currency", None) or "").strip() or "ACC"
            balance = float(getattr(acc, "balance", 0.0) or 0.0)

            return {
                "currency": currency,
                "wallet_balance": balance,  # ???듭떖: ?듭씪 ?꾨뱶
                "balance": balance,  # (?좏깮) 湲곗〈 ?꾨뱶 ?좎??대룄 ??
                "equity": float(getattr(acc, "equity", 0.0) or 0.0),
                "margin": float(getattr(acc, "margin", 0.0) or 0.0),
                "free_margin": float(getattr(acc, "margin_free", 0.0) or 0.0),
                "leverage": int(getattr(acc, "leverage", 0) or 0),
            }

        except Exception as e:
            if getattr(self, "system_logger", None):
                self.system_logger.error(f"[ERROR] 怨꾩쥖 議고쉶 ?ㅽ뙣: {e}")
            return None

    # -------------------------
    # ?뱀젙 ?щ낵 ?ъ???議고쉶
    # -------------------------
    def get_positions(self, symbol: str = None):
        try:
            if not self._ensure_mt5():
                return []

            if symbol:
                rows = mt5.positions_get(symbol=self._broker_sym(symbol)) or []
            else:
                rows = mt5.positions_get() or []

            return list(rows)

        except Exception as e:
            if getattr(self, "system_logger", None):
                self.system_logger.error(f"[ERROR] positions_get ?ㅽ뙣: {e}")
            return []

    def get_position_qty_sum(self, symbol: str, side: str) -> float:
        """
        ?щ낵怨?諛⑺뼢(LONG/SHORT)??二쇰㈃ ?꾩옱 蹂댁쑀 ?섎웾(Volume) ?⑷퀎瑜?諛섑솚?섎뒗 寃쎈웾 ?⑥닔
        """
        if not self._ensure_mt5():
            return 0.0

        sym = (symbol or "").upper()
        target_side = (side or "").upper()

        # 1. API ?몄텧 (?대떦 ?щ낵???ъ??섎쭔 議고쉶)
        rows = self.get_positions(symbol=sym)
        total_vol = 0.0

        # 2. 吏묎퀎 (?⑥닚 ?⑹궛)
        for p in rows:
            vol = float(getattr(p, "volume", 0.0) or 0.0)
            if vol <= 0:
                continue

            ptype = int(getattr(p, "type", -1))

            if target_side == "LONG":
                if ptype == mt5.POSITION_TYPE_BUY:
                    total_vol += vol
            elif target_side == "SHORT":
                if ptype == mt5.POSITION_TYPE_SELL:
                    total_vol += vol


        return float(total_vol)

