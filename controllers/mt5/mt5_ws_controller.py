# controllers/mt5/mt5_ws_controller.py
import threading
import time
import json
from typing import Optional

from websocket import WebSocketApp

from app.config import MT5_PRICE_WS_URL  # ✅ .env에서 관리


class Mt5WebSocketController:
    """
    MT5 WebSocket 클라이언트 컨트롤러
    - URL: MT5_PRICE_WS_URL (.env)
    - 프로토콜:
        - subscribe: {"op": "subscribe", "args": ["tickers.SYMBOL", "kline.1.SYMBOL", ...]}
        - unsubscribe: {"op": "unsubscribe", "args": [...]}
        - ticker 수신: {"topic": "tickers.SYMBOL", "data": {...}}
        - kline 수신: {"topic": "kline.INTERVAL.SYMBOL", "data": [ {...}, ... ]}
    """

    def __init__(self, symbols=("EURUSD",), system_logger=None, base_ws_url: str | None = None):
        self.kline_interval = "1"
        self._last_kline: dict[tuple[str, str], dict] = {}
        self._last_kline_confirmed: dict[tuple[str, str], dict] = {}

        self.symbols = list(symbols)
        self.system_logger = system_logger

        # ✅ 하드코딩 제거: env 또는 인자 override
        self.ws_url = base_ws_url or MT5_PRICE_WS_URL
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

        # 재연결 backoff
        self._reconnect_delay = 5

        self._start_public_websocket()

    # ──────────────────────────────────────────────
    # 외부 읽기 API
    # ──────────────────────────────────────────────
    def get_price(self, symbol: str) -> Optional[float]:
        with self._lock:
            return self._prices.get(symbol)

    def get_all_prices(self) -> dict[str, float]:
        with self._lock:
            return dict(self._prices)

    def get_last_tick_time(self, symbol: str) -> Optional[float]:
        with self._lock:
            return self._last_tick_monotonic.get(symbol)

    def get_last_exchange_ts(self, symbol: str) -> Optional[float]:
        with self._lock:
            return self._last_exchange_ts.get(symbol)

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

            self._last_frame_monotonic = time.monotonic()

            topic = parsed.get("topic") or ""
            data = parsed.get("data")
            if not topic or data is None:
                return

            # ─────────────────────
            # 1) Ticker
            # ─────────────────────
            if topic.startswith("tickers."):
                item = data if isinstance(data, dict) else None
                if not item:
                    return

                sym = item.get("symbol") or topic.split(".")[1]
                price_str = item.get("lastPrice") or item.get("ask1Price") or item.get("bid1Price")
                if price_str is None:
                    return
                try:
                    price = float(price_str)
                except (TypeError, ValueError):
                    return

                exch_ts = item.get("ts") or item.get("timestamp")
                try:
                    exch_ts = float(exch_ts) if exch_ts is not None else time.time()
                except Exception:
                    exch_ts = time.time()

                with self._lock:
                    self._prices[sym] = price
                    self._last_tick_monotonic[sym] = time.monotonic()
                    self._last_exchange_ts[sym] = exch_ts
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
                        t_sec = int(bar["time"])
                        o = float(bar["open"])
                        h = float(bar["high"])
                        l = float(bar["low"])
                        c = float(bar["close"])
                        v = float(bar.get("volume", 0) or 0)
                        confirm = bool(bar.get("confirm", False))
                    except Exception:
                        continue

                    step_ms = 60 * 1000 if interval == "1" else 24 * 60 * 60 * 1000
                    start_ms = t_sec * 1000
                    end_ms = start_ms + step_ms - 1

                    k = {
                        "symbol": sym,
                        "interval": interval,
                        "time": t_sec,
                        "open": o,
                        "high": h,
                        "low": l,
                        "close": c,
                        "volume": v,
                        "confirm": confirm,
                        "start": start_ms,
                        "end": end_ms,
                    }

                    key = (sym, interval)
                    with self._lock:
                        self._last_kline[key] = k
                        if confirm:
                            self._last_kline_confirmed[key] = k

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
