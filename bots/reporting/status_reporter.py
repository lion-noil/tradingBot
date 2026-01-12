# bots/reporting/status_reporter.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Callable, List, Union

from .reporting import build_full_status_log, extract_status_summary, should_log_update
from ..state.balances import get_total_balance_and_ccy


GetWalletFn = Callable[[], Dict[str, Any]]
GetSymbolsFn = Callable[[], List[str]]
GetJumpStateFn = Callable[[], Dict[str, Dict[str, Any]]]
GetMaThrFn = Callable[[], Dict[str, Optional[float]]]
GetMa100Fn = Callable[[], Dict[str, Optional[float]]]
GetPositionsFn = Callable[[], Dict[str, Dict[str, Any]]]
GetPriceFn = Callable[[str, float], Optional[float]]
GetTakerFeeFn = Callable[[], float]

# ✅ 추가
GetMaCheckEnabledFn = Callable[[], Dict[str, bool]]
GetMinMaThrFn = Callable[[], Union[Dict[str, Optional[float]], float, None]]


@dataclass
class StatusReporterDeps:
    get_wallet: GetWalletFn
    get_symbols: GetSymbolsFn
    get_jump_state: GetJumpStateFn
    get_ma_threshold: GetMaThrFn
    get_now_ma100: GetMa100Fn
    get_positions_by_symbol: GetPositionsFn
    get_price: GetPriceFn
    get_taker_fee_rate: GetTakerFeeFn

    # ✅ 추가: disabled 표시/토글 감지에 필요
    get_ma_check_enabled: GetMaCheckEnabledFn
    get_min_ma_threshold: GetMinMaThrFn


class StatusReporter:
    def __init__(self, *, system_logger=None, deps: StatusReporterDeps):
        self.system_logger = system_logger
        self.deps = deps
        self._last_log_snapshot: Optional[str] = None
        self._last_log_summary: Optional[Dict[str, Any]] = None
        self._last_log_reason: Optional[str] = None

    def tick(self, now_ts: float) -> None:
        wallet = self.deps.get_wallet() or {}
        total_balance, ccy = get_total_balance_and_ccy(wallet)

        symbols = self.deps.get_symbols()
        jump_state = self.deps.get_jump_state()
        ma_threshold = self.deps.get_ma_threshold()
        now_ma100 = self.deps.get_now_ma100()
        positions_by_symbol = self.deps.get_positions_by_symbol()
        taker_fee_rate = float(self.deps.get_taker_fee_rate() or 0.0)

        # ✅ 추가
        ma_check_enabled = self.deps.get_ma_check_enabled() or {}
        min_ma_threshold = self.deps.get_min_ma_threshold()

        new_status = build_full_status_log(
            total_usdt=float(total_balance),
            currency=ccy,
            symbols=symbols,
            jump_state=jump_state,
            ma_threshold=ma_threshold,
            now_ma100=now_ma100,
            get_price=lambda s: self.deps.get_price(s, now_ts),
            positions_by_symbol=positions_by_symbol,
            taker_fee_rate=taker_fee_rate,
            # ✅ 핵심: reporting.py에 전달
            ma_check_enabled=ma_check_enabled,
            min_ma_threshold=min_ma_threshold,
        )

        new_summary = extract_status_summary(new_status, fallback_ma_threshold_pct=None)
        should, reason = should_log_update(self._last_log_summary, new_summary)
        if should:
            if self.system_logger:
                self.system_logger.debug((reason or "") + new_status)
            self._last_log_snapshot = new_status
            self._last_log_summary = new_summary
            self._last_log_reason = reason
