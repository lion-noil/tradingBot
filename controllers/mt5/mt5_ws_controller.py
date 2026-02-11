# controllers/mt5/mt5_ws_controller.py
import threading
import time
import json
from typing import Optional
from websocket import WebSocketApp


class Mt5WebSocketController:
    def __init__(self, symbols=("EURUSD",), system_logger=None,price_ws_url=None):
        self.kline_interval = "1"
        self._last_kline: dict[tuple[str, str], dict] = {}
        self._last_kline_confirmed: dict[tuple[str, str], dict] = {}

        self._last_recv_monotonic_global = 0.0
        self._last_recv_monotonic: dict[str, float] = {}


        self.symbols = list(symbols)
        self.system_logger = system_logger

        self.ws_url = price_ws_url
        if not self.ws_url:
            raise RuntimeError("MT5_PRICE_WS_URL is missing (.env)")

        # 공유 상태
        self._lock = threading.Lock()
        self.ws: WebSocketApp | None = None
        self._last_frame_monotonic = 0.0

        # 시세/타임스탬프
        self._prices: dict[str, float] = {}
        self._last_tick_monotonic: dict[str, float] = {}
        self._last_exchange_ts: dict[str, float] = {}

        # ✅ 추가: 원천 틱 값 저장
        self._last: dict[str, float] = {}
        self._bid: dict[str, float] = {}
        self._ask: dict[str, float] = {}

        # 재연결 backoff
        self._reconnect_delay = 5

        self._start_public_websocket()

    # ──────────────────────────────────────────────
    # 외부 읽기 API
    # ──────────────────────────────────────────────
    def get_price(self, symbol: str) -> Optional[float]:
        with self._lock:
            last = float(self._last.get(symbol) or 0.0)
            bid = float(self._bid.get(symbol) or 0.0)
            ask = float(self._ask.get(symbol) or 0.0)

        # 1) last가 유효하면 last
        if last > 0:
            return last

        # 2) last가 없으면 mid
        if bid > 0 and ask > 0:
            return (bid + ask) / 2.0

        # 3) fallback
        if bid > 0:
            return bid
        if ask > 0:
            return ask

        return None

    def get_bid(self, symbol: str) -> Optional[float]:
        with self._lock:
            return self._bid.get(symbol)

    def get_ask(self, symbol: str) -> Optional[float]:
        with self._lock:
            return self._ask.get(symbol)

    def get_last(self, symbol: str) -> Optional[float]:
        with self._lock:
            return self._last.get(symbol)

    def get_all_prices(self) -> dict[str, float]:
        with self._lock:
            return dict(self._prices)

    def get_last_tick_time(self, symbol: str) -> Optional[float]:
        with self._lock:
            return self._last_tick_monotonic.get(symbol)

    def get_last_exchange_ts(self, symbol: str) -> Optional[float]:
        with self._lock:
            return self._last_exchange_ts.get(symbol)

    def get_last_recv_time(self, symbol: str | None = None) -> float | None:
        """
        마지막으로 WS에서 메시지를 '수신'한 시각 (monotonic).
        - symbol 주면 심볼별
        - None이면 전역
        """
        with self._lock:
            if symbol is None:
                return self._last_recv_monotonic_global or None
            return self._last_recv_monotonic.get(symbol)

    def get_last_frame_time(self) -> Optional[float]:
        return self._last_frame_monotonic or None

    def get_last_kline(self, symbol: str, interval: str | None = None) -> Optional[dict]:
        interval = interval or self.kline_interval
        with self._lock:
            return self._last_kline.get((symbol, interval))

    def get_last_confirmed_kline(self, symbol: str, interval: str | None = None) -> Optional[dict]:
        interval = interval or self.kline_interval
        with self._lock:
            return self._last_kline_confirmed.get((symbol, interval))

    # ──────────────────────────────────────────────
    # 구독 제어
    # ──────────────────────────────────────────────
    def subscribe_symbols(self, *new_symbols: str):
        to_add = [s for s in new_symbols if s and s not in self.symbols]
        if not to_add:
            return

        with self._lock:
            self.symbols.extend(to_add)
            ws = self.ws  # ✅ lock 안에서 핸들 스냅샷

        if ws:
            args = [f"tickers.{s}" for s in to_add] + [f"kline.{self.kline_interval}.{s}" for s in to_add]
            msg = {"op": "subscribe", "args": args}
            try:
                ws.send(json.dumps(msg))
            except Exception as e:
                if self.system_logger:
                    self.system_logger.debug(f"MT5 WS subscribe 전송 실패(무시): {e}")

    def unsubscribe_symbols(self, *symbols_to_remove: str):
        to_remove = [s for s in symbols_to_remove if s in self.symbols]
        if not to_remove:
            return

        with self._lock:
            self.symbols = [s for s in self.symbols if s not in to_remove]
            ws = self.ws

        if ws:
            args = [f"tickers.{s}" for s in to_remove] + [f"kline.{self.kline_interval}.{s}" for s in to_remove]
            msg = {"op": "unsubscribe", "args": args}
            try:
                ws.send(json.dumps(msg))
            except Exception as e:
                if self.system_logger:
                    self.system_logger.debug(f"MT5 WS unsubscribe 전송 실패(무시): {e}")

    # ──────────────────────────────────────────────
    # 내부: WS 수명주기
    # ──────────────────────────────────────────────
    def _start_public_websocket(self):
        def on_open(ws: WebSocketApp):
            with self._lock:
                self.ws = ws
                self._reconnect_delay = 5
                self._last_frame_monotonic = time.monotonic()

            if self.system_logger:
                self.system_logger.debug("✅ MT5 WebSocket 연결됨")

            # 현재 symbols 재구독
            with self._lock:
                syms = list(self.symbols)

            args = [f"tickers.{sym}" for sym in syms] + [f"kline.{self.kline_interval}.{sym}" for sym in syms]
            msg = {"op": "subscribe", "args": args}
            try:
                ws.send(json.dumps(msg))
            except Exception as e:
                if self.system_logger:
                    self.system_logger.debug(f"❌ MT5 subscribe 전송 실패: {e}")

        def on_pong(ws: WebSocketApp, data):
            self._last_frame_monotonic = time.monotonic()

        def on_message(ws: WebSocketApp, message: str):


            try:
                parsed = json.loads(message)
            except Exception:
                if self.system_logger:
                    self.system_logger.debug(f"❌ MT5 WS JSON 파싱 실패: {message[:200]}")
                return



            now_mono = time.monotonic()

            topic = parsed.get("topic") or ""
            if not topic:
                return

            with self._lock:
                self._last_frame_monotonic = now_mono
                self._last_recv_monotonic_global = now_mono

            # ✅ heartbeat는 여기서 끝
            if topic == "hb":
                return
            data = parsed.get("data")
            if data is None:
                return

            # ─────────────────────
            # 1) Ticker
            # ─────────────────────
            if topic.startswith("tickers."):
                item = data if isinstance(data, dict) else None
                if not item:
                    return

                sym = item.get("symbol") or topic.split(".")[1]

                # ✅ 원천 값들
                def _to_float(v):
                    try:
                        if v is None:
                            return 0.0
                        return float(v)
                    except Exception:
                        return 0.0

                last = _to_float(item.get("lastPrice"))
                bid = _to_float(item.get("bid1Price"))
                ask = _to_float(item.get("ask1Price"))

                # ✅ 대표 price 정책: last 우선 → 없으면 mid → fallback
                price = last if last > 0 else ((bid + ask) / 2.0 if (bid > 0 and ask > 0) else (bid or ask))
                if not price or price <= 0:
                    return

                exch_ts_sec = item.get("tsSec")
                if exch_ts_sec is not None:
                    exch_ts = float(exch_ts_sec)
                else:
                    exch_ts_ms = item.get("ts") or item.get("timestamp")
                    exch_ts = float(exch_ts_ms) / 1000.0 if exch_ts_ms is not None else time.time()

                with self._lock:
                    # ✅ 저장: 원천
                    if last > 0:
                        self._last[sym] = last
                    if bid > 0:
                        self._bid[sym] = bid
                    if ask > 0:
                        self._ask[sym] = ask

                    # ✅ 저장: 대표
                    self._prices[sym] = price
                    self._last_tick_monotonic[sym] = now_mono   # ✅ 복구
                    self._last_exchange_ts[sym] = exch_ts
                    self._last_recv_monotonic[sym] = now_mono   # ✅ 추가
                return
            # ─────────────────────
            # 2) Kline
            # ─────────────────────
            if topic.startswith("kline."):
                parts = topic.split(".")
                if len(parts) < 3:
                    return
                interval, sym = parts[1], parts[2]

                items = data if isinstance(data, list) else [data]

                for bar in items:
                    try:
                        confirm = bool(bar.get("confirm", False))
                    except Exception:
                        continue

                    start_ms = int(bar["start"])
                    end_ms = int(bar.get("end") or (start_ms + 60_000))
                    k = {
                        "symbol": sym,
                        "interval": interval,
                        "start": start_ms,
                        "end": end_ms,
                        "confirm": confirm,
                        "open": float(bar["open"]),
                        "high": float(bar["high"]),
                        "low": float(bar["low"]),
                        "close": float(bar["close"]),
                        "volume": float(bar.get("volume", 0) or 0),
                        "turnover": float(bar.get("turnover", 0) or 0),  # 없으면 0
                        "ts": int(bar.get("timestamp") or bar.get("ts") or 0),
                    }

                    key = (sym, interval)
                    with self._lock:
                        self._last_kline[key] = k
                        if k["confirm"]:
                            self._last_kline_confirmed[key] = k
                        self._last_recv_monotonic[sym] = now_mono  # ✅ 추가

        def on_error(ws: WebSocketApp, error):
            if self.system_logger:
                self.system_logger.debug(f"❌ MT5 WebSocket 오류: {error}")

        def on_close(ws: WebSocketApp, *args):
            if self.system_logger:
                self.system_logger.debug("🔌 MT5 WebSocket closed.")

            with self._lock:
                self.ws = None

        def run():
            while True:
                try:
                    ws_app = WebSocketApp(
                        self.ws_url,
                        on_open=on_open,
                        on_message=on_message,
                        on_error=on_error,
                        on_close=on_close,
                        on_pong=on_pong,
                    )
                    ws_app.run_forever(ping_interval=20, ping_timeout=10)

                    # ✅ 여기로 내려오면 연결이 종료된 것 → backoff 후 재연결
                    delay = self._reconnect_delay
                    if self.system_logger:
                        self.system_logger.debug(f"⏳ {delay}s 후 MT5 WS 재연결 시도…")
                    time.sleep(delay)
                    self._reconnect_delay = min(self._reconnect_delay * 2, 60)

                except Exception as e:
                    if self.system_logger:
                        self.system_logger.exception(f"🔥 MT5 WebSocket 스레드 예외: {e}")
                    time.sleep(self._reconnect_delay)
                    self._reconnect_delay = min(self._reconnect_delay * 2, 60)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
