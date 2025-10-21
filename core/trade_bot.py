
from strategies.basic_strategy import get_short_entry_signal,get_long_entry_signal,get_exit_signal
import time
import math
import asyncio
import json, hashlib
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import deque
from core.redis_client import redis_client
from typing import Any
from decimal import Decimal, ROUND_HALF_UP

_TZ = ZoneInfo("Asia/Seoul")
class TradeBot:
    def __init__(
            self, bybit_websocket_controller, bybit_rest_controller, manual_queue,
            system_logger=None, trading_logger=None, symbols=("BTCUSDT",)
    ):
        self.bybit_websocket_controller = bybit_websocket_controller
        self.bybit_rest_controller = bybit_rest_controller
        self.manual_queue = manual_queue
        self.system_logger = system_logger
        self.trading_logger = trading_logger

        self.symbols = list(symbols)
        subscribe = getattr(self.bybit_websocket_controller, "subscribe_symbols", None)
        if callable(subscribe):
            try:
                subscribe(*self.symbols)
            except Exception:
                pass

        # ===== 공통 파라미터(변하지 않는 값) =====
        self.running = True
        self.candles_num = 10080
        self.closes_num = 10080
        self.TAKER_FEE_RATE = 0.00055
        self.target_cross = 10
        self.leverage = 50
        self.history_num = 10
        self.polling_interval = 0.5
        self._sync_lock = asyncio.Lock()
        self._just_traded_until = 0.0

        # ===== 심볼별 상태 dict =====
        self.price_history = {s: deque(maxlen=self.history_num) for s in self.symbols}
        self.candles = {s: deque(maxlen=self.candles_num) for s in self.symbols}
        self.closes = {s: [] for s in self.symbols}
        self.ma100s = {s: None for s in self.symbols}
        self.now_ma100 = {s: None for s in self.symbols}
        self.ma_threshold = {s: None for s in self.symbols}
        self.momentum_threshold = {s: None for s in self.symbols}
        self.exit_ma_threshold = {s: 0.0005 for s in self.symbols}  # 청산 기준(고정)
        self.last_closes_update = {s: 0.0 for s in self.symbols}

        self.status = {s: {} for s in self.symbols}
        self.pos_dict = {s: {} for s in self.symbols}
        self.balance = {s: {} for s in self.symbols}
        self.last_position_time = {s: {"LONG": None, "SHORT": None} for s in self.symbols}
        self.prev = {s: None for s in self.symbols}  # 3분 전 가격
        self.percent = 5 #진입 비율
        self.leverage_limit = 50 # 최대 비율
        self._thr_quantized = {s: None for s in self.symbols}

        for symbol in symbols:
            self.bybit_rest_controller.set_leverage(symbol=symbol, leverage=self.leverage_limit)

    def _quantize_ma_threshold(self, thr: float | None) -> float | None:
        if thr is None:
            return None
        p = (Decimal(str(thr)) * Decimal('100')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)  # % 단위 2자리
        return float(p) / 100.0  # 다시 [0,1]로

    def record_price(self, symbol):
        ts = time.time()
        price = None
        get_price_fn = getattr(self.bybit_websocket_controller, "get_price", None)

        price = get_price_fn(symbol)
            # 유효성 체크
        if not isinstance(price, (int, float)) or not (price > 0) or math.isnan(price) or math.isinf(price):
            return

            # 타임스탬프 단조 증가
        ph = self.price_history[symbol]
        if ph and ts <= ph[-1][0]:
            ts = ph[-1][0] + 1e-6
        ph.append((ts, float(price)))

    def check_price_jump(self, symbol):
        min_sec = self.polling_interval
        max_sec = self.polling_interval * self.history_num
        jump_pct = self.ma_threshold.get(symbol)

        ph = self.price_history[symbol]
        if len(ph) < self.history_num or jump_pct is None:
            return None, None, None

        now_ts, now_price = ph[-1]
        in_window = False
        dts = []

        for ts, past_price in list(ph)[:-1]:
            dt = now_ts - ts
            if min_sec <= dt <= max_sec:
                in_window = True
                dts.append(dt)
                if past_price != 0:
                    change_rate = (now_price - past_price) / past_price
                    if abs(change_rate) >= jump_pct:
                        return ("UP" if change_rate > 0 else "DOWN", min(dts), max(dts))
        if in_window:
            return True, min(dts), max(dts)
        else:
            return None, None, None

    def _apply_status(self, symbol, status):
        self.status[symbol] = status
        status_list = status.get("positions", [])
        self.balance[symbol] = status.get("balance", {})
        self.pos_dict[symbol] = {p["position"]: p for p in status_list}
        self.last_position_time[symbol] = {
            "LONG": (self.pos_dict[symbol].get("LONG", {}).get("entries") or [[None]])[-1][0]
            if self.pos_dict[symbol].get("LONG") and self.pos_dict[symbol]["LONG"]["entries"] else None,
            "SHORT": (self.pos_dict[symbol].get("SHORT", {}).get("entries") or [[None]])[-1][0]
            if self.pos_dict[symbol].get("SHORT") and self.pos_dict[symbol]["SHORT"]["entries"] else None,
        }

    async def _execute_and_sync(self, fn, prev_status, symbol, *args, **kwargs):
        async with self._sync_lock:
            # 1) 주문 실행
            try:
                result = fn(*args, **kwargs)
            except Exception as e:
                self.system_logger.error(f"❌ 주문 실행 예외: {e}")
                return None

            if not result or not isinstance(result, dict):
                self.system_logger.warning("⚠️ 주문 결과가 비었습니다(또는 dict 아님).")
                return result

            order_id = result.get("orderId")
            if not order_id:
                self.system_logger.warning("⚠️ orderId 없음 → 체결 대기 스킵")
                return result

            # 2) 체결 확인
            filled = self.bybit_rest_controller.wait_order_fill(symbol, order_id)
            orderStatus = (filled or {}).get("orderStatus", "").upper()

            if orderStatus == "FILLED":
                self._log_fill(filled, prev_status=prev_status)

                self.bybit_rest_controller.set_full_position_info(symbol)
                trade = self.bybit_rest_controller.get_trade_w_order_id(symbol, order_id)
                if trade:
                    self.bybit_rest_controller.append_order(symbol, trade)
                self.bybit_rest_controller.set_wallet_balance()
                now_status = self.bybit_rest_controller.get_current_position_status(symbol=symbol)
                self._apply_status(symbol, now_status)
                self.system_logger.info(self._format_asset_section(symbol))

            elif orderStatus in ("CANCELLED", "REJECTED"):
                self.system_logger.warning(f"⚠️ 주문 {order_id[-6:]} 상태: {orderStatus} (체결 없음)")
            elif orderStatus == "TIMEOUT":
                self.system_logger.warning(f"⚠️ 주문 {order_id[-6:]} 체결 대기 타임아웃 → 취소 시도")
                try:
                    cancel_res = self.bybit_rest_controller.cancel_order(symbol, order_id)
                    self.system_logger.warning(f"🗑️ 단일 주문 취소 결과: {cancel_res}")
                except Exception as e:
                    self.system_logger.error(f"단일 주문 취소 실패: {e}")
            else:
                self.system_logger.warning(f"ℹ️ 주문 {order_id[-6:]} 상태: {orderStatus or 'UNKNOWN'} → 정책에 따라 처리")

            # 같은 루프에서 자동 조건이 바로 또 트리거되지 않도록 짧은 쿨다운
            self._just_traded_until = time.monotonic() + 0.8
            return result

    def _make_id(self, symbol: str, ts_iso: str) -> str:
        # 결정적 ID: 같은 (symbol, ts)이면 같은 id → 중복 안전
        return hashlib.sha1(f"{symbol}|{ts_iso}".encode("utf-8")).hexdigest()

    def _to_epoch_sec(self, ts_iso: str) -> int:
        # ts 예: "2025-10-07T22:43:46.885465+09:00"
        return int(datetime.fromisoformat(ts_iso).timestamp())

    def upload_signal(self, sig: Any):
        symbol = sig["symbol"]
        ts_iso = sig["ts"]  # 예) '2025-10-07T22:43:46.885465+09:00'
        day = ts_iso[:10]  # 'YYYY-MM-DD'
        sid = self._make_id(symbol, ts_iso)  # 결정적 ID

        field = f"{day}|{sid}"
        value = json.dumps(sig, ensure_ascii=False, separators=(",", ":"))

        redis_client.hset("trading:signal", field, value)

    async def run_once(self,):

        now = time.time()

        if not self.manual_queue.empty():
            cmd = await self.manual_queue.get()
            # dict면 {command, percent, side, symbol} 가능
            if isinstance(cmd, dict):
                command = cmd.get("command")
                percent = cmd.get("percent", self.percent)
                close_side = cmd.get("side")
                symbol = cmd.get("symbol") or (self.symbols[0] if self.symbols else None)
            else:
                command = cmd
                percent = self.percent
                close_side = None
                symbol = self.symbols[0]
            # 심볼이 유효하지 않으면 무시
            if symbol not in self.symbols:
                self.system_logger.info(f"❗ 알 수 없는 심볼: {symbol}")
            else:
                # 최신가 필요
                self.record_price(symbol)
                if not self.price_history[symbol]:
                    return
                _, latest_price = self.price_history[symbol][-1]

                if command in ("long", "short"):
                    await self._execute_and_sync(
                        self.bybit_rest_controller.open_market,
                        self.status[symbol], symbol,
                        symbol,  # REST 시그니처 맞춰 전달
                        command, latest_price, percent, self.balance[symbol]
                    )
                elif command == "close":
                    if close_side and close_side in self.pos_dict[symbol]:
                        pos_amt = float(self.pos_dict[symbol][close_side]["position_amt"])
                        if pos_amt != 0:
                            await self._execute_and_sync(
                                self.bybit_rest_controller.close_market,
                                self.status[symbol], symbol,
                                symbol, side=close_side, qty=pos_amt
                            )
                        else:
                            self.system_logger.info(f"❗ ({symbol}) 청산 {close_side} 없음 (수량 0)")
                    else:
                        self.system_logger.info(f"❗ ({symbol}) 포지션 정보 없음/잘못된 side: {close_side}")

        # 4) 모든 심볼 순회
        for symbol in self.symbols:
            # (a) 현재가 기록
            self.record_price(symbol)
            if not self.price_history[symbol]:
                continue
            _, latest_price = self.price_history[symbol][-1]

            # (b) 1분마다 캔들/지표 갱신
            if now - self.last_closes_update[symbol] >= 60:
                self.bybit_rest_controller.update_candles(self.candles[symbol], symbol=symbol,
                                                          count=self.candles_num)
                self.closes[symbol] = [c["close"] for c in self.candles[symbol]]
                self.ma100s[symbol] = self.bybit_rest_controller.ma100_list(self.closes[symbol])
                self.last_closes_update[symbol] = now

                raw_thr = self.bybit_rest_controller.find_optimal_threshold(
                    self.closes[symbol], self.ma100s[symbol],
                    min_thr=0.005, max_thr=0.03, target_cross=self.target_cross
                )
                quant_thr = self._quantize_ma_threshold(raw_thr)  # 퍼센트 2자리 반올림 반영값
                prev_quant = self._thr_quantized[symbol]
                if quant_thr != prev_quant:
                    self.ma_threshold[symbol] = quant_thr
                    self.momentum_threshold[symbol] = (quant_thr / 3) if quant_thr is not None else None
                    self._thr_quantized[symbol] = quant_thr
                    self.system_logger.info(
                        f"[{symbol}] 🔧 MA threshold 업데이트: raw={raw_thr!r} → 적용={quant_thr:.4%}"
                    )

                self.bybit_rest_controller.set_full_position_info(symbol)
                self.bybit_rest_controller.sync_orders_from_bybit(symbol)
                self.bybit_rest_controller.set_wallet_balance()
                new_status = self.bybit_rest_controller.get_current_position_status(symbol=symbol)
                self._apply_status(symbol, new_status)

                self.now_ma100[symbol] = self.ma100s[symbol][-1] if self.ma100s[symbol] else None
                # 3분 전 가격: 단순히 3틱 전으로 유지하던 로직 → 네 상황에 맞게 조정
                if len(self.closes[symbol]) >= 3:
                    self.prev[symbol] = self.closes[symbol][-3]
                self.bybit_rest_controller.sync_time()

            # (c) 급등락 테스트
            state, min_dt, max_dt = self.check_price_jump(symbol)
            if state == "UP":
                self.system_logger.info(f"({symbol}) 📈 급등 감지! (Δ {min_dt:.3f}~{max_dt:.3f}s)")
            elif state == "DOWN":
                self.system_logger.info(f"({symbol}) 📉 급락 감지! (Δ {min_dt:.3f}~{max_dt:.3f}s)")

            # (d) 상태 로그
            self.system_logger.debug(self.make_status_log_msg(symbol))

            # (e) 자동매매
            if time.monotonic() >= self._just_traded_until:
                # --- 청산 ---
                for side in ["LONG", "SHORT"]:
                    recent_time = self.last_position_time[symbol].get(side)
                    if not recent_time:
                        continue
                    pos_amt = float(self.pos_dict[symbol].get(side, {}).get("position_amt", 0))
                    sig = get_exit_signal(
                        side, latest_price, self.now_ma100[symbol],
                        recent_entry_time=recent_time,
                        ma_threshold = self.ma_threshold[symbol],
                        exit_ma_threshold=self.exit_ma_threshold[symbol],
                        time_limit_sec=24 * 3600,
                        near_touch_window_sec=30 * 60
                    )

                    if sig:
                        sig_dict = {
                            "kind": sig.kind, "side": sig.side, "symbol": symbol,
                            "ts": datetime.now(_TZ).isoformat(),
                            "price": sig.price, "ma100": sig.ma100,
                            "ma_delta_pct": sig.ma_delta_pct,
                            "thresholds": sig.thresholds, "reasons": sig.reasons,
                            "extra": sig.extra or {}
                        }
                        self.trading_logger.info('SIG ' + json.dumps(sig_dict, ensure_ascii=False))
                        self.upload_signal(sig_dict)

                        await self._execute_and_sync(
                            self.bybit_rest_controller.close_market,
                            self.status[symbol], symbol,
                            symbol, side=side, qty=pos_amt
                        )

                # --- Short 진입 ---
                recent_short_time = self.last_position_time[symbol].get("SHORT")
                short_amt = abs(float(self.pos_dict[symbol].get("SHORT", {}).get("position_amt", 0)))
                short_position_value = short_amt * latest_price
                total_balance = self.balance[symbol].get("total", 0) or 0
                position_ratio = (short_position_value / total_balance) if total_balance else 0
                if position_ratio < self.leverage_limit:
                    sig = get_short_entry_signal(
                        price=latest_price, ma100=self.now_ma100[symbol], prev=self.prev[symbol],
                        ma_threshold=self.ma_threshold[symbol],
                        momentum_threshold=self.momentum_threshold[symbol],
                        recent_entry_time=recent_short_time, reentry_cooldown_sec=60 * 60
                    )
                    if sig:
                        sig_dict = {
                            "kind": sig.kind, "side": sig.side, "symbol": symbol,
                            "ts": datetime.now(_TZ).isoformat(),
                            "price": sig.price, "ma100": sig.ma100,
                            "ma_delta_pct": sig.ma_delta_pct,
                            "thresholds": sig.thresholds, "reasons": sig.reasons,
                            "extra": sig.extra or {}
                        }
                        self.trading_logger.info('SIG ' + json.dumps(sig_dict, ensure_ascii=False))
                        self.upload_signal(sig_dict)

                        await self._execute_and_sync(
                            self.bybit_rest_controller.open_market,
                            self.status[symbol], symbol,
                            symbol, "short", latest_price, self.percent, self.balance[symbol]
                        )

                # --- Long 진입 ---
                recent_long_time = self.last_position_time[symbol].get("LONG")
                long_amt = abs(float(self.pos_dict[symbol].get("LONG", {}).get("position_amt", 0)))
                long_position_value = long_amt * latest_price
                total_balance = self.balance[symbol].get("total", 0) or 0
                position_ratio = (long_position_value / total_balance) if total_balance else 0
                if position_ratio < self.leverage_limit:
                    sig = get_long_entry_signal(
                        price=latest_price, ma100=self.now_ma100[symbol], prev=self.prev[symbol],
                        ma_threshold=self.ma_threshold[symbol],
                        momentum_threshold=self.momentum_threshold[symbol],
                        recent_entry_time=recent_long_time, reentry_cooldown_sec=60 * 60
                    )
                    if sig:
                        sig_dict = {
                            "kind": sig.kind, "side": sig.side, "symbol": symbol,
                            "ts": datetime.now(_TZ).isoformat(),
                            "price": sig.price, "ma100": sig.ma100,
                            "ma_delta_pct": sig.ma_delta_pct,
                            "thresholds": sig.thresholds, "reasons": sig.reasons,
                            "extra": sig.extra or {}
                        }
                        self.trading_logger.info('SIG ' + json.dumps(sig_dict, ensure_ascii=False))
                        self.upload_signal(sig_dict)

                        await self._execute_and_sync(
                            self.bybit_rest_controller.open_market,
                            self.status[symbol], symbol,
                            symbol, "long", latest_price, self.percent, self.balance[symbol]
                        )


    def make_status_log_msg(self, symbol):
        parts = []
        parts.append(self._format_watch_section(symbol))
        parts.append(self._format_market_section(symbol))
        parts.append(self._format_asset_section(symbol))
        return "".join(parts).rstrip()

    # ⏱️ 감시 구간
    def _format_watch_section(self, symbol):
        min_sec = self.polling_interval
        max_sec = self.polling_interval * self.history_num
        jump_state, min_dt, max_dt = self.check_price_jump(symbol)
        thr = (self.ma_threshold.get(symbol) or 0) * 100
        log_msg = (
            f"\n[{symbol}] ⏱️ 감시 구간(±{thr:.2f}%)\n"
            f"  • 체크 구간 : {min_sec:.1f}초 ~ {max_sec:.1f}초\n"
        )
        if jump_state is True:
            log_msg += "  • 상태      : 👀 감시 중\n"
        if min_dt is not None and max_dt is not None:
            log_msg += f"  • 데이터간격 : 최소 {min_dt:.3f}s / 최대 {max_dt:.3f}s\n"
        return log_msg


    # 💹 시세 정보
    def _format_market_section(self, symbol):
        ph = self.price_history[symbol]
        price = ph[-1][1] if ph else None
        ma100 = self.now_ma100.get(symbol)
        prev = self.prev.get(symbol)
        thr = (self.ma_threshold.get(symbol) or 0) * 100
        mom_thr = self.momentum_threshold.get(symbol)

        if price is None or ma100 is None or prev is None or thr is None:
            return ""

        ma_upper = ma100 * (1 + thr)
        ma_lower = ma100 * (1 - thr)
        ma_diff_pct = ((price - ma100) / ma100) * 100
        chg_3m_pct = ((price - prev) / prev * 100) if (prev and prev > 0) else None
        chg_3m_str = f"{chg_3m_pct:+.3f}%" if chg_3m_pct is not None else "N/A"

        return (
            f"\n[{symbol}] 💹 시세 정보\n"
            f"  • 현재가      : {price:,.1f} (MA대비 👉[{ma_diff_pct:+.3f}%]👈)\n"
            f"  • MA100       : {ma100:,.1f}\n"
            f"  • 진입목표 : {ma_lower:,.1f} / {ma_upper:,.1f} (👉[±{thr*100:.2f}%]👈)\n"
            f"  • 급등락목표 : {mom_thr*100:.3f}% ( 3분전대비 👉[{chg_3m_str}]👈)\n"
            f"  • 청산기준 : {self.exit_ma_threshold[symbol]*100:.3f}%\n"
            f"  • 목표 크로스: {self.target_cross}회 / {self.closes_num} 분)\n"
        )

    # 💰 자산 정보
    def _format_asset_section(self, symbol):
        status = self.status.get(symbol, {}) or {}
        status_list = status.get("positions", [])
        balance = self.balance.get(symbol, {})
        total = balance.get("total", 0.0)
        available = balance.get("available", 0.0)
        available_pct = (available / total * 100) if total else 0
        ph = self.price_history[symbol]
        price = ph[-1][1] if ph else None

        log_msg = (
            f"\n[{symbol}] 💰 자산정보(총 {total:.2f} USDT)\n"
            f"    진입 가능: {available:.2f} USDT ({available_pct:.1f}%) (레버리지: {self.leverage}x)"
        )

        if status_list and price is not None:
            for position in status_list:
                pos_amt = float(position["position_amt"])
                entry_price = float(position["entryPrice"])
                side = position["position"]

                if pos_amt != 0:
                    if side == "LONG":
                        profit_rate = ((price - entry_price) / entry_price) * 100
                        gross_profit = (price - entry_price) * pos_amt
                    else:
                        profit_rate = ((entry_price - price) / entry_price) * 100
                        gross_profit = (entry_price - price) * abs(pos_amt)
                else:
                    profit_rate, gross_profit = 0.0, 0.0

                position_value = abs(pos_amt) * entry_price
                fee_total = position_value * self.TAKER_FEE_RATE * 2
                net_profit = gross_profit - fee_total

                log_msg += f"  - 포지션: {side} ({pos_amt}, {entry_price:.1f}, {profit_rate:+.3f}%, {net_profit:+.1f})\n"

                if position.get("entries"):
                    for i, (timestamp, qty, entryPrice, t_str) in enumerate(position["entries"], start=1):
                        signed_qty = -qty if side == "SHORT" else qty
                        log_msg += f"     └#{i} {signed_qty:+.3f} : {t_str}, {entryPrice:.1f} \n"
        else:
            log_msg += "  - 포지션 없음\n"
        return log_msg

    def _classify_intent(self, filled: dict) -> str:
        side = (filled.get("side") or "").upper()  # BUY/SELL
        pos = int(filled.get("positionIdx") or 0)  # 1/2
        ro = bool(filled.get("reduceOnly"))  # True/False
        if ro:  # 청산
            if pos == 1 and side == "SELL":  return "LONG_CLOSE"
            if pos == 2 and side == "BUY":   return "SHORT_CLOSE"
        else:  # 진입
            if pos == 1 and side == "BUY":   return "LONG_OPEN"
            if pos == 2 and side == "SELL":  return "SHORT_OPEN"

    def _log_fill(self, filled: dict, prev_status: dict | None = None):
        side,intent = self._classify_intent(filled).split("_") # LONG_OPEN / SHORT_OPEN / LONG_CLOSE / SHORT_CLOSE ...
        order_tail = (filled.get("orderId") or "")[-6:] or "UNKNOWN"
        avg_price = float(filled.get("avgPrice") or 0.0)  # 이번 체결가 (청산가)
        exec_qty = float(filled.get("cumExecQty") or filled.get("qty") or 0.0)


        # 진입(OPEN): 기본 로그
        if not intent.endswith("CLOSE"):
            self.trading_logger.info(
                f"✅ {side} 주문 체결 완료\n"
                f" | 주문ID(뒷6자리): {order_tail}\n"
                f" | 평균진입가: {avg_price:.2f}\n"
                f" | 체결수량: {exec_qty}"
            )
            return

        # 청산(CLOSE): prev_status에서 평균진입가 자동 해석
        entry_price = self._extract_entry_price_from_prev(filled, prev_status)

        # 손익 계산
        if (side,intent) == ("LONG","CLOSE"):
            profit_gross = (avg_price - entry_price) * exec_qty
        else:  # SHORT_CLOSE
            profit_gross = (entry_price - avg_price) * exec_qty

        notional_entry = entry_price * exec_qty
        notional_close = avg_price * exec_qty
        total_notional = notional_entry + notional_close

        total_fee = total_notional * self.TAKER_FEE_RATE  # 양쪽 수수료 합
        profit_net = profit_gross - total_fee
        profit_rate = (profit_gross / entry_price) * 100 if entry_price else 0.0

        self.trading_logger.info(
            f"✅ {side} 포지션 청산 완료\n"
            f" | 주문ID: {order_tail}\n"
            f" | 평균진입가: {entry_price:.2f}\n"
            f" | 청산가: {avg_price:.2f}\n"
            f" | 체결수량: {exec_qty}\n"
            f" | 수익금(수수료 제외): {profit_net:.2f}\n"
            f" | 수익금(총): {profit_gross:.2f}, 총 수수료: {total_fee:.2f}\n"
            f" | 수익률: {profit_rate:.2f}%"
        )

    def _extract_entry_price_from_prev(self, filled: dict, prev_status: dict | None) -> float | None:
        if not prev_status:
            return None

        # filled의 포지션 방향 파악
        pos_idx = int(filled.get("positionIdx") or 0)  # 1: LONG, 2: SHORT
        side_key = "LONG" if pos_idx == 1 else "SHORT"

        positions = prev_status.get("positions") or []
        # 1) 우선 entryPrice 필드
        for p in positions:
            if (p.get("position") or "").upper() == side_key:
                ep = p.get("entryPrice") or p.get("avgPrice")
                if ep is not None:
                    try:
                        return float(ep)
                    except Exception:
                        pass
                # 2) 없으면 entries 마지막 체결가로 폴백
                entries = p.get("entries") or []
                if entries:
                    try:
                        # (ts, qty, price, time_str)
                        return float(entries[-1][2])
                    except Exception:
                        pass
        return None