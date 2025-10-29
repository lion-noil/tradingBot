# trade_bot.py
import time, json, hashlib
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any
from core.engines import CandleEngine, IndicatorEngine, JumpDetector
from core.execution import ExecutionEngine
from strategies.basic_strategy import (
    get_short_entry_signal, get_long_entry_signal, get_exit_signal
)
from core.redis_client import redis_client
from decimal import Decimal, ROUND_HALF_UP

_TZ = ZoneInfo("Asia/Seoul")

class TradeBot:
    def __init__(self, bybit_websocket_controller, bybit_rest_controller, manual_queue,
                 system_logger=None, trading_logger=None, symbols=("BTCUSDT",)):
        self.ws = bybit_websocket_controller
        self.rest = bybit_rest_controller
        self.manual_queue = manual_queue
        self.system_logger = system_logger
        self.trading_logger = trading_logger
        self.symbols = list(symbols)
        self.last_entry_signal_time = {s: {"LONG": None, "SHORT": None} for s in self.symbols}
        try:
            vals = redis_client.hgetall("trading:last_entry_signal_ts") or {}
            for k, v in vals.items():
                # k = "SYMBOL|SIDE", v = "ms"
                try:
                    sym, side = k.split("|", 1)
                    if sym in self.last_entry_signal_time and side in ("LONG", "SHORT"):
                        self.last_entry_signal_time[sym][side] = int(v)
                except Exception:
                    pass
        except Exception:
            pass

        # 구성 요소
        self.candle = CandleEngine(candles_num=10080)
        self.indicator = IndicatorEngine(min_thr=0.005, max_thr=0.03, target_cross=10)
        self.jump = JumpDetector(history_num=10, polling_interval=0.5)
        self.exec = ExecutionEngine(self.rest, system_logger, trading_logger, taker_fee_rate=0.00055)

        # 파라미터
        self.ws_stale_sec = 30.0
        self.ws_global_stale_sec = 60.0
        self.leverage = 50
        self.percent = 5
        self.leverage_limit = 50

        # 상태
        self.status = {s: {} for s in self.symbols}
        self.pos_dict = {s: {} for s in self.symbols}
        self.balance = {s: {} for s in self.symbols}
        self.last_position_time = {s: {"LONG": None, "SHORT": None} for s in self.symbols}
        self.ma100s = {s: None for s in self.symbols}
        self.now_ma100 = {s: None for s in self.symbols}
        self.ma_threshold = {s: None for s in self.symbols}
        self.momentum_threshold = {s: None for s in self.symbols}
        self.exit_ma_threshold = {s: 0.0005 for s in self.symbols}
        self._thr_quantized = {s: None for s in self.symbols}
        self.prev = {s: None for s in self.symbols}
        self._rest_fallback_on = {s: False for s in self.symbols}
        self._stale_counts = {s: 0 for s in self.symbols}

        self._last_closed_minute = {s: None for s in self.symbols}

        # 구독 시작
        subscribe = getattr(self.ws, "subscribe_symbols", None)
        if callable(subscribe):
            try: subscribe(*self.symbols)
            except: pass

        # 초기 세팅
        for sym in self.symbols:
            # 레버리지
            try: self.rest.set_leverage(symbol=sym, leverage=self.leverage)
            except Exception: pass
            try:
                self.rest.update_candles(self.candle.get_candles(sym), symbol=sym, count=10080)
                self._refresh_indicators(sym)
            except Exception as e:
                if self.system_logger: self.system_logger.warning(f"[{sym}] 초기 부트스트랩 실패: {e}")

    # ─────────────────────────────────────────────
    # 보조
    def _ws_is_fresh(self, symbol: str) -> bool:
        get_last_tick = getattr(self.ws, "get_last_tick_time", None)
        get_last_frame = getattr(self.ws, "get_last_frame_time", None)
        now_m = time.monotonic()
        if callable(get_last_tick):
            lt = get_last_tick(symbol)
            if lt and (now_m - lt) < self.ws_stale_sec:
                return True
        if callable(get_last_frame):
            lf = get_last_frame()
            if lf and (now_m - lf) < self.ws_global_stale_sec:
                return True
        return False

    # ── helpers ─────────────────────────────────────────────────────────
    def _record_entry_signal_ts(self, symbol: str, side: str, ts_ms: int):
        """엔트리 '시그널' 발생 시각(체결 무관)을 메모리/Redis에 기록"""
        self.last_entry_signal_time[symbol][side] = ts_ms
        try:
            redis_client.hset(
                "trading:last_entry_signal_ts",
                f"{symbol}|{side}",
                str(ts_ms)
            )
        except Exception:
            pass

    def _get_recent_entry_signal_ts(self, symbol: str, side: str) -> int | None:
        return self.last_entry_signal_time.get(symbol, {}).get(side)


    def _arrow(self, prev, new) -> str:
        if prev is None or new is None:
            return "→"
        return "↑" if new > prev else ("↓" if new < prev else "→")

    def _fmt_pct(self, v) -> str:
        return "—" if v is None else f"{float(v) * 100:.3f}%"

    def _xadd_one(self, symbol: str, name: str, prev, new, arrow: str, msg: str):
        stream_key = "OpenPctLog"
        fields = {
            "ts": self._kst_now_str(),  # KST
            "sym": symbol,
            "name": name,
            "prev": "" if prev is None else f"{float(prev):.10f}",  # 0~1 스케일
            "new": "" if new is None else f"{float(new):.10f}",  # 0~1 스케일
            "arrow": arrow,  # ← 보관(소비자 쪽 스키마 맞춰 사용)
            "msg": msg,  # 읽기 좋은 원문
        }
        redis_client.xadd(stream_key, fields, maxlen=30, approximate=False)

    def _quantize_thr(self, thr: float | None, lo=0.005, hi=0.03) -> float | None:
        """thr(0~1)를 0.0001 정밀도로 '내림' 양자화. 0.0103001 -> 0.0103"""
        if thr is None:
            return None
        v = Decimal(str(max(lo, min(hi, float(thr)))))
        return float(v.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


    def _refresh_indicators(self, symbol: str):
        closes = self.candle._get_closes(symbol)
        now_ma100, thr_raw, mom_raw, ma100s = self.indicator.compute_all(
            closes, self.rest.ma100_list, self.rest.find_optimal_threshold
        )
        if now_ma100 is None:
            return

        prev_q = self._thr_quantized.get(symbol)

        # 1) 현재 계산값 반영 (raw는 내부 계산용으로 유지 가능)
        self.ma100s[symbol] = ma100s
        self.now_ma100[symbol] = now_ma100
        self.ma_threshold[symbol] = thr_raw
        self.momentum_threshold[symbol] = mom_raw

        q = self._quantize_thr(thr_raw)  # step에 맞춰 버킷화
        self._thr_quantized[symbol] = q

        new_mom_from_q = (q / 3.0) if q is not None else None
        self.momentum_threshold[symbol] = new_mom_from_q

        if q != prev_q:
            arrow = self._arrow(prev_q, q)
            msg = f"[{symbol}] 🔧 MA threshold: {self._fmt_pct(prev_q)} {arrow} {self._fmt_pct(q)}"
            if self.system_logger:
                self.system_logger.debug(msg)
            self._xadd_one(symbol, "MA threshold", prev_q, q, arrow, msg)

        # prev(3틱 전) 갱신
        if len(closes) >= 3:
            self.prev[symbol] = closes[-3]

        # 상태 동기화
        self.rest.set_full_position_info(symbol)
        self.rest.sync_orders_from_bybit(symbol)
        self.rest.set_wallet_balance()
        self.status[symbol] = self.rest.get_current_position_status(symbol=symbol)
        st_list = self.status[symbol].get("positions", [])
        self.pos_dict[symbol] = {p["position"]: p for p in st_list}
        self.balance[symbol] = self.status[symbol].get("balance", {})
        self.last_position_time[symbol] = {
            "LONG": (self.pos_dict[symbol].get("LONG", {}).get("entries") or [[None]])[-1][0]
            if self.pos_dict[symbol].get("LONG") and self.pos_dict[symbol]["LONG"]["entries"] else None,
            "SHORT": (self.pos_dict[symbol].get("SHORT", {}).get("entries") or [[None]])[-1][0]
            if self.pos_dict[symbol].get("SHORT") and self.pos_dict[symbol]["SHORT"]["entries"] else None,
        }

    def _kst_now_str(self):
        return datetime.now(_TZ).strftime("%Y-%m-%d %H:%M:%S %z")

    def upload_signal(self, sig: Any):
        symbol = sig["symbol"]
        ts_iso = sig["ts"]
        day = ts_iso[:10]
        sid = hashlib.sha1(f"{symbol}|{ts_iso}".encode("utf-8")).hexdigest()
        field = f"{day}|{sid}"
        extra = sig.get("extra") or {}
        if "ts_ms" not in extra:
            extra["ts_ms"] = int(time.time() * 1000)
            sig["extra"] = extra

        value = json.dumps(sig, ensure_ascii=False, separators=(",", ":"))
        redis_client.hset("trading:signal", field, value)

    # ─────────────────────────────────────────────
    async def run_once(self):
        # 수동 명령
        if not self.manual_queue.empty():
            cmd = await self.manual_queue.get()
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
            if symbol not in self.symbols:
                if self.system_logger: self.system_logger.info(f"❗ 알 수 없는 심볼: {symbol}")
            else:
                price = getattr(self.ws, "get_price")(symbol)
                if price:
                    prev_status = self.status[symbol]
                    if command in ("long", "short"):
                        await self.exec.execute_and_sync(
                            self.rest.open_market, prev_status, symbol,
                            symbol, command, price, percent, self.balance[symbol]
                        )
                    elif command == "close":
                        if close_side and close_side in self.pos_dict[symbol]:
                            pos_amt = float(self.pos_dict[symbol][close_side]["position_amt"])
                            if pos_amt != 0:
                                await self.exec.execute_and_sync(
                                    self.rest.close_market, prev_status, symbol,
                                    symbol, side=close_side, qty=pos_amt
                                )
                                self._record_entry_signal_ts(symbol, close_side, None)
                            else:
                                if self.system_logger: self.system_logger.info(f"❗ ({symbol}) 청산 {close_side} 없음 (수량 0)")
                        else:
                            if self.system_logger: self.system_logger.info(f"❗ ({symbol}) 포지션 정보 없음/잘못된 side: {close_side}")

        # 자동 루프
        now = time.time()
        for symbol in self.symbols:
            # 1) 실시간 가격 기록
            price = getattr(self.ws, "get_price")(symbol)
            if price:
                self.jump.record_price(symbol, price)

            # 2) kline(확정 봉) 반영 → 지표 업데이트
            get_ck = getattr(self.ws, "get_last_confirmed_kline", None)
            if callable(get_ck):
                k = get_ck(symbol, "1")
                if k and k.get("confirm"):
                    k_start_minute = int(k["start"] // 60) if "start" in k else None
                    if k_start_minute is None or k_start_minute != self._last_closed_minute[symbol]:
                        self.candle.apply_confirmed_kline(symbol, k)
                        self._refresh_indicators(symbol)
                        self._last_closed_minute[symbol] = k_start_minute

            # 3) WS 상태에 따라 진행중 봉 누적 혹은 REST 백필
            use_ws = self._ws_is_fresh(symbol)
            if use_ws:
                ts = getattr(self.ws, "get_last_exchange_ts")(symbol) or now
                if price:
                    self.candle.accumulate_with_ticker(symbol, price, float(ts))
                if self._rest_fallback_on[symbol]:
                    self._rest_fallback_on[symbol] = False
                    if self.system_logger: self.system_logger.info(f"[{symbol}] ✅ WS 복구, 실시간 집계 재개")
                self._stale_counts[symbol] = 0
            else:
                self._stale_counts[symbol] += 1
                if self._stale_counts[symbol] >= 2:
                    if not self._rest_fallback_on[symbol]:
                        self._rest_fallback_on[symbol] = True
                        if self.system_logger: self.system_logger.error(f"[{symbol}] ⚠️ WS stale → REST 백필")
                    self.rest.update_candles(self.candle.get_candles(symbol), symbol=symbol, count=10080)
                    self._refresh_indicators(symbol)

            # 4) 급등락 테스트
            state, min_dt, max_dt = self.jump.check_jump(symbol, self.ma_threshold.get(symbol))
            if state == "UP" and self.system_logger:
                self.system_logger.info(f"({symbol}) 📈 급등 감지! (Δ {min_dt:.3f}~{max_dt:.3f}s)")
            elif state == "DOWN" and self.system_logger:
                self.system_logger.info(f"({symbol}) 📉 급락 감지! (Δ {min_dt:.3f}~{max_dt:.3f}s)")

            # 5) 상태 로그
            if self.system_logger:
                self.system_logger.debug(self.make_status_log_msg(symbol))

            # 6) 자동매매 (쿨다운은 ExecutionEngine 내부에서 관리)
            if price is None or self.now_ma100[symbol] is None:
                continue

            # --- 청산 시그널 ---
            for side in ["LONG", "SHORT"]:
                recent_time = self._get_recent_entry_signal_ts(symbol, side)
                if not recent_time:
                    continue
                ma_thr = self.ma_threshold.get(symbol) or 0.005
                ex_thr = self.exit_ma_threshold.get(symbol) or 0.0005

                sig = get_exit_signal(
                    side,
                    price,
                    self.now_ma100[symbol],
                    recent_entry_time=recent_time,  # ← 체인 기준
                    ma_threshold=ma_thr,
                    exit_ma_threshold=ex_thr,
                    time_limit_sec=24 * 3600,
                    near_touch_window_sec=60 * 60
                )
                if not sig:
                    continue
                self._record_entry_signal_ts(symbol, side, None)

                sig_dict = {
                    "kind": sig.kind,
                    "side": sig.side,
                    "symbol": symbol,
                    "ts": datetime.now(_TZ).isoformat(),
                    "price": sig.price,
                    "ma100": sig.ma100,
                    "ma_delta_pct": sig.ma_delta_pct,
                    "thresholds": sig.thresholds,
                    "reasons": sig.reasons,
                }
                if self.trading_logger: self.trading_logger.info('SIG ' + json.dumps(sig_dict, ensure_ascii=False))
                self.upload_signal(sig_dict)
                pos_amt = float(self.pos_dict[symbol].get(side, {}).get("position_amt", 0))
                if pos_amt == 0:
                    if self.system_logger:
                        self.system_logger.info(f"({symbol}) EXIT 신호 발생했지만 포지션 {side} 수량 0 → 체결 스킵")
                    continue

                await self.exec.execute_and_sync(
                    self.rest.close_market, self.status[symbol], symbol,
                    symbol, side=side, qty=pos_amt
                )

            # --- Short 진입 ---
            recent_short_signal_time = self._get_recent_entry_signal_ts(symbol, "SHORT")

            short_amt = abs(float(self.pos_dict[symbol].get("SHORT", {}).get("position_amt", 0)))
            short_pos_val = short_amt * price
            total_balance = self.balance[symbol].get("total", 0) or 0
            position_ratio = (short_pos_val / total_balance) if total_balance else 0
            if position_ratio < self.leverage_limit:
                sig = get_short_entry_signal(
                    price=price, ma100=self.now_ma100[symbol], prev=self.prev[symbol],
                    ma_threshold=self.ma_threshold[symbol],
                    momentum_threshold=self.momentum_threshold[symbol],
                    recent_entry_time=recent_short_signal_time, reentry_cooldown_sec=60 * 60
                )
                if sig:
                    now_ms = int(time.time() * 1000)
                    sig_dict = {
                        "kind": sig.kind, "side": sig.side, "symbol": symbol,
                        "ts": datetime.now(_TZ).isoformat(),
                        "price": sig.price, "ma100": sig.ma100,
                        "ma_delta_pct": sig.ma_delta_pct,
                        "thresholds": sig.thresholds, "reasons": sig.reasons,
                        "extra": sig.extra or {}
                    }
                    if self.trading_logger: self.trading_logger.info('SIG ' + json.dumps(sig_dict, ensure_ascii=False))
                    self.upload_signal(sig_dict)

                    self._record_entry_signal_ts(symbol, "SHORT", now_ms)

                    await self.exec.execute_and_sync(
                        self.rest.open_market, self.status[symbol], symbol,
                        symbol, "short", price, self.percent, self.balance[symbol]
                    )

            # --- Long 진입 ---
            recent_long_signal_time = self._get_recent_entry_signal_ts(symbol, "LONG")
            long_amt = abs(float(self.pos_dict[symbol].get("LONG", {}).get("position_amt", 0)))
            long_pos_val = long_amt * price
            position_ratio = (long_pos_val / total_balance) if total_balance else 0
            if position_ratio < self.leverage_limit:
                now_ms = int(time.time() * 1000)
                sig = get_long_entry_signal(
                    price=price, ma100=self.now_ma100[symbol], prev=self.prev[symbol],
                    ma_threshold=self.ma_threshold[symbol],
                    momentum_threshold=self.momentum_threshold[symbol],
                    recent_entry_time=recent_long_signal_time, reentry_cooldown_sec=60 * 60
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
                    if self.trading_logger: self.trading_logger.info('SIG ' + json.dumps(sig_dict, ensure_ascii=False))
                    self.upload_signal(sig_dict)
                    self._record_entry_signal_ts(symbol, "LONG", now_ms)

                    await self.exec.execute_and_sync(
                        self.rest.open_market, self.status[symbol], symbol,
                        symbol, "long", price, self.percent, self.balance[symbol]
                    )

    # ─────────────────────────────────────────────
    # 로그 포맷
    def make_status_log_msg(self, symbol):
        parts = []
        parts.append(self._format_watch_section(symbol))
        parts.append(self._format_market_section(symbol))
        parts.append(self._format_asset_section(symbol))
        return "".join(parts).rstrip()

    def _format_watch_section(self, symbol):
        min_sec = self.jump.polling_interval
        max_sec = self.jump.polling_interval * self.jump.history_num
        state, min_dt, max_dt = self.jump.check_jump(symbol, self.ma_threshold.get(symbol))
        thr = (self.ma_threshold.get(symbol) or 0) * 100
        log_msg = (
            f"\n[{symbol}] ⏱️ 감시 구간(±{thr:.2f}%)\n"
            f"  • 체크 구간 : {min_sec:.1f}초 ~ {max_sec:.1f}초\n"
        )
        if state is True: log_msg += "  • 상태      : 👀 감시 중\n"
        if min_dt is not None and max_dt is not None:
            log_msg += f"  • 데이터간격 : 최소 {min_dt:.3f}s / 최대 {max_dt:.3f}s\n"
        return log_msg

    def _format_market_section(self, symbol):
        price = getattr(self.ws, "get_price")(symbol)
        ma100 = self.now_ma100.get(symbol)
        prev = self.prev.get(symbol)
        thr = (self.ma_threshold.get(symbol) or 0)
        mom_thr_ratio = (self.momentum_threshold.get(symbol) or 0.0)
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
            f"  • 급등락목표 : {mom_thr_ratio*100:.3f}% ( 3분전대비 👉[{chg_3m_str}]👈)\n"
            f"  • 청산기준 : {self.exit_ma_threshold[symbol]*100:.3f}%\n"
            f"  • 목표 크로스: {self.indicator.target_cross}회 / {len(self.candle._get_closes(symbol))} 분)\n"
        )

    def _format_asset_section(self, symbol):
        status_list = self.status.get(symbol, {}).get("positions", [])
        balance = self.balance.get(symbol, {})
        total = balance.get("total", 0.0)
        available = balance.get("available", 0.0)
        available_pct = (available / total * 100) if total else 0
        price = getattr(self.ws, "get_price")(symbol)
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
                fee_total = position_value * self.exec.TAKER_FEE_RATE * 2
                net_profit = gross_profit - fee_total
                log_msg += f"  - 포지션: {side} ({pos_amt}, {entry_price:.1f}, {profit_rate:+.3f}%, {net_profit:+.1f})\n"
                if position.get("entries"):
                    for i, (timestamp, qty, entryPrice, t_str) in enumerate(position["entries"], start=1):
                        signed_qty = -qty if side == "SHORT" else qty
                        log_msg += f"     └#{i} {signed_qty:+.3f} : {t_str}, {entryPrice:.1f} \n"
        else:
            log_msg += "  - 포지션 없음\n"
        return log_msg
