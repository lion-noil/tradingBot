# controllers/bybit/bybit_ws_controller.py
import threading
import time
import json
from websocket import WebSocketApp


class BybitWebSocketController:
    def __init__(self, symbols=("BTCUSDT",), system_logger=None):
        self.kline_interval = "1"  # "1" = 1분봉
        self._last_kline: dict[tuple[str, str], dict] = {}  # {(symbol, interval): kline dict}
        self._last_kline_confirmed: dict[tuple[str, str], dict] = {}  # 마지막으로 마감된 봉

        self.symbols = list(symbols)
        self.system_logger = system_logger
        self.ws_url = "wss://stream.bybit.com/v5/public/linear"

        # 공유 상태
        self._lock = threading.Lock()
        self.ws: WebSocketApp | None = None
        self._last_frame_monotonic = 0.0

        # 시세/타임스탬프(스레드 안전)
        self._prices: dict[str, float] = {}
        self._last_tick_monotonic: dict[str, float] = {}   # WS 신선도 판단용 (monotonic)
        self._last_exchange_ts: dict[str, float] = {}      # 거래소가 준 ts(초) 기반 분캔들 경계용

        # 재연결 backoff
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
        """마지막 틱 수신 시각(monotonic) → 신선도 체크에 사용"""
        with self._lock:
            return self._last_tick_monotonic.get(symbol)

    def get_last_exchange_ts(self, symbol: str) -> float | None:
        """거래소가 제공한 마지막 업데이트 시각(초) → 분 경계 정확도 향상"""
        with self._lock:
            return self._last_exchange_ts.get(symbol)

    # ──────────────────────────────────────────────
    # 런타임 구독 제어
    def subscribe_symbols(self, *new_symbols):
        to_add = [s for s in new_symbols if s not in self.symbols]
        if not to_add:
            return
        with self._lock:
            self.symbols.extend(to_add)

        ws = self.ws
        if ws:
            # ✅ ticker + kline.1 동시 구독
            args = [f"tickers.{s}" for s in to_add] + [f"kline.{self.kline_interval}.{s}" for s in to_add]
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

        ws = self.ws
        if ws:
            # ✅ ticker + kline.1 동시 해제
            args = [f"tickers.{s}" for s in to_remove] + [f"kline.{self.kline_interval}.{s}" for s in to_remove]
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

    # 최근 '마감된' kline (confirm=True)
    def get_last_confirmed_kline(self, symbol: str, interval: str | None = None) -> dict | None:
        interval = interval or self.kline_interval
        with self._lock:
            return self._last_kline_confirmed.get((symbol, interval))

    # ──────────────────────────────────────────────
    # 내부: WS 수명주기
    def _start_public_websocket(self):
        def on_open(ws):
            self.ws = ws
            self._reconnect_delay = 5
            self._last_frame_monotonic = time.monotonic()
            if self.system_logger:
                self.system_logger.debug("✅ Public WebSocket 연결됨")

            # ✅ ticker + kline.1 두 토픽 모두 재구독
            args = [f"tickers.{sym}" for sym in self.symbols] + [
                f"kline.{self.kline_interval}.{sym}" for sym in self.symbols
            ]
            ws.send(json.dumps({"op": "subscribe", "args": args}))

        def on_pong(ws, data):
            # ✅ 핑/퐁만 와도 연결은 살아있음
            self._last_frame_monotonic = time.monotonic()

        def on_message(ws, message: str):
            try:
                parsed = json.loads(message)
                self._last_frame_monotonic = time.monotonic()

                data = parsed.get("data")
                if not data:
                    return

                items = data if isinstance(data, list) else [data]
                topic = parsed.get("topic", "")
                frame_ts_ms = parsed.get("ts")

                with self._lock:
                    for item in items:
                        # ── 1) ticker 처리 ─────────────────────────────
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
                            self._last_tick_monotonic[sym] = time.monotonic()
                            self._last_exchange_ts[sym] = exch_ts
                            continue

                        # ── 2) kline 처리 ──────────────────────────────
                        if topic.startswith("kline."):
                            # topic 예: "kline.1.BTCUSDT"
                            parts = topic.split(".")
                            if len(parts) < 3:
                                continue
                            interval, sym = parts[1], parts[2]

                            # item 필드: start/end/confirm/open/high/low/close/volume/turnover 등(문자열/숫자 혼재)
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
                                # 필수 필드가 없거나 타입 변환 실패 시 skip
                                continue

                            key = (sym, interval)
                            self._last_kline[key] = k
                            if k["confirm"]:
                                self._last_kline_confirmed[key] = k
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
            # 끊길 때 핸들 비움
            self.ws = None
            # 재연결
            delay = self._reconnect_delay
            if self.system_logger:
                self.system_logger.debug(f"⏳ {delay}s 후 재연결 시도…")
            time.sleep(delay)
            # 점진적 backoff 최대 60초
            self._reconnect_delay = min(self._reconnect_delay * 2, 60)
            self._start_public_websocket()

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
                    # ping을 주기적으로 보내 연결 유지
                    ws_app.run_forever(ping_interval=20, ping_timeout=10)
                except Exception as e:
                    if self.system_logger:
                        self.system_logger.exception(f"🔥 Public WebSocket 스레드 예외: {e}")
                    # 치명적 예외 시에도 재시도
                    time.sleep(self._reconnect_delay)
                    self._reconnect_delay = min(self._reconnect_delay * 2, 60)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
