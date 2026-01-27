# bots/trading/trade_executor.py
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional
from bots.state.balances import get_total_balance_usd
import math


@dataclass
class TradeExecutorDeps:
    is_signal_only: Callable[[], bool]
    get_asset: Callable[[], Dict[str, Any]]
    set_asset: Callable[[Dict[str, Any]], None]
    get_entry_percent: Callable[[str], float]  # ✅ 심볼별
    get_max_effective_leverage: Callable[[], float]
    save_asset: Callable[[Dict[str, Any], Optional[str]], None]

    # lots_store hooks (keyword 호출로 통일)
    # lots.py 시그니처
    open_lot: Callable[
        ..., str]  # open_lot(symbol=..., side=..., entry_ts_ms=..., entry_price=..., qty_total=..., entry_signal_id=None) -> lot_id
    close_lot_full: Callable[..., bool]  # close_lot_full(lot_id=...) -> bool
    get_lot_qty_total: Callable[[str], Optional[float]]

    # lots_index cache hooks (주문 성공 후에만 호출)
    on_lot_open: Callable[[str, str, str, int, float, float, str, int], None]
    # (symbol, side, lot_id, entry_ts_ms, qty_total, entry_price, entry_signal_id, ex_lot_id)

    on_lot_close: Callable[[str, str, str], None]  # (symbol, side, lot_id)

    get_lot_ex_lot_id: Callable[[str], Optional[int]]  # ✅ 추가

class TradeExecutor:
    """
    + 주문 성공 시 lots_store + lots_index(cache) 갱신(open/close)

    설계 원칙:
    - signal layer는 lot_id를 모른다(독립). CLOSE action은 lot_id=None일 수 있다.
    - executor가 lots_index를 통해 닫을 lot을 선택한다.
    - signals(open_zset/stream 등)은 executor가 건드리지 않는다(신호/체결 분리).
    """

    def __init__(
            self,
            *,
            rest: Any,
            exec_engine: Any,
            deps: TradeExecutorDeps,
            system_logger=None,
    ):
        self.rest = rest
        self.exec = exec_engine
        self.deps = deps
        self.system_logger = system_logger

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
        total_balance = get_total_balance_usd(wallet)
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
        # 주문/저장용 float 찌꺼기 완화
        return float(f"{n * step:.12f}")

    def _normalize_qty(self, symbol: str, qty: float, mode: str = "floor") -> float:
        """
        rest._symbol_rules[symbol]에서 qtyStep/minOrderQty/maxOrderQty 를 읽어
        step 기준으로 버림 + min 미만 0 처리.
        """
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
            exit_signal_id: Optional[str] = None,  # 메타데이터(원하면 lot에 저장 가능)
    ) -> None:
        if not lot_id:
            raise ValueError("lot_id is required")

        if self.deps.is_signal_only():
            if self.system_logger:
                self.system_logger.info(f"[signal_only] CLOSE 스킵 ({symbol} {side} lot_id={lot_id})")
            return

        side_u = (side or "").upper().strip()
        if side_u not in ("LONG", "SHORT"):
            side_u = side  # 원본 유지(혹시 커스텀)

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
        # 포지션 ref 안전 체크
        pos_ref = self._ensure_pos_ref(asset, symbol, side_u)

        ex_lot_id = None
        try:
            ex_lot_id = self.deps.get_lot_ex_lot_id(lot_id)
        except Exception:
            ex_lot_id = None

        res = await self.exec.execute_and_sync(
            self.rest.close_market,
            pos_ref,
            symbol,
            symbol,
            side=side_u,
            qty=qty_n,
            ex_lot_id=ex_lot_id,
        )

        filled = (res or {}).get("_filled") if isinstance(res, dict) else None
        status = (filled or {}).get("orderStatus", "").upper()

        if status != "FILLED":
            if self.system_logger:
                self.system_logger.warning(f"[CLOSE] not filled -> keep lot (lot_id={lot_id} status={status})")
            return

        # asset refresh
        new_asset = self.rest.build_asset(asset=asset, symbol=symbol)
        self.deps.set_asset(new_asset)
        try:
            self.deps.save_asset(new_asset, symbol)
        except Exception as e:
            if self.system_logger:
                self.system_logger.error(f"[WARN] save_asset failed ({symbol}): {e}")

        # lot close (주문 성공 후)
        ok = False
        try:
            ok = bool(self.deps.close_lot_full(lot_id=lot_id))
        except Exception as e:
            if self.system_logger:
                self.system_logger.info(f"[lots_store] close_lot_full 실패 ({lot_id}) err={e}")

        # cache 반영 (close_lot_full 성공했을 때만)
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
            entry_signal_id: Optional[str] = None,  # 액션에서 넘어온 OPEN signal_id → lot에 저장
    ) -> None:
        """
        OPEN 주문 성공 후 lot OPEN 기록 + cache 반영
        - qty_total은 포지션 qty 변화량(diff)으로 추정
        """

        if self.deps.is_signal_only():
            if self.system_logger:
                self.system_logger.info(f"[signal_only] OPEN 스킵 ({symbol} {side} price={price})")
            return

        side_u = (side or "").upper().strip()
        if side_u not in ("LONG", "SHORT"):
            side_u = side

        asset = self.deps.get_asset()

        # ✅ 최대 진입(노출) 게이트: 현재 같은 방향 노출이 max_eff 이상이면 추가 진입 금지
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

        # ✅ 포지션 ref 안전 체크
        pos_ref = self._ensure_pos_ref(asset, symbol, side_u)

        res = await self.exec.execute_and_sync(
            self.rest.open_market,
            pos_ref,
            symbol,
            symbol,
            side_u,
            float(price),
            entry_percent,
            asset.get("wallet") or {},
        )

        filled = (res or {}).get("_filled") if isinstance(res, dict) else None
        status = (filled or {}).get("orderStatus", "").upper()
        if status != "FILLED":
            if self.system_logger:
                self.system_logger.warning(f"[OPEN] not filled -> skip lot (sym={symbol} status={status})")
            return

        ex_lot_id = None
        try:
            ex_lot_id = (filled or {}).get("ex_lot_id")
            ex_lot_id = int(ex_lot_id) if ex_lot_id else None
        except Exception:
            ex_lot_id = None

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

        # lot open (주문 성공 후)
        lot_id: Optional[str] = None
        try:
            lot_id = self.deps.open_lot(
                symbol=symbol,
                side=side_u,
                entry_ts_ms=entry_ts_ms,
                entry_price=float(price),
                qty_total=float(delta),
                entry_signal_id=entry_signal_id,
                ex_lot_id=ex_lot_id,   # ✅ 추가
            )
        except Exception as e:
            if self.system_logger:
                self.system_logger.info(f"[lots_store] open_lot 실패 ({symbol} {side_u}) err={e}")
            return

        # ✅ cache 반영 (open_lot 성공했을 때만)
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
                    int(ex_lot_id or 0),   # ✅ 추가
                )
            except Exception as e:
                if self.system_logger:
                    self.system_logger.info(f"[lots_index] on_lot_open 실패 ({lot_id}) err={e}")
