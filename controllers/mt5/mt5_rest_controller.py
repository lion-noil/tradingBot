# controllers/mt5/mt5_rest_controller.py

from __future__ import annotations

from pprint import pprint
from datetime import datetime, timedelta
import time


import MetaTrader5 as mt5

from controllers.mt5.mt5_rest_base import Mt5RestBase
from controllers.mt5.mt5_rest_market import Mt5RestMarketMixin

# ✅ 터미널 기반 기능들(REST 아님)
from controllers.mt5.mt5_rest_account import Mt5RestAccountMixin
from controllers.mt5.mt5_rest_orders import Mt5RestOrdersMixin
from controllers.mt5.mt5_rest_trade import Mt5RestTradeMixin


class Mt5RestController(
    Mt5RestBase,
    Mt5RestMarketMixin,    # ✅ price REST 사용 (use="price")
    Mt5RestAccountMixin,   # ✅ MT5 터미널 API
    Mt5RestOrdersMixin,    # ✅ 로컬 파일 + (필요시) 히스토리 동기화
    Mt5RestTradeMixin,     # ✅ MT5 터미널 API로 주문
):
    """
    MT5 최종 컨트롤러 (가격=REST, 거래=터미널)

    - Mt5RestBase: price_base_url만 필수로 유지
    - Mt5RestMarketMixin: 캔들/시세는 REST(ONLINE)로 호출 (_request(use="price"))
    - Mt5RestAccount/Orders/Trade: MT5 터미널 API로 처리 (MetaTrader5)
    """

    def __init__(self, system_logger=None):
        super().__init__(
            system_logger=system_logger,
        )

        # 트레이딩 공통 설정
        self.leverage = 50
        self.TAKER_FEE_RATE = 0.00055  # 0.055%


def _pos_snapshot(symbol: str):
    sym = symbol.upper()
    poss = mt5.positions_get(symbol=sym) or []
    rows = []
    for p in poss:
        rows.append({
            "ticket": int(getattr(p, "ticket", 0) or 0),
            "type": int(getattr(p, "type", -1)),
            "volume": float(getattr(p, "volume", 0.0) or 0.0),
            "price_open": float(getattr(p, "price_open", 0.0) or 0.0),
        })
    return rows



if __name__ == "__main__":
    SYMBOL = "SOLUSD"
    SIDE = "LONG"            # "LONG" / "SHORT"
    PERCENT = 0.1            # 0.1% 같은 것도 테스트 가능
    # PERCENT = 0
    WALLET = {"USD": 10000.0}

    c = Mt5RestController(system_logger=None)

    print("\n[0] mt5 initialize")
    if not c._ensure_mt5():
        raise SystemExit("mt5 init failed")

    if not mt5.symbol_select(SYMBOL.upper(), True):
        raise SystemExit(f"symbol_select failed: {mt5.last_error()}")

    print("\n[1] fetch_symbol_rules")
    pprint(c.fetch_symbol_rules(SYMBOL))

    print("\n[2] position snapshot BEFORE")
    before_pos = _pos_snapshot(SYMBOL)
    pprint(before_pos)
    before_qty = c._get_position_qty(SYMBOL, SIDE)

    tick = mt5.symbol_info_tick(SYMBOL.upper())
    if not tick:
        raise SystemExit(f"symbol_info_tick failed: {mt5.last_error()}")

    open_price = float(tick.ask or 0.0) if SIDE.upper() == "LONG" else float(tick.bid or 0.0)
    print("\n[3] OPEN open_market() price:", open_price)
    out_open = c.open_market(SYMBOL, SIDE, open_price, PERCENT, WALLET)
    pprint(out_open)

    if not out_open or not out_open.get("orderId"):
        raise SystemExit("OPEN failed or missing orderId")

    open_order_id = out_open["orderId"]

    match_hint = int(out_open.get("match_hint") or 0)
    print("\n[4] wait_order_fill(OPEN)")
    filled_open = c.wait_order_fill(
        SYMBOL,
        open_order_id,
        expected="OPEN",
        side=SIDE,
        before_qty=before_qty,
        match_hint=match_hint,
        expected_qty=out_open.get("qty"),   # ✅ 이게 핵심
    )
    pprint(filled_open)

    if (filled_open or {}).get("orderStatus") != "FILLED":
        raise SystemExit(f"OPEN not filled: {filled_open}")

    ex_lot_id = int((filled_open or {}).get("ex_lot_id") or 0) or None
    exec_qty = float((filled_open or {}).get("cumExecQty") or 0.0)
    exec_qty = c.normalize_qty(SYMBOL, exec_qty, mode="round")  # ✅ 추가


    print("\n[5] position snapshot AFTER OPEN")
    time.sleep(0.5)
    after_open_pos = _pos_snapshot(SYMBOL)
    pprint(after_open_pos)

    print("\n[6] CLOSE close_market() using ex_lot_id:", ex_lot_id)
    before_qty_close = c._get_position_qty(SYMBOL, SIDE)  # 청산 직전 qty 스냅샷
    out_close = c.close_market(SYMBOL, SIDE, exec_qty, ex_lot_id=ex_lot_id)
    pprint(out_close)

    if not out_close or not out_close.get("orderId"):
        raise SystemExit("CLOSE failed or missing orderId")

    close_order_id = out_close["orderId"]
    match_hint2 = int(out_close.get("match_hint") or 0) or None

    print("\n[7] wait_order_fill(CLOSE)")
    filled_close = c.wait_order_fill(
        SYMBOL,
        close_order_id,
        expected="CLOSE",
        side=SIDE,
        before_qty=before_qty_close,
        match_hint=match_hint2,
        expected_qty=exec_qty,      # ✅ 추가
    )
    pprint(filled_close)

    print("\n[8] position snapshot AFTER CLOSE")
    time.sleep(0.5)
    after_close_pos = _pos_snapshot(SYMBOL)
    pprint(after_close_pos)

    print("\n[9] SUMMARY")
    print("open_order_id:", open_order_id, "ex_lot_id:", ex_lot_id, "open_exec_qty:", exec_qty)
    print("close_order_id:", close_order_id, "close_status:", (filled_close or {}).get("orderStatus"))
    print("\nDONE ✅")