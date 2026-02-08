# bots/trading/trade_executor.py
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional ,List
import math
import asyncio


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

        entry_percent = float(self.deps.get_entry_percent(symbol))
        wallet = asset.get("wallet") or {}
        balance = float(wallet.get("USDT") or wallet.get("USD") or 0.0)
        leverage = float(getattr(self.rest, "leverage", 1.0))
        if price <= 0:
            return  # 가격 오류 시 중단

        raw_qty = (balance * leverage * (entry_percent / 100.0)) / price
        qty = self._normalize_qty(symbol, raw_qty, mode="floor")

        if qty <= 0:
            if self.system_logger:
                self.system_logger.info(
                    f"[OPEN] 수량 계산 결과 0 (raw={raw_qty:.6f}, norm={qty:.6f}) -> 스킵"
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


