# bots/trading/trade_executor.py
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional ,List
import math
import asyncio


@dataclass
class MinEntryResult:
    ok: bool
    symbol: str
    wallet_ccy: str
    wallet_balance: float
    price: float
    leverage: float
    min_qty: float
    required_notional: float
    required_balance: float
    reasons: List[str]
    extra: Dict[str, Any]



@dataclass
class TradeExecutorDeps:
    get_asset: Callable[[], Dict[str, Any]]
    set_asset: Callable[[Dict[str, Any]], None]
    get_entry_percent: Callable[[str], float]
    get_max_effective_leverage: Callable[[], float]
    save_asset: Callable[[Dict[str, Any], Optional[str]], None]

    open_lot: Callable[..., str]
    close_lot_full: Callable[..., bool]
    get_lot_qty_total: Callable[[str], Optional[float]]

    on_lot_open: Callable[[str, str, str, int, float, float, str, Optional[str]], None]
    on_lot_close: Callable[[str, str, str], None]

    get_lot_ex_lot_id: Callable[[str], Optional[str]]
    lots_index: Any = None


class TradeExecutor:
    def __init__(
            self,
            *,
            rest: Any,
            deps: TradeExecutorDeps,
            system_logger=None,
            trading_logger=None,  # ✅ 추가
            engine_tag: str = "",  # ✅ engine_tag로
            taker_fee_rate: float = 0.00055,   # ✅ 엔진에 있던 설정을 여기로
    ):
        self.rest = rest
        self.deps = deps
        self.system_logger = system_logger
        self.trading_logger = trading_logger  # ✅ 추가
        self.engine_tag = (engine_tag or "").strip()

        self.TAKER_FEE_RATE = float(taker_fee_rate or 0.0)
        self._sync_lock = asyncio.Lock()  # ✅ 추가
        self._just_traded_until = 0.0

    @classmethod
    def build(
            cls,
            *,
            rest: Any,
            deps: TradeExecutorDeps,
            system_logger=None,
            trading_logger=None,
            taker_fee_rate: float = 0.00055,
            engine_tag: str = "",          # ✅ 추가
    ) -> "TradeExecutor":
        return cls(
            rest=rest,
            deps=deps,
            system_logger=system_logger,
            trading_logger=trading_logger,
            taker_fee_rate=taker_fee_rate,
            engine_tag=engine_tag,         # ✅ 핵심
        )

    def _pick_wallet_balance(self) -> tuple[str, float]:
        wallet = (self.deps.get_asset() or {}).get("wallet") or {}
        if wallet.get("USD") is not None:
            return "USD", float(wallet.get("USD") or 0.0)
        if wallet.get("USDT") is not None:
            return "USDT", float(wallet.get("USDT") or 0.0)
        k0 = next(iter(wallet.keys()), "")
        return (k0 or "ACC"), float(wallet.get(k0) or 0.0) if k0 else 0.0

    def calc_entry_qty_for_symbol(self, symbol: str, side_u: str) -> tuple[float, dict]:
        sym = symbol.upper().strip()
        ccy, bal = self._pick_wallet_balance()
        entry_percent = float(self.deps.get_entry_percent(sym) or 0.0)
        lev = float(getattr(self.rest, "leverage", 1.0) or 1.0)

        entry_notional = bal * (entry_percent / 100.0) * lev

        fn = getattr(self.rest, "calc_notional_per_qty_account", None)
        if not callable(fn):
            raise RuntimeError(f"{sym}: rest.calc_notional_per_qty_account missing")

        per = fn(sym, side="buy" if side_u == "LONG" else "sell") or {}
        n = float(per.get("notionalPerQtyAccount") or 0.0)
        if n <= 0:
            raise RuntimeError(f"{sym}: notionalPerQtyAccount invalid per={per}")

        raw = entry_notional / n
        qty = self._normalize_qty(sym, raw, mode="floor")
        return qty, {"ccy": ccy, "bal": bal, "entry_notional": entry_notional, "raw_qty": raw, "per": per}


    def _price_from_rules(self, symbol: str) -> float:
        r = self._get_rules(symbol) or {}
        bid = float(r.get("bid") or 0.0)
        ask = float(r.get("ask") or 0.0)
        last = float(r.get("last") or 0.0)

        # mid 우선
        if bid > 0 and ask > 0:
            return (bid + ask) / 2.0
        # 한쪽만 있으면 그 값
        if bid > 0:
            return bid
        if ask > 0:
            return ask
        # 마지막 fallback
        if last > 0:
            return last
        return 0.0

    def assert_min_entry_notional_ok(self, symbol: str) -> None:
        sym = (symbol or "").upper().strip()

        # 1) rules에서 min_qty 확보
        rules_fn = getattr(self.rest, "get_symbol_rules", None)
        rules = rules_fn(sym) if callable(rules_fn) else (self._get_rules(sym) or {})

        step = float(rules.get("qtyStep") or 0.0) or 0.0
        min_qty = float(rules.get("minOrderQty") or 0.0) or 0.0
        if min_qty <= 0:
            min_qty = step
        if min_qty <= 0:
            raise RuntimeError(f"[preflight] {sym}: min_qty missing (step/minOrderQty invalid) rules={rules}")

        # 2) qty 1.0 당 명목가치(계정통화)
        fn = getattr(self.rest, "calc_notional_per_qty_account", None)
        if not callable(fn):
            raise RuntimeError(f"[preflight] {sym}: rest.calc_notional_per_qty_account missing")

        per = fn(sym, side="buy") or {}
        n_per_qty = float(per.get("notionalPerQtyAccount") or 0.0)
        if n_per_qty <= 0:
            raise RuntimeError(f"[preflight] {sym}: notionalPerQtyAccount invalid per={per}")

        min_notional = n_per_qty * float(min_qty)

        # 3) 내 전략 entry_notional
        ccy, bal = self._pick_wallet_balance()
        if bal <= 0:
            raise RuntimeError(f"[preflight] {sym}: wallet empty ({ccy})")

        entry_percent = float(self.deps.get_entry_percent(sym) or 0.0)
        if entry_percent <= 0:
            raise RuntimeError(f"[preflight] {sym}: entry_percent invalid ({entry_percent})")

        lev = float(getattr(self.rest, "leverage", 1.0) or 1.0)
        if lev <= 0:
            raise RuntimeError(f"[preflight] {sym}: leverage invalid ({lev})")

        entry_notional = bal * (entry_percent / 100.0) * lev

        if entry_notional + 1e-12 < min_notional:
            raise RuntimeError(
                f"[preflight] {sym}: entry_notional too small. "
                f"entry_notional={entry_notional:.6f}({ccy}) < "
                f"min_notional={min_notional:.6f}({per.get('accountCcy') or 'ACC'}) "
                f"(min_qty={min_qty} notionalPerQty={n_per_qty:.6f})"
            )

    def calc_entry_qty_for_warmup(self, symbol: str, *, side: str = "LONG") -> tuple[float, dict]:
        sym = (symbol or "").upper().strip()

        asset = self.deps.get_asset() or {}
        wallet = asset.get("wallet") or {}

        # balance
        if wallet.get("USD") is not None:
            bal = float(wallet.get("USD") or 0.0);
            ccy = "USD"
        elif wallet.get("USDT") is not None:
            bal = float(wallet.get("USDT") or 0.0);
            ccy = "USDT"
        else:
            k0 = next(iter(wallet.keys()), "")
            bal = float(wallet.get(k0) or 0.0) if k0 else 0.0
            ccy = k0 or "ACC"

        entry_percent = float(self.deps.get_entry_percent(sym) or 0.0)
        lev = float(getattr(self.rest, "leverage", 1.0) or 1.0)

        entry_notional = bal * (entry_percent / 100.0) * lev

        # 1) MT5/CFD: notionalPerLotAccount 사용
        per_fn = getattr(self.rest, "calc_notional_per_lot_account", None)
        if callable(per_fn):
            per = per_fn(sym, side="buy" if str(side).upper() == "LONG" else "sell") or {}
            n1 = float(per.get("notionalPerLotAccount") or 0.0)
            if n1 > 0:
                raw_qty = entry_notional / n1
                norm_qty = self._normalize_qty(sym, raw_qty, mode="floor")
                return float(norm_qty), {
                    "method": "mt5_notionalPerLot",
                    "ccy": ccy,
                    "bal": bal,
                    "entry_percent": entry_percent,
                    "leverage": lev,
                    "entry_notional": entry_notional,
                    "notional_1lot": n1,
                    "raw_qty": raw_qty,
                    "accountCcy": per.get("accountCcy"),
                }

        # 2) fallback(Bybit 등): price*contractSize 기반
        rules = self._get_rules(sym) or {}
        px = float(self._price_from_rules(sym) or 0.0)
        cs = float(rules.get("contractSize") or 1.0) or 1.0
        denom = px * cs
        raw_qty = (entry_notional / denom) if denom > 0 else 0.0
        norm_qty = self._normalize_qty(sym, raw_qty, mode="floor")
        return float(norm_qty), {
            "method": "price_contractSize",
            "ccy": ccy,
            "bal": bal,
            "entry_percent": entry_percent,
            "leverage": lev,
            "entry_notional": entry_notional,
            "price": px,
            "contractSize": cs,
            "raw_qty": raw_qty,
        }


    def preflight_min_entry(self, symbol: str) -> MinEntryResult:
        sym = (symbol or "").upper().strip()
        reasons: List[str] = []

        # rules
        rules = self._get_rules(sym) or {}
        step = float(rules.get("qtyStep") or 0.0) or 0.0
        min_qty = float(rules.get("minOrderQty") or 0.0) or 0.0
        max_qty = float(rules.get("maxOrderQty") or 0.0) or 0.0

        if step <= 0:
            reasons.append("rules_step_missing")
            step = 0.0

        if min_qty <= 0:
            # 최소수량이 없으면 step을 최소수량으로 간주
            min_qty = step

        if min_qty <= 0:
            reasons.append("min_qty_missing")

        # price: rules의 bid/ask/last(mid)
        px = float(self._price_from_rules(sym) or 0.0)
        if px <= 0:
            reasons.append("price_missing")

        # leverage
        lev = float(getattr(self.rest, "leverage", 1.0) or 1.0)
        if lev <= 0:
            reasons.append("leverage_invalid")
            lev = 0.0

        # wallet balance
        asset = self.deps.get_asset() or {}
        wallet = asset.get("wallet") or {}
        wallet_ccy = "USDT" if (wallet.get("USDT") is not None) else ("USD" if (wallet.get("USD") is not None) else "")
        bal = float(wallet.get(wallet_ccy) or 0.0) if wallet_ccy else 0.0
        if bal <= 0:
            reasons.append(f"wallet_empty:{wallet_ccy or 'UNKNOWN'}")

        # required
        required_notional = 0.0
        required_balance = 0.0

        if px > 0 and min_qty > 0 and lev > 0:
            # 1) 최소 주문 수량 * 현재가(대충 mid) = 최소 명목
            required_notional = float(min_qty) * float(px)

            # 2) 네 시스템 qty 공식이 balance*leverage/price 기반이니까:
            #    required_balance = required_notional / leverage
            required_balance = required_notional / lev

            # (선택) 수수료/슬리피지 버퍼 조금
            required_balance *= (1.0 + float(self.TAKER_FEE_RATE or 0.0))

            # max_qty 체크(의미는 없지만 룰 깨졌을 때 표시)
            if max_qty > 0 and min_qty > max_qty:
                reasons.append("min_qty_gt_max_qty")

        else:
            # 이미 reasons에 다 들어감
            pass

        ok = (len(reasons) == 0) and (bal >= required_balance) and (required_balance > 0)

        if (len(reasons) == 0) and (required_balance > 0) and (bal < required_balance):
            reasons.append(f"insufficient_balance need={required_balance:.6f} have={bal:.6f}")

        return MinEntryResult(
            ok=ok,
            symbol=sym,
            wallet_ccy=wallet_ccy or "UNKNOWN",
            wallet_balance=float(bal),
            price=float(px),
            leverage=float(lev),
            min_qty=float(min_qty),
            required_notional=float(required_notional),
            required_balance=float(required_balance),
            reasons=reasons,
            extra={"rules": rules},
        )

    @staticmethod
    def _get_pos_qty(asset: Dict[str, Any], symbol: str, side: str) -> float:
        try:
            return abs(float((((asset.get("positions") or {}).get(symbol) or {}).get(side) or {}).get("qty") or 0.0))
        except Exception:
            return 0.0

    async def _execute_and_wait(
            self,
            fn,
            symbol: str,
            side: str,
            qty: float,
            *,
            action: str,  # "OPEN" | "CLOSE"
            max_retries: int = 12,
            sleep_sec: float = 0.8,
            cancel_on_timeout: bool = True,
            **kwargs
    ) -> Dict[str, Any]:
        async with self._sync_lock:
            side_u = (side or "").upper().strip()
            act_u = (action or "").upper().strip()
            if act_u not in ("OPEN", "CLOSE"):
                act_u = "OPEN"

            # ✅ 주문 전 before qty는 "live"로
            before_qty = float(self._pos_qty_live(symbol, side_u) or 0.0)

            # 1) 주문 실행
            try:
                raw = fn(symbol, side_u, qty, **kwargs)
            except Exception as e:
                if self.system_logger:
                    self.system_logger.error(f"❌ 주문 실행 예외: {e}")
                return {"ok": False, "status": "ERROR", "order_id": None, "raw": None}

            if not raw or not isinstance(raw, dict):
                if self.system_logger:
                    self.system_logger.warning("⚠️ 주문 결과가 비었습니다(또는 dict 아님).")
                return {"ok": False, "status": "EMPTY_RESULT", "order_id": None, "raw": raw}

            # 2) orderId 확보
            order_id = raw.get("orderId") or raw.get("deal") or raw.get("order")
            if not order_id:
                if self.system_logger:
                    self.system_logger.warning(
                        f"⚠️ orderId/order/deal 없음 → 체결 대기 스킵 (keys={list(raw.keys())})"
                    )
                return {"ok": False, "status": "NO_ORDER_ID", "order_id": None, "raw": raw}
            order_id = str(order_id)

            # ------------------------------------------------------------
            # 3) delta 기반 체결 대기 (여기서 _wait_fill_by_delta 인라인)
            # ------------------------------------------------------------
            eps = 1e-12
            try:
                rules_fn = getattr(self.rest, "get_symbol_rules", None)
                if callable(rules_fn):
                    r = rules_fn(symbol) or {}
                    step = float(r.get("qtyStep") or 0.0) or 0.0
                    if step > 0:
                        eps = max(step * 0.5, 1e-12)
            except Exception:
                pass


            last_cur = float(before_qty)

            filled = {}
            for i in range(int(max_retries)):
                cur = float(self._pos_qty_live(symbol, side_u) or 0.0)
                last_cur = cur

                # filled delta 계산
                if act_u == "OPEN":
                    filled_qty = max(cur - before_qty, 0.0)
                else:  # CLOSE
                    filled_qty = max(before_qty - cur, 0.0)

                if qty > 0 and (filled_qty + eps >= qty):
                    filled = {
                        "orderStatus": "FILLED",
                        "cumExecQty": float(filled_qty),
                        "beforeQty": float(before_qty),
                        "afterQty": float(cur),
                        "expectedQty": float(qty),
                    }
                    break

                if self.system_logger:
                    self.system_logger.debug(
                        f"⌛ fill-wait({act_u}) {symbol} {side_u} "
                        f"{i + 1}/{int(max_retries)} filled={filled_qty:.8f}/{qty:.8f} "
                        f"before={before_qty:.8f} cur={cur:.8f}"
                    )

                await asyncio.sleep(float(sleep_sec))

            if not filled:
                # timeout
                if act_u == "OPEN":
                    filled_qty = max(last_cur - before_qty, 0.0)
                else:
                    filled_qty = max(before_qty - last_cur, 0.0)

                filled = {
                    "orderStatus": "TIMEOUT",
                    "cumExecQty": float(filled_qty),
                    "beforeQty": float(before_qty),
                    "afterQty": float(last_cur),
                    "expectedQty": float(qty),
                }

            status = (filled.get("orderStatus") or "").upper() or "UNKNOWN"
            ex_lot_id = str(raw.get("ex_lot_id") or order_id).strip()

            if status == "FILLED":
                if self.system_logger:
                    self.system_logger.debug(f"✅ 주문 FILLED: {order_id[-6:]} ex_lot_id={ex_lot_id}")

            elif status == "TIMEOUT":
                if self.system_logger:
                    self.system_logger.warning(f"⚠️ 주문 {order_id[-6:]} 체결 대기 타임아웃")

                if cancel_on_timeout:
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
                "action": act_u,
                "side": side_u,
                "filled": filled,
                "ex_lot_id": ex_lot_id,
                "raw": raw,
            }

    def _pos_qty_live(self, symbol: str, side_u: str) -> float:
        try:
            fn = getattr(self.rest, "get_position_qty_sum", None)
            if callable(fn):
                v = fn(symbol, side_u)
                return float(v or 0.0)
        except Exception as e:
            if self.system_logger:
                self.system_logger.warning(f"[pos_qty_live] failed: {e}")
        return 0.0

    def _calc_eff_x(self, asset: Dict[str, Any], symbol: str, side: str, price: float) -> float:
        wallet = asset.get("wallet") or {}
        total_balance = float(wallet.get("USDT") or wallet.get("USD") or 0.0)
        if total_balance <= 0:
            return 0.0
        qty = self._get_pos_qty(asset, symbol, side)
        return (qty * float(price)) / float(total_balance)

    def _get_rules(self, symbol: str) -> dict:
        sym = (symbol or "").upper().strip()
        try:
            rules_map = getattr(self.rest, "_symbol_rules", None)
            if isinstance(rules_map, dict):
                return rules_map.get(sym) or {}
        except Exception:
            pass
        return {}

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
        return float(f"{n * step:.12f}")

    def _normalize_qty(self, symbol: str, qty: float, mode: str = "floor") -> float:
        rules = self._get_rules(symbol)
        q = max(0.0, float(qty or 0.0))

        step = float(rules.get("qtyStep") or rules.get("qty_step") or rules.get("step") or 0.0) or 0.0
        min_qty = float(rules.get("minOrderQty") or rules.get("min_qty") or 0.0) or 0.0
        max_qty = float(rules.get("maxOrderQty") or rules.get("max_qty") or 0.0) or 0.0

        if step <= 0:
            if self.system_logger:
                self.system_logger.info(f"[normalize_qty] missing rules/step (sym={symbol}) -> 0")
            return 0.0

        if min_qty <= 0:
            min_qty = step

        qn = self._round_step(q, step, mode=mode)

        if qn < min_qty:
            return 0.0

        if max_qty > 0 and qn > max_qty:
            qn = self._round_step(max_qty, step, mode="floor")

        return float(qn)

    def _build_asset_snapshot(self, *, asset: dict | None = None, symbol: str | None = None) -> dict:
        asset = dict(asset or {})
        wallet = dict(asset.get("wallet") or {})
        positions = dict(asset.get("positions") or {})

        # ---- 1) wallet ----
        try:
            bal_fn = getattr(self.rest, "get_account_balance", None)
            if callable(bal_fn):
                bal = bal_fn() or {}
                if isinstance(bal, dict):
                    ccy = (bal.get("currency")).strip()
                    wallet[ccy] = float(bal.get('wallet_balance') or 0.0)
        except Exception as e:
            if self.system_logger:
                self.system_logger.warning(f"[asset] wallet refresh failed: {e}")

        asset["wallet"] = wallet
        asset["positions"] = positions

        # symbol 없으면 wallet만 갱신하고 리턴
        if not symbol:
            return asset

        sym = str(symbol).upper().strip()
        positions.setdefault(sym, {"LONG": None, "SHORT": None})

        # ---- 2) qty ----
        lots_index = getattr(self.deps, "lots_index", None)

        def _qty_from_lots(side: str) -> float:
            if lots_index is None:
                return 0.0
            try:
                items = lots_index.list_open_items(sym, side) or []
                return float(sum(float(getattr(x, "qty_total", 0.0) or 0.0) for x in items))
            except Exception:
                return 0.0

        long_qty = 0.0
        short_qty = 0.0

        # ✅ lots_index가 있으면: Redis(lots) 기준이 진실
        if lots_index is not None:
            long_qty = _qty_from_lots("LONG")
            short_qty = _qty_from_lots("SHORT")

        # ---- 3) entries ----
        lots_index = getattr(self.deps, "lots_index", None)  # ✅ 이 줄 추가

        def _entries(side: str) -> List[Dict[str, Any]]:
            try:
                if lots_index is not None and hasattr(lots_index, "list_open_entries"):
                    return list(lots_index.list_open_entries(sym, side, sort_asc=True) or [])  # Bybit lots 기반
            except Exception as e:
                if self.system_logger:
                    self.system_logger.warning(f"[asset] entries build failed: {e}")
            return []

        positions[sym]["LONG"] = {"qty": long_qty, "entries": _entries("LONG")} if long_qty > 0 else None
        positions[sym]["SHORT"] = {"qty": short_qty, "entries": _entries("SHORT")} if short_qty > 0 else None
        asset["positions"] = positions
        return asset

    async def open_position(
            self,
            symbol: str,
            side: str,
            price: float,
            *,
            entry_signal_id: Optional[str] = None,
    ) -> None:
        side_u = (side or "").upper().strip()
        if side_u not in ("LONG", "SHORT"):
            side_u = side

        asset = self.deps.get_asset()

        try:
            max_eff = float(self.deps.get_max_effective_leverage() or 0.0)
        except Exception:
            max_eff = 0.0

        if max_eff > 0:
            eff_x = self._calc_eff_x(asset, symbol, side_u, float(price))
            if eff_x >= max_eff:
                if self.system_logger:
                    self.system_logger.info(
                        f"[OPEN] max_eff block ({symbol} {side_u}) eff_x={eff_x:.4f} >= max_eff={max_eff:.4f}"
                    )
                return

        # ✅ 명목가치 기반 qty 계산
        qty, qmeta = self.calc_entry_qty_for_symbol(symbol, side_u)

        if qty <= 0:
            if self.system_logger:
                self.system_logger.info(
                    f"[OPEN] qty=0 -> skip (sym={symbol} side={side_u} meta={qmeta})"
                )
            return
        res = await self._execute_and_wait(
            self.rest.open_market,
            symbol,
            side_u,
            float(qty),
            action="OPEN",
            cancel_on_timeout=True,
        )

        if not res.get("ok"):
            if self.system_logger:
                self.system_logger.warning(
                    f"[OPEN] not filled -> skip lot (sym={symbol} status={res.get('status')})"
                )
            return

        ex_lot_id = res.get("ex_lot_id")
        entry_ts_ms = int(time.time() * 1000)

        lot_id: Optional[str] = None
        try:
            lot_id = self.deps.open_lot(
                symbol=symbol,
                side=side_u,
                entry_ts_ms=entry_ts_ms,
                entry_price=float(price),
                qty_total=float(qty),
                entry_signal_id=entry_signal_id,
                ex_lot_id=ex_lot_id,
            )
        except Exception as e:
            if self.system_logger:
                self.system_logger.info(f"[lots_store] open_lot 실패 ({symbol} {side_u}) err={e}")
            return

        # cache update
        try:
            self.deps.on_lot_open(
                symbol, side_u, lot_id, entry_ts_ms, float(qty), float(price),
                entry_signal_id or "", ex_lot_id,
            )
        except Exception:
            pass

        new_asset = self._build_asset_snapshot(asset=self.deps.get_asset(), symbol=symbol)
        self.deps.set_asset(new_asset)
        try:
            self.deps.save_asset(new_asset, symbol)
        except Exception as e:
            if self.system_logger:
                self.system_logger.error(f"[WARN] save_asset failed ({symbol}): {e}")

        # ✅ FILLED 로그
        self._log_fill(
            symbol,
            logical_side=side_u,
            action="OPEN",
            lot_id=lot_id,
            ex_lot_id=ex_lot_id,
            qty=float(qty),
        )

    async def close_position(
            self,
            symbol: str,
            side: str,
            lot_id: str,
            *,
            exit_signal_id: Optional[str] = None,
    ) -> None:
        if not lot_id:
            raise ValueError("lot_id is required")

        side_u = (side or "").upper().strip()
        if side_u not in ("LONG", "SHORT"):
            side_u = side

        # 0) lot 기준 qty (Redis truth)
        qty = self.deps.get_lot_qty_total(lot_id)
        if qty is None or qty <= 0:
            if self.system_logger:
                self.system_logger.info(
                    f"[CLOSE] lot qty 없음/0 → 스킵 ({symbol} {side_u} lot_id={lot_id} qty={qty})"
                )
            return

        lot_qty_n = self._normalize_qty(symbol, float(qty), mode="floor")
        if lot_qty_n <= 0:
            if self.system_logger:
                self.system_logger.info(
                    f"[CLOSE] normalize 후 qty=0 → 스킵 ({symbol} {side_u} lot_id={lot_id} raw={qty} norm={lot_qty_n})"
                )
            return

        # 1) 거래소 live qty (남은 포지션)
        ex_before = float(self._pos_qty_live(symbol, side_u) or 0.0)

        ex_before_n = self._normalize_qty(symbol, ex_before, mode="floor")
        close_qty = min(float(lot_qty_n), float(ex_before_n))

        # 2) ex_lot_id (가능하면 전달)
        ex_lot_id = None
        try:
            ex_lot_id = self.deps.get_lot_ex_lot_id(lot_id)
        except Exception:
            ex_lot_id = None

        # 3) 거래소에 이미 포지션이 없으면: 주문 없이 lot 정리
        if close_qty <= 0:
            if ex_before <= 0:
                ok = False
                try:
                    ok = bool(self.deps.close_lot_full(lot_id=lot_id))
                except Exception as e:
                    if self.system_logger:
                        self.system_logger.info(f"[lots_store] close_lot_full 실패 ({lot_id}) err={e}")

                if ok:
                    try:
                        self.deps.on_lot_close(symbol, side_u, lot_id)
                    except Exception as e:
                        if self.system_logger:
                            self.system_logger.info(f"[lots_index] on_lot_close 실패 ({lot_id}) err={e}")

                new_asset = self._build_asset_snapshot(asset=self.deps.get_asset(), symbol=symbol)
                self.deps.set_asset(new_asset)
                try:
                    self.deps.save_asset(new_asset, symbol)
                except Exception as e:
                    if self.system_logger:
                        self.system_logger.error(f"[WARN] save_asset failed ({symbol}): {e}")

                # 로그(강제 close 느낌 원하면 메시지 바꾸면 됨)
                self._log_fill(
                    symbol,
                    logical_side=side_u,
                    action="CLOSE",
                    lot_id=lot_id,
                    ex_lot_id=ex_lot_id,
                    qty=0.0,
                )
            else:
                if self.system_logger:
                    self.system_logger.info(
                        f"[CLOSE] ex_before={ex_before:.12f} but close_qty=0 (step/minQty) -> skip order, keep lot "
                        f"(sym={symbol} {side_u} lot_id={lot_id})"
                    )
            return




        # 4) 실제 청산 주문 (✅ close_qty로!)
        res = await self._execute_and_wait(
            self.rest.close_market,
            symbol,
            side_u,
            float(close_qty),
            action="CLOSE",
            ex_lot_id=ex_lot_id,
            cancel_on_timeout=False,
        )

        if not res.get("ok"):
            if self.system_logger:
                self.system_logger.warning(
                    f"[CLOSE] not filled -> keep lot (lot_id={lot_id} status={res.get('status')})"
                )
            return

        # 5) lot 정리 (현재 구조는 full close만 지원)
        ok = False
        try:
            ok = bool(self.deps.close_lot_full(lot_id=lot_id))
        except Exception as e:
            if self.system_logger:
                self.system_logger.info(f"[lots_store] close_lot_full 실패 ({lot_id}) err={e}")

        if ok:
            try:
                self.deps.on_lot_close(symbol, side_u, lot_id)
            except Exception as e:
                if self.system_logger:
                    self.system_logger.info(f"[lots_index] on_lot_close 실패 ({lot_id}) err={e}")

        new_asset = self._build_asset_snapshot(asset=self.deps.get_asset(), symbol=symbol)
        self.deps.set_asset(new_asset)
        try:
            self.deps.save_asset(new_asset, symbol)
        except Exception as e:
            if self.system_logger:
                self.system_logger.error(f"[WARN] save_asset failed ({symbol}): {e}")

        # ✅ 로그도 실제 주문 수량(close_qty)로
        self._log_fill(
            symbol,
            logical_side=side_u,
            action="CLOSE",
            lot_id=lot_id,
            ex_lot_id=ex_lot_id,
            qty=float(close_qty),
        )

    def _short_id(self, v: Any) -> str:
        s = str(v).strip() if v is not None else ""
        return s[:6] if s else "UNKNOWN"

    def _log_fill(
            self,
            symbol: str,
            *,
            logical_side: str,  # "LONG"/"SHORT"
            action: str,  # "OPEN"/"CLOSE"
            lot_id: Optional[str] = None,
            ex_lot_id: Optional[str] = None,
            qty: float = 0.0,
    ) -> None:
        if not self.trading_logger:
            return

        side_u = (logical_side or "").upper().strip()
        act_u = (action or "").upper().strip()
        if side_u not in ("LONG", "SHORT"):
            return
        if act_u not in ("OPEN", "CLOSE"):
            return

        engine = self.engine_tag or "UNKNOWN"
        tag = f"[{engine}][{symbol}]"

        lot_s = self._short_id(lot_id)
        ex_s = self._short_id(ex_lot_id)

        q = float(qty or 0.0)
        qty_str = f"{q:.8f}".rstrip("0").rstrip(".") if q else "0"

        if act_u == "OPEN":
            self.trading_logger.info(
                f"{tag} ⊕ {side_u} 진입 완료 | lot:{lot_s} | ex:{ex_s} | qty:{qty_str}"
            )
        else:
            self.trading_logger.info(
                f"{tag} ⊖ {side_u} 청산 완료 | lot:{lot_s} | ex:{ex_s} | qty:{qty_str}"
            )


