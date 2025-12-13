# controllers/mt5/mt5_rest_trade.py
from __future__ import annotations

import math
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any

import MetaTrader5 as mt5

KST = timezone(timedelta(hours=9))


class Mt5RestTradeMixin:
    # -------------------------
    # 내부: MT5 연결 보장
    # -------------------------
    def _ensure_mt5(self) -> bool:
        if mt5.initialize():
            return True
        if getattr(self, "system_logger", None):
            self.system_logger.error(f"[ERROR] MT5 initialize failed: {mt5.last_error()}")
        return False

    # -------------------------
    # (선택) 주문 결과를 로컬 orders 파일에 기록
    # -------------------------
    def _record_trade_if_possible(self, out: Dict[str, Any]) -> None:
        """
        주문 성공 시, Mt5RestOrdersMixin.append_order()가 같이 믹스인되어 있으면
        바로 로컬 파일에 trade 기록을 남긴다.

        - history_deals_get이 브로커/상품에 따라 비거나 제한될 수 있어
          "주문 성공 순간에 저장"이 가장 안정적.
        """
        try:
            if not out or not out.get("ok"):
                return

            # append_order가 없으면 조용히 스킵
            if not hasattr(self, "append_order"):
                return

            sym = out.get("symbol") or ""
            if not sym:
                return

            # id는 가능하면 deal -> order -> time_ms 순으로
            trade_id = str(out.get("deal") or out.get("order") or out.get("time_ms") or int(time.time() * 1000))

            side = "LONG" if (out.get("side") == "Buy") else "SHORT"
            trade_type = "CLOSE" if out.get("reduce_only") else "OPEN"

            ts_ms = int(out.get("time_ms") or int(time.time() * 1000))
            ts_str = datetime.fromtimestamp(ts_ms / 1000, tz=KST).strftime("%Y-%m-%d %H:%M:%S")

            trade = {
                "id": trade_id,
                "symbol": sym,
                "side": side,                 # LONG / SHORT
                "type": trade_type,           # OPEN / CLOSE
                "qty": float(out.get("qty") or 0.0),
                "price": float(out.get("price") or 0.0),
                "time": ts_ms,
                "time_str": ts_str,
                "fee": 0.0,                   # MT5 수수료는 API로 즉시 못 받을 수 있어 일단 0
                "order_id": str(out.get("order") or "0"),
                "position_id": "0",
                "profit": 0.0,
                "retcode": int(out.get("retcode") or -1),
                "mt5_comment": str(out.get("comment") or ""),
            }

            # 실제 저장 (Mt5RestOrdersMixin.append_order)
            self.append_order(sym, trade)

            if getattr(self, "system_logger", None):
                self.system_logger.debug(f"🧾 [MT5] trade recorded: {trade['type']} {trade['side']} {sym} id={trade['id']}")

        except Exception as e:
            if getattr(self, "system_logger", None):
                self.system_logger.error(f"[MT5] record_trade failed: {e}")

    # -------------------------
    # 심볼 룰(랏 규칙) 조회
    # -------------------------
    def get_symbol_rules(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Bybit의 get_symbol_rules 유사: volume_min/step/max 반환
        """
        if not self._ensure_mt5():
            return None
        sym = symbol.upper()
        info = mt5.symbol_info(sym)
        if info is None:
            if getattr(self, "system_logger", None):
                self.system_logger.error(f"[ERROR] symbol_info({sym}) failed: {mt5.last_error()}")
            return None
        return {
            "symbol": sym,
            "volume_min": float(getattr(info, "volume_min", 0.0) or 0.0),
            "volume_step": float(getattr(info, "volume_step", 0.0) or 0.0),
            "volume_max": float(getattr(info, "volume_max", 0.0) or 0.0),
        }

    # -------------------------
    # 수량(랏) 정규화
    # -------------------------
    def _round_step(self, value: float, step: float, mode: str = "floor") -> float:
        if step <= 0:
            return float(value)
        n = float(value) / step
        if mode == "ceil":
            n = math.ceil(n - 1e-12)
        elif mode == "round":
            n = round(n)
        else:
            n = math.floor(n + 1e-12)
        return float(f"{n * step:.8f}")

    def normalize_qty(self, symbol: str, qty: float, mode: str = "floor") -> float:
        """
        MT5 volume_min/step/max에 맞춰 랏 정규화.
        """
        rules = self.get_symbol_rules(symbol) or {}
        step = float(rules.get("volume_step") or 0.01) or 0.01
        min_qty = float(rules.get("volume_min") or step) or step
        max_qty = float(rules.get("volume_max") or 0.0) or 0.0

        q = max(0.0, float(qty))
        q = self._round_step(q, step, mode=mode)
        if q < min_qty:
            return 0.0
        if max_qty > 0 and q > max_qty:
            q = self._round_step(max_qty, step, mode="floor")
        return q

    # -------------------------
    # 주문 생성/청산 래퍼
    # -------------------------
    def submit_market_order(
        self,
        symbol: str,
        order_side: str,  # "Buy"/"Sell"
        qty: float,
        position_idx: int = 0,  # 호환용(무시)
        reduce_only: bool = False,
        deviation: int = 20,
        magic: int = 20251213,
        comment: str = "mt5-market",
    ) -> Optional[Dict[str, Any]]:
        """
        MT5 시장가 주문 전송.
        reduce_only=True면 현재 포지션(ticket) 지정해서 반대매매로 청산 시도.
        """
        if not self._ensure_mt5():
            return None

        sym = symbol.upper()

        if not mt5.symbol_select(sym, True):
            if getattr(self, "system_logger", None):
                self.system_logger.error(f"[ERROR] symbol_select({sym}) failed: {mt5.last_error()}")
            return None

        vol = self.normalize_qty(sym, qty, mode="floor")
        if vol <= 0:
            if getattr(self, "system_logger", None):
                self.system_logger.error(f"[ERROR] normalized qty is 0 (raw={qty}) for {sym}")
            return None

        tick = mt5.symbol_info_tick(sym)
        if tick is None:
            if getattr(self, "system_logger", None):
                self.system_logger.error(f"[ERROR] symbol_info_tick({sym}) failed: {mt5.last_error()}")
            return None

        side = (order_side or "").strip().lower()
        if side == "buy":
            otype = mt5.ORDER_TYPE_BUY
            price = float(tick.ask or 0.0)
            closing_position_type = mt5.POSITION_TYPE_SELL
        elif side == "sell":
            otype = mt5.ORDER_TYPE_SELL
            price = float(tick.bid or 0.0)
            closing_position_type = mt5.POSITION_TYPE_BUY
        else:
            if getattr(self, "system_logger", None):
                self.system_logger.error(f"[ERROR] invalid order_side: {order_side}")
            return None

        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": sym,
            "type": otype,
            "volume": float(vol),
            "price": float(price),
            "deviation": int(deviation),
            "magic": int(magic),
            "comment": str(comment),
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        if reduce_only:
            poss = mt5.positions_get(symbol=sym) or []
            targets = [p for p in poss if int(getattr(p, "type", -1)) == closing_position_type]
            if not targets:
                if getattr(self, "system_logger", None):
                    self.system_logger.warning(f"[WARN] reduce_only but no opposite position to close: {sym}")
                return None

            # hedging이면 여러개일 수 있어 가장 큰 포지션 1개만 대상
            p = max(targets, key=lambda x: float(getattr(x, "volume", 0.0) or 0.0))
            req["position"] = int(getattr(p, "ticket", 0) or 0)

            pos_vol = float(getattr(p, "volume", 0.0) or 0.0)
            if vol > pos_vol:
                req["volume"] = float(self.normalize_qty(sym, pos_vol, mode="floor"))
                if req["volume"] <= 0:
                    return None

        res = mt5.order_send(req)
        if res is None:
            if getattr(self, "system_logger", None):
                self.system_logger.error(f"[ERROR] order_send returned None: {mt5.last_error()}")
            return None

        retcode = int(getattr(res, "retcode", -1))
        ok = retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED)

        out = {
            "ok": bool(ok),
            "retcode": retcode,
            "comment": str(getattr(res, "comment", "")),
            "order": int(getattr(res, "order", 0) or 0),
            "deal": int(getattr(res, "deal", 0) or 0),
            "symbol": sym,
            "side": "Buy" if otype == mt5.ORDER_TYPE_BUY else "Sell",
            "qty": float(req["volume"]),
            "price": float(req["price"]),
            "reduce_only": bool(reduce_only),
            "time_ms": int(time.time() * 1000),
        }

        if not ok and getattr(self, "system_logger", None):
            self.system_logger.error(f"[ERROR] mt5 order failed: {out}")

        # ✅✅✅ 핵심: 성공이면 즉시 로컬 기록 저장
        self._record_trade_if_possible(out)

        return out

    # -------------------------
    # Bybit 스타일 래퍼: wallet/percent 기반 진입(편의용)
    # -------------------------
    def open_market(self, symbol: str, side: str, price: float, percent: float, wallet: dict):
        if price is None or wallet is None:
            if getattr(self, "system_logger", None):
                self.system_logger.error("❌ 가격 또는 잔고 정보가 누락되었습니다.")
            return None

        total_balance = wallet.get("USD") or wallet.get("USDT") or next(iter(wallet.values()), 0) or 0

        rules = self.get_symbol_rules(symbol) or {}
        vmin = float(rules.get("volume_min") or 0.01) or 0.01

        raw_lot = vmin * max(1.0, float(percent) / 1.0)
        qty = self.normalize_qty(symbol, raw_lot, mode="floor")

        if qty <= 0:
            if getattr(self, "system_logger", None):
                self.system_logger.error(f"❗ 주문 수량이 최소단위 미만입니다. raw={raw_lot} norm={qty} ({symbol})")
            return None

        if side.lower() == "long":
            order_side = "Buy"
            position_idx = 1
        elif side.lower() == "short":
            order_side = "Sell"
            position_idx = 2
        else:
            if getattr(self, "system_logger", None):
                self.system_logger.error(f"❌ 알 수 없는 side 값: {side}")
            return None

        if getattr(self, "system_logger", None):
            self.system_logger.debug(
                f"📥 [MT5] {side.upper()} 진입 시도 | qty(lot)={qty:.4f} @ {price:.5f} ({symbol}) "
                f"(wallet≈{total_balance})"
            )

        return self.submit_market_order(symbol, order_side, qty, position_idx, reduce_only=False)

    # -------------------------
    # Bybit 스타일 래퍼: 청산
    # -------------------------
    def close_market(self, symbol: str, side: str, qty: float):
        qty = float(qty)
        qty = self.normalize_qty(symbol, qty, mode="floor")
        if qty <= 0:
            if getattr(self, "system_logger", None):
                self.system_logger.warning("❗ 청산 수량이 최소단위 미만입니다. 중단.")
            return None

        if side.upper() == "LONG":
            order_side, position_idx = "Sell", 1
        elif side.upper() == "SHORT":
            order_side, position_idx = "Buy", 2
        else:
            if getattr(self, "system_logger", None):
                self.system_logger.error(f"❌ 알 수 없는 side 값: {side}")
            return None

        if getattr(self, "system_logger", None):
            self.system_logger.debug(f"📤 [MT5] {side.upper()} 포지션 청산 시도 | qty(lot)={qty:.4f} ({symbol})")

        return self.submit_market_order(symbol, order_side, qty, position_idx, reduce_only=True)


if __name__ == "__main__":
    """
    단독 테스트:
    - MT5 터미널 실행 중이어야 함
    - 모의계좌에서 BTCUSD로 테스트 권장

    실행 예:
      MT5_TEST_SYMBOL=BTCUSD MT5_TEST_SIDE=long MT5_TEST_LOT=0.01 python -m controllers.mt5.mt5_rest_trade
      MT5_TEST_CLOSE=1 MT5_TEST_SYMBOL=BTCUSD MT5_TEST_SIDE=long MT5_TEST_LOT=0.01 python -m controllers.mt5.mt5_rest_trade
    """
    import os
    from pprint import pprint

    try:
        from app import config as cfg  # noqa: F401
    except Exception:
        cfg = None

    SYMBOL = os.getenv("MT5_TEST_SYMBOL", "BTCUSD").upper()
    SIDE = os.getenv("MT5_TEST_SIDE", "long").lower()
    LOT = float(os.getenv("MT5_TEST_LOT", "0.01"))
    DO_CLOSE = os.getenv("MT5_TEST_CLOSE", "0").strip().lower() in ("1", "true", "yes", "y", "on")

    # ✅ Orders mixin이 함께 있어야 append_order가 실제로 동작함
    try:
        from controllers.mt5.mt5_rest_orders import Mt5RestOrdersMixin
    except Exception:
        Mt5RestOrdersMixin = object  # fallback

    class _Tester(Mt5RestTradeMixin, Mt5RestOrdersMixin):
        system_logger = None

    t = _Tester()

    print("\n[0] SETTINGS")
    print("SYMBOL:", SYMBOL, "SIDE:", SIDE, "LOT:", LOT, "DO_CLOSE:", DO_CLOSE)

    print("\n[1] symbol rules")
    pprint(t.get_symbol_rules(SYMBOL))

    print("\n[2] submit_market_order (OPEN)")
    if SIDE == "long":
        r = t.submit_market_order(SYMBOL, "Buy", LOT, position_idx=1, reduce_only=False, comment="py-test-open")
    else:
        r = t.submit_market_order(SYMBOL, "Sell", LOT, position_idx=2, reduce_only=False, comment="py-test-open")
    pprint(r)

    time.sleep(1.0)

    print("\n[3] positions_get")
    poss = mt5.positions_get(symbol=SYMBOL) or []
    print("positions:", len(poss))
    if poss:
        try:
            pprint(poss[0]._asdict())
        except Exception:
            pprint(poss[0])

    if DO_CLOSE:
        print("\n[4] submit_market_order (CLOSE via reduce_only)")
        if SIDE == "long":
            rc = t.submit_market_order(SYMBOL, "Sell", LOT, position_idx=1, reduce_only=True, comment="py-test-close")
        else:
            rc = t.submit_market_order(SYMBOL, "Buy", LOT, position_idx=2, reduce_only=True, comment="py-test-close")
        pprint(rc)

        time.sleep(1.0)
        poss2 = mt5.positions_get(symbol=SYMBOL) or []
        print("\n[5] positions after close:", len(poss2))

    # 로컬 기록 확인
    if hasattr(t, "load_orders"):
        print("\n[6] local orders file check")
        saved = t.load_orders(SYMBOL)
        print("saved count:", len(saved))
        if saved:
            print("last saved:")
            pprint(saved[-1])

    print("\nDONE")
