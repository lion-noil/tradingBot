# bots/trading/trade_executor.py
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional
import math

from core.execution import ExecutionEngine  # ✅ 추가


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



class TradeExecutor:
    def __init__(
            self,
            *,
            rest: Any,
            exec_engine: Any,
            deps: TradeExecutorDeps,
            system_logger=None,
            trading_logger=None,  # ✅ 추가
            engine_tag: str = "",  # ✅ engine_tag로
    ):
        self.rest = rest
        self.exec = exec_engine
        self.deps = deps
        self.system_logger = system_logger
        self.trading_logger = trading_logger  # ✅ 추가
        self.engine_tag = (engine_tag or "").strip()

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
        """
        ✅ TradeExecutor 하나만 만들면 내부에서 ExecutionEngine까지 구성
        """
        exec_engine = ExecutionEngine(
            rest,
            system_logger=system_logger,
            trading_logger=trading_logger,
            taker_fee_rate=taker_fee_rate,
        )
        return cls(
            rest=rest,
            exec_engine=exec_engine,
            deps=deps,
            system_logger=system_logger,
            trading_logger=trading_logger,
            engine_tag=engine_tag,         # ✅ 핵심
        )

    @staticmethod
    def _get_pos_qty(asset: Dict[str, Any], symbol: str, side: str) -> float:
        try:
            return abs(float((((asset.get("positions") or {}).get(symbol) or {}).get(side) or {}).get("qty") or 0.0))
        except Exception:
            return 0.0

    @staticmethod
    def _ensure_pos_ref(asset: Dict[str, Any], symbol: str, side: str) -> Dict[str, Any]:
        asset.setdefault("positions", {})
        asset["positions"].setdefault(symbol, {})
        ref = asset["positions"][symbol].get(side)
        if not isinstance(ref, dict):
            ref = {"qty": 0.0}
            asset["positions"][symbol][side] = ref
        return ref

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

        qty = self.deps.get_lot_qty_total(lot_id)
        if qty is None or qty <= 0:
            if self.system_logger:
                self.system_logger.info(
                    f"[CLOSE] lot qty 없음/0 → 스킵 ({symbol} {side_u} lot_id={lot_id} qty={qty})"
                )
            return

        qty_n = self._normalize_qty(symbol, float(qty), mode="floor")
        if qty_n <= 0:
            if self.system_logger:
                self.system_logger.info(
                    f"[CLOSE] normalize 후 qty=0 → 스킵 ({symbol} {side_u} lot_id={lot_id} raw={qty} norm={qty_n})"
                )
            return

        asset = self.deps.get_asset()
        pos_ref = self._ensure_pos_ref(asset, symbol, side_u)

        ex_lot_id = None
        try:
            ex_lot_id = self.deps.get_lot_ex_lot_id(lot_id)
        except Exception:
            ex_lot_id = None

        res = await self.exec.execute_and_sync(
            self.rest.close_market,
            symbol,
            side_u,
            qty_n,
            ex_lot_id=ex_lot_id,
        )

        if not res.get("ok"):
            if self.system_logger:
                self.system_logger.warning(
                    f"[CLOSE] not filled -> keep lot (lot_id={lot_id} status={res.get('status')})"
                )
            return

        filled = res.get("filled") or {}

        # ✅ FILLED 로그
        self._log_fill(
            symbol,
            filled,
            logical_side=side_u,
            action="CLOSE",
            position_detail=pos_ref,
        )

        # asset refresh
        new_asset = self.rest.build_asset(asset=asset, symbol=symbol)
        self.deps.set_asset(new_asset)
        try:
            self.deps.save_asset(new_asset, symbol)
        except Exception as e:
            if self.system_logger:
                self.system_logger.error(f"[WARN] save_asset failed ({symbol}): {e}")

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
        before_qty = self._get_pos_qty(asset, symbol, side_u)
        pos_ref = self._ensure_pos_ref(asset, symbol, side_u)

        # ✅ open_market(position_detail, symbol, side, price, entry_percent, wallet)
        res = await self.exec.execute_and_sync(
            self.rest.open_market,
            symbol,
            side_u,
            float(price),
            entry_percent,
            asset.get("wallet") or {},
        )

        if not res.get("ok"):
            if self.system_logger:
                self.system_logger.warning(
                    f"[OPEN] not filled -> skip lot (sym={symbol} status={res.get('status')})"
                )
            return

        filled = res.get("filled") or {}
        ex_lot_id = res.get("ex_lot_id")

        # ✅ FILLED 로그
        self._log_fill(
            symbol,
            filled,
            logical_side=side_u,
            action="OPEN",
            position_detail=pos_ref,
        )

        new_asset = self.rest.build_asset(asset=asset, symbol=symbol)
        self.deps.set_asset(new_asset)
        try:
            self.deps.save_asset(new_asset, symbol)
        except Exception as e:
            if self.system_logger:
                self.system_logger.error(f"[WARN] save_asset failed ({symbol}): {e}")

        after_qty = self._get_pos_qty(new_asset, symbol, side_u)
        raw_delta = max(0.0, abs(after_qty) - abs(before_qty))
        delta = self._normalize_qty(symbol, raw_delta, mode="floor")

        if delta <= 0:
            if self.system_logger:
                self.system_logger.info(
                    f"[OPEN] qty 변화 없음/normalize 후 0 → lot 생성 스킵 ({symbol} {side_u} raw={raw_delta} norm={delta} before={before_qty} after={after_qty})"
                )
            return

        entry_ts_ms = int(time.time() * 1000)

        lot_id: Optional[str] = None
        try:
            lot_id = self.deps.open_lot(
                symbol=symbol,
                side=side_u,
                entry_ts_ms=entry_ts_ms,
                entry_price=float(price),
                qty_total=float(delta),
                entry_signal_id=entry_signal_id,
                ex_lot_id=ex_lot_id,
            )
        except Exception as e:
            if self.system_logger:
                self.system_logger.info(f"[lots_store] open_lot 실패 ({symbol} {side_u}) err={e}")
            return

        if lot_id:
            try:
                self.deps.on_lot_open(
                    symbol,
                    side_u,
                    lot_id,
                    entry_ts_ms,
                    float(delta),
                    float(price),
                    entry_signal_id or "",
                    ex_lot_id,
                )
            except Exception as e:
                if self.system_logger:
                    self.system_logger.info(f"[lots_index] on_lot_open 실패 ({lot_id}) err={e}")

    # ---- logging helpers ----
    def _classify_intent(self, filled: dict) -> Optional[str]:
        side = (filled.get("side") or "").upper()  # BUY/SELL
        pos = int(filled.get("positionIdx") or 0)  # 1/2
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
        v = filled.get("ex_lot_id")
        s = str(v).strip() if v is not None else ""
        return s[:6] if s else "UNKNOWN"

    def _log_fill(
            self,
            symbol: str,
            filled: dict,
            *,
            logical_side: str,  # "LONG" / "SHORT"
            action: str,  # "OPEN" / "CLOSE"
            position_detail: Optional[dict] = None,
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

        ex_lot_id = self._short_ex_lot_id(filled)

        filled_avg_price = float(filled.get("avgPrice") or 0.0)
        exec_qty = float(filled.get("cumExecQty") or filled.get("qty") or 0.0)
        qty_str = f"{exec_qty:.8f}".rstrip("0").rstrip(".") if exec_qty else "0"

        # OPEN
        if act_u == "OPEN":
            self.trading_logger.info(
                f"{tag} ⊕ {side_u} 진입 완료 | ex_lot_id:{ex_lot_id} | avg:{filled_avg_price:.2f} | qty:{qty_str}"
            )
            return

        # CLOSE
        avg_price = 0.0
        if isinstance(position_detail, dict):
            avg_price = float(position_detail.get("avg_price") or 0.0)

        if avg_price > 0:
            self.trading_logger.info(
                f"{tag} ⊖ {side_u} 청산 완료 | ex_lot_id:{ex_lot_id} | avg:{avg_price:.2f} -> filled:{filled_avg_price:.2f} | qty:{qty_str}"
            )
        else:
            self.trading_logger.info(
                f"{tag} ⊖ {side_u} 청산 완료 | ex_lot_id:{ex_lot_id} | filled:{filled_avg_price:.2f} | qty:{qty_str}"
            )

