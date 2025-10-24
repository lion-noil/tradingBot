
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

from dataclasses import dataclass

@dataclass
class CandleState:
    minute: int      # epoch // 60 (분 단위)
    o: float
    h: float
    l: float
    c: float

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
        self.ws_stale_sec = 30.0
        self.ws_global_stale_sec = 60.0  # 전체 프레임 기준

        self.symbols = list(symbols)
        self._candle_state: dict[str, CandleState | None] = {s: None for s in self.symbols}
        self._rest_fallback_on: dict[str, bool] = {s: False for s in self.symbols}
        self._stale_counts = {s: 0 for s in self.symbols}
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
        self.target_cross = 5
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

        self._seeded = {s: False for s in self.symbols}
        self._bootstrapped = {s: False for s in self.symbols}
        for s in self.symbols:
            ok = self._bootstrap_indicators_once(s)
            self._bootstrapped[s] = bool(ok)
            if not ok:
                self.system_logger.warning(f"[{s}] 초기 부트스트랩 실패(캔들/MA 미계산)")


        for symbol in symbols:
            self.bybit_rest_controller.set_leverage(symbol=symbol, leverage=self.leverage_limit)

    def _upsert_last_minute_candle(self, symbol: str, st: CandleState):
        """
        st.minute와 self.candles[symbol]의 마지막 minute가 같으면 교체, 다르면 append.
        REST 마지막 캔들이 '현재 분'을 이미 포함한 경우 중복 방지.
        """
        dq = self.candles[symbol]
        item = {"open": st.o, "high": st.h, "low": st.l, "close": st.c, "minute": st.minute}

        if dq and isinstance(dq[-1], dict) and dq[-1].get("minute") == st.minute:
            dq[-1] = item  # replace (덮어쓰기)
        else:
            dq.append(item)
        # closes 갱신
        self.closes[symbol] = [c["close"] for c in dq]

    def _ws_is_fresh(self, symbol: str) -> bool:
        get_last_tick = getattr(self.bybit_websocket_controller, "get_last_tick_time", None)
        get_last_frame = getattr(self.bybit_websocket_controller, "get_last_frame_time", None)
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

    def _accumulate_candle_with_ws(self, symbol: str, price: float, ts_sec: float):
        """WS 틱(가격, epoch초)으로 1분 캔들을 로컬 집계."""
        minute = int(ts_sec) // 60
        st = self._candle_state[symbol]

        if st is None or st.minute != minute:
            # 이전 분 캔들 마감 → self.candles / self.closes에 반영
            if st is not None:
                self._close_minute_candle(symbol, st)
            # 새 분 시작
            self._candle_state[symbol] = CandleState(
                minute=minute, o=price, h=price, l=price, c=price
            )
        else:
            # 현재 분 갱신
            st.h = max(st.h, price)
            st.l = min(st.l, price)
            st.c = price

    def _kst_now_str(self):
        kst = ZoneInfo("Asia/Seoul")

        return datetime.now(kst).strftime("%Y-%m-%d %H:%M:%S %z")

    def _fmt_pct(self, p):
        if p is None:
            return "—"
        s = f"{p * 100:.2f}".rstrip("0").rstrip(".")
        return f"{s}%"

    def _arrow(self, prev, new, eps=1e-9):
        if prev is None or new is None or abs(new - prev) < eps:
            return "→"
        return "↑" if new > prev else "↓"

    def _xadd_one(self, symbol: str, name: str, prev, new, arrow: str, msg: str):
        stream_key = f"OpenPctLog"

        fields = {
            "ts": self._kst_now_str(),  # KST
            "sym": symbol,
            "name": name,
            "prev": "" if prev is None else f"{float(prev):.10f}",  # 계산/조회용(0~1)
            "new": "" if new is None else f"{float(new):.10f}",  # 계산/조회용(0~1)
            "msg": msg,  # 원문 로그(선택)
        }
        redis_client.xadd(stream_key, fields, maxlen=30, approximate=False)

    def _log_change(self, symbol: str, name: str, prev, new, fmt="pct"):
        if fmt == "pct":
            f = self._fmt_pct
        elif fmt == "float":
            f = (lambda v: "—" if v is None else f"{v:.4f}")
        elif fmt == "int":
            f = (lambda v: "—" if v is None else str(int(v)))
        else:
            f = (lambda v: "—" if v is None else str(v))

        arrow = self._arrow(prev, new)
        msg = f"[{symbol}] 🔧 {name}: {f(prev)} {arrow} {f(new)}"
        self.system_logger.info(msg)
        self._xadd_one(symbol, name, prev, new, arrow, msg)

    def _close_minute_candle(self, symbol: str, st: CandleState):
        self._upsert_last_minute_candle(symbol, st)
        self.ma100s[symbol] = self.bybit_rest_controller.ma100_list(self.closes[symbol])
        raw_thr = self.bybit_rest_controller.find_optimal_threshold(
            self.closes[symbol], self.ma100s[symbol],
            min_thr=0.005, max_thr=0.03, target_cross=self.target_cross
        )
        quant_thr = self._quantize_ma_threshold(raw_thr)

        prev_thr = self._thr_quantized.get(symbol)

        if quant_thr != prev_thr:
            self.ma_threshold[symbol] = quant_thr
            new_mom = (quant_thr / 3) if quant_thr is not None else None
            self.momentum_threshold[symbol] = new_mom
            self._thr_quantized[symbol] = quant_thr

            self._log_change(symbol, "MA threshold", prev_thr, quant_thr, fmt="pct")

        self._after_candle_update(symbol)

    def _rest_backfill_one_minute(self, symbol: str):
        try:
            self.bybit_rest_controller.update_candles(self.candles[symbol], symbol=symbol,
                                                      count=self.candles_num)
            self.closes[symbol] = [c["close"] for c in self.candles[symbol]]
            self.ma100s[symbol] = self.bybit_rest_controller.ma100_list(self.closes[symbol])

            raw_thr = self.bybit_rest_controller.find_optimal_threshold(
                self.closes[symbol], self.ma100s[symbol],
                min_thr=0.005, max_thr=0.03, target_cross=self.target_cross
            )
            quant_thr = self._quantize_ma_threshold(raw_thr)
            prev_thr = self._thr_quantized.get(symbol)
            if quant_thr != prev_thr:
                self.ma_threshold[symbol] = quant_thr
                self.momentum_threshold[symbol] = (quant_thr / 3) if quant_thr is not None else None
                self._thr_quantized[symbol] = quant_thr
                # ✅ 초기/변경 모두 공통 로그
                self._log_change(symbol, "MA threshold(REST)", prev_thr, quant_thr, fmt="pct")

            # ✅ 공통 처리
            self._after_candle_update(symbol)

        except Exception as e:
            self.system_logger.error(f"[{symbol}] REST 백필 실패: {e}")

    def _seed_state_from_rest(self, symbol: str):
        dq = self.candles[symbol]
        if not dq:
            return
        last = dq[-1]
        # minute 필드가 없다면 여기서 채워 넣기(가능하면 REST 리스폰스에서 원천 ts를 써주세요)
        minute = last.get("minute")
        if minute is None:
            # 1분봉이라면 '해당 캔들의 시작 epoch초 // 60'으로 넣는 게 가장 정확
            # 불가피하면 현재 시간을 쓰되, 중복 위험이 있으니 WS 첫 틱에서 덮어쓰기 기대
            minute = int(time.time()) // 60

        o = float(last.get("open", last.get("close")))
        h = float(last.get("high", last.get("close")))
        l = float(last.get("low", last.get("close")))
        c = float(last.get("close"))

        self._candle_state[symbol] = CandleState(minute=minute, o=o, h=h, l=l, c=c)

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

    def _bootstrap_indicators_once(self, symbol: str):
        # 1) 캔들/클로즈 로드
        self.bybit_rest_controller.update_candles(self.candles[symbol], symbol=symbol, count=self.candles_num)
        self.closes[symbol] = [c["close"] for c in self.candles[symbol]]
        if not self.closes[symbol]:
            return False

        # 2) MA/threshold 계산
        self.ma100s[symbol] = self.bybit_rest_controller.ma100_list(self.closes[symbol])
        if not self.ma100s[symbol]:
            return False
        self.now_ma100[symbol] = self.ma100s[symbol][-1]

        raw_thr = self.bybit_rest_controller.find_optimal_threshold(
            self.closes[symbol], self.ma100s[symbol],
            min_thr=0.005, max_thr=0.03, target_cross=self.target_cross
        )
        quant = self._quantize_ma_threshold(raw_thr)
        prev_thr = self._thr_quantized.get(symbol)  # 초기에는 None
        self.ma_threshold[symbol] = quant
        new_mom = (quant / 3) if quant is not None else None
        self.momentum_threshold[symbol] = new_mom
        # ✅ 초기 세팅도 변경으로 간주하여 로그 남김
        self._log_change(symbol, "MA threshold(init)", prev_thr, quant, fmt="pct")
        self._thr_quantized[symbol] = quant

        # 3) prev(3틱 전) 세팅
        if len(self.closes[symbol]) >= 3:
            self.prev[symbol] = self.closes[symbol][-3]

        # 4) 상태 동기화(선택)
        self._after_candle_update(symbol)
        return True


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

    def _after_candle_update(self, symbol: str):
        """1분 캔들이 갱신된 '직후'에 항상 실행할 공통 처리."""
        # closes/ma100/threshold는 이미 갱신되어 있다는 전제(WS/REST 경로에서 갱신 완료 후 호출)
        # now_ma100, prev(3틱 전), 포지션/주문/잔고/시간 동기화 등 공통 처리
        self.now_ma100[symbol] = self.ma100s[symbol][-1] if self.ma100s[symbol] else None
        if len(self.closes[symbol]) >= 3:
            self.prev[symbol] = self.closes[symbol][-3]

        # 거래소 상태/주문/지갑 정보 동기화
        self.bybit_rest_controller.set_full_position_info(symbol)
        self.bybit_rest_controller.sync_orders_from_bybit(symbol)
        self.bybit_rest_controller.set_wallet_balance()
        new_status = self.bybit_rest_controller.get_current_position_status(symbol=symbol)
        self._apply_status(symbol, new_status)

        # 서버/클라 시간 싱크(기존에 하던 것 그대로)
        self.bybit_rest_controller.sync_time()


    async def run_once(self,):
        for s in self.symbols:
            if not self._seeded[s] and self.candles[s]:
                self._seed_state_from_rest(s)
                self._seeded[s] = True

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

            use_ws = self._ws_is_fresh(symbol)
            if use_ws:
                self._stale_counts[symbol] = 0
            else:
                self._stale_counts[symbol] += 1

            if use_ws or self._stale_counts[symbol] < 2:

                # fallback에서 복구되었다면 한번만 info
                if self._rest_fallback_on[symbol]:
                    self._rest_fallback_on[symbol] = False
                    self.system_logger.info(f"[{symbol}] ✅ WS 복구, 실시간 집계 재개")

                # WS 틱으로 1분 캔들 집계
                _, latest_price = self.price_history[symbol][-1]
                # WS가 거래소 ts를 제공한다면 그걸 쓰고, 아니면 now
                get_ts = getattr(self.bybit_websocket_controller, "get_last_exchange_ts", None)
                if callable(get_ts):
                    ts_sec = float(get_ts(symbol) or now)
                else:
                    ts_sec = now

                self._accumulate_candle_with_ws(symbol, latest_price, ts_sec)

            else:
                # d) REST fallback (WS stale)
                if not self._rest_fallback_on[symbol]:
                    self._rest_fallback_on[symbol] = True
                    self.system_logger.error(f"[{symbol}] ⚠️ WS stale → REST 백필 모드 진입")
                self._rest_backfill_one_minute(symbol)
                rest_price = self.closes[symbol][-1] if self.closes[symbol] else None
                if rest_price is not None:
                    # price_history에도 보강해서 이후 로직이 동일하게 동작하도록
                    ts = time.time()
                    ph = self.price_history[symbol]
                    if ph and ts <= ph[-1][0]:
                        ts = ph[-1][0] + 1e-6  # 단조 증가 보장
                    ph.append((ts, float(rest_price)))
                    latest_price = rest_price
                else:
                    # closes가 비어있을 예외 케이스에선 기존 히스토리의 마지막가로 대체
                    if self.price_history[symbol]:
                        _, latest_price = self.price_history[symbol][-1]
                    else:
                        # 정말 아무 가격도 없다면 이 심볼은 이번 턴 스킵
                        self.system_logger.error(f"[{symbol}] REST 백필 후에도 가격 미존재 → 심볼 스킵")
                        continue

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