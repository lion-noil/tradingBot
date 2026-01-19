# controllers/mt5/mt5_rest_trade.py
from __future__ import annotations

import math
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any
from datetime import datetime, timedelta, timezone
import MetaTrader5 as mt5

KST = timezone(timedelta(hours=9))


class Mt5RestTradeMixin:
    # -------------------------
    # 내부: MT5 연결 보장
    # -------------------------

    def _get_position_qty(self, symbol: str, side: str | None = None) -> float:
        """
        side:
          - None: 심볼 전체 포지션 수량 합(절대값 합)
          - "LONG"/"SHORT": 방향별 합
        """
        sym = (symbol or "").upper()
        s = (side or "").upper()

        poss = mt5.positions_get(symbol=sym) or []
        total = 0.0
        for p in poss:
            try:
                vol = float(getattr(p, "volume", 0.0) or 0.0)
                ptype = int(getattr(p, "type", -1))  # 0=BUY, 1=SELL (MT5)
            except Exception:
                continue

            if s == "LONG":
                if ptype == mt5.POSITION_TYPE_BUY:
                    total += vol
            elif s == "SHORT":
                if ptype == mt5.POSITION_TYPE_SELL:
                    total += vol
            else:
                total += abs(vol)
        return float(total)


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

        sym = symbol.upper()
        info = mt5.symbol_info(sym)
        if info is None:
            raise RuntimeError(f"symbol_info({sym}) failed: {mt5.last_error()}")

        if not info.visible:
            mt5.symbol_select(sym, True)

        # tickSize
        tick_size = float(getattr(info, "point", 0.0) or 0.0)
        if tick_size <= 0:
            digits = int(getattr(info, "digits", 0) or 0)
            tick_size = 10 ** (-digits) if digits > 0 else 0.0

        # contractSize (fallback)
        contract_size = float(getattr(info, "trade_contract_size", 0.0) or 0.0)
        if contract_size <= 0:
            contract_size = 1.0

        # tick (optional but useful)
        tick = mt5.symbol_info_tick(sym)
        bid = float(getattr(tick, "bid", 0.0) or 0.0) if tick else 0.0
        ask = float(getattr(tick, "ask", 0.0) or 0.0) if tick else 0.0
        last = float(getattr(tick, "last", 0.0) or 0.0) if tick else 0.0

        rules = {
            "qtyStep": float(getattr(info, "volume_step", 0.0) or 0.0),
            "minOrderQty": float(getattr(info, "volume_min", 0.0) or 0.0),
            "maxOrderQty": float(getattr(info, "volume_max", 0.0) or 0.0),

            "tickSize": tick_size,
            "minPrice": 0.0,
            "maxPrice": 0.0,

            "digits": int(getattr(info, "digits", 0) or 0),
            "contractSize": contract_size,
            "currencyProfit": str(getattr(info, "currency_profit", "") or ""),
            "currencyMargin": str(getattr(info, "currency_margin", "") or ""),

            # optional
            "bid": bid,
            "ask": ask,
            "last": last,
        }

        if rules["qtyStep"] <= 0:
            rules["qtyStep"] = 0.01
        if rules["minOrderQty"] <= 0:
            rules["minOrderQty"] = rules["qtyStep"]

        self._symbol_rules[sym] = rules
        return rules

    def get_symbol_rules(self, symbol: str) -> dict:
        return self._symbol_rules.get(symbol) or self.fetch_symbol_rules(symbol)

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
        rules = self.get_symbol_rules(symbol) or {}
        step = float(rules.get("qtyStep") or 0.01) or 0.01
        min_qty = float(rules.get("minOrderQty") or step) or step
        max_qty = float(rules.get("maxOrderQty") or 0.0) or 0.0

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
        ex_lot_id: int | None = None,   # ✅ 추가
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

            # 1) ex_lot_id가 있으면 그 포지션만 대상으로
            if ex_lot_id:
                p = next((x for x in poss if int(getattr(x, "ticket", 0) or 0) == int(ex_lot_id)), None)
                if not p:
                    if getattr(self, "system_logger", None):
                        self.system_logger.warning(
                            f"[WARN] ex_lot_id not found in positions: {sym} ex_lot_id={ex_lot_id}")
                    return None
            else:
                # 2) 기존 로직: 반대 포지션 중 가장 큰 1개
                targets = [p for p in poss if int(getattr(p, "type", -1)) == closing_position_type]
                if not targets:
                    if getattr(self, "system_logger", None):
                        self.system_logger.warning(f"[WARN] reduce_only but no opposite position to close: {sym}")
                    return None
                p = max(targets, key=lambda x: float(getattr(x, "volume", 0.0) or 0.0))

            # ✅ 지정된 포지션 ticket으로 닫기
            req["position"] = int(getattr(p, "ticket", 0) or 0)

            pos_vol = float(getattr(p, "volume", 0.0) or 0.0)
            if vol > pos_vol:
                req["volume"] = float(self.normalize_qty(sym, pos_vol, mode="floor"))
                if req["volume"] <= 0:
                    return None

        for tf in (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN):
            req["type_filling"] = tf
            res = mt5.order_send(req)
            last_res = res

            if res is None:
                continue

            last_retcode = int(getattr(res, "retcode", -1))
            last_comment = str(getattr(res, "comment", ""))

            # 성공 코드면 즉시 종료
            if last_retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
                break

            # filling mode 미지원이면 다음 모드로 계속
            if last_retcode == 10030 or "filling" in (last_comment or "").lower():
                if getattr(self, "system_logger", None):
                    self.system_logger.debug(f"[MT5] {sym} filling={tf} rejected: ret={last_retcode} {last_comment}")
                continue

            # 그 외 실패는 루프 끊고 실패 처리
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
        if not ok and getattr(self, "system_logger", None):
            self.system_logger.error(f"[ERROR] mt5 order failed: {out}")
        return out

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


    def open_market(self, symbol: str, side: str, price: float, percent: float, wallet: dict):
        if not symbol or wallet is None:
            if getattr(self, "system_logger", None):
                self.system_logger.error("❌ symbol 또는 wallet 정보가 누락되었습니다.")
            return None

        sym = symbol.upper()
        side_norm = (side or "").strip().lower()

        # 1) side 확정
        if side_norm == "long":
            order_side = "Buy"
            position_idx = 1
            side2 = "buy"
        elif side_norm == "short":
            order_side = "Sell"
            position_idx = 2
            side2 = "sell"
        else:
            if getattr(self, "system_logger", None):
                self.system_logger.error(f"❌ 알 수 없는 side 값: {side}")
            return None

        # 2) price 확보(없으면 tick에서)
        p = float(price or 0.0)
        if p <= 0:
            if not self._ensure_mt5():
                return None
            if not mt5.symbol_select(sym, True):
                if getattr(self, "system_logger", None):
                    self.system_logger.error(f"[ERROR] symbol_select({sym}) failed: {mt5.last_error()}")
                return None
            tick = mt5.symbol_info_tick(sym)
            if not tick:
                if getattr(self, "system_logger", None):
                    self.system_logger.error(f"[ERROR] symbol_info_tick({sym}) failed: {mt5.last_error()}")
                return None
            p = float(tick.ask if side2 == "buy" else tick.bid)

        # 3) 명목 기준 percent -> raw lot 계산
        raw_lot, meta = self._calc_raw_lot_from_percent_notional(sym, p, percent, wallet, side=side2)

        # meta 안전 접근
        raw_lot_val = float(meta.get("raw_lot") or raw_lot or 0.0)
        qty = self.normalize_qty(sym, raw_lot_val, mode="floor")

        if qty <= 0:
            if getattr(self, "system_logger", None):
                per = meta.get("per") or {}
                acct_ccy = per.get("accountCcy") or meta.get("currency") or ""
                # 환산 실패/페어 없음 같은 경우 meta에 error 들어있도록 만들어둔 상태일 것
                err = meta.get("error") or ""
                self.system_logger.error(
                    f"❗ 주문 수량이 최소단위 미만이거나 계산 실패. "
                    f"raw_lot={raw_lot_val:.8f} norm_lot={qty:.8f} "
                    f"(sym={sym} price={p:.5f} pct={float(meta.get('percent') or percent):.4f} "
                    f"target_notional≈{float(meta.get('target_notional') or 0.0):.2f}{acct_ccy} "
                    f"fx={per.get('fxUsed') or 'N/A'} {('err=' + err) if err else ''})"
                )
            return None

        # 4) 진짜 “명목/마진 추정치” 로그 (다심볼 대응)
        per = meta.get("per") or {}
        acct_ccy = per.get("accountCcy") or meta.get("currency") or ""
        fx_used = per.get("fxUsed") or "N/A"

        # 4-1) 계정통화 기준 1lot 명목이 있으면 그걸로, 없으면 quote 기준이라도
        est_notional = None
        try:
            n1_acc = meta.get("notional_1lot_account")
            if n1_acc is not None:
                est_notional = float(n1_acc) * float(qty)
            else:
                # fallback: quote 기준(환산 불가한 케이스)
                n1_q = per.get("notionalPerLotQuote")
                qccy = per.get("quoteCcy") or ""
                if n1_q is not None:
                    est_notional = float(n1_q) * float(qty)
                    acct_ccy = qccy or acct_ccy  # 표시 통화 fallback
        except Exception:
            est_notional = None

        # 4-2) 마진은 MT5 서버 계산이 제일 정확
        est_margin = None
        try:
            est_margin = self.calc_margin(sym, float(qty), side=side2)
        except Exception:
            est_margin = None

        if getattr(self, "system_logger", None):
            self.system_logger.debug(
                f"📥 [MT5] {side.upper()} 진입 시도 | {sym} lot={qty:.4f} (raw={raw_lot_val:.6f}) "
                f"@{float(per.get('price') or p):.5f} "
                f"target_notional≈{float(meta.get('target_notional') or 0.0):.2f}{acct_ccy} "
                f"1lot_notional≈{float(meta.get('notional_1lot_account') or 0.0):.2f}{acct_ccy} "
                f"est_notional≈{(float(est_notional) if est_notional is not None else 0.0):.2f}{acct_ccy} "
                f"est_margin≈{(float(est_margin) if est_margin is not None else 0.0):.2f}{acct_ccy} "
                f"fx={fx_used}"
            )

        return self.submit_market_order(sym, order_side, qty, position_idx, reduce_only=False)

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

    def wait_order_fill(
            self,
            symbol: str,
            order_id: str | int,
            *,
            expected: str = "OPEN",  # "OPEN" or "CLOSE"
            side: str | None = None,  # "LONG" / "SHORT"
            before_qty: float | None = None,  # 주문 직전 qty
            match_hint: int | None = None,  # deal ticket (가능하면)
            expected_qty: float | None = None,  # 요청 lot
            max_retries: int = 10,
            sleep_sec: float = 0.5,
    ):
        """
        MT5 체결 확인 (전량판정=포지션 qty delta, 부가정보=deals에서 보조)

        - 전량체결 판정: positions_get 기반 delta >= expected_qty
        - ex_lot_id/avgPrice/deal: history_deals_get에서 match_hint(deal.ticket)로 보조 추출
        """
        if not self._ensure_mt5():
            return {"orderId": str(order_id), "orderStatus": "REJECTED", "comment": "mt5 initialize failed"}

        sym = (symbol or "").upper()
        oid = str(order_id)
        exp = (expected or "OPEN").upper()
        s = (side or "").upper() if side else None

        # before_qty
        if before_qty is None:
            before_qty = self._get_position_qty(sym, s)

        # positionIdx / reduceOnly
        pos_idx = 1 if s == "LONG" else 2 if s == "SHORT" else 0
        reduce_only = (exp == "CLOSE")

        # qtyStep 기반 eps + expected_qty 정규화(중요)
        try:
            rules = self.get_symbol_rules(sym) or {}
            step = float(rules.get("qtyStep") or 0.01) or 0.01
        except Exception:
            step = 0.01
        eps = max(step * 0.5, 1e-8)

        target_qty = 0.0
        if expected_qty is not None:
            # float 오차 제거: step 기준으로 반올림 정규화
            target_qty = float(self.normalize_qty(sym, float(expected_qty), mode="round") or 0.0)

        # deal에서 뽑을 보조정보
        last_ex_lot_id = 0
        last_avg_price = 0.0
        last_deal_ticket = 0
        last_seen = {"minutes": 0, "deals": 0}

        def _get_dt_to() -> datetime:
            now = datetime.now()
            try:
                tick = mt5.symbol_info_tick(sym)
                if tick and getattr(tick, "time", 0):
                    tick_dt = datetime.fromtimestamp(int(tick.time))
                    if tick_dt >= now - timedelta(minutes=2):
                        return tick_dt
            except Exception:
                pass
            return now

        def _update_from_deals(minutes: int = 60) -> None:
            """match_hint(deal ticket)가 있을 때만 deal에서 ex_lot_id/avgPrice 갱신."""
            nonlocal last_ex_lot_id, last_avg_price, last_deal_ticket, last_seen

            if not match_hint:
                return

            dt_to = _get_dt_to()
            dt_from = dt_to - timedelta(minutes=minutes)
            deals = mt5.history_deals_get(dt_from, dt_to) or []
            deals = [d for d in deals if (getattr(d, "symbol", "") or "").upper() == sym]
            last_seen = {"minutes": minutes, "deals": len(deals)}
            if not deals:
                return

            # entry 필터(OPEN=IN(0), CLOSE=OUT(1))
            want_entry = 0 if exp == "OPEN" else 1

            matched = []
            mh = int(match_hint or 0)
            for d in deals:
                try:
                    if int(getattr(d, "entry", -999)) != want_entry:
                        continue
                    if int(getattr(d, "ticket", 0) or 0) == mh:
                        matched.append(d)
                except Exception:
                    continue

            if not matched:
                return

            # (deal ticket는 보통 1개지만) 방어적으로 평균가/수량도 계산
            total_qty = 0.0
            total_px_qty = 0.0
            pos_ticket = 0
            deal_ticket = 0

            for d in matched:
                try:
                    qty = float(getattr(d, "volume", 0.0) or 0.0)
                    px = float(getattr(d, "price", 0.0) or 0.0)
                    if qty > 0:
                        total_qty += qty
                        total_px_qty += px * qty
                    deal_ticket = int(getattr(d, "ticket", 0) or 0) or deal_ticket

                    pid = (
                            getattr(d, "position_id", 0)
                            or getattr(d, "position", 0)
                            or getattr(d, "position_by_id", 0)
                            or 0
                    )
                    pid = int(pid or 0)
                    if pid > 0:
                        pos_ticket = pid
                except Exception:
                    continue

            if total_qty > 0:
                last_avg_price = float(total_px_qty / total_qty)
            if deal_ticket > 0:
                last_deal_ticket = deal_ticket
            if pos_ticket > 0:
                last_ex_lot_id = pos_ticket

        def _filled_qty(cur_qty: float) -> float:
            if exp == "OPEN":
                return max(cur_qty - before_qty, 0.0)
            return max(before_qty - cur_qty, 0.0)

        # ---- loop ----
        # 거래 직후면 5~20분이면 충분한데, 서버 지연 대비로 60분까지 한 번만 훑자
        deal_windows = (5, 20)
        post_fill_poll_sec = 0.2

        for i in range(max_retries):
            # deal 보조정보 업데이트
            for m in deal_windows:
                _update_from_deals(minutes=m)

            # 전량판정(핵심)
            cur_qty = self._get_position_qty(sym, s)
            filled_qty = _filled_qty(cur_qty)

            if target_qty > 0 and (filled_qty + eps >= target_qty):
                # ✅ 전량체결은 확정. 이제 ex_lot_id만 짧게 더 기다려본다.
                deadline = time.time() + 10
                while time.time() < deadline:
                    for m in (5, 20):
                        _update_from_deals(minutes=m)

                    if last_ex_lot_id > 0:
                        break

                    time.sleep(post_fill_poll_sec)

                return {
                    "orderId": oid,
                    "orderStatus": "FILLED",
                    "symbol": sym,
                    "deal": int(last_deal_ticket or 0),
                    "ex_lot_id": int(last_ex_lot_id or 0),
                    "positionIdx": pos_idx,
                    "reduceOnly": reduce_only,
                    "side": "BUY" if (s == "LONG" and exp == "OPEN") or (s == "SHORT" and exp == "CLOSE") else "SELL",
                    "avgPrice": float(last_avg_price or 0.0),
                    "cumExecQty": float(filled_qty),
                    "beforeQty": float(before_qty),
                    "afterQty": float(cur_qty),
                    "expectedQty": float(target_qty),
                    "match_hint": match_hint or 0,
                }

            if getattr(self, "system_logger", None):
                self.system_logger.debug(
                    f"⌛ [MT5] 체결 대기중... ({i + 1}/{max_retries}) {sym} exp={exp} side={s} "
                    f"qty: before={before_qty:.8f} after={cur_qty:.8f} filled≈{filled_qty:.8f}/{target_qty:.8f} "
                    f"match_hint={int(match_hint or 0)} ex_lot_id={last_ex_lot_id} "
                    f"last_deals={last_seen['deals']}@{last_seen['minutes']}m"
                )

            time.sleep(sleep_sec)

        return {
            "orderId": oid,
            "orderStatus": "TIMEOUT",
            "symbol": sym,
            "expected": exp,
            "beforeQty": float(before_qty),
            "afterQty": float(self._get_position_qty(sym, s)),
            "match_hint": int(match_hint or 0),
            "expectedQty": float(target_qty),
            "last_ex_lot_id": int(last_ex_lot_id or 0),
            "last_deals_count": int(last_seen["deals"] or 0),
            "last_window_min": int(last_seen["minutes"] or 0),
        }



