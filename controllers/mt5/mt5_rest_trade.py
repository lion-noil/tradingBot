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

        order_id = int(out.get("deal") or out.get("order") or 0)
        if order_id <= 0:
            order_id = int(out.get("time_ms") or int(time.time() * 1000))

        out["orderId"] = str(order_id)  # ✅ 엔진이 바로 찾게
        out["result"] = {"orderId": str(order_id)}  # ✅ Bybit 스타일 호환(엔진이 result를 볼 수도 있어서)

        if not ok and getattr(self, "system_logger", None):
            self.system_logger.error(f"[ERROR] mt5 order failed: {out}")

        # ✅✅✅ 핵심: 성공이면 즉시 로컬 기록 저장
        self._record_trade_if_possible(out)

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

    def _calc_raw_lot_from_percent(
            self,
            symbol: str,
            price: float,
            percent: float,
            wallet: dict,
            side: str = "buy",
    ) -> tuple[float, dict]:
        cc, balance = self._pick_balance(wallet)

        sym = symbol.upper()
        pct = float(percent or 0.0)
        p = float(price or 0.0)

        # ✅ 목표는 "노출"이 아니라 "사용할 마진"
        target_margin = float(balance) * (pct / 100.0) * self.leverage

        # ✅ 1 lot 마진(서버 규칙 그대로)
        order_type = mt5.ORDER_TYPE_BUY if side.lower() == "buy" else mt5.ORDER_TYPE_SELL
        margin_1lot = mt5.order_calc_margin(order_type, sym, 1.0, p)

        raw_lot = 0.0
        if margin_1lot and float(margin_1lot) > 0:
            raw_lot = target_margin / float(margin_1lot)

        meta = {
            "currency": cc,
            "balance": float(balance),
            "percent": pct,
            "price": p,
            "target_margin": target_margin,
            "margin_1lot": float(margin_1lot) if margin_1lot else None,
            "raw_lot": raw_lot,
            "method": "order_calc_margin",
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
            self.system_logger.info(
                f"📥 [MT5] {side.upper()} 진입 | {sym} lot={qty:.4f} (raw={raw_lot_val:.6f}) "
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
            self.system_logger.info(f"📤 [MT5] {side.upper()} 포지션 청산 시도 | qty(lot)={qty:.4f} ({symbol})")

        return self.submit_market_order(symbol, order_side, qty, position_idx, reduce_only=True)


    # -------------------------
    # 주문 취소
    # -------------------------
    def cancel_order(self, symbol: str, order_id: str | int):
        """
        MT5용 주문 취소.
        - MT5에서는 'pending order'만 취소 가능(지정가/스탑 등).
        - 시장가 DEAL은 취소 개념이 거의 없음(이미 체결 시도).
        Bybit 스타일 유사 응답을 리턴한다.
        """
        if not self._ensure_mt5():
            return {"ok": False, "orderId": str(order_id), "orderStatus": "REJECTED",
                    "comment": "mt5 initialize failed"}

        sym = (symbol or "").upper()
        oid = int(order_id) if str(order_id).isdigit() else 0
        if oid <= 0:
            return {"ok": False, "orderId": str(order_id), "orderStatus": "REJECTED", "comment": "invalid order_id"}

        # 1) pending order 존재 확인
        try:
            # MT5 python은 orders_get(ticket=...) 지원
            orders = mt5.orders_get(ticket=oid) or []
        except Exception:
            orders = []

        if not orders:
            # 이미 체결됐거나(딜), 존재하지 않거나, 다른 심볼일 수 있음
            return {
                "ok": True,
                "orderId": str(order_id),
                "orderStatus": "NOT_FOUND",
                "comment": "no pending order found (maybe filled/canceled/already dealt)",
                "symbol": sym,
            }

        # 2) pending 취소 시도 (TRADE_ACTION_REMOVE)
        req = {
            "action": mt5.TRADE_ACTION_REMOVE,
            "order": oid,
            "symbol": sym,
            "comment": "mt5-cancel",
        }

        res = mt5.order_send(req)
        if res is None:
            return {"ok": False, "orderId": str(order_id), "orderStatus": "REJECTED",
                    "comment": f"order_send None: {mt5.last_error()}"}

        retcode = int(getattr(res, "retcode", -1))
        ok = retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED)

        return {
            "ok": bool(ok),
            "orderId": str(order_id),
            "orderStatus": "CANCELLED" if ok else "REJECTED",
            "retcode": retcode,
            "comment": str(getattr(res, "comment", "")),
            "symbol": sym,
        }

    def wait_order_fill(
            self,
            symbol: str,
            order_id: str | int,
            *,
            expected: str = "OPEN",  # ✅ "OPEN" or "CLOSE"
            side: str | None = None,  # ✅ "LONG"/"SHORT" (가능하면 넘겨라)
            before_qty: float | None = None,  # ✅ CLOSE에서 핵심
            max_retries: int = 10,
            sleep_sec: float = 1.0,
    ):
        if not self._ensure_mt5():
            return {"orderId": str(order_id), "orderStatus": "REJECTED", "comment": "mt5 initialize failed"}

        sym = (symbol or "").upper()
        oid = str(order_id)

        exp = (expected or "OPEN").upper()
        s = (side or "").upper() if side else None

        # order_id가 MT5 ticket일 수도 있으니 int로도 들고 있음
        oid_int = 0
        try:
            oid_int = int(float(oid))
        except Exception:
            oid_int = 0

        # ✅ BEFORE가 안 들어오면 현재 값을 기준으로 잡아버림(최소 방어)
        if before_qty is None:
            before_qty = self._get_position_qty(sym, s)

        for i in range(max_retries):
            # 1) ✅ 딜 히스토리로 체결 확인 (UTC 추천)
            try:
                dt_to = datetime.now(timezone.utc)
                dt_from = dt_to - timedelta(minutes=5)
                try:
                    deals = mt5.history_deals_get(dt_from, dt_to, group=sym) or []
                except TypeError:
                    deals = mt5.history_deals_get(dt_from, dt_to) or []
            except Exception:
                deals = []

            for d in reversed(deals):
                try:
                    dsym = str(getattr(d, "symbol", "") or "").upper()
                    if dsym != sym:
                        continue
                    deal_ticket = int(getattr(d, "ticket", 0) or 0)
                    deal_order = int(getattr(d, "order", 0) or 0)
                    if oid_int and (deal_ticket == oid_int or deal_order == oid_int):
                        return {
                            "orderId": oid,
                            "orderStatus": "FILLED",
                            "symbol": sym,
                            "deal": deal_ticket,
                            "order": deal_order,
                        }
                except Exception:
                    continue

            # 2) ✅ 포지션 변화로 판정
            cur_qty = self._get_position_qty(sym, s)

            if exp == "OPEN":
                # OPEN은 증가/생성되면 체결로 본다
                if cur_qty > (before_qty + 1e-12):
                    return {"orderId": oid, "orderStatus": "FILLED", "symbol": sym, "beforeQty": before_qty,
                            "afterQty": cur_qty}

            else:  # CLOSE
                # CLOSE는 감소/소멸되면 체결로 본다
                if cur_qty < (before_qty - 1e-12):
                    return {"orderId": oid, "orderStatus": "FILLED", "symbol": sym, "beforeQty": before_qty,
                            "afterQty": cur_qty}
                # 완전 청산 기대면 0 근처도 인정
                if before_qty > 0 and cur_qty <= 1e-12:
                    return {"orderId": oid, "orderStatus": "FILLED", "symbol": sym, "beforeQty": before_qty,
                            "afterQty": cur_qty}

            if getattr(self, "system_logger", None):
                self.system_logger.debug(
                    f"⌛ [MT5] 체결 대기중... ({i + 1}/{max_retries}) | {sym} exp={exp} {before_qty:.4f}->{cur_qty:.4f}"
                )
            time.sleep(sleep_sec)

        return {"orderId": oid, "orderStatus": "TIMEOUT", "symbol": sym, "expected": exp, "beforeQty": before_qty}