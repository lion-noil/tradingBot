# trade_bot.py
import time, json, hashlib
from zoneinfo import ZoneInfo
from typing import Any
from core.engines import CandleEngine, IndicatorEngine, JumpDetector
from core.execution import ExecutionEngine
from strategies.basic_strategy import (
    get_short_entry_signal, get_long_entry_signal, get_exit_signal
)
from core.redis_client import redis_client
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone, timedelta
_TZ = ZoneInfo("Asia/Seoul")
KST = timezone(timedelta(hours=9))


class TradeBot:
    def __init__(self, bybit_websocket_controller, bybit_rest_controller, manual_queue,
                 system_logger=None, trading_logger=None, symbols=("BTCUSDT",)):
        self.ws = bybit_websocket_controller
        self.rest = bybit_rest_controller
        self.manual_queue = manual_queue
        self.system_logger = system_logger
        self.trading_logger = trading_logger
        self.symbols = list(symbols)
        self.last_entry_signal_time = self._load_entry_signal_ts_from_redis()
        self.target_cross = 5
        # 구성 요소
        self.candle = CandleEngine(candles_num=10080)
        self.indicator = IndicatorEngine(min_thr=0.005, max_thr=0.03, target_cross=self.target_cross)
        self.jump = JumpDetector(history_num=10, polling_interval=0.5)
        self.exec = ExecutionEngine(self.rest, system_logger, trading_logger, taker_fee_rate=0.00055)

        # 파라미터
        self.ws_stale_sec = 30.0
        self.ws_global_stale_sec = 60.0
        self.leverage = 50
        self.percent = 5
        self.leverage_limit = 50

        # 상태
        self.asset = {
            "wallet": {"USDT": 0.0},
            "positions": {s: {} for s in self.symbols},
        }
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

        self.jump_state = {
            s: {
                "state": None,  # "UP" / "DOWN" / True / None
                "min_dt": None,
                "max_dt": None,
                "ts": None,  # 감지된 시각 (time.time())
            } for s in self.symbols
        }

        # 구독 시작
        subscribe = getattr(self.ws, "subscribe_symbols", None)
        if callable(subscribe):
            try: subscribe(*self.symbols)
            except: pass

        # 초기 세팅
        for sym in self.symbols:
            self.asset = self.rest.getNsav_asset(asset = self.asset, symbol=sym, save_redis=True)
            # 레버리지
            try: self.rest.set_leverage(symbol=sym, leverage=self.leverage)
            except Exception: pass
            try:
                self.rest.update_candles(self.candle.get_candles(sym), symbol=sym, count=10080)
                self._refresh_indicators(sym)
                self.rest.sync_orders_from_bybit(sym)

            except Exception as e:
                if self.system_logger: self.system_logger.warning(f"[{sym}] 초기 부트스트랩 실패: {e}")

        self._last_log_snapshot = None  # 마지막 로그 원문
        self._last_log_summary = None  # 마지막 요약(파싱결과)
        self._last_log_reason = None
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

    def _load_entry_signal_ts_from_redis(self):
        """Redis에 저장된 마지막 엔트리 시그널 타임스탬프 불러오기"""
        base = {s: {"LONG": None, "SHORT": None} for s in self.symbols}

        try:
            vals = redis_client.hgetall("trading:last_entry_signal_ts") or {}
            for k, v in vals.items():
                # Redis는 bytes 반환 가능 → 문자열로 변환
                key = k.decode() if isinstance(k, (bytes, bytearray)) else k
                val = v.decode() if isinstance(v, (bytes, bytearray)) else v

                # 키 파싱: 예) "BTCUSDT|LONG"
                try:
                    sym, side = key.split("|", 1)
                except ValueError:
                    continue

                # 유효성 검사
                if sym not in base or side not in ("LONG", "SHORT"):
                    continue

                # 숫자만 허용 (None, '', NaN 등 필터)
                if val and val.isdigit():
                    base[sym][side] = int(val)
        except Exception as e:
            if self.system_logger:
                self.system_logger.warning(f"[WARN] Redis entry-signal 로드 실패: {e}")

        return base


    def _record_entry_signal_ts(self, symbol: str, side: str, ts_ms: int | None):
        """엔트리 '시그널' 발생 시각(체결 무관)을 메모리/Redis에 기록"""
        self.last_entry_signal_time[symbol][side] = ts_ms

        try:
            key = f"{symbol}|{side}"
            if ts_ms is None:
                # ✅ None은 해시 필드 삭제 (잔재/오염 방지)
                redis_client.hdel("trading:last_entry_signal_ts", key)
            else:
                # ✅ 정수만 저장
                redis_client.hset("trading:last_entry_signal_ts", key, str(int(ts_ms)))
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

        self.ma100s[symbol] = ma100s
        self.now_ma100[symbol] = now_ma100
        self.ma_threshold[symbol] = thr_raw
        self.momentum_threshold[symbol] = mom_raw

        q = self._quantize_thr(thr_raw)
        self._thr_quantized[symbol] = q

        # 모멘텀 스레시홀드 재설정(기존 로직 유지)
        self.momentum_threshold[symbol] = (q / 3.0) if q is not None else None

        if q != prev_q:
            arrow = self._arrow(prev_q, q)
            msg = f"[{symbol}] 🔧 MA threshold: {self._fmt_pct(prev_q)} {arrow} {self._fmt_pct(q)}"
            if self.system_logger:
                self.system_logger.debug(msg)
            self._xadd_one(symbol, "MA threshold", prev_q, q, arrow, msg)

        # prev(3틱 전) 갱신
        if len(closes) >= 3:
            self.prev[symbol] = closes[-3]



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
        # 자동 루프
        now = time.time()
        for symbol in self.symbols:
            # 1) 실시간 가격 기록
            price = getattr(self.ws, "get_price")(symbol)
            exchange_ts = getattr(self.ws, "get_last_exchange_ts")(symbol)
            if price:
                self.jump.record_price(symbol, price, exchange_ts)

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
            self.jump_state[symbol]["state"] = state
            self.jump_state[symbol]["min_dt"] = min_dt
            self.jump_state[symbol]["max_dt"] = max_dt
            self.jump_state[symbol]["ts"] = time.time() if state else self.jump_state[symbol]["ts"]

            if state == "UP" and self.system_logger:
                self.system_logger.info(f"({symbol}) 📈 급등 감지! (Δ {min_dt:.3f}~{max_dt:.3f}s)")
            elif state == "DOWN" and self.system_logger:
                self.system_logger.info(f"({symbol}) 📉 급락 감지! (Δ {min_dt:.3f}~{max_dt:.3f}s)")

            # 6) 자동매매 (쿨다운은 ExecutionEngine 내부에서 관리)
            if price is None or self.now_ma100[symbol] is None:
                continue

            # --- 청산 시그널 ---
            for side in ["LONG", "SHORT"]:
                recent_time = self._get_recent_entry_signal_ts(symbol, side)
                if not recent_time:
                    continue
                ma_thr = self.ma_threshold.get(symbol)
                ex_thr = self.exit_ma_threshold.get(symbol)

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
                pos_amt = abs(float((self.asset['positions'][symbol].get(side) or {}).get('qty') or 0))
                if pos_amt == 0:
                    if self.system_logger:
                        self.system_logger.info(f"({symbol}) EXIT 신호 발생했지만 포지션 {side} 수량 0 → 체결 스킵")
                    continue

                await self.exec.execute_and_sync(
                    self.rest.close_market, self.asset['positions'][symbol][side], symbol,
                    symbol, side=side, qty=pos_amt
                )
                self.asset = self.rest.getNsav_asset(asset=self.asset,symbol=symbol, save_redis=True)

            # --- Short 진입 ---
            recent_short_signal_time = self._get_recent_entry_signal_ts(symbol, "SHORT")
            short_amt = abs(float((self.asset['positions'][symbol].get('SHORT') or {}).get('qty') or 0))
            short_pos_val = short_amt * price
            total_balance = self.asset['wallet'].get('USDT',0) or 0
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
                        self.rest.open_market, self.asset['positions'][symbol][sig.side], symbol,
                        symbol, "short", price, self.percent, self.asset['wallet']
                    )
                    self.rest.getNsav_asset(asset=self.asset, symbol=symbol, save_redis=True)

            # --- Long 진입 ---
            recent_long_signal_time = self._get_recent_entry_signal_ts(symbol, "LONG")
            long_amt = abs(float((self.asset['positions'][symbol].get('LONG') or {}).get('qty') or 0))
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
                        self.rest.open_market, self.asset['positions'][symbol][sig.side], symbol,
                        symbol, "long", price, self.percent, self.asset['wallet']
                    )
                    self.asset = self.rest.getNsav_asset(asset=self.asset,symbol=symbol, save_redis=True)


        new_status = self.make_status_log_msg()

        if self._should_log_update(new_status):
            if self.system_logger:
                self.system_logger.debug(self._last_log_reason+new_status)
            self._last_log_snapshot = new_status

    # ─────────────────────────────────────────────
    # 로그 포맷
    def make_status_log_msg(self):
        total_usdt = self.asset['wallet'].get('USDT', 0)
        log_msg = f"\n💰 총 자산: {total_usdt:.2f} USDT\n"
        # 각 심볼별로 jump 상태 + 포지션 정보 출력
        for symbol in self.symbols:
            js = (self.jump_state or {}).get(symbol, {})
            state = js.get("state")
            min_dt = js.get("min_dt")
            max_dt = js.get("max_dt")
            thr_pct = (self.ma_threshold.get(symbol) or 0) * 100

            # 상태 이모지 결정
            if state == "UP":
                emoji = "📈"
            elif state == "DOWN":
                emoji = "📉"
            else:
                emoji = "👀"

            if min_dt and max_dt:
                jump_info = f"{emoji} ma_thr({thr_pct:.2f}%) ma100({self.now_ma100[symbol]:.2f}%) Δ={min_dt:.3f}~{max_dt:.3f}s"
            else:
                jump_info = f"{emoji} ma_thr({thr_pct:.2f}%)"

            # 포지션 상세는 기존 로직 그대로 재사용
            pos_info = self._format_asset_section(symbol)

            log_msg += f"[{symbol}] {jump_info}\n{pos_info}"

        return log_msg.rstrip()


    def _format_asset_section(self, symbol):
        """self.asset['positions'] 기반 포지션 요약 출력"""
        pos = (self.asset.get("positions") or {}).get(symbol, {})
        long_pos = pos.get("LONG")
        short_pos = pos.get("SHORT")

        price = getattr(self.ws, "get_price")(symbol)
        if price is None:
            return f"  - 시세 없음\n"

        log = []
        # 지갑 요약은 상단에서 한 번만 찍으므로 여기선 포지션만
        taker_fee = getattr(self.exec, "TAKER_FEE_RATE", 0.00055)

        def _fmt_one(side_name, rec):
            if not rec:
                return None
            qty = float(rec.get("qty", 0.0) or 0.0)
            entry = float(rec.get("avg_price", 0.0) or 0.0)
            if qty <= 0 or entry <= 0:
                return None

            if side_name == "LONG":
                profit_rate = (price - entry) / entry * 100.0
                gross_profit = (price - entry) * qty
            else:  # SHORT
                profit_rate = (entry - price) / entry * 100.0
                gross_profit = (entry - price) * qty

            position_value = qty * entry
            fee_total = position_value * taker_fee * 2  # 왕복
            net_profit = gross_profit - fee_total

            s = [f"  - 포지션: {side_name} ({qty}, {entry:.1f}, {profit_rate:+.3f}%, {net_profit:+.1f})"]
            # entries 출력(있으면)
            entries = rec.get("entries") or []
            for i, e in enumerate(entries, start=1):
                q = float(e.get("qty", 0.0) or 0.0)
                signed_qty = (-q) if side_name == "SHORT" else q
                t_str = e.get("ts_str")
                if not t_str and (ts := e.get("ts")):
                    from datetime import datetime, timezone, timedelta
                    t_str = datetime.fromtimestamp(int(ts) / 1000, tz=_TZ).strftime("%Y-%m-%d %H:%M:%S")
                price_e = float(e.get("price", 0.0) or 0.0)
                s.append(f"     └#{i} {signed_qty:+.3f} : {t_str or '-'}, {price_e:.1f} ")
            return "\n".join(s)

        line_l = _fmt_one("LONG", long_pos)
        line_s = _fmt_one("SHORT", short_pos)

        if not line_l and not line_s:
            return "  - 포지션 없음\n"

        if line_l: log.append(line_l)
        if line_s: log.append(line_s)
        return "\n".join(log) + "\n"

    def _extract_status_summary_from_text(self, text: str) -> dict:
        import re
        summary = {}
        lines = text.splitlines()

        cur_sym = None
        header_ma_re = re.compile(
            r"^\[(?P<sym>[A-Z0-9]+)\]\s+(?P<emoji>[📈📉👀—])\s+ma_thr\(\s*(?P<thr>[0-9.]+)\s*%\s*\)"
        )
        # jump 헤더가 남아 있더라도 무시해도 되지만, 이모지 추출용으로 두면 UI 일관성에 좋아요.
        header_jump_re = re.compile(
            r"^\[(?P<sym>[A-Z0-9]+)\]\s+(?P<emoji>[📈📉👀—])\s+jump\("
        )

        pos_re = re.compile(
            r"^\s*-\s*포지션:\s*(?P<side>LONG|SHORT)\s*\("
            r"\s*(?P<qty>\d+(?:\.\d+)?)\s*,\s*[^,]+,\s*(?P<pct>[+\-]?\d+\.\d+)%"
        )

        def _ensure(sym, emoji=None):
            if sym not in summary:
                summary[sym] = {"jump": "—", "ma_thr": None, "LONG": None, "SHORT": None}
            if emoji is not None:
                summary[sym]["jump"] = emoji

        for raw in lines:
            line = raw.strip()

            m = header_ma_re.match(line)
            if m:
                cur_sym = m.group("sym")
                _ensure(cur_sym, m.group("emoji"))
                try:
                    summary[cur_sym]["ma_thr"] = float(m.group("thr"))  # 단위: %
                except:
                    summary[cur_sym]["ma_thr"] = None
                continue

            # jump(...) 헤더가 있어도 emoji만 확보
            m = header_jump_re.match(line)
            if m:
                cur_sym = m.group("sym")
                _ensure(cur_sym, m.group("emoji"))
                continue

            if cur_sym:
                pm = pos_re.match(line)
                if pm:
                    side = pm.group("side")
                    qty = float(pm.group("qty") or 0.0)
                    pct = float(pm.group("pct") or 0.0)
                    _ensure(cur_sym)
                    summary[cur_sym][side] = {"q": round(qty, 6), "pr": round(pct, 1)}

        # ma_thr 보강: 로그에 없으면 self.ma_threshold에서 가져오기 (내부 값이 비율이면 %로 변환)
        mt = getattr(self, "ma_threshold", {}) or {}
        for sym, v in summary.items():
            if v.get("ma_thr") is None and sym in mt and mt[sym] is not None:
                try:
                    raw = float(mt[sym])
                    # make_status_log_msg에서 thr_pct = ma_threshold[sym]*100 쓰고 있으니 여기서도 %값으로 맞춥니다.
                    v["ma_thr"] = raw * 100.0
                except:
                    pass

        return summary

    def _should_log_update(self, new_status: str) -> bool:
        new_summary = self._extract_status_summary_from_text(new_status)

        if getattr(self, "_last_log_summary", None) is None:
            self._last_log_summary = new_summary
            self._last_log_snapshot = new_status
            self._last_log_reason = "initial snapshot"
            return True

        old = self._last_log_summary
        symbols = set(new_summary.keys()) | set(old.keys())

        rate_thr = 1
        qty_thr = getattr(self, "_qty_trigger_abs", 0.0001)

        def _norm(val):
            if val is None:
                return (None, None)
            if isinstance(val, dict):
                q = val.get("q");
                pr = val.get("pr")
                try:
                    q = None if q is None else float(q)
                except:
                    q = None
                try:
                    pr = None if pr is None else float(pr)
                except:
                    pr = None
                return (q, pr)
            try:
                return (None, float(val))
            except:
                return (None, None)

        def _fmt_delta(cur, prev, unit=""):
            try:
                d = float(cur) - float(prev)
                sign = "+" if d >= 0 else ""
                return f"{sign}{d:.6f}{unit} ({prev}→{cur})"
            except Exception:
                return f"{prev}→{cur}{unit}"

        def _as_float(x):
            try:
                return None if x is None else float(x)
            except:
                return None

        for sym in symbols:
            n = new_summary.get(sym, {"jump": "—", "ma_thr": None, "LONG": None, "SHORT": None})
            o = old.get(sym, {"jump": "—", "ma_thr": None, "LONG": None, "SHORT": None})

            # 0) ma_thr 변화 → 임계치 없이 '값만' 달라도 무조건 로그 (단위: %)
            nth, oth = _as_float(n.get("ma_thr")), _as_float(o.get("ma_thr"))
            if (nth is None) != (oth is None) or (nth is not None and oth is not None and nth != oth):
                self._last_log_summary = new_summary
                self._last_log_snapshot = new_status
                self._last_log_reason = f"{sym} MA threshold Δ={_fmt_delta(nth, oth, unit='%')}"
                return True

            # 1) jump 이모지 변화
            if n.get("jump") != o.get("jump"):
                self._last_log_summary = new_summary
                self._last_log_snapshot = new_status
                self._last_log_reason = f"{sym} jump {o.get('jump')}→{n.get('jump')}"
                return True

            # 2) 포지션 등장/소멸
            for side in ("LONG", "SHORT"):
                n_has = n.get(side) is not None
                o_has = o.get(side) is not None
                if n_has != o_has:
                    mode = "appeared" if n_has else "disappeared"
                    self._last_log_summary = new_summary
                    self._last_log_snapshot = new_status
                    self._last_log_reason = f"{sym} {side} position {mode}"
                    return True

            # 3) qty / 수익률 변화
            for side in ("LONG", "SHORT"):
                npos = n.get(side);
                opos = o.get(side)
                if npos is None or opos is None:
                    continue
                nq, npr = _norm(npos);
                oq, opr = _norm(opos)

                if nq is not None and oq is not None:
                    try:
                        if abs(nq - oq) >= qty_thr:
                            self._last_log_summary = new_summary
                            self._last_log_snapshot = new_status
                            self._last_log_reason = f"{sym} {side} qty Δ={_fmt_delta(nq, oq)}"
                            return True
                    except Exception:
                        pass

                if npr is not None and opr is not None:
                    try:
                        if abs(npr - opr) >= rate_thr:
                            self._last_log_summary = new_summary
                            self._last_log_snapshot = new_status
                            self._last_log_reason = f"{sym} {side} PnL Δ={_fmt_delta(f'{npr:.1f}%', f'{opr:.1f}%')}"
                            return True
                    except Exception:
                        pass

        return False



