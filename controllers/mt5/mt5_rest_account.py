# controllers/mt5/mt5_rest_account.py

import json
import MetaTrader5 as mt5
from datetime import datetime, timezone, timedelta
from typing import List

KST = timezone(timedelta(hours=9))

class Mt5RestAccountMixin:
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

    def get_position_entries(self, symbol: str, side: str) -> List[dict]:
        """
        MT5의 ticket-level entries 반환.
        return: [{ "ts": ms, "qty": float, "price": float }, ...]  (ts 오름차순 권장)
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

            currency = (getattr(acc, "currency", None) or "").strip() or "ACC"
            balance = float(getattr(acc, "balance", 0.0) or 0.0)

            return {
                "currency": currency,
                "wallet_balance": balance,  # ✅ 핵심: 통일 필드
                "balance": balance,  # (선택) 기존 필드 유지해도 됨
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

    def get_position_qty_sum(self, symbol: str, side: str) -> float:
        """
        심볼과 방향(LONG/SHORT)을 주면 현재 보유 수량(Volume) 합계를 반환하는 경량 함수
        """
        if not self._ensure_mt5():
            return 0.0

        sym = (symbol or "").upper()
        target_side = (side or "").upper()

        # 1. API 호출 (해당 심볼의 포지션만 조회)
        rows = self.get_positions(symbol=sym)
        total_vol = 0.0

        # 2. 집계 (단순 합산)
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
