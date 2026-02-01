# bots/reporting/status_reporter.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Callable, List, Union, Tuple

from .reporting import build_market_status_log, extract_market_status_summary, should_log_update_market

GetSymbolsFn = Callable[[], List[str]]
GetJumpStateFn = Callable[[], Dict[str, Dict[str, Any]]]
GetMaThrFn = Callable[[], Dict[str, Optional[float]]]
GetMa100Fn = Callable[[], Dict[str, Optional[float]]]
GetPriceFn = Callable[[str, float], Optional[float]]
GetMaCheckEnabledFn = Callable[[], Dict[str, bool]]
GetMinMaThrFn = Callable[[], Union[Dict[str, Optional[float]], float, None]]

# ✅ 주입 함수 타입 (간단 버전)
BuildFn = Callable[..., str]
ExtractFn = Callable[..., Dict[str, Any]]
ShouldFn = Callable[[Optional[Dict[str, Any]], Dict[str, Any]], Tuple[bool, Optional[str]]]


@dataclass
class StatusReporterDeps:
    # ✅ 필수(시장/봇 리포팅)
    get_symbols: GetSymbolsFn
    get_jump_state: GetJumpStateFn
    get_ma_threshold: GetMaThrFn
    get_now_ma100: GetMa100Fn
    get_price: GetPriceFn
    get_ma_check_enabled: GetMaCheckEnabledFn
    get_min_ma_threshold: GetMinMaThrFn
class StatusReporter:
    def __init__(
        self,
        *,
        system_logger=None,
        deps: StatusReporterDeps,
        build_fn: Optional[BuildFn] = None,
        extract_fn: Optional[ExtractFn] = None,
        should_fn: Optional[ShouldFn] = None,
    ):
        self.system_logger = system_logger
        self.deps = deps
        self._last_log_summary: Optional[Dict[str, Any]] = None

        self.build_fn = build_fn
        self.extract_fn = extract_fn
        self.should_fn = should_fn

    def tick(self, now_ts: float) -> None:

        symbols = self.deps.get_symbols() or []
        jump_state = self.deps.get_jump_state() or {}
        ma_threshold = self.deps.get_ma_threshold() or {}
        now_ma100 = self.deps.get_now_ma100() or {}
        ma_check_enabled = self.deps.get_ma_check_enabled() or {}
        min_ma_threshold = self.deps.get_min_ma_threshold()

        get_price_fn = lambda s: self.deps.get_price(s, now_ts)

        # ✅ build
        if self.build_fn:
                new_status = self.build_fn(
                    symbols = symbols,
                    jump_state = jump_state,
                    ma_threshold = ma_threshold,
                    now_ma100 = now_ma100,
                    get_price = get_price_fn,
                    ma_check_enabled = ma_check_enabled,
                    min_ma_threshold = min_ma_threshold,
                )
        else:
            new_status = build_market_status_log(
                symbols=symbols,
                jump_state=jump_state,
                ma_threshold=ma_threshold,
                now_ma100=now_ma100,
                get_price=get_price_fn,
                ma_check_enabled=ma_check_enabled,
                min_ma_threshold=min_ma_threshold,
            )

        # ✅ extract
        if self.extract_fn:
            try:
                new_summary = self.extract_fn(new_status, fallback_ma_threshold_pct=None)
            except TypeError:
                new_summary = self.extract_fn(new_status)
        else:
            new_summary = extract_market_status_summary(new_status, fallback_ma_threshold_pct=None)

        # ✅ should
        if self.should_fn:
            should, reason = self.should_fn(self._last_log_summary, new_summary)
        else:
            should, reason = should_log_update_market(self._last_log_summary, new_summary)


        if should and self.system_logger:
            self.system_logger.debug((reason or "") + new_status)
            self._last_log_summary = new_summary

