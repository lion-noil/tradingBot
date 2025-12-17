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

            # 0) 주문 전 before_qty 스냅샷
            fn_name = getattr(fn, "__name__", "").lower()
            expected = "CLOSE" if "close" in fn_name else "OPEN"
            side_hint = kwargs.get("side")

            before_qty = None

            try:
                get_qty = getattr(self.rest, "_get_position_qty", None)
                if callable(get_qty) and side_hint:
                    before_qty = float(get_qty(symbol, str(side_hint).upper()))
            except Exception:
                before_qty = None

            try:
                result = fn(*args, **kwargs)
            except Exception as e:
                if self.system_logger: self.system_logger.error(f"❌ 주문 실행 예외: {e}")
                return None

            if not result or not isinstance(result, dict):
                if self.system_logger: self.system_logger.warning("⚠️ 주문 결과가 비었습니다(또는 dict 아님).")
                return result

            # 2) MT5 즉시체결 신호 처리 (핵심)
            #    submit_market_order가 out에 deal을 넣고 있으니 그걸 신뢰
            if result.get("ok") and int(result.get("deal") or 0) > 0:
                # 이미 FILLED로 확정: wait/cancel 스킵
                filled_like = {
                    "orderId": str(result.get("order") or result.get("deal") or ""),
                    "orderStatus": "FILLED",
                    "symbol": symbol,
                    "deal": int(result.get("deal") or 0),
                    "order": int(result.get("order") or 0),
                }
                self._log_fill(filled_like, position_detail)

                # 이미 _record_trade_if_possible로 로컬 저장까지 하고 있다면 생략 가능
                if self.system_logger:
                    self.system_logger.info(f"🧾 [MT5] 즉시체결 처리: deal={filled_like['deal']}")
                self._just_traded_until = time.monotonic() + 0.8
                return result
            # 3) orderId 확보 (Bybit/MT5 호환)
            order_id = result.get("orderId") or result.get("order") or result.get("deal")
            if not order_id:
                if self.system_logger:
                    self.system_logger.warning(f"⚠️ orderId/order/deal 없음 → 체결 대기 스킵 (keys={list(result.keys())})")
                return result
            order_id = str(order_id)

            # 4) wait_order_fill
            try:
                filled = self.rest.wait_order_fill(
                    symbol,
                    order_id,
                    expected=expected,
                    side=(str(side_hint).upper() if side_hint else None),
                    before_qty=before_qty,
                )
            except TypeError:
                filled = self.rest.wait_order_fill(symbol, order_id)

            orderStatus = (filled or {}).get("orderStatus", "").upper()

            if orderStatus == "FILLED":
                self._log_fill(filled, position_detail)
                trade = getattr(self.rest, "get_trade_w_order_id", lambda *_: None)(symbol, order_id)
                if trade and hasattr(self.rest, "append_order"):
                    self.rest.append_order(symbol, trade)
                if self.system_logger:
                    self.system_logger.debug(f"🧾 체결 동기화 완료: {order_id[-6:]}")
            elif orderStatus in ("CANCELLED", "REJECTED"):
                if self.system_logger:
                    self.system_logger.warning(f"⚠️ 주문 {order_id[-6:]} 상태: {orderStatus} (체결 없음)")
            elif orderStatus == "TIMEOUT":
                # MT5 시장가는 “timeout=미확인”일 뿐 “미체결”이 아닐 수 있음.
                # 그래서 MT5는 cancel 시도 자체를 막거나, expected=CLOSE/OPEN별로 추가확인을 넣는 게 좋음.
                if self.system_logger:
                    self.system_logger.warning(f"⚠️ 주문 {order_id[-6:]} 체결 대기 타임아웃")
                try:
                    cancel = getattr(self.rest, "cancel_order", None)
                    if callable(cancel):
                        cancel_res = cancel(symbol, order_id)
                        if self.system_logger:
                            self.system_logger.warning(f"🗑️ 취소 결과: {cancel_res}")
                except Exception as e:
                    if self.system_logger:
                        self.system_logger.error(f"단일 주문 취소 실패: {e}")
            else:
                if self.system_logger:
                    self.system_logger.warning(f"ℹ️ 주문 {order_id[-6:]} 상태: {orderStatus or 'UNKNOWN'}")

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
