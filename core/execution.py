# execution.py
import asyncio, time
from typing import Optional
from datetime import datetime
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("Asia/Seoul")

class ExecutionEngine:
    """주문 실행 + 체결 대기 + 상태 동기화 + 손익 로그"""
    def __init__(self, rest, system_logger=None, trading_logger=None, taker_fee_rate: float = 0.00055):
        self.rest = rest
        self.system_logger = system_logger
        self.trading_logger = trading_logger
        self.TAKER_FEE_RATE = taker_fee_rate
        self._sync_lock = asyncio.Lock()
        self._just_traded_until = 0.0

    async def execute_and_sync(self, fn, position_detail, symbol, *args, **kwargs):
        async with self._sync_lock:
            try:
                result = fn(*args, **kwargs)
            except Exception as e:
                if self.system_logger: self.system_logger.error(f"❌ 주문 실행 예외: {e}")
                return None

            if not result or not isinstance(result, dict):
                if self.system_logger: self.system_logger.warning("⚠️ 주문 결과가 비었습니다(또는 dict 아님).")
                return result

            order_id = result.get("orderId")
            if not order_id:
                if self.system_logger: self.system_logger.warning("⚠️ orderId 없음 → 체결 대기 스킵")
                return result

            filled = self.rest.wait_order_fill(symbol, order_id)
            orderStatus = (filled or {}).get("orderStatus", "").upper()

            if orderStatus == "FILLED":
                self._log_fill(filled, position_detail)
                trade = self.rest.get_trade_w_order_id(symbol, order_id)
                if trade:
                    self.rest.append_order(symbol, trade)
                if self.system_logger:
                    self.system_logger.debug(f"🧾 체결 동기화 완료: {order_id[-6:]}")
            elif orderStatus in ("CANCELLED", "REJECTED"):
                if self.system_logger: self.system_logger.warning(f"⚠️ 주문 {order_id[-6:]} 상태: {orderStatus} (체결 없음)")
            elif orderStatus == "TIMEOUT":
                if self.system_logger: self.system_logger.warning(f"⚠️ 주문 {order_id[-6:]} 체결 대기 타임아웃 → 취소 시도")
                try:
                    cancel_res = self.rest.cancel_order(symbol, order_id)
                    if self.system_logger: self.system_logger.warning(f"🗑️ 취소 결과: {cancel_res}")
                except Exception as e:
                    if self.system_logger: self.system_logger.error(f"단일 주문 취소 실패: {e}")
            else:
                if self.system_logger: self.system_logger.warning(f"ℹ️ 주문 {order_id[-6:]} 상태: {orderStatus or 'UNKNOWN'}")

            self._just_traded_until = time.monotonic() + 0.8
            return result

    # --- 체결 로그 & 손익 ---
    def _classify_intent(self, filled: dict) -> Optional[str]:
        side = (filled.get("side") or "").upper()   # BUY/SELL
        pos  = int(filled.get("positionIdx") or 0)  # 1/2
        ro   = bool(filled.get("reduceOnly"))
        if ro:
            if pos == 1 and side == "SELL":  return "LONG_CLOSE"
            if pos == 2 and side == "BUY":   return "SHORT_CLOSE"
        else:
            if pos == 1 and side == "BUY":   return "LONG_OPEN"
            if pos == 2 and side == "SELL":  return "SHORT_OPEN"
        return None

    def _log_fill(self, filled: dict, position_detail: dict | None = None):
        intent = self._classify_intent(filled)
        if not intent: return
        side, action = intent.split("_")
        order_tail = (filled.get("orderId") or "")[-6:] or "UNKNOWN"
        filled_avg_price = float(filled.get("avgPrice") or 0.0)
        exec_qty  = float(filled.get("cumExecQty") or filled.get("qty") or 0.0)

        if not action.endswith("CLOSE"):
            if self.trading_logger:
                self.trading_logger.info(
                    f"✅ {side} 주문 체결 완료 | id:{order_tail} | avg:{filled_avg_price:.2f} | qty:{exec_qty}"
                )
            return
        avg_price = position_detail.get('avg_price')

        if (side, action) == ("LONG", "CLOSE"):
            profit_gross = (filled_avg_price - avg_price) * exec_qty
        else:
            profit_gross = (avg_price - filled_avg_price) * exec_qty

        total_fee = (avg_price * exec_qty + filled_avg_price * exec_qty) * self.TAKER_FEE_RATE
        profit_net = profit_gross - total_fee
        profit_rate = (profit_gross / avg_price) * 100 if avg_price else 0.0

        if self.trading_logger:
            self.trading_logger.info(
                f"✅ {side} 청산 | id:{order_tail} | avg:{avg_price:.2f} / filled:{filled_avg_price:.2f} | "
                f"qty:{exec_qty} | PnL(net):{profit_net:.2f} | gross:{profit_gross:.2f}, fee:{total_fee:.2f} | "
                f"rate:{profit_rate:.2f}%"
            )
