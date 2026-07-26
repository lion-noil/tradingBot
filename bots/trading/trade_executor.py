# bots/trading/trade_executor.py
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, List
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
    get_entry_percent: Callable[..., float]  # (symbol) 또는 (symbol, strategy) — 후자로 (전략,심볼)별 %
    get_max_effective_leverage: Callable[[], float]
    save_asset: Callable[[Dict[str, Any], Optional[str]], None]
    save_trade_record: Callable[[Dict[str, Any]], None]

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
            taker_fee_rate: float = 0.00055,  # ✅ 엔진에 있던 설정을 여기로
    ):
        self.rest = rest
        self.deps = deps
        self.system_logger = system_logger
        self.trading_logger = trading_logger  # ✅ 추가
        self.engine_tag = (engine_tag or "").strip()

        self.TAKER_FEE_RATE = float(taker_fee_rate or 0.0)
        self._sync_lock = asyncio.Lock()  # ✅ 추가
        self._just_traded_until = 0.0
        # ✅ 장닫힘(10018) 개장대기 진입 재시도 — entry_signal_id -> asyncio.Task
        self._pending_open_retries: Dict[str, asyncio.Task] = {}
        # ✅ 장닫힘(10018) 개장대기 청산 재시도 — lot_id -> asyncio.Task (2026-07-27)
        #   시그널 측은 EXIT '기록' 시점에 오픈 장부에서 제거(재발행 없음) → 휴장 중 EXIT가
        #   미체결이면 고아 랏 발생(7/27 USDCHF·USDCAD 실사례). 청산 재시도는 executor가 책임진다.
        self._pending_close_retries: Dict[str, asyncio.Task] = {}

    @classmethod
    def build(
            cls,
            *,
            rest: Any,
            deps: TradeExecutorDeps,
            system_logger=None,
            trading_logger=None,
            taker_fee_rate: float = 0.00055,
            engine_tag: str = "",  # ✅ 추가
    ) -> "TradeExecutor":
        return cls(
            rest=rest,
            deps=deps,
            system_logger=system_logger,
            trading_logger=trading_logger,
            taker_fee_rate=taker_fee_rate,
            engine_tag=engine_tag,  # ✅ 핵심
        )

    def _pick_wallet_balance(self) -> tuple[str, float]:
        wallet = (self.deps.get_asset() or {}).get("wallet") or {}
        if wallet.get("USD") is not None:
            return "USD", float(wallet.get("USD") or 0.0)
        if wallet.get("USDT") is not None:
            return "USDT", float(wallet.get("USDT") or 0.0)
        k0 = next(iter(wallet.keys()), "")
        return (k0 or "ACC"), float(wallet.get(k0) or 0.0) if k0 else 0.0

    ENTRY_MAX_MULT = 16   # 1진입 단계 상향 상한: base×16까지 (소액 자본 최소주문 대응).
    #   최소주문 처음 충족하는 최소 단계에서 멈춤 → 포지션은 항상 min-qty 크기(상한↑=진입가능 심볼↑, 크기↑ 아님).
    #   예: BTCUSDT(Bybit) 잔고 443·2%base → mult7(≈0.001 BTC)에서 최소주문 충족. (4로는 미달=skip이었음)
    #   16으로 상향(2026-07-14): MT5 XAUUSD(min_notional≈4020 USD)가 s11m 1%base×8(0.16%)로도 미달 →
    #     ×16(0.32%)에서 충족. 단, XAUUSD 실매매는 s3/s4(2%base)라 실제론 mult≈5에서 진입됨.

    def calc_entry_qty_for_symbol(self, symbol: str, side_u: str, *, strategy: Optional[str] = None) -> tuple[float, dict]:
        sym = symbol.upper().strip()
        ccy, bal = self._pick_wallet_balance()
        base_pct = float(self.deps.get_entry_percent(sym, strategy) or 0.0)
        lev = float(getattr(self.rest, "leverage", 1.0) or 1.0)

        fn = getattr(self.rest, "calc_notional_per_qty_account", None)
        if not callable(fn):
            raise RuntimeError(f"{sym}: rest.calc_notional_per_qty_account missing")
        per = fn(sym, side="buy" if side_u == "LONG" else "sell") or {}
        n = float(per.get("notionalPerQtyAccount") or 0.0)
        if n <= 0:
            raise RuntimeError(f"{sym}: notionalPerQtyAccount invalid per={per}")

        # 최소수량
        rules = self._get_rules(sym) or {}
        min_qty = float(rules.get("minOrderQty") or 0.0) or float(rules.get("qtyStep") or 0.0) or 0.0

        # base×1..8 단계 상향: 최소수량 충족하는 최소 단계 채택. 8배로도 미달이면 0(skip).
        qty, used_pct, raw = 0.0, base_pct, 0.0
        for mult in range(1, int(self.ENTRY_MAX_MULT) + 1):
            pct = base_pct * mult
            raw_m = (bal * (pct / 100.0) * lev) / n
            q = self._normalize_qty(sym, raw_m, mode="floor", log_below_min=False)
            if q > 0 and (min_qty <= 0 or q + 1e-12 >= min_qty):
                qty, used_pct, raw = q, pct, raw_m
                break

        entry_notional = bal * (used_pct / 100.0) * lev
        meta = {"ccy": ccy, "bal": bal, "entry_notional": entry_notional,
                "raw_qty": raw, "per": per, "entry_percent_used": used_pct}

        if qty <= 0:
            # 스케일업 실패 사유를 meta에 남김 — [OPEN] qty=0 skip 로그에서 원인 즉시 확인용
            raw_1 = (bal * (base_pct / 100.0) * lev) / n
            needed_mult = (min_qty / raw_1) if raw_1 > 0 else float("inf")
            meta["skip_reason"] = (
                f"min_qty={min_qty} needs base×{needed_mult:.1f} "
                f"> ENTRY_MAX_MULT={self.ENTRY_MAX_MULT} (base_pct={base_pct} bal={bal:.2f})"
            )
            if self.system_logger:
                self.system_logger.warning(
                    f"[entry-scaleup] {sym} {side_u}: 최소주문 미달 — {meta['skip_reason']}"
                )
        return qty, meta

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

    def assert_min_entry_notional_ok(self, symbol: str, *, entry_percent: Optional[float] = None) -> None:
        # entry_percent 미지정 시 전역/심볼 % 사용. 전략별 %가 있는 엔진은 호출측에서
        # 최소(가장 보수적인) 전략 %를 넘겨야 실제 진입 skip 가능성과 경보가 일치함.
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

        if entry_percent is None:
            entry_percent = float(self.deps.get_entry_percent(sym) or 0.0)
        entry_percent = float(entry_percent or 0.0)
        if entry_percent <= 0:
            raise RuntimeError(f"[preflight] {sym}: entry_percent invalid ({entry_percent})")

        lev = float(getattr(self.rest, "leverage", 1.0) or 1.0)
        if lev <= 0:
            raise RuntimeError(f"[preflight] {sym}: leverage invalid ({lev})")

        # 1진입은 base→최대 base×ENTRY_MAX_MULT(=8배)까지 상향 가능 → 최대치로도 최소 못 넘기면 진입 불가
        max_pct = entry_percent * float(self.ENTRY_MAX_MULT)
        entry_notional_max = bal * (max_pct / 100.0) * lev

        if entry_notional_max + 1e-12 < min_notional:
            raise RuntimeError(
                f"[preflight] {sym}: 최대 {max_pct:.2f}%로도 최소주문 미달. "
                f"max_notional={entry_notional_max:.2f}({ccy}) < "
                f"min_notional={min_notional:.2f}({per.get('accountCcy') or 'ACC'}) "
                f"(min_qty={min_qty} notionalPerQty={n_per_qty:.2f})"
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
                norm_qty = self._normalize_qty(sym, raw_qty, mode="floor", log_below_min=False)
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
        norm_qty = self._normalize_qty(sym, raw_qty, mode="floor", log_below_min=False)
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
        # HKD/JPY 등 non-USD 심볼은 price를 그대로 쓰면 FX 배율만큼 부풀어오름.
        # fetch_symbol_rules에서 미리 계산된 USD 환산값(notionalPerLotAccount)이 있으면 사용.
        rules = self._get_rules(symbol)
        n_per_lot = float(rules.get("notionalPerLotAccount") or 0.0)
        if n_per_lot > 0:
            return (qty * n_per_lot) / float(total_balance)
        return (qty * float(price)) / float(total_balance)

    def _get_rules(self, symbol: str) -> dict:
        sym = (symbol or "").upper().strip()
        try:
            rules_map = getattr(self.rest, "_symbol_rules", None)
            if isinstance(rules_map, dict):
                result = rules_map.get(sym)
                if result is not None:
                    return result
                # rules는 broker 심볼 키로 저장되므로 (e.g. ETHUSD -> #ETHUSD)
                broker_fn = getattr(self.rest, "_broker_sym", None)
                if callable(broker_fn):
                    bsym = broker_fn(sym)
                    if bsym and bsym != sym:
                        result = rules_map.get(bsym)
                        if result is not None:
                            return result
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

    def _normalize_qty(self, symbol: str, qty: float, mode: str = "floor",
                       log_below_min: bool = True) -> float:
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
            # log_below_min=False: 워밍업 기준수량 계산·스케일업 래더의 중간 단계 등
            # '실패가 아닌' 호출 — 로그 오탐 방지(진짜 스킵은 [OPEN] qty=0/[entry-scaleup]가 남김)
            if log_below_min and self.system_logger:
                self.system_logger.info(
                    f"[normalize_qty] below min_qty (sym={symbol} raw={q:.6f} step={step} qn={qn:.6f} min={min_qty:.6f}) -> 0"
                )
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

        # ---- 4) 실시간 손익/명목가치 (MT5만, 있으면) ----
        # MT5는 심볼별 계약크기가 달라 (가격−진입가)×수량 근사가 크게 틀림(특히 FX).
        # 브로커가 계산한 정확한 profit/명목가치를 붙여 프론트가 그대로 표시하게 함.
        metrics_fn = getattr(self.rest, "get_position_metrics", None)
        if callable(metrics_fn):
            try:
                m = metrics_fn(sym) or {}
                for side in ("LONG", "SHORT"):
                    pos = positions[sym].get(side)
                    side_m = m.get(side)
                    if pos and isinstance(side_m, dict):
                        if side_m.get("pnl") is not None:
                            pos["pnl"] = side_m["pnl"]
                        if side_m.get("value") is not None:
                            pos["value"] = side_m["value"]
            except Exception as e:
                if self.system_logger:
                    self.system_logger.warning(f"[asset] position metrics failed ({sym}): {e}")

        asset["positions"] = positions
        return asset

    # 장닫힘(10018) 개장대기 재시도 — 주초 개장 갭(월 07:0x KST WTI 등)에서 1회성 진입 신호 유실 방지.
    #   submit_market_order(_sync_lock 안)는 15s×1만 재시도하므로, 락 밖 asyncio 태스크로 길게 커버.
    MC_RETRY_DELAY_SEC = 180
    MC_RETRY_MAX = 10          # 3분×10회 = 개장까지 최대 ~30분 대기

    def _maybe_schedule_open_retry(
            self,
            symbol: str,
            side_u: str,
            price: float,
            entry_signal_id: Optional[str],
            strategy: Optional[str],
            attempt: int,
    ) -> None:
        """미체결 진입이 '방금 장닫힘(10018) 거절' 때문이면 개장대기 재시도 예약.
        rest(MT5)가 last_market_closed_reject를 마킹한 경우에만 동작 — Bybit 등은 no-op."""
        if not entry_signal_id:
            return
        sym_u = (symbol or "").upper().strip()
        rej = getattr(self.rest, "last_market_closed_reject", None) or {}
        if rej.get("symbol") != sym_u or (time.time() - float(rej.get("ts") or 0)) > 60:
            return
        if attempt >= self.MC_RETRY_MAX:
            if self.system_logger:
                self.system_logger.warning(
                    f"[OPEN] 개장대기 재시도 소진 — 진입 포기 (sym={sym_u} {side_u} attempts={attempt})"
                )
            return
        key = str(entry_signal_id)
        old = self._pending_open_retries.pop(key, None)
        if old and not old.done():
            old.cancel()
        self._pending_open_retries[key] = asyncio.create_task(
            self._mc_retry_open(sym_u, side_u, float(price), key, strategy, attempt + 1)
        )
        if self.system_logger:
            self.system_logger.warning(
                f"[OPEN] {sym_u} 장닫힘(10018) — {self.MC_RETRY_DELAY_SEC}s 후 개장대기 재시도 "
                f"({attempt + 1}/{self.MC_RETRY_MAX})"
            )

    async def _mc_retry_open(
            self, symbol: str, side_u: str, price: float,
            entry_signal_id: str, strategy: Optional[str], attempt: int,
    ) -> None:
        try:
            await asyncio.sleep(self.MC_RETRY_DELAY_SEC)
        except asyncio.CancelledError:
            return
        self._pending_open_retries.pop(entry_signal_id, None)
        await self.open_position(
            symbol, side_u, price,
            entry_signal_id=entry_signal_id, strategy=strategy, _mc_retry=attempt,
        )

    def cancel_pending_open_retry(self, entry_signal_id: Optional[str]) -> bool:
        """해당 진입 시그널의 EXIT가 먼저 도착한 경우 대기중 재시도 취소 — 고아 포지션 방지."""
        t = self._pending_open_retries.pop(str(entry_signal_id or ""), None)
        if t and not t.done():
            t.cancel()
            return True
        return False

    def _maybe_schedule_close_retry(
            self,
            symbol: str,
            side_u: str,
            lot_id: str,
            exit_signal_id: Optional[str],
            exit_price: Optional[float],
            close_open_signal_id: Optional[str],
            attempt: int,
    ) -> None:
        """미체결 청산이 '방금 장닫힘(10018) 거절' 때문이면 개장대기 재시도 예약(진입과 대칭).
        시그널 측은 EXIT를 재발행하지 않으므로(기록 즉시 오픈 장부 제거) 여기서 끝까지 책임진다."""
        sym_u = (symbol or "").upper().strip()
        rej = getattr(self.rest, "last_market_closed_reject", None) or {}
        if rej.get("symbol") != sym_u or (time.time() - float(rej.get("ts") or 0)) > 60:
            return
        if attempt >= self.MC_RETRY_MAX:
            if self.system_logger:
                self.system_logger.warning(
                    f"[CLOSE] 개장대기 재시도 소진 — 랏 유지 (sym={sym_u} {side_u} lot_id={lot_id} attempts={attempt})"
                )
            return
        key = str(lot_id)
        old = self._pending_close_retries.pop(key, None)
        if old and not old.done():
            old.cancel()
        self._pending_close_retries[key] = asyncio.create_task(
            self._mc_retry_close(sym_u, side_u, key, exit_signal_id, exit_price,
                                 close_open_signal_id, attempt + 1)
        )
        if self.system_logger:
            self.system_logger.warning(
                f"[CLOSE] {sym_u} 장닫힘(10018) — {self.MC_RETRY_DELAY_SEC}s 후 개장대기 청산 재시도 "
                f"({attempt + 1}/{self.MC_RETRY_MAX}) lot_id={lot_id}"
            )

    async def _mc_retry_close(
            self, symbol: str, side_u: str, lot_id: str,
            exit_signal_id: Optional[str], exit_price: Optional[float],
            close_open_signal_id: Optional[str], attempt: int,
    ) -> None:
        try:
            await asyncio.sleep(self.MC_RETRY_DELAY_SEC)
        except asyncio.CancelledError:
            return
        self._pending_close_retries.pop(lot_id, None)
        await self.close_position(
            symbol, side_u, lot_id,
            exit_signal_id=exit_signal_id, exit_price=exit_price,
            close_open_signal_id=close_open_signal_id, _mc_retry=attempt,
        )

    async def open_position(
            self,
            symbol: str,
            side: str,
            price: float,
            *,
            entry_signal_id: Optional[str] = None,
            strategy: Optional[str] = None,  # ✅ (전략,심볼)별 진입% 조회용
            _mc_retry: int = 0,   # 내부용: 개장대기 재시도 회차
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

        # ✅ 명목가치 기반 qty 계산 (전략별 진입% 반영)
        qty, qmeta = self.calc_entry_qty_for_symbol(symbol, side_u, strategy=strategy)

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

        # ✅ 장부는 요청수량이 아니라 실체결 수량(포지션 델타)을 기록해야
        #    거래소-장부 불일치(더스트)가 누적되지 않는다.
        filled_info = res.get("filled") or {}
        exec_qty = float(filled_info.get("cumExecQty") or 0.0)
        step = float((self._get_rules(symbol) or {}).get("qtyStep") or 0.0)
        eps = max(step * 0.5, 1e-12)

        if not res.get("ok"):
            # 타임아웃이라도 부분체결(1스텝 이상)이 있으면 체결분만큼 lot을 기록해
            # 고아 포지션(장부 없는 거래소 잔량) 생성을 막는다.
            if exec_qty < max(step, 1e-12):
                if self.system_logger:
                    self.system_logger.warning(
                        f"[OPEN] not filled -> skip lot (sym={symbol} status={res.get('status')})"
                    )
                self._maybe_schedule_open_retry(symbol, side_u, float(price), entry_signal_id, strategy, _mc_retry)
                return
            if self.system_logger:
                self.system_logger.warning(
                    f"[OPEN] 부분체결 감지 — 체결분만 lot 기록 (sym={symbol} {side_u} "
                    f"filled={exec_qty} req={qty} status={res.get('status')})"
                )

        if exec_qty > 0 and abs(exec_qty - float(qty)) > eps and self.system_logger:
            self.system_logger.warning(
                f"[OPEN] 요청/체결 수량 불일치 — 장부는 체결분 기록 (sym={symbol} {side_u} "
                f"req={qty} filled={exec_qty})"
            )
        if _mc_retry > 0 and self.system_logger:
            self.system_logger.warning(
                f"[OPEN] 개장대기 재시도 성공 (sym={symbol} {side_u} attempt={_mc_retry})"
            )
        rec_qty = exec_qty if exec_qty > 0 else float(qty)
        # 포지션 델타(뺄셈)의 float 노이즈 제거 — 비정규 수량이 장부에 기록되면
        # 청산 floor에서 또 더스트가 생기므로 반드시 스텝에 반올림 정렬한다.
        if step > 0:
            rec_qty = self._round_step(rec_qty, step, mode="round")

        ex_lot_id = res.get("ex_lot_id")
        entry_ts_ms = int(time.time() * 1000)

        lot_id: Optional[str] = None
        try:
            lot_id = self.deps.open_lot(
                symbol=symbol,
                side=side_u,
                entry_ts_ms=entry_ts_ms,
                entry_price=float(price),
                qty_total=float(rec_qty),
                entry_signal_id=entry_signal_id,
                ex_lot_id=ex_lot_id,
            )
        except Exception as e:
            if self.system_logger:
                self.system_logger.info(f"[lots_store] open_lot 실패 ({symbol} {side_u}) err={e}")
            return

        try:
            self.deps.save_trade_record({
                "kind": "ENTRY",
                "symbol": symbol,
                "side": side_u,
                "qty": float(rec_qty),
                "price": float(price),
                "entry_price": float(price),
                "ts_ms": entry_ts_ms,
                "signal_id": entry_signal_id,
                "entry_signal_id": entry_signal_id,
                "lot_id": lot_id,
                "ex_lot_id": ex_lot_id,
                "engine": self.engine_tag,
                "fee_rate": self.TAKER_FEE_RATE,
            })
        except Exception as e:
            if self.system_logger:
                self.system_logger.warning(f"[trade_record] ENTRY save failed ({symbol} {side_u}) err={e}")

        # cache update
        try:
            self.deps.on_lot_open(
                symbol, side_u, lot_id, entry_ts_ms, float(rec_qty), float(price),
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
            qty=float(rec_qty),
        )

    async def close_position(
            self,
            symbol: str,
            side: str,
            lot_id: str,
            *,
            exit_signal_id: Optional[str] = None,
            exit_price: Optional[float] = None,
            close_open_signal_id: Optional[str] = None,
            _mc_retry: int = 0,   # 내부용: 개장대기 청산 재시도 회차
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
            # ✅ 장닫힘(10018)이면 개장대기 재시도 — 시그널 측 재발행이 없으므로 여기서 완결
            self._maybe_schedule_close_retry(
                symbol, side_u, lot_id, exit_signal_id, exit_price,
                close_open_signal_id, attempt=_mc_retry,
            )
            return

        if _mc_retry and self.system_logger:
            self.system_logger.warning(
                f"[CLOSE] 개장대기 청산 재시도 성공 (sym={symbol} {side_u} lot_id={lot_id} attempt={_mc_retry})"
            )

        # ✅ trade_records: EXIT 기록 저장
        try:
            exit_price_f = float(exit_price or 0.0)
            entry_price_f = 0.0

            # asset snapshot의 entries에서 lot_id로 entry_price 찾기
            try:
                asset_now = self.deps.get_asset() or {}
                pos = ((asset_now.get("positions") or {}).get(symbol) or {}).get(side_u) or {}
                entries = pos.get("entries") or []

                for e in entries:
                    if str(e.get("lot_id") or "") == str(lot_id):
                        entry_price_f = float(e.get("price") or e.get("entry_price") or 0.0)
                        break
            except Exception:
                entry_price_f = 0.0

            # fallback: entry_price를 못 찾으면 PnL 계산은 하지 않음
            gross_pnl_usdt = None
            fee_usdt = None
            pnl_usdt = None

            if entry_price_f > 0 and exit_price_f > 0 and float(close_qty) > 0:
                if side_u == "LONG":
                    gross_pnl_usdt = (exit_price_f - entry_price_f) * float(close_qty)
                else:
                    gross_pnl_usdt = (entry_price_f - exit_price_f) * float(close_qty)

                fee_usdt = (entry_price_f * float(close_qty) + exit_price_f * float(close_qty)) * float(self.TAKER_FEE_RATE)
                pnl_usdt = gross_pnl_usdt - fee_usdt

            self.deps.save_trade_record({
                "kind": "EXIT",
                "symbol": symbol,
                "side": side_u,
                "qty": float(close_qty),
                "price": exit_price_f,
                "entry_price": entry_price_f,
                "exit_price": exit_price_f,
                "gross_pnl_usdt": gross_pnl_usdt,
                "fee_usdt": fee_usdt,
                "pnl_usdt": pnl_usdt,
                "fee_rate": self.TAKER_FEE_RATE,
                "ts_ms": int(time.time() * 1000),
                "signal_id": exit_signal_id,
                "exit_signal_id": exit_signal_id,
                "close_open_signal_id": close_open_signal_id,
                "entry_signal_id": close_open_signal_id,
                "lot_id": lot_id,
                "ex_lot_id": ex_lot_id,
                "engine": self.engine_tag,
            })
        except Exception as e:
            if self.system_logger:
                self.system_logger.warning(f"[trade_record] EXIT save failed ({symbol} {side_u} lot={lot_id}) err={e}")

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

        # ✅ 더스트 스윕: 이 (symbol, side)의 장부 lot이 전부 소진됐는데 거래소에 소량 잔량이
        #    남아있으면(과거 요청/체결 불일치·비정규 수량의 잔재) 즉시 정리한다.
        try:
            self._sweep_residual_after_close(symbol, side_u)
        except Exception as e:
            if self.system_logger:
                self.system_logger.warning(f"[dust-sweep] failed ({symbol} {side_u}) err={e}")

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

    def _sweep_residual_after_close(self, symbol: str, side_u: str) -> None:
        """장부(lots) 합계와 거래소 실포지션을 대조해 잔량 더스트를 정리한다.

        - lot이 하나라도 남아있으면 주문하지 않는다(진행 중 체결과의 경합 방지) — 경고만.
        - lot 합계가 0인데 거래소 잔량이 있으면: 더스트 수준(최소주문 vs 3스텝 중 큰 쪽 이하)일 때만
          reduce-only 시장가로 정리. 그 이상 크기의 고아 포지션은 자동 청산하지 않고 경고 로그.
        """
        lots_index = getattr(self.deps, "lots_index", None)
        if lots_index is None:
            return
        try:
            items = lots_index.list_open_items(symbol, side_u) or []
            lots_sum = float(sum(float(getattr(x, "qty_total", 0.0) or 0.0) for x in items))
        except Exception:
            return

        live = float(self._pos_qty_live(symbol, side_u) or 0.0)
        residual = live - lots_sum

        rules = self._get_rules(symbol) or {}
        step = float(rules.get("qtyStep") or 0.0) or 0.0
        if step <= 0 or residual < step - 1e-12:
            return

        min_qty = float(rules.get("minOrderQty") or 0.0) or step

        if lots_sum > 0:
            if self.system_logger:
                self.system_logger.warning(
                    f"[dust-sweep] 거래소 잔량 > 장부 lot 합 (sym={symbol} {side_u} "
                    f"live={live} lots={lots_sum} residual={residual:.12f}) — lot 소진 시 정리 예정"
                )
            return

        cap = max(min_qty, step * 3.0)
        if residual > cap + 1e-12:
            if self.system_logger:
                self.system_logger.warning(
                    f"[dust-sweep] 장부 lot 0인데 잔량 과다 — 자동정리 상한 초과, 수동 확인 필요 "
                    f"(sym={symbol} {side_u} residual={residual:.12f} cap={cap})"
                )
            return

        qn = self._normalize_qty(symbol, residual, mode="floor")
        if qn <= 0 or qn < min_qty - 1e-12:
            if self.system_logger:
                self.system_logger.warning(
                    f"[dust-sweep] 잔량이 최소주문 미만이라 정리 불가 "
                    f"(sym={symbol} {side_u} residual={residual:.12f} min_qty={min_qty})"
                )
            return

        res = self.rest.close_market(symbol, side_u, qn)
        if self.system_logger:
            self.system_logger.warning(
                f"[dust-sweep] 장부 lot 소진 후 거래소 잔량 정리 주문 "
                f"(sym={symbol} {side_u} qty={qn} ok={bool(res)})"
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
