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


def _safe_int(x):
    try:
        return int(x)
    except Exception:
        return int(float(x))

class BybitWebSocketController:
    def __init__(self, symbol="BTCUSDT",system_logger=None):
        self.symbol = symbol
        self.system_logger = system_logger
        self.ws_url = "wss://stream.bybit.com/v5/public/linear"
        self.private_ws_url = "wss://stream-demo.bybit.com/v5/private"
        # self.private_ws_url = "wss://stream.bybit.com/v5/private"  # 실전용
        self.price = None
        self.ws = None
        self.api_key = os.getenv("BYBIT_TEST_API_KEY")
        self.api_secret = os.getenv("BYBIT_TEST_API_SECRET")

        self.position = None
        # self._start_private_websocket()
        self._start_public_websocket()

    def _start_public_websocket(self):
        def on_open(ws):
            self.system_logger.debug("✅ Public WebSocket 연결됨")
            subscribe = {
                "op": "subscribe",
                "args": [f"tickers.{self.symbol}"]
            }
            ws.send(json.dumps(subscribe))

        def on_message(ws, message):
            try:
                parsed = json.loads(message)
                if "data" not in parsed or not parsed["data"]:
                    return
                data = parsed["data"]
                if "lastPrice" in data:
                    self.price = float(data["lastPrice"])
                elif "ask1Price" in data:
                    self.price = float(data["ask1Price"])
            except Exception as e:
                self.system_logger.debug(f"❌ Public 메시지 처리 오류: {e}")

        def on_error(ws, error):
            self.system_logger.debug(f"❌ Public WebSocket 오류: {error}")

        def on_close(ws, *args):
            self.system_logger.debug("🔌 WebSocket closed. Reconnecting in 5 seconds...")
            time.sleep(5)
            self._start_public_websocket()  # or private

        def run():
            try:
                ws_app = WebSocketApp(
                    self.ws_url,
                    on_open=on_open,
                    on_message=on_message,
                    on_error=on_error,
                    on_close=on_close
                )
                ws_app.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                self.system_logger.exception(f"🔥 Public WebSocket 스레드 예외: {e}")
                time.sleep(5)
                self._start_public_websocket()

        thread = threading.Thread(target=run)



        thread.daemon = True
        thread.start()

    def _start_private_websocket(self):
        def on_open(ws):
            try:
                self.system_logger.debug("🔐 Private WebSocket 연결됨")
                expires = str(int((time.time() + 10) * 1000))  # ✅ ms 단위로 변경

                signature_payload = f"GET/realtime{expires}"
                signature = hmac.new(
                    self.api_secret.encode("utf-8"),
                    signature_payload.encode("utf-8"),
                    hashlib.sha256
                ).hexdigest()

                auth_payload = {
                    "op": "auth",
                    "args": [self.api_key, expires, signature]
                }
                ws.send(json.dumps(auth_payload))
            except Exception as e:
                self.system_logger.exception(f"❌ 인증 요청 실패: {e}")

        def on_message(ws, message):
            try:
                parsed = json.loads(message)
                if parsed.get("op") == "auth":
                    if parsed.get("success"):
                        self.system_logger.debug("✅ 인증 성공, 포지션 구독 시작")
                        time.sleep(0.5)  # 🔧 구독 전 0.5초 대기
                        ws.send(json.dumps({
                            "op": "subscribe",
                            "args": ["position.linear", "execution", "order", "wallet"]
                        }))
                    else:
                        self.system_logger.error(f"❌ 인증 실패: {parsed}")

                elif parsed.get("op") == "subscribe":
                    self.system_logger.debug(f"✅ 구독 성공 응답: {parsed}")


                elif "topic" in parsed and parsed["topic"].startswith("position"):

                    data = parsed.get("data", [])
                    if data:
                        self.position = data[0]
            except Exception as e:
                self.system_logger.debug(f"❌ Private 메시지 처리 오류: {e}")

        def on_error(ws, error):
            self.system_logger.error(f"❌ WebSocket 오류 발생: {error}")
            ws.close()

        def on_close(ws, *args):
            self.system_logger.debug("🔌 Private WebSocket 종료됨. 5초 후 재연결 시도...")
            time.sleep(5)
            self._start_private_websocket()

        def run():
            try:
                ws_app = WebSocketApp(
                    self.private_ws_url,
                    on_open=on_open,
                    on_message=on_message,
                    on_error=on_error,
                    on_close=on_close
                )
                ws_app.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                self.system_logger.exception(f"🔥 Private WebSocket 스레드 예외: {e}")
                time.sleep(5)
                self._start_private_websocket()

        thread = threading.Thread(target=run)
        thread.daemon = True
        thread.start()

class BybitRestController:
    def __init__(self, symbol="BTCUSDT", system_logger=None):
        self.symbol = symbol
        self.system_logger = system_logger
        self.base_url = "https://api-demo.bybit.com"
        self.api_key = os.getenv("BYBIT_TEST_API_KEY")
        self.api_secret = os.getenv("BYBIT_TEST_API_SECRET").encode()  # HMAC 서명용
        self.recv_window = "15000"
        self._time_offset_ms = 0  # ✅ 오프셋 초기화
        self.positions_file = f"{symbol}_positions.json"
        self.orders_file = f"{symbol}_orders.json"
        self.wallet_file = f"{symbol}_wallet.json"
        self.leverage = 50
        self.sync_time()
        self.set_leverage(leverage = self.leverage)
        self.FEE_RATE = 0.00055  # 0.055%

    def _build_query(self, params_pairs: list[tuple[str, str]] | None) -> str:
        # dict 말고 '순서 있는 리스트'로 받아서, 이 순서대로 정확히 인코딩 → 서명/전송 모두 동일 문자열 사용
        if not params_pairs:
            return ""
        return urlencode(params_pairs, doseq=False)

    def _request_with_resync(self, method: str, endpoint: str,
                             params_pairs: list[tuple[str, str]] | None = None,
                             body_dict: dict | None = None,
                             timeout: float = 5.0):
        """
        1) 쿼리/바디 문자열 생성
        2) 헤더(타임스탬프/서명) 생성
        3) 요청 전송
        4) timestamp 관련 에러면 sync_time 후 1회 재시도
        """
        base = self.base_url + endpoint
        query_string = self._build_query(params_pairs)
        url = f"{base}?{query_string}" if query_string else base

        body_str = ""
        headers = None

        def _make_headers():
            nonlocal body_str
            if body_dict is not None:
                # Bybit 권장: JSON을 key 정렬한 문자열로 서명
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
            # 즉시 재동기화 후 재시도(재서명 포함)
            self.sync_time()
            resp = _send()

        return resp

    def sync_time(self):
        # NTP 스타일 왕복지연 보정
        t0 = time.time()
        r = requests.get(f"{self.base_url}/v5/market/time", timeout=5)
        t1 = time.time()

        # Bybit v5 응답은 보통 {"time": "173...."} (ms, 문자열)
        server_ms = int((r.json() or {}).get("time"))
        rtt_ms = (t1 - t0) * 1000.0
        # 편도 지연을 뺀 '로컬 기준' 시각을 만들고 그에 대한 오프셋 저장
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

            # 📌 크로스 감지
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
        symbol = symbol or self.symbol
        endpoint = "/v5/position/list"
        params_pairs = [("category", category), ("symbol", symbol)]

        resp = self._request_with_resync("GET", endpoint, params_pairs=params_pairs, body_dict=None, timeout=5)
        data = resp.json()
        return data


    def load_local_positions(self):
        if not os.path.exists(self.positions_file):
            return []
        try:
            with open(self.positions_file, "r", encoding="utf-8") as f:
                content = f.read().strip()
                return json.loads(content) if content else []
        except Exception as e:
            self.system_logger.error(f"[ERROR] 로컬 포지션 파일 읽기 오류: {e}")
            return []

    def save_local_positions(self, data):
        try:
            with open(self.positions_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.system_logger.error(f"[ERROR] 포지션 저장 실패: {e}")

    def set_full_position_info(self, symbol="BTCUSDT"):
        # Bybit에서 포지션 조회
        result = self.get_positions(symbol=symbol)
        new_positions = result.get("result", {}).get("list", [])
        new_positions = [p for p in new_positions if float(p.get("size", 0)) != 0]

        local_positions = self.load_local_positions()

        def clean_position(pos):
            """불변 비교 + 저장을 위한 핵심 필드"""
            return {
                "symbol": pos.get("symbol"),
                "side": pos.get("side"),
                "size": str(pos.get("size")),
                "avgPrice": str(pos.get("avgPrice")),
                "leverage": str(pos.get("leverage")),
                "positionValue": str(pos.get("positionValue", "")),  # 평가금액
                "positionStatus": pos.get("positionStatus"),  # Normal 등 상태
            }

        cleaned_local = [clean_position(p) for p in local_positions]
        cleaned_new = [clean_position(p) for p in new_positions]

        if json.dumps(cleaned_local, sort_keys=True) != json.dumps(cleaned_new, sort_keys=True):
            self.system_logger.debug("📌 포지션 변경 감지됨 → 로컬 파일 업데이트")
            self.save_local_positions(cleaned_new)


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
                # Bybit API 레벨 오류
                ret_code = data.get("retCode")
                if ret_code != 0:
                    self.system_logger.error(f"❌ Bybit 오류 retCode={ret_code}, retMsg={data.get('retMsg')}")
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
                return self.load_orders()

        ####
        try:
            local_orders = self.load_orders()
            existing_ids = {str(order["id"]) for order in local_orders}
            appended = 0
            for e in reversed(executions):
                if e.get("execType") != "Trade" or float(e.get("execQty", 0)) == 0:
                    continue

                exec_id = str(e["execId"])
                if exec_id in existing_ids:
                    continue

                # 포지션 방향 추정 (Buy → Long / Sell → Short)
                side = e["side"]
                position_side = "LONG" if side == "Buy" else "SHORT"

                # 진입/청산 추정: 임시 기준 - 시장가 + 잔여 수량 0이면 청산
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
                self.save_orders(local_orders)
                self.system_logger.debug(f"📥 신규 체결 {appended}건 저장됨")
            return local_orders

        except Exception as e:
            self.system_logger.error(f"[ERROR] 주문 동기화 실패: {e}")
            return self.load_orders()

    def get_trade_w_order_id(self, symbol="BTCUSDT",order_id=None):

        ####
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
        found = False
        poll_interval_sec = 1

        method = "GET"
        category = "linear"
        endpoint = "/v5/execution/list"
        params_dict = {
            "category": category,
            "symbol": symbol,
            "orderId": order_id,
        }

        while True:
            executions = _fetch_once()
            if executions !=[]:
                found = True
            if found:
                break
            if time.time() - t1 > exec_timeout_sec:
                self.system_logger.error(f"⏰ executions 반영 대기 타임아웃({exec_timeout_sec}s). 부분 체결/전파 지연 가능.")

            time.sleep(poll_interval_sec)
        e = executions[0]
        exec_id = str(e["execId"])

        # 포지션 방향 추정 (Buy → Long / Sell → Short)
        side = e["side"]
        position_side = "LONG" if side == "Buy" else "SHORT"

        # 진입/청산 추정: 임시 기준 - 시장가 + 잔여 수량 0이면 청산
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

    def get_current_position_status(self, symbol="BTCUSDT"):
        local_positions = self.load_local_positions()
        local_orders = self.load_orders()
        balance_info = self.load_local_wallet_balance()
        total = float(balance_info.get("coin_equity", 0.0))  # ✅ 수정됨
        avail = float(balance_info.get("available_balance", 0.0))  # ✅

        results = []
        leverage = self.leverage
        for pos in local_positions or []:
            position_amt = abs(float(pos.get("size", 0)))
            if position_amt == 0:
                continue

            side = pos.get("side", "").upper()
            direction = "LONG" if side == "BUY" else "SHORT"

            # 진입가 / 현재가
            entry_price = float(pos.get("avgPrice", 0)) or 0.0

            # 진입 주문 로그 추출
            remaining_qty = position_amt
            open_orders = [
                o for o in local_orders
                if o["symbol"] == symbol and o["side"] == direction and o["type"] == "OPEN"
            ]

            open_orders.sort(key=lambda x: x["time"], reverse=True)

            entry_logs = []
            for order in open_orders:
                order_qty = float(order["qty"])
                used_qty = min(order_qty, remaining_qty)
                price = float(order["price"])
                order_time = int(order["time"])
                order_time_str = datetime.fromtimestamp(order_time / 1000, tz=KST).strftime("%Y-%m-%d %H:%M:%S")
                entry_logs.append((order_time, used_qty, price,order_time_str))
                remaining_qty -= used_qty

                if abs(remaining_qty) < 1e-8:
                    break

            results.append({
                "position": direction,
                "position_amt": position_amt,
                "entryPrice": entry_price,
                "entries": entry_logs
            })

        return {
            "balance": {
                "total": total,
                "available": avail,
                "leverage": leverage if results else 0  # 포지션 없으면 0
            },
            "positions": results
        }

    def set_wallet_balance(self, coin="USDT", account_type="UNIFIED", save_local=True):
        method = "GET"
        endpoint = "/v5/account/wallet-balance"
        params_pairs = [("accountType", account_type), ("coin", coin)]

        try:
            resp = self._request_with_resync(method, endpoint, params_pairs=params_pairs, body_dict=None, timeout=5)
            data = resp.json()
        except Exception as e:
            self.system_logger.error(f"[ERROR] 지갑 조회 실패 (API): {e}")
            return self.load_local_wallet_balance()

        if isinstance(data, dict) and data.get("retCode") != 0:
            self.system_logger.error(f"[ERROR] 잔고 조회 실패: {data.get('retMsg')}")
            return self.load_local_wallet_balance()

        account_data = data["result"]["list"][0]
        coin_data = next((c for c in account_data["coin"] if c["coin"] == coin), {})

        result = {
            "coin_equity": float(coin_data.get("equity", 0)),
            "available_balance": float(account_data.get("totalAvailableBalance", 0)),
        }

        if save_local:
            self.save_local_wallet_balance(result)

        return result

    def load_local_wallet_balance(self):
        if not os.path.exists(self.wallet_file):
            return {}
        try:
            with open(self.wallet_file, "r", encoding="utf-8") as f:
                content = f.read().strip()
                return json.loads(content) if content else {}
        except Exception as e:
            self.system_logger.error(f"[ERROR] 로컬 지갑 파일 읽기 오류: {e}")
            return {}

    def save_local_wallet_balance(self, data):
        try:
            with open(self.wallet_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.system_logger.error(f"[ERROR] 지갑 저장 실패: {e}")

    def load_orders(self):
        if not os.path.exists(self.orders_file):
            return []
        try:
            with open(self.orders_file, "r", encoding="utf-8") as f:
                content = f.read().strip()
                return json.loads(content) if content else []
        except Exception as e:
            self.system_logger.error(f"거래기록 로드 실패: {e}")
            return []

    def save_orders(self, trades):
        try:
            with open(self.orders_file, "w", encoding="utf-8") as f:
                json.dump(trades, f, indent=2)
        except Exception as e:
            self.system_logger.error(f"[ERROR] 거래기록 저장 실패: {e}")

    def append_order(self, trade: dict):
        """
        trade 하나를 로컬 파일에 append (중복 방지)
        """
        try:
            local_orders = []
            if os.path.exists(self.orders_file):
                with open(self.orders_file, "r", encoding="utf-8") as f:
                    try:
                        local_orders = json.load(f)
                    except json.JSONDecodeError:
                        self.system_logger.warning("⚠️ orders_file JSON 파싱 실패, 새로 시작")
                        local_orders = []

            # 중복 확인 (execId 또는 id 기준)
            existing_ids = {str(o.get("id")) for o in local_orders}
            if str(trade.get("id")) in existing_ids:
                self.system_logger.debug(f"⏩ 이미 존재하는 trade id={trade.get('id')}, 스킵")
                return local_orders

            local_orders.append(trade)

            with open(self.orders_file, "w", encoding="utf-8") as f:
                json.dump(local_orders, f, indent=2, ensure_ascii=False)

            self.system_logger.debug(f"📥 신규 trade {trade.get('id')} 저장됨")

        except Exception as e:
            self.system_logger.error(f"[ERROR] 거래기록 append 실패: {e}")
            return self.load_orders()

    def update_candles(self, candles, count=None):
        """
        candles: 리스트(바깥에서 넘겨주는 mutable).
                 각 원소는 {start, open, high, low, close} 딕셔너리.
        count:   최종적으로 가져올 캔들 개수
        """
        try:
            url = f"{self.base_url}/v5/market/kline"

            target = count if (isinstance(count, int) and count > 0) else 1000
            all_candles = []
            latest_end = None  # ms

            while len(all_candles) < target:
                # 루프마다 필요한 만큼만 요청(최대 1000)
                req_limit = min(1000, target - len(all_candles))

                params = {
                    "category": "linear",
                    "symbol": self.symbol,
                    "interval": "1",
                    "limit": req_limit,
                }
                if latest_end is not None:
                    params["end"] = latest_end  # 이 시각(포함) 이전까지만

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
                    # 간헐적으로 result 자체가 list로 오는 케이스
                    raw_list = result
                else:
                    raise RuntimeError(f"unexpected 'result' type: {type(result).__name__}")

                if not isinstance(raw_list, list):
                    raise RuntimeError(f"'list' is {type(raw_list).__name__}, not list")
                # -------------------------------------

                if not raw_list:
                    break

                # Bybit는 최신순으로 오므로 역순으로 뒤집어 페이지 내 시간을 오름차순으로 맞춤
                raw_list = raw_list[::-1]

                # 0=startTime(ms), 1=open, 2=high, 3=low, 4=close
                chunk = []
                for c in raw_list:
                    try:
                        # 각 항목이 리스트/튜플이고 길이가 충분한지 방어
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
                        # 개별 변환 실패는 스킵
                        continue

                # 더 오래된 묶음이 앞에 오도록 누적(전체는 오래된→최신 순서 유지)
                if chunk:
                    all_candles = chunk + all_candles
                    # 다음 페이지는 이번 묶음의 가장 오래된 캔들 시작 직전까지로 이동
                    latest_end = _safe_int(raw_list[0][0]) - 1
                else:
                    break

                # 마지막 페이지(요청 수보다 적게 온 경우)면 종료
                if len(raw_list) < req_limit:
                    break

            # 최종 개수로 슬라이싱
            if isinstance(count, int) and count > 0:
                all_candles = all_candles[-count:]

            candles.clear()
            candles.extend(all_candles)

            last = candles[-1] if candles else None
            if last:
                self.system_logger.debug(
                    f"📊 캔들 갱신 완료: {len(candles)}개, "
                    f"최근 OHLC=({last['open']}, {last['high']}, {last['low']}, {last['close']})"
                )
            else:
                self.system_logger.debug("📊 캔들 갱신: 결과 없음")

        except Exception as e:
            self.system_logger.warning(f"❌ 캔들 요청 실패: {e}")

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

        # 1) 파라미터를 '리스트(tuple)'로 만들고, 이 순서를 전 구간에서 재사용
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

    def open_market(self, symbol, side, price, percent, balance):
        if price is None or balance is None:
            self.system_logger.error("❌ 가격 또는 잔고 정보가 누락되었습니다.")
            return None

        total_balance = balance.get("total", 0)
        qty = round(total_balance * self.leverage / price * percent / 100, 3)
        if qty < 0.001:
            self.system_logger.warning("❗ 주문 수량이 너무 작습니다. 주문 중단.")
            return None

        if side.lower() == "long":
            order_side, position_idx = "Buy", 1
        elif side.lower() == "short":
            order_side, position_idx = "Sell", 2
        else:
            self.system_logger.error(f"❌ 알 수 없는 side 값: {side}")
            return None

        self.system_logger.debug(f"📥 {side.upper()} 진입 시도 | 수량: {qty} @ {price:.2f}")
        return self.submit_market_order(symbol, order_side, qty, position_idx, reduce_only=False)

    def close_market(self, symbol, side, qty):
        qty = float(qty)
        if qty < 0.001:
            self.system_logger.warning("❗ 청산 수량이 너무 작습니다. 중단.")
            return None

        if side.upper() == "LONG":
            order_side, position_idx = "Sell", 1
        elif side.upper() == "SHORT":
            order_side, position_idx = "Buy", 2
        else:
            self.system_logger.error(f"❌ 알 수 없는 side 값: {side}")
            return None

        self.system_logger.debug(f"📤 {side.upper()} 포지션 청산 시도 | 수량: {qty}")
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



