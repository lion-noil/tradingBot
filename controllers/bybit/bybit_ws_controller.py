# controllers/bybit/bybit_ws_controller.py
import threading
import time
import json
from websocket import WebSocketApp


class BybitWebSocketController:
    def __init__(self, symbols=("BTCUSDT",), system_logger=None, price_ws_url=None):

        self.kline_interval = "1"  # "1" = 1분봉
        self._last_kline: dict[tuple[str, str], dict] = {}
        self._last_kline_confirmed: dict[tuple[str, str], dict] = {}

        self._last_recv_monotonic_global = 0.0
        self._last_recv_monotonic: dict[str, float] = {}

        self.symbols = list(symbols)
        self.system_logger = system_logger
        self.ws_url = price_ws_url

        self._lock = threading.Lock()
        self.ws: WebSocketApp | None = None
        self._last_frame_monotonic = 0.0

        self._prices: dict[str, float] = {}
        self._last_tick_monotonic: dict[str, float] = {}
        self._last_exchange_ts: dict[str, float] = {}

        self._reconnect_delay = 5
        self._start_public_websocket()

    # ──────────────────────────────────────────────
    # 외부에서 쓰는 읽기 API
    def get_price(self, symbol: str) -> float | None:
        with self._lock:
            return self._prices.get(symbol)

    def get_all_prices(self) -> dict[str, float]:
        with self._lock:
            return dict(self._prices)

    def get_last_tick_time(self, symbol: str) -> float | None:
        with self._lock:
            return self._last_tick_monotonic.get(symbol)

    def get_last_exchange_ts(self, symbol: str) -> float | None:
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

    # ──────────────────────────────────────────────
    # 런타임 구독 제어
    def subscribe_symbols(self, *new_symbols):
        to_add = [s for s in new_symbols if s not in self.symbols]
        if not to_add:
            return

        with self._lock:
            self.symbols.extend(to_add)
            ws = self.ws  # ✅ lock 안에서 ws 핸들 확보

        if ws:
            args = [f"tickers.{s}" for s in to_add] + [
                f"kline.{self.kline_interval}.{s}" for s in to_add
            ]
            msg = {"op": "subscribe", "args": args}
            try:
                ws.send(json.dumps(msg))
            except Exception:
                pass

    def unsubscribe_symbols(self, *symbols_to_remove):
        to_remove = [s for s in symbols_to_remove if s in self.symbols]
        if not to_remove:
            return

        with self._lock:
            self.symbols = [s for s in self.symbols if s not in to_remove]
            ws = self.ws  # ✅ lock 안에서 ws 핸들 확보

        if ws:
            args = [f"tickers.{s}" for s in to_remove] + [
                f"kline.{self.kline_interval}.{s}" for s in to_remove
            ]
            msg = {"op": "unsubscribe", "args": args}
            try:
                ws.send(json.dumps(msg))
            except Exception:
                pass

    def get_last_frame_time(self) -> float | None:
        return self._last_frame_monotonic or None

    def get_last_kline(self, symbol: str, interval: str | None = None) -> dict | None:
        interval = interval or self.kline_interval
        with self._lock:
            return self._last_kline.get((symbol, interval))

    def get_last_confirmed_kline(self, symbol: str, interval: str | None = None) -> dict | None:
        interval = interval or self.kline_interval
        with self._lock:
            return self._last_kline_confirmed.get((symbol, interval))

    # ──────────────────────────────────────────────
    def _start_public_websocket(self):
        def on_open(ws):
            with self._lock:
                self.ws = ws
                self._reconnect_delay = 5
                self._last_frame_monotonic = time.monotonic()

            if self.system_logger:
                self.system_logger.debug("✅ Public WebSocket 연결됨")

            args = [f"tickers.{sym}" for sym in self.symbols] + [
                f"kline.{self.kline_interval}.{sym}" for sym in self.symbols
            ]
            try:
                ws.send(json.dumps({"op": "subscribe", "args": args}))
            except Exception:
                pass

        def on_pong(ws, data):
            self._last_frame_monotonic = time.monotonic()

        def on_message(ws, message: str):
            try:
                parsed = json.loads(message)
                now_mono = time.monotonic()
                topic = parsed.get("topic", "")
                with self._lock:
                    self._last_frame_monotonic = now_mono
                    self._last_recv_monotonic_global = now_mono
                if topic == "hb":
                    return

                data = parsed.get("data")
                if not data:
                    return

                items = data if isinstance(data, list) else [data]
                frame_ts_ms = parsed.get("ts")

                with self._lock:
                    for item in items:
                        if topic.startswith("tickers."):
                            sym = item.get("symbol") or topic.split(".")[1]

                            price_str = (
                                    item.get("lastPrice")
                                    or item.get("ask1Price")
                                    or item.get("bid1Price")
                            )
                            if price_str is None:
                                continue
                            try:
                                price = float(price_str)
                            except (TypeError, ValueError):
                                continue

                            exch_ts_ms = item.get("ts") or item.get("timestamp") or frame_ts_ms
                            if exch_ts_ms:
                                try:
                                    exch_ts = float(exch_ts_ms) / 1000.0
                                except Exception:
                                    exch_ts = time.time()
                            else:
                                exch_ts = time.time()

                            self._prices[sym] = price
                            self._last_tick_monotonic[sym] = now_mono
                            self._last_exchange_ts[sym] = exch_ts
                            self._last_recv_monotonic[sym] = now_mono  # ✅ 추가 (심볼별 recv)
                            continue

                        if topic.startswith("kline."):
                            parts = topic.split(".")
                            if len(parts) < 3:
                                continue
                            interval, sym = parts[1], parts[2]

                            try:
                                k = {
                                    "symbol": sym,
                                    "interval": interval,
                                    "start": int(item["start"]),
                                    "end": int(item["end"]),
                                    "confirm": bool(item["confirm"]),
                                    "open": float(item["open"]),
                                    "high": float(item["high"]),
                                    "low": float(item["low"]),
                                    "close": float(item["close"]),
                                    "volume": float(item.get("volume", 0) or 0),
                                    "turnover": float(item.get("turnover", 0) or 0),
                                    "ts": int(item.get("timestamp") or frame_ts_ms or 0),
                                }
                            except Exception:
                                continue

                            key = (sym, interval)
                            self._last_kline[key] = k
                            if k["confirm"]:
                                self._last_kline_confirmed[key] = k

                            self._last_recv_monotonic[sym] = now_mono  # ✅ 추가 (심볼별 recv)
                            continue

            except Exception as e:
                if self.system_logger:
                    self.system_logger.debug(f"❌ Public 메시지 처리 오류: {e}")

        def on_error(ws, error):
            if self.system_logger:
                self.system_logger.debug(f"❌ Public WebSocket 오류: {error}")

        def on_close(ws, *args):
            if self.system_logger:
                self.system_logger.debug("🔌 WebSocket closed.")
            with self._lock:
                self.ws = None
            # ⚠️ 재연결은 아래 run() 루프가 '전담'한다. 여기서 _start_public_websocket()를
            # 호출하면(예전 코드) 끊길 때마다 [while 루프 + on_close 새 스레드]로 재연결 경로가
            # 2배씩 증식 → 스레드 폭주 → Bybit "too many requests" 403 자초 + lock 경합으로 hang.

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
                except Exception as e:
                    if self.system_logger:
                        self.system_logger.exception(f"🔥 Public WebSocket 스레드 예외: {e}")

                # run_forever가 반환됨(정상 close 또는 예외) → 지수 백오프 후 재연결.
                # backoff는 on_open 성공 시 5로 리셋됨.
                delay = self._reconnect_delay
                if self.system_logger:
                    self.system_logger.debug(f"⏳ {delay}s 후 재연결 시도…")
                time.sleep(delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 60)

        # ✅ 단일 재연결 스레드만 유지 (중복 호출돼도 새 스레드 안 띄움)
        existing = getattr(self, "_ws_thread", None)
        if existing is not None and existing.is_alive():
            return
        self._ws_thread = threading.Thread(target=run, daemon=True)
        self._ws_thread.start()
