# controllers/mt5/mt5_ws_controller.py
import threading
import time
import json
from typing import Optional

from websocket import WebSocketApp


class Mt5WebSocketController:
    """
    MT5 WebSocket 클라이언트 컨트롤러
    - 서버: wss://api.hyeongeonnoil.com/ws
    - 프로토콜:
        - subscribe: {"op": "subscribe", "args": ["tickers.SYMBOL", "kline.1.SYMBOL", ...]}
        - unsubscribe: {"op": "unsubscribe", "args": [...]}
        - ticker 수신: {"topic": "tickers.SYMBOL", "data": {...}}
        - kline 수신: {"topic": "kline.INTERVAL.SYMBOL", "data": [ {...}, ... ]}
    """

    def __init__(self, symbols=("EURUSD",), system_logger=None, base_ws_url: str | None = None):
        # 기본은 1분 봉
        self.kline_interval = "1"  # "1" = 1분봉
        self._last_kline: dict[tuple[str, str], dict] = {}  # {(symbol, interval): kline dict}
        self._last_kline_confirmed: dict[tuple[str, str], dict] = {}  # 마지막으로 마감된 봉

        self.symbols = list(symbols)
        self.system_logger = system_logger

        # 서버 WebSocket URL (역프록시 기준 wss://api.hyeongeonnoil.com/ws 가정)
        self.ws_url = base_ws_url or "wss://api.hyeongeonnoil.com/ws"

        # 공유 상태
        self._lock = threading.Lock()
        self.ws: WebSocketApp | None = None
        self._last_frame_monotonic = 0.0  # 마지막 프레임 수신 시각(monotonic)

        # 시세/타임스탬프(스레드 안전)
        self._prices: dict[str, float] = {}
        self._last_tick_monotonic: dict[str, float] = {}   # WS 신선도 판단용 (monotonic)
        self._last_exchange_ts: dict[str, float] = {}      # 서버가 준 ts(초 단위)

        # 재연결 backoff
        self._reconnect_delay = 5

        self._start_public_websocket()

    # ──────────────────────────────────────────────
    # 외부에서 쓰는 읽기 API
    # ──────────────────────────────────────────────
    def get_price(self, symbol: str) -> Optional[float]:
        with self._lock:
            return self._prices.get(symbol)

    def get_all_prices(self) -> dict[str, float]:
        with self._lock:
            return dict(self._prices)

    def get_last_tick_time(self, symbol: str) -> Optional[float]:
        """마지막 틱 수신 시각(monotonic) → 신선도 체크에 사용"""
        with self._lock:
            return self._last_tick_monotonic.get(symbol)

    def get_last_exchange_ts(self, symbol: str) -> Optional[float]:
        """서버가 제공한 마지막 업데이트 시각(초 단위)"""
        with self._lock:
            return self._last_exchange_ts.get(symbol)

    def get_last_frame_time(self) -> Optional[float]:
        """마지막으로 아무 메시지/프레임을 받은 시각(monotonic)"""
        return self._last_frame_monotonic or None

    def get_last_kline(self, symbol: str, interval: str | None = None) -> Optional[dict]:
        """
        마지막으로 수신한 kline (마감 여부와 상관 없음)
        반환 예시:
        {
            "symbol": "EURUSD",
            "interval": "1",
            "time": 1710000000,   # bar 시작 시간(sec, UTC epoch)
            "open": ...,
            "high": ...,
            "low": ...,
            "close": ...,
            "volume": ...,
            "confirm": True/False,
            "start": 1710000000000,      # ms
            "end":   1710000059999,      # ms
        }
        """
        interval = interval or self.kline_interval
        with self._lock:
            return self._last_kline.get((symbol, interval))

    def get_last_confirmed_kline(self, symbol: str, interval: str | None = None) -> Optional[dict]:
        """
        마지막으로 'confirm=True' 인 kline
        """
        interval = interval or self.kline_interval
        with self._lock:
            return self._last_kline_confirmed.get((symbol, interval))

    # ──────────────────────────────────────────────
    # 런타임 구독 제어
    # ──────────────────────────────────────────────
    def subscribe_symbols(self, *new_symbols: str):
        """
        런타임 중 심볼 추가 구독
        - tickers.SYM
        - kline.{interval}.SYM
        """
        to_add = [s for s in new_symbols if s not in self.symbols]
        if not to_add:
            return
        with self._lock:
            self.symbols.extend(to_add)

        ws = self.ws
        if ws:
            args = [f"tickers.{s}" for s in to_add] + [
                f"kline.{self.kline_interval}.{s}" for s in to_add
            ]
            msg = {"op": "subscribe", "args": args}
            try:
                ws.send(json.dumps(msg))
            except Exception:
                if self.system_logger:
                    self.system_logger.debug("MT5 WS subscribe 전송 실패(무시).")

    def unsubscribe_symbols(self, *symbols_to_remove: str):
        """
        런타임 중 심볼 구독 해제
        - 서버 구현상 unsubscribe 시 전체 토픽을 끊지만,
          args 는 호환 차원에서 전달.
        """
        to_remove = [s for s in symbols_to_remove if s in self.symbols]
        if not to_remove:
            return
        with self._lock:
            self.symbols = [s for s in self.symbols if s not in to_remove]

        ws = self.ws
        if ws:
            args = [f"tickers.{s}" for s in to_remove] + [
                f"kline.{self.kline_interval}.{s}" for s in to_remove
            ]
            msg = {"op": "unsubscribe", "args": args}
            try:
                ws.send(json.dumps(msg))
            except Exception:
                if self.system_logger:
                    self.system_logger.debug("MT5 WS unsubscribe 전송 실패(무시).")

    # ──────────────────────────────────────────────
    # 내부: WS 수명주기
    # ──────────────────────────────────────────────
    def _start_public_websocket(self):
        def on_open(ws: WebSocketApp):
            self.ws = ws
            self._reconnect_delay = 5
            self._last_frame_monotonic = time.monotonic()
            if self.system_logger:
                self.system_logger.debug("✅ MT5 WebSocket 연결됨")

            # 접속 시 현재 symbols 에 대해 ticker + kline 재구독
            args = [f"tickers.{sym}" for sym in self.symbols] + [
                f"kline.{self.kline_interval}.{sym}" for sym in self.symbols
            ]
            msg = {"op": "subscribe", "args": args}
            try:
                ws.send(json.dumps(msg))
            except Exception as e:
                if self.system_logger:
                    self.system_logger.debug(f"❌ MT5 subscribe 전송 실패: {e}")

        def on_pong(ws: WebSocketApp, data):
            # 핑/퐁만 와도 연결은 살아있다고 간주
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
                # subscribe 응답 등은 무시
                return

            with self._lock:
                # ─────────────────────
                # 1) Ticker
                # ─────────────────────
                if topic.startswith("tickers."):
                    # 서버: data 는 dict
                    item = data if isinstance(data, dict) else None
                    if not item:
                        return
                    sym = item.get("symbol") or topic.split(".")[1]

                    price_str = (
                        item.get("lastPrice")
                        or item.get("ask1Price")
                        or item.get("bid1Price")
                    )
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

                    self._prices[sym] = price
                    self._last_tick_monotonic[sym] = time.monotonic()
                    self._last_exchange_ts[sym] = exch_ts
                    return

                # ─────────────────────
                # 2) Kline
                # ─────────────────────
                if topic.startswith("kline."):
                    # topic: "kline.{interval}.{symbol}"
                    parts = topic.split(".")
                    if len(parts) < 3:
                        return
                    interval, sym = parts[1], parts[2]

                    # 서버: data 는 [ {time, open, high, low, close, volume, confirm}, ... ]
                    items = data if isinstance(data, list) else [data]

                    for bar in items:
                        try:
                            t_sec = int(bar["time"])  # sec
                            o = float(bar["open"])
                            h = float(bar["high"])
                            l = float(bar["low"])
                            c = float(bar["close"])
                            v = float(bar.get("volume", 0) or 0)
                            confirm = bool(bar.get("confirm", False))
                        except Exception:
                            # 필수 필드 없거나 타입 실패
                            continue

                        # interval 에 따라 bar 길이(ms) 계산 (서버 구현과 맞춤)
                        if interval == "1":
                            step_ms = 60 * 1000
                        else:
                            # 서버 구현: else 는 하루 단위
                            step_ms = 24 * 60 * 60 * 1000

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
                        self._last_kline[key] = k
                        if confirm:
                            self._last_kline_confirmed[key] = k

        def on_error(ws: WebSocketApp, error):
            if self.system_logger:
                self.system_logger.debug(f"❌ MT5 WebSocket 오류: {error}")

        def on_close(ws: WebSocketApp, *args):
            if self.system_logger:
                self.system_logger.debug("🔌 MT5 WebSocket closed.")
            # 끊길 때 핸들 비움
            self.ws = None
            # 재연결
            delay = self._reconnect_delay
            if self.system_logger:
                self.system_logger.debug(f"⏳ {delay}s 후 MT5 WS 재연결 시도…")
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
                    # ping_interval 을 사용해서 연결 유지
                    ws_app.run_forever(ping_interval=20, ping_timeout=10)
                except Exception as e:
                    if self.system_logger:
                        self.system_logger.exception(f"🔥 MT5 WebSocket 스레드 예외: {e}")
                    time.sleep(self._reconnect_delay)
                    self._reconnect_delay = min(self._reconnect_delay * 2, 60)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
