# core/execution.py
import asyncio, time
from typing import Optional
import inspect

class ExecutionEngine:
    """주문 실행 + 체결 대기 + 상태 동기화 + 손익 로그"""

    def __init__(
        self,
        rest,
        system_logger=None,
        trading_logger=None,
        taker_fee_rate: float = 0.00055,
        engine_name: str = "",
    ):
        self.rest = rest
        self.system_logger = system_logger
        self.trading_logger = trading_logger
        self.TAKER_FEE_RATE = taker_fee_rate
        self.engine_name = (engine_name or "").upper()
        self._sync_lock = asyncio.Lock()
        self._just_traded_until = 0.0



    async def execute_and_sync(self, fn, position_detail, symbol, *args, **kwargs):
        def _extract_side_hint(fn, args, kwargs):
            try:
                sig = inspect.signature(fn)
                bound = sig.bind_partial(*args, **kwargs)
                if "side" in bound.arguments:
                    return bound.arguments["side"]
            except Exception:
                pass

            # 2) kwargs에 side가 있으면(바인딩 실패 대비)
            return kwargs.get("side")

        async with self._sync_lock:
            # 0) 주문 전 before_qty 스냅샷
            fn_name = getattr(fn, "__name__", "").lower()
            expected = "CLOSE" if "close" in fn_name else "OPEN"

            side_hint = _extract_side_hint(fn, args, kwargs)
            expected_override = kwargs.pop("expected", None)  # OPEN/CLOSE 강제
            if expected_override in ("OPEN", "CLOSE"):
                expected = expected_override

            before_qty = None
            try:
                get_qty = getattr(self.rest, "_get_position_qty", None)
                if callable(get_qty) and side_hint:
                    before_qty = float(get_qty(symbol, str(side_hint).upper()))
            except Exception:
                before_qty = None

            # 1) 주문 실행
            try:
                result = fn(*args, **kwargs)
            except Exception as e:
                if self.system_logger:
                    self.system_logger.error(f"❌ 주문 실행 예외: {e}")
                return None

            if not result or not isinstance(result, dict):
                if self.system_logger:
                    self.system_logger.warning("⚠️ 주문 결과가 비었습니다(또는 dict 아님).")
                return result

            # 2) orderId 확보 (Bybit/MT5 호환)
            order_id = result.get("orderId") or result.get("deal") or result.get("order")
            if not order_id:
                if self.system_logger:
                    self.system_logger.warning(
                        f"⚠️ orderId/order/deal 없음 → 체결 대기 스킵 (keys={list(result.keys())})"
                    )
                return result
            order_id = str(order_id)

            raw_hint = result.get("match_hint") or result.get("deal") or result.get("order") or None
            match_hint = None
            try:
                if raw_hint is not None:
                    match_hint = int(raw_hint)
            except Exception:
                match_hint = None

            # 3) wait_order_fill (Bybit/MT5 공통)
            filled = self.rest.wait_order_fill(
                symbol,
                order_id,
                expected=expected,
                side=(str(side_hint).upper() if side_hint else None),
                before_qty=before_qty,
                match_hint=match_hint,
                expected_qty=result.get("qty"),   # ✅ 이게 핵심
            )
            result["_filled"] = filled or {}
            if isinstance(filled, dict) and filled.get("ex_lot_id"):
                result["ex_lot_id"] = filled.get("ex_lot_id")

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
        pos = int(filled.get("positionIdx") or 0)   # 1/2
        ro = bool(filled.get("reduceOnly"))
        if ro:
            if pos == 1 and side == "SELL":
                return "LONG_CLOSE"
            if pos == 2 and side == "BUY":
                return "SHORT_CLOSE"
        else:
            if pos == 1 and side == "BUY":
                return "LONG_OPEN"
            if pos == 2 and side == "SELL":
                return "SHORT_OPEN"
        return None

    def _short_ex_lot_id(self, filled: dict) -> str:
        ex_lot_id = filled.get("ex_lot_id")
        if ex_lot_id is not None:
            s = str(ex_lot_id).strip()
            if s:
                return s[:6]  # ✅ 앞 6자리

        return "UNKNOWN"

    def _log_fill(self, filled: dict, position_detail: dict | None = None):
        intent = self._classify_intent(filled)
        if not intent:
            return

        side, action = intent.split("_")  # side: LONG/SHORT, action: OPEN/CLOSE

        ex_lot_id = self._short_ex_lot_id(filled)
        filled_avg_price = float(filled.get("avgPrice") or 0.0)
        exec_qty = float(filled.get("cumExecQty") or filled.get("qty") or 0.0)

        qty_str = f"{exec_qty:.8f}".rstrip("0").rstrip(".")
        # --- OPEN 체결 ---
        if action == "OPEN":
            if self.trading_logger:
                self.trading_logger.info(
                    f"➕ {side} 진입 완료 | ex_lot_id:{ex_lot_id} | avg:{filled_avg_price:.2f} | qty:{qty_str}"
                )
            return

        # --- CLOSE 청산 ---
        if not position_detail or "avg_price" not in position_detail:
            if self.trading_logger:
                self.trading_logger.info(
                    f"➖ {side} 청산 완료| ex_lot_id:{ex_lot_id} | filled:{filled_avg_price:.2f} | qty:{qty_str} | (avg_price 없음)"
                )
            return

        avg_price = float(position_detail.get("avg_price") or 0.0)

        # ✅ 체결수량도 없으면 PnL 계산 스킵
        if exec_qty <= 0:
            if self.trading_logger:
                self.trading_logger.info(
                    f"➖ {side} 청산 완료| ex_lot_id:{ex_lot_id} | avg:{avg_price:.2f} / filled:{filled_avg_price:.2f} | "
                    f"qty:{qty_str} | PnL 스킵(qty missing)"
                )
            return

        # ✅ 체결가 못받은 케이스(=0.0)이면 PnL 계산 스킵
        if filled_avg_price <= 0:
            if self.trading_logger:
                self.trading_logger.info(
                    f"➖ {side} 청산 완료| ex_lot_id:{ex_lot_id} | avg:{avg_price:.2f} / filled:UNKNOWN | "
                    f"qty:{qty_str} | PnL 스킵(avgPrice missing)"
                )
            return

        if side == "LONG":
            profit_gross = (filled_avg_price - avg_price) * exec_qty
        else:
            profit_gross = (avg_price - filled_avg_price) * exec_qty

        total_fee = (avg_price * exec_qty + filled_avg_price * exec_qty) * self.TAKER_FEE_RATE
        profit_net = profit_gross - total_fee
        profit_rate = (profit_gross / avg_price) * 100 if avg_price else 0.0

        if self.trading_logger:
            self.trading_logger.info(
                f"➖ {side} 청산 완료| ex_lot_id:{ex_lot_id} | avg:{avg_price:.2f} / filled:{filled_avg_price:.2f} | "
                f"qty:{qty_str} | PnL(net):{profit_net:.2f} | gross:{profit_gross:.2f}, fee:{total_fee:.2f} | "
                f"rate:{profit_rate:.2f}%"
            )
