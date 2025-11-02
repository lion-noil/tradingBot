# controllers/controller.py

import requests
import hmac, hashlib
import threading
from websocket import WebSocketApp
from dotenv import load_dotenv
import os
from datetime import datetime, timezone, timedelta
import time
load_dotenv()
import json
KST = timezone(timedelta(hours=9))
from urllib.parse import urlencode
import math
from core.redis_client import redis_client

def _safe_int(x):
    try:
        return int(x)
    except Exception:
        return int(float(x))

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
            args = [f"tickers.{sym}" for sym in self.symbols] + [f"kline.{self.kline_interval}.{sym}" for sym in
                                                                 self.symbols]
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
                            price_str = item.get("lastPrice") or item.get("ask1Price") or item.get("bid1Price")
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



class BybitRestController:
    def __init__(self, system_logger=None):
        self.system_logger = system_logger
        self.base_url = "https://api-demo.bybit.com"
        self.api_key = os.getenv("BYBIT_TEST_API_KEY")
        self.api_secret = os.getenv("BYBIT_TEST_API_SECRET").encode()  # HMAC 서명용
        self.recv_window = "15000"
        self._time_offset_ms = 0
        self.leverage = 50
        self.FEE_RATE = 0.00055  # 0.055%

        self._symbol_rules = {}
        # ⏱ 서버-로컬 시간 동기화
        self.sync_time()

    # -------------------------
    # Path helpers (심볼별 로컬 파일 경로)
    # -------------------------

    def _fp_orders(self, symbol: str) -> str:
        return f"{symbol}_orders.json"

    def _fp_asset(self) -> str:
        return "asset.json"

    def _build_query(self, params_pairs: list[tuple[str, str]] | None) -> str:
        if not params_pairs:
            return ""
        return urlencode(params_pairs, doseq=False)

    def _request_with_resync(self, method: str, endpoint: str,
                             params_pairs: list[tuple[str, str]] | None = None,
                             body_dict: dict | None = None,
                             timeout: float = 5.0):
        base = self.base_url + endpoint
        query_string = self._build_query(params_pairs)
        url = f"{base}?{query_string}" if query_string else base

        body_str = ""

        def _make_headers():
            nonlocal body_str
            if body_dict is not None:
                body_str = json.dumps(body_dict, separators=(",", ":"), sort_keys=True)
            else:
                body_str = ""
            return self._get_headers(method, endpoint, params=query_string, body=body_str)

        def _send():
            hdrs = _make_headers()
            if method == "GET":
                return requests.get(url, headers=hdrs, timeout=timeout)
            else:
                hdrs = {**hdrs, "Content-Type": "application/json"}
                return requests.post(url, headers=hdrs, data=body_str, timeout=timeout)

        # 1차 시도
        resp = _send()
        j = None
        try:
            j = resp.json()
        except Exception:
            # JSON이 아니면 그대로 리턴
            return resp

        # 타임스탬프/윈도우 오류 감지
        ret_code = j.get("retCode")
        ret_msg = (j.get("retMsg") or "").lower()
        needs_resync = (
                ret_code == 10002 or
                "timestamp" in ret_msg or
                "recv_window" in ret_msg or
                "check your server timestamp" in ret_msg
        )

        if needs_resync:
            self.sync_time()
            resp = _send()

        return resp

    def sync_time(self):
        t0 = time.time()
        r = requests.get(f"{self.base_url}/v5/market/time", timeout=5)
        t1 = time.time()
        server_ms = int((r.json() or {}).get("time"))
        rtt_ms = (t1 - t0) * 1000.0
        local_est_ms = int(t1 * 1000 - rtt_ms / 2)
        self._time_offset_ms = server_ms - local_est_ms

    def _now_ms(self):
        # 미래 금지 마진으로 10ms 빼기
        return str(int(time.time() * 1000 + self._time_offset_ms - 10))

    def _generate_signature(self, timestamp, method, params="", body=""):
        query_string = params if method == "GET" else body
        payload = f"{timestamp}{self.api_key}{self.recv_window}{query_string}"
        return hmac.new(self.api_secret, payload.encode(), hashlib.sha256).hexdigest()

    def _get_headers(self, method, endpoint, params="", body=""):
        timestamp = self._now_ms()  # ✅ 오프셋 반영 & 미래 방지
        sign = self._generate_signature(timestamp, method,params=params, body=body)
        return {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": self.recv_window,
            "X-BAPI-SIGN": sign
        }

    def count_cross(self, closes, ma100s, threshold):
        count = 0
        cross_times = []  # 📌 크로스 발생 시간 저장
        last_state = None  # "above", "below", "in"
        closes = list(closes)  # 🔧 deque → list로 변환
        now_kst = datetime.now(KST)

        last_cross_time_up = None
        last_cross_time_down = None


        for i, (price, ma) in enumerate(zip(closes, ma100s)):
            if ma is None:  # MA100 계산 안된 구간은 건너뜀
                continue
            upper = ma * (1 + threshold)
            lower = ma * (1 - threshold)

            if price > upper:
                state = "above"
            elif price < lower:
                state = "below"
            else:
                state = "in"

            if last_state in ("below", "in") and state == "above":
                cross_time = now_kst - timedelta(minutes=len(closes) - i)
                if not last_cross_time_up or (cross_time - last_cross_time_up).total_seconds() > 1800:
                    count += 1
                    cross_times.append(("UP", cross_time.strftime("%Y-%m-%d %H:%M:%S"), upper, price, ma))
                    last_cross_time_up = cross_time


            if last_state in ("above", "in") and state == "below":
                cross_time = now_kst - timedelta(minutes=len(closes) - i)
                if not last_cross_time_down or (cross_time - last_cross_time_down).total_seconds() > 1800:
                    count += 1
                    cross_times.append(("DOWN", cross_time.strftime("%Y-%m-%d %H:%M:%S"), lower, price, ma))
                    last_cross_time_down = cross_time

            last_state = state

        return count, cross_times

    def find_optimal_threshold(self, closes, ma100s, min_thr=0.005, max_thr=0.05, target_cross=None):
        left, right = min_thr, max_thr
        optimal = max_thr
        for _ in range(20):  # 충분히 반복
            mid = (left + right) / 2
            crosses, _ = self.count_cross(closes, ma100s, mid)  # 시간은 무시

            if crosses > target_cross:
                left = mid  # threshold를 키워야 cross가 줄어듦
            else:
                optimal = mid
                right = mid
        crosses, cross_times = self.count_cross(closes, ma100s, optimal)

        return max(optimal, min_thr)

    def get_positions(self, symbol=None, category="linear"):
        symbol = symbol
        endpoint = "/v5/position/list"
        params_pairs = [("category", category), ("symbol", symbol)]
        resp = self._request_with_resync("GET", endpoint, params_pairs=params_pairs, body_dict=None, timeout=5)
        return resp.json()

    def load_orders(self, symbol: str):
        path = self._fp_orders(symbol)
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                return json.loads(content) if content else []
        except Exception as e:
            self.system_logger.error(f"거래기록 로드 실패: {e}")
            return []

    def save_orders(self, symbol: str, trades):
        path = self._fp_orders(symbol)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(trades, f, indent=2)
        except Exception as e:
            self.system_logger.error(f"[ERROR] 거래기록 저장 실패: {e}")

    def append_order(self, symbol: str, trade: dict):
        """
        trade 하나를 로컬 파일에 append (중복 방지)
        """
        try:
            local_orders = self.load_orders(symbol)
            existing_ids = {str(o.get("id")) for o in local_orders}
            if str(trade.get("id")) in existing_ids:
                self.system_logger.debug(f"⏩ 이미 존재하는 trade id={trade.get('id')} ({symbol}), 스킵")
                return local_orders

            local_orders.append(trade)
            self.save_orders(symbol, local_orders)
            self.system_logger.debug(f"📥 ({symbol}) 신규 trade {trade.get('id')} 저장됨")
            return local_orders
        except Exception as e:
            self.system_logger.error(f"[ERROR] 거래기록 append 실패: {e}")
            return self.load_orders(symbol)


    def sync_orders_from_bybit(self, symbol="BTCUSDT"):

        ####
        method = "GET"
        category = "linear"
        limit = 20
        endpoint = "/v5/execution/list"
        params_dict = {
            "category": category,
            "symbol": symbol,
            "limit": limit
        }
        params_str = "&".join([f"{k}={params_dict[k]}" for k in sorted(params_dict)])
        url = f"{self.base_url}{endpoint}?{params_str}"

        def _fetch_once():
            # 매 호출마다 재서명(타임스탬프 최신화)
            headers = self._get_headers(method, endpoint, params=params_str, body="")
            try:
                resp = requests.get(url, headers=headers, timeout=5)
                # HTTP 레벨 오류
                if resp.status_code != 200:
                    self.system_logger.error(f"❌ HTTP 오류 {resp.status_code}: {resp.text[:200]}")
                    return None
                try:
                    data = resp.json()
                except Exception:
                    self.system_logger.error(f"❌ JSON 파싱 실패: {resp.text[:200]}")
                    return None
                if data.get("retCode") != 0:
                    self.system_logger.error(f"❌ Bybit 오류 retCode={data.get('retCode')}, retMsg={data.get('retMsg')}")
                    return None
                result = data.get("result") or {}
                lst = result.get("list")
                if not isinstance(lst, list):
                    self.system_logger.error(f"❌ result.list가 리스트가 아님: {type(lst)}")
                    return None
                return lst
            except requests.exceptions.Timeout:
                self.system_logger.error("⏱️ 요청 타임아웃")
                return None
            except requests.exceptions.RequestException as e:
                self.system_logger.error(f"🌐 네트워크 예외: {e}")
                return None

        # 1차 요청
        executions = _fetch_once()
        # (옵션) 실패 시 1회 재시도
        if executions is None:
            self.system_logger.debug("↻ 재시도: 서명/타임스탬프 갱신")
            executions = _fetch_once()
            if executions is None:
                # 완전 실패면 기존 로컬 그대로 반환
                return self.load_orders(symbol)

        ####
        try:
            local_orders = self.load_orders(symbol)
            existing_ids = {str(order["id"]) for order in local_orders}
            appended = 0
            for e in reversed(executions):
                if e.get("execType") != "Trade" or float(e.get("execQty", 0)) == 0:
                    continue

                exec_id = str(e["execId"])
                if exec_id in existing_ids:
                    continue

                side = e["side"]
                position_side = "LONG" if side == "Buy" else "SHORT"
                trade_type = "OPEN" if float(e.get("closedSize", 0)) == 0 else "CLOSE"

                try:
                    exec_price = float(e["execPrice"])
                except (ValueError, TypeError):
                    exec_price = 0.0

                trade = {
                    "id": exec_id,
                    "symbol": e["symbol"],
                    "side": position_side,  # LONG / SHORT
                    "type": trade_type,  # OPEN / CLOSE
                    "qty": float(e["execQty"]),
                    "price": exec_price,
                    "time": int(e["execTime"]),
                    "time_str": datetime.fromtimestamp(int(e["execTime"]) / 1000, tz=KST).strftime("%Y-%m-%d %H:%M:%S"),
                    "fee": float(e.get("execFee", 0))
                }

                local_orders.append(trade)
                existing_ids.add(exec_id)
                appended += 1

            # ✅ 시간순 정렬 (옛날 → 최신)
            if local_orders:
                local_orders.sort(key=lambda x: x.get("time", 0))

            if appended > 0:
                self.save_orders(symbol, local_orders)
                self.system_logger.debug(f"📥 ({symbol}) 신규 체결 {appended}건 저장됨")
            return local_orders

        except Exception as e:
            self.system_logger.error(f"[ERROR] 주문 동기화 실패: {e}")
            return self.load_orders(symbol)

    def get_trade_w_order_id(self, symbol="BTCUSDT",order_id=None):
        if not order_id:
            self.system_logger.error("❌ order_id가 필요합니다.")
            return self.load_orders()

        method = "GET"
        endpoint = "/v5/execution/list"
        params_dict = {
            "category": "linear",
            "symbol": symbol,
            "orderId": order_id,  # orderId 필터링 → limit 불필요
        }

        # 공통 GET 유틸
        def _fetch_once() -> list | None:
            params_str = "&".join([f"{k}={params_dict[k]}" for k in sorted(params_dict)])
            url = f"{self.base_url}{endpoint}?{params_str}"
            headers = self._get_headers(method, endpoint, params=params_str, body="")
            try:
                resp = requests.get(url, headers=headers, timeout=5)
                if resp.status_code != 200:
                    self.system_logger.error(f"❌ HTTP 오류 {resp.status_code}: {resp.text[:200]}")
                    return None
                try:
                    data = resp.json()
                except Exception:
                    self.system_logger.error(f"❌ JSON 파싱 실패: {resp.text[:200]}")
                    return None
                if data.get("retCode") != 0:
                    self.system_logger.error(f"❌ Bybit 오류 retCode={data.get('retCode')}, retMsg={data.get('retMsg')}")
                    return None
                result = data.get("result") or {}
                lst = result.get("list")
                if not isinstance(lst, list):
                    self.system_logger.error(f"❌ result.list가 리스트가 아님: {type(lst)}")
                    return None
                return lst
            except requests.exceptions.Timeout:
                self.system_logger.error("⏱️ 요청 타임아웃")
                return None
            except requests.exceptions.RequestException as e:
                self.system_logger.error(f"🌐 네트워크 예외: {e}")
                return None

        t1 = time.time()
        exec_timeout_sec = 10
        poll_interval_sec = 1

        while True:
            executions = _fetch_once()
            if executions !=[]:
                found = True
            if executions:
                break
            if time.time() - t1 > exec_timeout_sec:
                self.system_logger.error(f"⏰ executions 반영 대기 타임아웃({exec_timeout_sec}s). 부분 체결/전파 지연 가능.")
                break
            time.sleep(poll_interval_sec)
        if not executions:
            return []

        e = executions[0]
        exec_id = str(e["execId"])
        side = e["side"]
        position_side = "LONG" if side == "Buy" else "SHORT"
        trade_type = "OPEN" if float(e.get("closedSize", 0)) == 0 else "CLOSE"

        try:
            exec_price = float(e["execPrice"])
        except (ValueError, TypeError):
            exec_price = 0.0

        trade = {
            "id": exec_id,
            "symbol": e["symbol"],
            "side": position_side,  # LONG / SHORT
            "type": trade_type,  # OPEN / CLOSE
            "qty": float(e["execQty"]),
            "price": exec_price,
            "time": int(e["execTime"]),
            "time_str": datetime.fromtimestamp(int(e["execTime"]) / 1000, tz=KST).strftime("%Y-%m-%d %H:%M:%S"),
            "fee": float(e.get("execFee", 0))
        }

        return trade

    def get_usdt_balance(self):
        method = "GET"
        endpoint = "/v5/account/wallet-balance"
        coin = "USDT"
        params_pairs = [("accountType", "UNIFIED"), ("coin", coin)]

        try:
            resp = self._request_with_resync(method, endpoint, params_pairs=params_pairs, body_dict=None, timeout=5)
            data = resp.json()
        except Exception as e:
            self.system_logger.error(f"[ERROR] 지갑 조회 실패 (API): {e}")
            return None

        # API 에러 처리
        if not isinstance(data, dict) or data.get("retCode") != 0:
            self.system_logger.error(
                f"[ERROR] 잔고 조회 실패: {data.get('retMsg') if isinstance(data, dict) else 'Unknown error'}")
            return None

        try:
            account = (data.get("result", {}).get("list") or [{}])[0]
            coin_list = account.get("coin", [])
            coin_data = next((c for c in coin_list if c.get("coin") == coin), {})

            # 1순위: 코인 레벨 walletBalance
            wb_raw = coin_data.get("walletBalance")

            # 값이 없거나 빈 문자열이면 계정 레벨 totalWalletBalance로 폴백
            if wb_raw in (None, "", "null"):
                wb_raw = account.get("totalWalletBalance", 0)

            wallet_balance = float(wb_raw or 0)

            result = {
                "coin": coin_data.get("coin") or coin,  # 명시적으로 USDT 표기
                "wallet_balance": wallet_balance,  # 스네이크 케이스 권장
            }

        except Exception as e:
            self.system_logger.error(f"[ERROR] 지갑 응답 파싱 실패: {e}")
            return None

        return result

    def _json_or_empty_list(self,inpobj):
        # 포지션 없으면 [] 그대로, 있으면 compact JSON
        if inpobj is None:
            return "[]"
        return json.dumps(inpobj, separators=(",", ":"), ensure_ascii=False)


    def getNsav_asset(self, asset, symbol : str = None, save_redis: bool = True):
        result = self.get_usdt_balance()

        if asset["wallet"]["USDT"] != result["wallet_balance"] and save_redis:
            try:
                redis_client.hset("asset", f"wallet.{result['coin']}", f"{result['wallet_balance']:.10f}")
                asset["wallet"]["USDT"] = result["wallet_balance"]
            except Exception as e:
                if self.system_logger:
                    self.system_logger.error(f"[WARN] Redis 저장 실패: {e}")

        try:
            resp = self.get_positions(symbol=symbol)
            rows = (resp.get("result") or {}).get("list") or []
        except Exception:
            rows = []

        long_pos, short_pos = None, None

        for r in rows:
            size = float(r.get("size", 0) or 0)
            if size == 0:
                continue

            avg_price = float(r.get("avgPrice", 0) or 0)
            idx = r.get("positionIdx")

            if idx == 1:
                long_pos = {"qty": size, "avg_price": avg_price}
            elif idx == 2:
                short_pos = {"qty": size, "avg_price": avg_price}
            else:
                side = r.get("side", "").upper()
                if side == "BUY":
                    long_pos = {"qty": size, "avg_price": avg_price}
                elif side == "SELL":
                    short_pos = {"qty": size, "avg_price": avg_price}

        local_orders = self.load_orders(symbol)

        if long_pos is not None:
            long_pos["entries"] = self._build_entries_from_orders(
                local_orders, symbol, "LONG", long_pos["qty"]
            )
        if short_pos is not None:
            short_pos["entries"] = self._build_entries_from_orders(
                local_orders, symbol, "SHORT", short_pos["qty"]
            )
        asset["positions"][symbol]["LONG"] = long_pos
        asset["positions"][symbol]["SHORT"] = short_pos

        if save_redis:
            try:
                redis_client.hset("asset", f"positions.{symbol}", self._json_or_empty_list(asset["positions"][symbol]))
            except Exception as e:
                if self.system_logger:
                    self.system_logger.error(f"[WARN] Redis 저장 실패({symbol}): {e}")

        return asset

    def _build_entries_from_orders(self, local_orders: list, symbol: str, direction: str, target_qty: float):
        if not target_qty or target_qty <= 0:
            return []

        # 해당 심볼, 해당 방향(LONG/SHORT), OPEN 체결만 추출
        open_orders = [
            o for o in local_orders
            if o.get("symbol") == symbol and o.get("side") == direction and o.get("type") == "OPEN"
        ]
        # 최신부터 소비하기 위해 시간 내림차순
        open_orders.sort(key=lambda x: x.get("time", 0), reverse=True)

        remaining = float(target_qty)
        picked = []
        for o in open_orders:
            if remaining <= 1e-12:
                break
            this_qty = float(o.get("qty", 0.0) or 0.0)
            use_qty = min(this_qty, remaining)
            ts_ms = int(o.get("time", 0) or 0)
            picked.append({
                "ts": ts_ms,
                "qty": use_qty,
                "price": float(o.get("price", 0.0) or 0.0),
                "ts_str": datetime.fromtimestamp(ts_ms / 1000, tz=KST).strftime("%Y-%m-%d %H:%M:%S"),
            })
            remaining -= use_qty

        # 오래된 → 최신 순으로 정렬해 반환
        picked.sort(key=lambda x: x["ts"])
        return picked


    def update_candles(self, candles, symbol=None, count=None):
        try:
            symbol = symbol or self.symbol
            url = f"{self.base_url}/v5/market/kline"

            target = count if (isinstance(count, int) and count > 0) else 1000
            all_candles = []
            latest_end = None  # ms

            while len(all_candles) < target:
                req_limit = min(1000, target - len(all_candles))
                params = {
                    "category": "linear",
                    "symbol": symbol,
                    "interval": "1",
                    "limit": req_limit,
                }
                if latest_end is not None:
                    params["end"] = latest_end

                res = requests.get(url, params=params, timeout=10)
                res.raise_for_status()

                data = res.json()
                if not isinstance(data, dict):
                    raise RuntimeError(f"unexpected JSON root: {type(data).__name__}")

                ret_code = data.get("retCode", 0)
                if ret_code != 0:
                    ret_msg = data.get("retMsg")
                    raise RuntimeError(f"bybit error retCode={ret_code}, retMsg={ret_msg}")

                result = data.get("result", {})
                if isinstance(result, dict):
                    raw_list = result.get("list") or []
                elif isinstance(result, list):
                    raw_list = result
                else:
                    raise RuntimeError(f"unexpected 'result' type: {type(result).__name__}")

                if not isinstance(raw_list, list):
                    raise RuntimeError(f"'list' is {type(raw_list).__name__}, not list")

                if not raw_list:
                    break

                raw_list = raw_list[::-1]

                chunk = []
                for c in raw_list:
                    try:
                        if not isinstance(c, (list, tuple)) or len(c) < 5:
                            continue
                        item = {
                            "start": _safe_int(c[0]),
                            "open": float(c[1]),
                            "high": float(c[2]),
                            "low": float(c[3]),
                            "close": float(c[4]),
                        }
                        chunk.append(item)
                    except Exception:
                        continue

                if chunk:
                    all_candles = chunk + all_candles
                    latest_end = _safe_int(raw_list[0][0]) - 1
                else:
                    break

                if len(raw_list) < req_limit:
                    break

            if isinstance(count, int) and count > 0:
                all_candles = all_candles[-count:]

            candles.clear()
            candles.extend(all_candles)

            last = candles[-1] if candles else None
            if last:
                self.system_logger.debug(
                    f"📊 ({symbol}) 캔들 갱신 완료: {len(candles)}개, last OHLC=({last['open']}, {last['high']}, {last['low']}, {last['close']})"
                )
            else:
                self.system_logger.debug(f"📊 ({symbol}) 캔들 갱신: 결과 없음")

        except Exception as e:
            self.system_logger.warning(f"❌ ({symbol}) 캔들 요청 실패: {e}")

    def ma100_list(self, closes):
        closes_list = list(closes)
        ma100s = []
        for i in range(len(closes_list)):
            if i < 99:
                ma100s.append(None)  # MA100 계산 안 되는 구간
            else:
                ma100s.append(sum(closes_list[i - 99:i + 1]) / 100)
        return ma100s

    def set_leverage(self, symbol="BTCUSDT", leverage=10, category="linear"):
        try:
            endpoint = "/v5/position/set-leverage"
            url = self.base_url + endpoint
            method = "POST"

            payload = {
                "category": category,
                "symbol": symbol,
                "buyLeverage": str(leverage),
                "sellLeverage": str(leverage)
            }

            body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
            headers = self._get_headers(method, endpoint, body=body)

            response = requests.post(url, headers=headers, data=body)

            if response.status_code == 200:
                data = response.json()
                ret_code = data.get("retCode")
                if ret_code == 0:
                    self.system_logger.debug(f"✅ 레버리지 {leverage}x 설정 완료 | 심볼: {symbol}")
                    return True
                elif ret_code == 110043:
                    self.system_logger.debug(f"⚠️ 이미 설정된 레버리지입니다: {leverage}x | 심볼: {symbol}")
                    return True  # 이건 실패 아님
                else:
                    self.system_logger.error(f"❌ 레버리지 설정 실패: {data.get('retMsg')} (retCode {ret_code})")
            else:
                self.system_logger.error(f"❌ HTTP 오류: {response.status_code} {response.text}")
        except Exception as e:
            self.system_logger.error(f"❌ 레버리지 설정 중 예외 발생: {e}")

        return False
    def wait_order_fill(self, symbol, order_id, max_retries=10, sleep_sec=1):
        endpoint = "/v5/order/realtime"
        base = self.base_url + endpoint

        params_pairs = [
            ("category", "linear"),
            ("symbol", symbol),
            ("orderId", order_id),
        ]
        # 2) 실제 전송될 쿼리스트링(인코딩 포함) 생성
        query_string = urlencode(params_pairs, doseq=False)

        # 4) 요청에도 '동일한 문자열'을 그대로 사용 (dict/params 쓰지 말고 완성 URL로)
        url = f"{base}?{query_string}"

        for i in range(max_retries):
            # 3) 이 쿼리스트링으로 서명 생성 (GET은 body 대신 queryString 사용)
            headers = self._get_headers("GET", endpoint, params=query_string, body="")

            r = requests.get(url, headers=headers, timeout=5)
            # retCode 확인 (에러면 디버그 찍고 다음 루프)
            try:
                data = r.json()
            except Exception:
                self.system_logger.debug(f"응답 JSON 파싱 실패: {r.text[:200]}")
                data = {}

            orders = data.get("result", {}).get("list", [])
            if orders:
                o = orders[0]
                status = (o.get("orderStatus") or "").upper()
                # ✅ 가득 체결만 인정
                if status == "FILLED" and str(o.get("cumExecQty")) not in ("0", "0.0", "", None):
                    return o
                # ❌ 취소/거절이면 즉시 반환 (호출부에서 분기)
                if status in ("CANCELLED", "REJECTED"):
                    return o

                # 그 외(New/PartiallyFilled 등)는 계속 대기
            self.system_logger.debug(
                f"⌛ 주문 체결 대기중... ({i + 1}/{max_retries}) | 심볼: {symbol} | 주문ID: {order_id[-6:]}"
            )
            time.sleep(sleep_sec)

            # ⏰ 타임아웃: 호출부가 분기할 수 있게 '타임아웃 상태' 반환
        return {"orderId": order_id, "orderStatus": "TIMEOUT"}

    def submit_market_order(self, symbol, order_side, qty, position_idx, reduce_only=False):
        endpoint = "/v5/order/create"
        body = {
            "category": "linear",
            "symbol": symbol,
            "side": order_side,
            "orderType": "Market",
            "qty": str(qty),
            "positionIdx": position_idx,
            "reduceOnly": bool(reduce_only),
            "timeInForce": "IOC",
        }
        resp = self._request_with_resync("POST", endpoint, params_pairs=None, body_dict=body, timeout=5)
        if resp.status_code != 200:
            self.system_logger.error(f"❌ HTTP 오류: {resp.status_code} {resp.text}")
            return None
        data = resp.json()
        if data.get("retCode") == 0:
            return data.get("result", {})
        self.system_logger.error(f"❌ 주문 실패: {data.get('retMsg')} (코드 {data.get('retCode')})")
        return None

    def open_market(self, symbol, side, price, percent, wallet):
        if price is None or wallet is None:
            self.system_logger.error("❌ 가격 또는 잔고 정보가 누락되었습니다.")
            return None

        total_balance = wallet.get("USDT", 0)
        raw_qty = total_balance * self.leverage / price * percent / 100.0
        qty = self.normalize_qty(symbol, raw_qty, mode="floor")
        if qty <= 0:
            self.system_logger.error(
                f"❗ 주문 수량이 최소단위 미만입니다. raw={raw_qty:.8f}, norm={qty:.8f} ({symbol})"
            )
            return None

        if side.lower() == "long":
            order_side, position_idx = "Buy", 1
        elif side.lower() == "short":
            order_side, position_idx = "Sell", 2
        else:
            self.system_logger.error(f"❌ 알 수 없는 side 값: {side}")
            return None

        self.system_logger.debug(
            f"📥 {side.upper()} 진입 시도 | raw_qty={raw_qty:.8f} → qty={qty:.8f} @ {price:.2f} ({symbol})"
        )
        return self.submit_market_order(symbol, order_side, qty, position_idx, reduce_only=False)

    def close_market(self, symbol, side, qty):
        qty = float(qty)
        qty = self.normalize_qty(symbol, qty, mode="floor")  # 청산은 floor가 안전
        if qty <= 0:
            self.system_logger.warning("❗ 청산 수량이 최소단위 미만입니다. 중단.")
            return None

        if side.upper() == "LONG":
            order_side, position_idx = "Sell", 1
        elif side.upper() == "SHORT":
            order_side, position_idx = "Buy", 2
        else:
            self.system_logger.error(f"❌ 알 수 없는 side 값: {side}")
            return None

        self.system_logger.debug(f"📤 {side.upper()} 포지션 청산 시도 | qty={qty:.8f} ({symbol})")
        return self.submit_market_order(symbol, order_side, qty, position_idx, reduce_only=True)

    def cancel_order(self, symbol, order_id):
        endpoint = "/v5/order/cancel"
        url = self.base_url + endpoint
        method = "POST"
        payload = {
            "category": "linear",
            "symbol": symbol,
            "orderId": order_id
        }
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        headers = self._get_headers(method, endpoint, body=body)
        headers["Content-Type"] = "application/json"
        r = requests.post(url, headers=headers, data=body, timeout=5)
        return r.json()

    def fetch_symbol_rules(self, symbol: str, category: str = "linear") -> dict:
        """
        v5/market/instruments-info에서 lotSizeFilter/priceFilter를 읽어 규칙 반환.
        네트워크/응답 이슈시 예외를 올림.
        """
        url = f"{self.base_url}/v5/market/instruments-info"
        params = {"category": category, "symbol": symbol}
        r = requests.get(url, params=params, timeout=5)
        r.raise_for_status()
        j = r.json()
        if j.get("retCode") != 0:
            raise RuntimeError(f"retCode={j.get('retCode')}, retMsg={j.get('retMsg')}")
        lst = (j.get("result") or {}).get("list") or []
        if not lst:
            raise RuntimeError("empty instruments list")
        info = lst[0]
        lot = info.get("lotSizeFilter", {}) or {}
        price = info.get("priceFilter", {}) or {}

        rules = {
            "qtyStep": float(lot.get("qtyStep", 0) or 0),
            "minOrderQty": float(lot.get("minOrderQty", 0) or 0),
            "maxOrderQty": float(lot.get("maxOrderQty", 0) or 0),
            "tickSize": float(price.get("tickSize", 0) or 0),
            "minPrice": float(price.get("minPrice", 0) or 0),
            "maxPrice": float(price.get("maxPrice", 0) or 0),
        }
        # 방어: 기본값 보정
        if rules["qtyStep"] <= 0:
            rules["qtyStep"] = 0.001  # 안전 폴백
        if rules["minOrderQty"] <= 0:
            rules["minOrderQty"] = rules["qtyStep"]

        self._symbol_rules[symbol] = rules
        return rules

    def get_symbol_rules(self, symbol: str) -> dict:
        return self._symbol_rules.get(symbol) or self.fetch_symbol_rules(symbol)

    # BybitRestController에 추가
    def _round_step(self, value: float, step: float, mode: str = "floor") -> float:
        """
        step 단위로 라운딩. mode: floor/ceil/round
        """
        if step <= 0:
            return float(value)
        n = float(value) / step
        if mode == "ceil":
            n = math.ceil(n - 1e-12)
        elif mode == "round":
            n = round(n)
        else:
            n = math.floor(n + 1e-12)
        return float(f"{n * step:.8f}")  # 부동소수 잡음 방지

    def normalize_qty(self, symbol: str, qty: float, mode: str = "floor") -> float:
        """
        심볼 규칙(qtyStep/minOrderQty)에 맞춰 수량 정규화.
        - open: 보통 'floor' (과다 주문 방지)
        - close: 보통 'floor' (잔량 남을 수 있으나 초과주문 방지)
        """
        rules = self.get_symbol_rules(symbol)
        step = rules.get("qtyStep", 0.001) or 0.001
        min_qty = rules.get("minOrderQty", step) or step
        q = max(0.0, float(qty))
        q = self._round_step(q, step, mode=mode)
        if q < min_qty:
            return 0.0
        # (옵션) maxOrderQty 적용 원하면 여기에서 min(q, maxOrderQty)
        return q
