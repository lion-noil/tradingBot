# bots/trading/trade_executor.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable, Dict


@dataclass
class TradeExecutorDeps:
    is_signal_only: Callable[[], bool]
    get_asset: Callable[[], Dict[str, Any]]
    set_asset: Callable[[Dict[str, Any]], None]
    get_entry_percent: Callable[[], float]


class TradeExecutor:
    """
    주문 실행 + 체결 후 asset 동기화(getNsav_asset) 책임만 가진다.
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

    async def close_position(self, symbol: str, side: str, qty: float) -> None:
        if self.deps.is_signal_only():
            if self.system_logger:
                self.system_logger.info(f"[signal_only] CLOSE 스킵 ({symbol} {side} qty={qty})")
            return

        asset = self.deps.get_asset()
        await self.exec.execute_and_sync(
            self.rest.close_market,
            asset["positions"][symbol][side],
            symbol,
            symbol,
            side=side,
            qty=qty,
        )

        new_asset = self.rest.getNsav_asset(asset=asset, symbol=symbol, save_redis=True)
        self.deps.set_asset(new_asset)

    async def open_position(self, symbol: str, side: str, price: float) -> None:
        if self.deps.is_signal_only():
            if self.system_logger:
                self.system_logger.info(f"[signal_only] OPEN 스킵 ({symbol} {side} price={price})")
            return

        asset = self.deps.get_asset()
        entry_percent = float(self.deps.get_entry_percent())

        await self.exec.execute_and_sync(
            self.rest.open_market,
            asset["positions"][symbol][side.upper()],
            symbol,
            symbol,
            side,
            price,
            entry_percent,
            asset["wallet"],
        )

        new_asset = self.rest.getNsav_asset(asset=asset, symbol=symbol, save_redis=True)
        self.deps.set_asset(new_asset)
