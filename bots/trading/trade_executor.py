# bots/trading/trade_executor.py
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional


@dataclass
class TradeExecutorDeps:
    is_signal_only: Callable[[], bool]
    get_asset: Callable[[], Dict[str, Any]]
    set_asset: Callable[[Dict[str, Any]], None]
    get_entry_percent: Callable[[], float]

    # lots_store hooks (keyword 호출로 통일)
    # lots.py 시그니처
    open_lot: Callable[
        ..., str]  # open_lot(symbol=..., side=..., entry_ts_ms=..., entry_price=..., qty_total=..., entry_signal_id=None) -> lot_id
    close_lot_full: Callable[..., bool]  # close_lot_full(lot_id=...) -> bool
    get_lot_qty_total: Callable[[str], Optional[float]]

    # lots_index cache hooks (주문 성공 후에만 호출)
    on_lot_open: Callable[[str, str, str, int, float, float,
                           str], None]  # (symbol, side, lot_id, entry_ts_ms, qty_total, entry_price, entry_signal_id)
    on_lot_close: Callable[[str, str, str], None]  # (symbol, side, lot_id)


class TradeExecutor:
    """
    주문 실행 + 체결 후 asset 동기화(getNsav_asset)
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
    def _get_pos_ref(asset: Dict[str, Any], symbol: str, side: str) -> Optional[Dict[str, Any]]:
        """
        asset["positions"][symbol][side] 를 안전하게 가져옴 (없으면 None)
        execute_and_sync에 넘길 포지션 ref
        """
        try:
            pos_map = asset.get("positions") or {}
            sym_map = pos_map.get(symbol) or {}
            ref = sym_map.get(side)
            return ref if isinstance(ref, dict) else None
        except Exception:
            return None

    @staticmethod
    def _ensure_pos_ref(asset: Dict[str, Any], symbol: str, side: str) -> Dict[str, Any]:
        asset.setdefault("positions", {})
        asset["positions"].setdefault(symbol, {})
        ref = asset["positions"][symbol].get(side)
        if not isinstance(ref, dict):
            ref = {"qty": 0.0}
            asset["positions"][symbol][side] = ref
        return ref

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
        asset = self.deps.get_asset()
        # 포지션 ref 안전 체크
        pos_ref = self._ensure_pos_ref(asset, symbol, side_u)

        # 실행
        await self.exec.execute_and_sync(
            self.rest.close_market,
            pos_ref,
            symbol,
            symbol,
            side=side_u,
            qty=float(qty),
        )

        # asset refresh
        new_asset = self.rest.getNsav_asset(asset=asset, symbol=symbol, save_redis=True)
        self.deps.set_asset(new_asset)

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
        entry_percent = float(self.deps.get_entry_percent())

        before_qty = self._get_pos_qty(asset, symbol, side_u)

        # ✅ 포지션 ref 안전 체크
        pos_ref = self._ensure_pos_ref(asset, symbol, side_u)

        await self.exec.execute_and_sync(
            self.rest.open_market,
            pos_ref,
            symbol,
            symbol,
            side_u,
            float(price),
            entry_percent,
            asset.get("wallet") or {},
        )

        new_asset = self.rest.getNsav_asset(asset=asset, symbol=symbol, save_redis=True)
        self.deps.set_asset(new_asset)

        after_qty = self._get_pos_qty(new_asset, symbol, side_u)
        delta = max(0.0, abs(after_qty) - abs(before_qty))

        if delta <= 0:
            if self.system_logger:
                self.system_logger.info(
                    f"[OPEN] qty 변화 없음 → lot 생성 스킵 ({symbol} {side_u} before={before_qty} after={after_qty})"
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
                )
            except Exception as e:
                if self.system_logger:
                    self.system_logger.info(f"[lots_index] on_lot_open 실패 ({lot_id}) err={e}")
