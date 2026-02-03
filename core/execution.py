# core/execution.py
import asyncio
import time
from typing import Optional, Any, Dict
import inspect


class ExecutionEngine:
    """주문 실행 + 체결 대기 + (필요시) 최소 로그"""

    def __init__(
        self,
        rest,
        system_logger=None,
        trading_logger=None,
        taker_fee_rate: float = 0.00055,
    ):
        self.rest = rest
        self.system_logger = system_logger
        self.trading_logger = trading_logger
        self.TAKER_FEE_RATE = taker_fee_rate
        self._sync_lock = asyncio.Lock()
        self._just_traded_until = 0.0

    def _extract_side_hint(self, fn, args, kwargs) -> Optional[str]:
        try:
            sig = inspect.signature(fn)
            bound = sig.bind_partial(*args, **kwargs)
            if "side" in bound.arguments:
                return bound.arguments["side"]
        except Exception:
            pass
        return kwargs.get("side")

    async def execute_and_sync(self, fn, symbol: str, *args, **kwargs) -> Dict[str, Any]:
        """
        반환 포맷(표준):
        {
          "ok": bool,                       # FILLED면 True
          "status": "FILLED" | ...,
          "order_id": Optional[str],
          "expected": "OPEN"|"CLOSE",
          "side": Optional[str],            # "LONG"|"SHORT"|None (hint)
          "filled": dict,                   # wait_order_fill 결과 원문(없으면 {})
          "ex_lot_id": Optional[int],
          "raw": Any,                       # fn 결과 원문(result)
        }
        """
        async with self._sync_lock:
            fn_name = getattr(fn, "__name__", "").lower()
            expected = "CLOSE" if "close" in fn_name else "OPEN"

            side_hint = self._extract_side_hint(fn, args, kwargs)
            side_u = (str(side_hint).upper() if side_hint else None)

            expected_override = kwargs.pop("expected", None)
            if expected_override in ("OPEN", "CLOSE"):
                expected = expected_override

            before_qty = None
            try:
                get_qty = getattr(self.rest, "_get_position_qty", None)
                if callable(get_qty) and side_u:
                    before_qty = float(get_qty(symbol, side_u))
            except Exception:
                before_qty = None

            # 1) 주문 실행
            try:
                raw = fn(symbol, *args, **kwargs)
            except Exception as e:
                if self.system_logger:
                    self.system_logger.error(f"❌ 주문 실행 예외: {e}")
                return {
                    "ok": False,
                    "status": "ERROR",
                    "order_id": None,
                    "expected": expected,
                    "side": side_u,
                    "filled": {},
                    "ex_lot_id": None,
                    "raw": None,
                }

            if not raw or not isinstance(raw, dict):
                if self.system_logger:
                    self.system_logger.warning("⚠️ 주문 결과가 비었습니다(또는 dict 아님).")
                return {
                    "ok": False,
                    "status": "EMPTY_RESULT",
                    "order_id": None,
                    "expected": expected,
                    "side": side_u,
                    "filled": {},
                    "ex_lot_id": None,
                    "raw": raw,  # ✅ 그대로 보존
                }

            # 2) orderId 확보 (Bybit/MT5 호환)
            order_id = raw.get("orderId") or raw.get("deal") or raw.get("order")
            if not order_id:
                if self.system_logger:
                    self.system_logger.warning(
                        f"⚠️ orderId/order/deal 없음 → 체결 대기 스킵 (keys={list(raw.keys())})"
                    )
                return {
                    "ok": False,
                    "status": "NO_ORDER_ID",
                    "order_id": None,
                    "expected": expected,
                    "side": side_u,
                    "filled": {},
                    "ex_lot_id": None,
                    "raw": raw,
                }
            order_id = str(order_id)

            raw_hint = raw.get("match_hint") or raw.get("deal") or raw.get("order") or None
            match_hint = None
            try:
                if raw_hint is not None:
                    match_hint = int(raw_hint)
            except Exception:
                match_hint = None

            # 3) 체결 대기
            filled = (
                self.rest.wait_order_fill(
                    symbol,
                    order_id,
                    expected=expected,
                    side=side_u,
                    before_qty=before_qty,
                    match_hint=match_hint,
                    expected_qty=raw.get("qty"),
                )
                or {}
            )

            status = (filled.get("orderStatus") or "").upper() or "UNKNOWN"

            ex_lot_id = None
            try:
                v = filled.get("ex_lot_id")
                if v is None:
                    v = raw.get("ex_lot_id")
                ex_lot_id = int(v) if v is not None and str(v).strip() else None
            except Exception:
                ex_lot_id = None

            # (선택) 최소 로그: 체결 성공/실패
            if status == "FILLED":
                trade = getattr(self.rest, "get_trade_w_order_id", lambda *_: None)(symbol, order_id)
                if trade and hasattr(self.rest, "append_order"):
                    try:
                        self.rest.append_order(symbol, trade)
                    except Exception:
                        pass
                if self.system_logger:
                    self.system_logger.debug(f"🧾 체결 동기화 완료: {order_id[-6:]}")

            elif status in ("CANCELLED", "REJECTED"):
                if self.system_logger:
                    self.system_logger.warning(f"⚠️ 주문 {order_id[-6:]} 상태: {status} (체결 없음)")

            elif status == "TIMEOUT":
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
                    self.system_logger.warning(f"ℹ️ 주문 {order_id[-6:]} 상태: {status}")

            self._just_traded_until = time.monotonic() + 0.8

            return {
                "ok": (status == "FILLED"),
                "status": status,
                "order_id": order_id,
                "expected": expected,
                "side": side_u,
                "filled": filled,
                "ex_lot_id": ex_lot_id,
                "raw": raw,
            }
