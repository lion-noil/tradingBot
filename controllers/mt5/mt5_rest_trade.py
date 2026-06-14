# controllers/mt5/mt5_rest_trade.py
from __future__ import annotations

import math
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any
from datetime import datetime, timedelta, timezone
try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None

KST = timezone(timedelta(hours=9))


class Mt5RestTradeMixin:

    def _ensure_mt5(self) -> bool:
        if mt5.initialize():
            return True
        if getattr(self, "system_logger", None):
            self.system_logger.error(f"[ERROR] MT5 initialize failed: {mt5.last_error()}")
        return False

    # -------------------------
    # 심볼 룰(랏 규칙) 조회
    # -------------------------
    def fetch_symbol_rules(self, symbol: str, category: str = "linear") -> dict:
        if not self._ensure_mt5():
            raise RuntimeError("mt5 initialize failed")

        sym = (symbol or "").upper().strip()
        if not sym:
            raise RuntimeError("empty symbol")

        info = mt5.symbol_info(sym)
        if info is None:
            raise RuntimeError(f"symbol_info({sym}) failed: {mt5.last_error()}")

        if not info.visible:
            mt5.symbol_select(sym, True)

        # tick (for bid/ask/last)
        tick = mt5.symbol_info_tick(sym)
        bid = float(getattr(tick, "bid", 0.0) or 0.0) if tick else 0.0
        ask = float(getattr(tick, "ask", 0.0) or 0.0) if tick else 0.0
        last = float(getattr(tick, "last", 0.0) or 0.0) if tick else 0.0

        # ✅ 최소 스키마만 유지
        rules = {
            "qtyStep": float(getattr(info, "volume_step", 0.0) or 0.0),
            "minOrderQty": float(getattr(info, "volume_min", 0.0) or 0.0),
            "maxOrderQty": float(getattr(info, "volume_max", 0.0) or 0.0),
            "bid": bid,
            "ask": ask,
            "last": last,
        }

        # ✅ 보정 (기존 로직 유지)
        if rules["qtyStep"] <= 0:
            rules["qtyStep"] = 0.01
        if rules["minOrderQty"] <= 0:
            rules["minOrderQty"] = rules["qtyStep"]
        if rules["maxOrderQty"] < 0:
            rules["maxOrderQty"] = 0.0

        # ✅ 캐시 키는 항상 UPPER
        if not hasattr(self, "_symbol_rules") or not isinstance(getattr(self, "_symbol_rules", None), dict):
            self._symbol_rules = {}
        self._symbol_rules[sym] = rules
        return rules

    def get_symbol_rules(self, symbol: str) -> dict:
        sym = (symbol or "").upper().strip()
        if not sym:
            return {}
        if not hasattr(self, "_symbol_rules") or not isinstance(getattr(self, "_symbol_rules", None), dict):
            self._symbol_rules = {}
        return self._symbol_rules.get(sym) or self.fetch_symbol_rules(sym)

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
        sym = (symbol or "").upper().strip()
        rules = self.get_symbol_rules(sym) or {}

        step = float(rules.get("qtyStep") or 0.0) or 0.0
        if step <= 0:
            step = 0.01  # MT5 default

        min_qty = float(rules.get("minOrderQty") or 0.0) or 0.0
        if min_qty <= 0:
            min_qty = step

        max_qty = float(rules.get("maxOrderQty") or 0.0) or 0.0

        q = max(0.0, float(qty or 0.0))
        q = self._round_step(q, step, mode=mode)

        if q < min_qty:
            return 0.0
        if max_qty > 0 and q > max_qty:
            q = self._round_step(max_qty, step, mode="floor")
            if q < min_qty:
                return 0.0
        return float(q)

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
            ex_lot_id: int | None = None,
            deviation: int = 20,
            magic: int = 20251213,
            comment: str = "mt5-market",
            *,
            # ✅ 추가: Market closed 재시도 옵션
            retry_on_market_closed: bool = True,
            market_closed_wait_sec: float = 30.0,
            market_closed_max_retries: int = 6,  # "추가 시도 횟수" (총 시도 = 1 + retries)
    ) -> Optional[Dict[str, Any]]:
        """
        MT5 시장가 주문 전송.
        ✅ retcode=10018 (Market closed) 발생 시:
           30초 대기 후 1~2회 재시도(옵션)
        """
        if not self._ensure_mt5():
            return None

        sym = symbol.upper()
        if not mt5.symbol_select(sym, True):
            if getattr(self, "system_logger", None):
                self.system_logger.error(f"[ERROR] symbol_select({sym}) failed: {mt5.last_error()}")
            return None

        # --- 내부: '실제 1회 주문 시도'를 함수로 분리 ---
        def _try_once(*, log_fail: bool = True) -> Optional[Dict[str, Any]]:
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

                if ex_lot_id:
                    p = next((x for x in poss if int(getattr(x, "ticket", 0) or 0) == int(ex_lot_id)), None)
                    if not p:
                        if getattr(self, "system_logger", None):
                            self.system_logger.warning(
                                f"[WARN] ex_lot_id not found in positions: {sym} ex_lot_id={ex_lot_id}"
                            )
                        return None
                else:
                    targets = [p for p in poss if int(getattr(p, "type", -1)) == closing_position_type]
                    if not targets:
                        if getattr(self, "system_logger", None):
                            self.system_logger.warning(f"[WARN] reduce_only but no opposite position to close: {sym}")
                        return None
                    p = max(targets, key=lambda x: float(getattr(x, "volume", 0.0) or 0.0))

                req["position"] = int(getattr(p, "ticket", 0) or 0)
                pos_vol = float(getattr(p, "volume", 0.0) or 0.0)
                if req["volume"] > pos_vol:
                    req["volume"] = float(self.normalize_qty(sym, pos_vol, mode="floor"))
                    if req["volume"] <= 0:
                        return None

            last_res = None
            for tf in (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN):
                req["type_filling"] = tf
                res = mt5.order_send(req)
                last_res = res
                if res is None:
                    continue

                last_retcode = int(getattr(res, "retcode", -1))
                last_comment = str(getattr(res, "comment", ""))

                if last_retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
                    break

                if last_retcode == 10030 or "filling" in (last_comment or "").lower():
                    if getattr(self, "system_logger", None):
                        self.system_logger.debug(
                            f"[MT5] {sym} filling={tf} rejected: ret={last_retcode} {last_comment}")
                    continue

                break

            res = last_res
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
            order_id = int(out.get("order") or 0) or int(out.get("deal") or 0) or int(out.get("time_ms") or 0)
            out["orderId"] = str(order_id)
            out["match_hint"] = int(out.get("deal") or 0) or int(out.get("order") or 0) or None

            return out

        # --- ✅ 여기서 Market closed 재시도 ---
        attempts_total = 1 + (market_closed_max_retries if retry_on_market_closed else 0)

        last_out: Optional[Dict[str, Any]] = None
        for attempt in range(1, attempts_total + 1):
            last_out = _try_once(log_fail=False)
            if last_out is None:
                return None

            if last_out.get("ok"):
                return last_out

            retcode = int(last_out.get("retcode", -1) or -1)
            comment_s = str(last_out.get("comment", "") or "").lower()

            is_market_closed = (retcode == 10018) or ("market closed" in comment_s)
            will_retry = retry_on_market_closed and is_market_closed and attempt < attempts_total

            if not will_retry:
                # ✅ 최종 실패인 경우에만 에러 로그
                if getattr(self, "system_logger", None):
                    self.system_logger.error(f"[ERROR] mt5 order failed: {last_out}")
                return last_out

            time.sleep(float(market_closed_wait_sec))
            # 다음 시도에서 tick/price는 _try_once()가 다시 읽음

        return last_out

    def _pick_balance(self, wallet: dict) -> tuple[str, float]:
        """
        wallet에서 기준통화/잔고를 선택.
        우선순위: USD → USDT → 그 외 첫 키
        """
        if not isinstance(wallet, dict) or not wallet:
            return ("", 0.0)

        for k in ("USD", "USDT"):
            v = wallet.get(k)
            if v is not None:
                try:
                    return (k, float(v) or 0.0)
                except Exception:
                    return (k, 0.0)

        # fallback
        k0 = next(iter(wallet.keys()))
        try:
            return (str(k0), float(wallet.get(k0)) or 0.0)
        except Exception:
            return (str(k0), 0.0)

    def calc_margin(self, symbol: str, lot: float, side: str = "buy") -> float | None:
        sym = symbol.upper()
        if not mt5.initialize():
            return None

        tick = mt5.symbol_info_tick(sym)
        if not tick:
            return None

        order_type = mt5.ORDER_TYPE_BUY if side.lower() == "buy" else mt5.ORDER_TYPE_SELL
        price = float(tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid)

        # ✅ MT5 서버가 실제 규칙으로 계산해줌 (FX/CFD/고정 notional 전부 커버)
        m = mt5.order_calc_margin(order_type, sym, float(lot), price)
        return float(m) if m is not None else None

    def _mid_price(self, sym: str) -> float | None:
        sym = sym.upper()
        if not mt5.symbol_select(sym, True):
            return None
        t = mt5.symbol_info_tick(sym)
        if not t:
            return None
        bid = float(getattr(t, "bid", 0.0) or 0.0)
        ask = float(getattr(t, "ask", 0.0) or 0.0)
        if bid > 0 and ask > 0:
            return (bid + ask) / 2.0
        last = float(getattr(t, "last", 0.0) or 0.0)
        return last if last > 0 else None

    def _fx_rate(self, ccy_from: str, ccy_to: str) -> tuple[float | None, str]:
        """
        ccy_from -> ccy_to 환산 레이트(중간값)
        예: KRW -> USD면 'USDKRW' 있으면 1/price, 'KRWUSD' 있으면 price
        """
        a = (ccy_from or "").upper()
        b = (ccy_to or "").upper()
        if not a or not b or a == b:
            return (1.0, "SAME")

        # 1) 직접 페어 a+b
        sym1 = f"{a}{b}"
        p1 = self._mid_price(sym1)
        if p1 and p1 > 0:
            return (p1, sym1)

        # 2) 역페어 b+a (invert)
        sym2 = f"{b}{a}"
        p2 = self._mid_price(sym2)
        if p2 and p2 > 0:
            return (1.0 / p2, sym2 + " (invert)")

        return (None, "NOT_FOUND")

    def calc_notional_per_qty_account(self, symbol: str, side: str = "buy") -> dict | None:
        per = self.calc_notional_per_lot_account(symbol, side=side)
        if not per:
            return None
        n = float(per.get("notionalPerLotAccount") or 0.0)
        if n <= 0:
            return None
        return {
            "accountCcy": per.get("accountCcy"),
            "notionalPerQtyAccount": n,  # MT5는 qty=lot
            "method": "mt5_notionalPerLot",
            "per": per,
        }


    def calc_notional_per_lot_account(self, symbol: str, side: str = "buy") -> dict | None:
        sym = symbol.upper()
        if not self._ensure_mt5():
            return None

        info = mt5.symbol_info(sym)
        if not info:
            return None

        tick = mt5.symbol_info_tick(sym)
        if not tick:
            return None

        # 계정통화
        acc = mt5.account_info()
        account_ccy = str(getattr(acc, "currency", "") or "USD").upper()

        order_type = mt5.ORDER_TYPE_BUY if side.lower() == "buy" else mt5.ORDER_TYPE_SELL
        price = float(tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid)

        contract_size = float(getattr(info, "trade_contract_size", 0.0) or 0.0)
        if contract_size <= 0:
            contract_size = 1.0

        base_ccy = str(getattr(info, "currency_base", "") or "").upper()
        quote_ccy = str(getattr(info, "currency_profit", "") or "").upper()  # 보통 quote로 쓰기 좋음

        # 1 lot 명목(quote 통화 기준) = contract_size * price
        notional_quote = contract_size * price

        # quote -> account 환산
        rate, used = self._fx_rate(quote_ccy, account_ccy)
        if rate is None:
            # 환산 못 하면 최소한 quote 기준 값이라도 리턴
            return {
                "symbol": sym,
                "price": price,
                "contractSize": contract_size,
                "baseCcy": base_ccy,
                "quoteCcy": quote_ccy,
                "accountCcy": account_ccy,
                "notionalPerLotQuote": notional_quote,
                "notionalPerLotAccount": None,
                "fxUsed": used,
            }

        return {
            "symbol": sym,
            "price": price,
            "contractSize": contract_size,
            "baseCcy": base_ccy,
            "quoteCcy": quote_ccy,
            "accountCcy": account_ccy,
            "notionalPerLotQuote": notional_quote,
            "notionalPerLotAccount": notional_quote * rate,
            "fxUsed": used,
        }

    def _calc_raw_lot_from_percent_notional(
            self,
            symbol: str,
            price: float,  # 지금 코드 유지용(사실 tick에서 다시 읽음)
            percent: float,
            wallet: dict,
            side: str = "buy",
    ) -> tuple[float, dict]:

        cc, balance = self._pick_balance(wallet)  # 네 엔진 잔고(대개 USD/USDT)
        pct = float(percent or 0.0)

        # ✅ 목표 명목가치(계정통화 기준)
        target_notional = float(balance) * (pct / 100.0) * self.leverage

        per = self.calc_notional_per_lot_account(symbol, side=side)
        if not per or not per.get("notionalPerLotAccount"):
            return 0.0, {
                "currency": cc, "balance": float(balance), "percent": pct,
                "target_notional": target_notional,
                "error": "cannot compute notionalPerLotAccount (fx pair missing?)",
                "per": per,
            }

        notional_1lot = float(per["notionalPerLotAccount"])
        raw_lot = target_notional / notional_1lot if notional_1lot > 0 else 0.0

        meta = {
            "currency": cc,
            "balance": float(balance),
            "percent": pct,
            "target_notional": target_notional,
            "notional_1lot_account": notional_1lot,
            "raw_lot": raw_lot,
            "per": per,
            "method": "notional",
        }
        return float(raw_lot), meta

    def open_market(self, symbol, side, qty=None, **kwargs):
        qty = float(qty or 0.0)
        qty = self.normalize_qty(symbol, qty, mode="floor")

        # 1) qty 체크
        if qty <= 0:
            if getattr(self, "system_logger", None):
                self.system_logger.error(f"❌ open_market 수량 오류: {qty} ({symbol})")
            return None

        # 2) side 매핑
        s = (side or "").strip().lower()
        if s == "long":
            order_side, position_idx = "Buy", 1
        elif s == "short":
            order_side, position_idx = "Sell", 2
        else:
            if getattr(self, "system_logger", None):
                self.system_logger.error(f"❌ 알 수 없는 side 값: {side}")
            return None

        # 3) 로그
        if getattr(self, "system_logger", None):
            self.system_logger.debug(f"📥 {side.upper()} 진입 주문 전송 | qty={qty} ({symbol})")

        # 4) 주문 전송
        res = self.submit_market_order(symbol, order_side, qty, position_idx, reduce_only=False)

        # ✅ MT5는 ok=False dict를 줄 수 있으니 통일(실패면 None)
        if not res or (isinstance(res, dict) and (res.get("ok") is False)):
            return None

        res["qty"] = float(qty)
        return res

    # -------------------------
    # Bybit 스타일 래퍼: 청산
    # -------------------------
    # controllers/mt5/mt5_rest_trade.py

    def close_market(
            self,
            symbol: str,
            side: str,
            qty: float | None = None,
            *,
            ex_lot_id: int | None = None,
    ):
        sym = (symbol or "").upper()
        side_u = (side or "").upper()

        if side_u == "LONG":
            order_side, position_idx = "Sell", 1
        elif side_u == "SHORT":
            order_side, position_idx = "Buy", 2
        else:
            if getattr(self, "system_logger", None):
                self.system_logger.error(f"❌ 알 수 없는 side 값: {side}")
            return None

        # ✅ 기본: 전량청산 (ex_lot_id가 있을 때 그 티켓의 volume)
        if qty is None:
            if not ex_lot_id:
                if getattr(self, "system_logger", None):
                    self.system_logger.error("❌ qty=None 인데 ex_lot_id가 없음 (전량청산 불가)")
                return None

            if not self._ensure_mt5():
                return None
            if not mt5.symbol_select(sym, True):
                return None

            poss = mt5.positions_get(symbol=sym) or []
            p = next((x for x in poss if int(getattr(x, "ticket", 0) or 0) == int(ex_lot_id)), None)
            if not p:
                if getattr(self, "system_logger", None):
                    self.system_logger.warning(f"[WARN] ex_lot_id not found: {sym} ex_lot_id={ex_lot_id}")
                return None

            qty = float(getattr(p, "volume", 0.0) or 0.0)

        # 기존 normalize + submit
        qty = self.normalize_qty(sym, float(qty), mode="floor")
        if qty <= 0:
            if getattr(self, "system_logger", None):
                self.system_logger.warning("❗ 청산 수량이 최소단위 미만입니다. 중단.")
            return None

        if getattr(self, "system_logger", None):
            self.system_logger.debug(
                f"📤 [MT5] {side_u} 포지션 청산 시도 | qty(lot)={qty:.4f} ({sym}) ex_lot_id={ex_lot_id or 0}"
            )

        return self.submit_market_order(
            sym,
            order_side,
            qty,
            position_idx,
            reduce_only=True,
            ex_lot_id=ex_lot_id,
        )

