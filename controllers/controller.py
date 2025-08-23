# controllers/controller.py

import requests
import hmac, hashlib
import threading
from websocket import WebSocketApp
from dotenv import load_dotenv
import os
from datetime import datetime, timezone, timedelta
import time
from utils.logger import setup_logger
logger = setup_logger()
load_dotenv()
import json
KST = timezone(timedelta(hours=9))
from urllib.parse import urlencode

class BybitWebSocketController:
    def __init__(self, symbol="BTCUSDT"):
        self.symbol = symbol
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
            logger.debug("✅ Public WebSocket 연결됨")
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
                logger.debug(f"❌ Public 메시지 처리 오류: {e}")

        def on_error(ws, error):
            logger.debug(f"❌ Public WebSocket 오류: {error}")

        def on_close(ws, *args):
            logger.warning("🔌 WebSocket closed. Reconnecting in 5 seconds...")
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
                logger.exception(f"🔥 Public WebSocket 스레드 예외: {e}")
                time.sleep(5)
                self._start_public_websocket()

        thread = threading.Thread(target=run)



        thread.daemon = True
        thread.start()

    def _start_private_websocket(self):
        def on_open(ws):
            try:
                logger.debug("🔐 Private WebSocket 연결됨")
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
                logger.exception(f"❌ 인증 요청 실패: {e}")

        def on_message(ws, message):
            try:
                parsed = json.loads(message)
                if parsed.get("op") == "auth":
                    if parsed.get("success"):
                        logger.debug("✅ 인증 성공, 포지션 구독 시작")
                        time.sleep(0.5)  # 🔧 구독 전 0.5초 대기
                        ws.send(json.dumps({
                            "op": "subscribe",
                            "args": ["position.linear", "execution", "order", "wallet"]
                        }))
                    else:
                        logger.error(f"❌ 인증 실패: {parsed}")

                elif parsed.get("op") == "subscribe":
                    logger.debug(f"✅ 구독 성공 응답: {parsed}")


                elif "topic" in parsed and parsed["topic"].startswith("position"):

                    data = parsed.get("data", [])
                    if data:
                        self.position = data[0]
            except Exception as e:
                logger.debug(f"❌ Private 메시지 처리 오류: {e}")

        def on_error(ws, error):
            logger.error(f"❌ WebSocket 오류 발생: {error}")
            ws.close()

        def on_close(ws, *args):
            logger.warning("🔌 Private WebSocket 종료됨. 5초 후 재연결 시도...")
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
                logger.exception(f"🔥 Private WebSocket 스레드 예외: {e}")
                time.sleep(5)
                self._start_private_websocket()

        thread = threading.Thread(target=run)
        thread.daemon = True
        thread.start()

class BybitRestController:
    def __init__(self, symbol="BTCUSDT"):
        self.symbol = symbol
        self.base_url = "https://api-demo.bybit.com"
        self.api_key = os.getenv("BYBIT_TEST_API_KEY")
        self.api_secret = os.getenv("BYBIT_TEST_API_SECRET")
        self.api_secret = os.getenv("BYBIT_TEST_API_SECRET").encode()  # HMAC 서명용
        self.recv_window = "5000"
        self.positions_file = f"{symbol}_positions.json"
        self.orders_file = f"{symbol}_orders.json"
        self.wallet_file = f"{symbol}_wallet.json"
        self.leverage = 50
        self.set_leverage(leverage = self.leverage)
        self.FEE_RATE = 0.00055  # 0.055%

    def _generate_signature(self, timestamp, method, params="", body=""):
        query_string = params if method == "GET" else body
        payload = f"{timestamp}{self.api_key}{self.recv_window}{query_string}"
        return hmac.new(self.api_secret, payload.encode(), hashlib.sha256).hexdigest()

    def _get_headers(self, method, endpoint, params="", body=""):
        timestamp = str(int(time.time() * 1000))
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
                if not last_cross_time_up or (cross_time - last_cross_time_up).total_seconds() > 3600:
                    count += 1
                    cross_times.append(("UP", cross_time.strftime("%Y-%m-%d %H:%M:%S"), upper, price, ma))
                    last_cross_time_up = cross_time


            if last_state in ("above", "in") and state == "below":
                cross_time = now_kst - timedelta(minutes=len(closes) - i)
                if not last_cross_time_down or (cross_time - last_cross_time_down).total_seconds() > 3600:
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
        method = "GET"
        endpoint = "/v5/position/list"
        params = f"category={category}&symbol={symbol}"
        url = f"{self.base_url}{endpoint}?{params}"
        headers = self._get_headers(method, endpoint, params=params, body="")
        response = requests.get(url, headers=headers)
        return response.json()


    def load_local_positions(self):
        if not os.path.exists(self.positions_file):
            return []
        try:
            with open(self.positions_file, "r", encoding="utf-8") as f:
                content = f.read().strip()
                return json.loads(content) if content else []
        except Exception as e:
            logger.error(f"[ERROR] 로컬 포지션 파일 읽기 오류: {e}")
            return []

    def save_local_positions(self, data):
        try:
            with open(self.positions_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[ERROR] 포지션 저장 실패: {e}")

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
            logger.debug("📌 포지션 변경 감지됨 → 로컬 파일 업데이트")
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
                    logger.error(f"❌ HTTP 오류 {resp.status_code}: {resp.text[:200]}")
                    return None
                try:
                    data = resp.json()
                except Exception:
                    logger.error(f"❌ JSON 파싱 실패: {resp.text[:200]}")
                    return None
                # Bybit API 레벨 오류
                ret_code = data.get("retCode")
                if ret_code != 0:
                    logger.error(f"❌ Bybit 오류 retCode={ret_code}, retMsg={data.get('retMsg')}")
                    return None
                result = data.get("result") or {}
                lst = result.get("list")
                if not isinstance(lst, list):
                    logger.error(f"❌ result.list가 리스트가 아님: {type(lst)}")
                    return None
                return lst
            except requests.exceptions.Timeout:
                logger.error("⏱️ 요청 타임아웃")
                return None
            except requests.exceptions.RequestException as e:
                logger.error(f"🌐 네트워크 예외: {e}")
                return None

        # 1차 요청
        executions = _fetch_once()
        # (옵션) 실패 시 1회 재시도
        if executions is None:
            logger.debug("↻ 재시도: 서명/타임스탬프 갱신")
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
                logger.debug(f"📥 신규 체결 {appended}건 저장됨")
            return local_orders

        except Exception as e:
            logger.error(f"[ERROR] 주문 동기화 실패: {e}")
            return self.load_orders()

    def get_trade_w_order_id(self, symbol="BTCUSDT",order_id=None):

        ####
        if not order_id:
            logger.error("❌ order_id가 필요합니다.")
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
                    logger.error(f"❌ HTTP 오류 {resp.status_code}: {resp.text[:200]}")
                    return None
                try:
                    data = resp.json()
                except Exception:
                    logger.error(f"❌ JSON 파싱 실패: {resp.text[:200]}")
                    return None
                if data.get("retCode") != 0:
                    logger.error(f"❌ Bybit 오류 retCode={data.get('retCode')}, retMsg={data.get('retMsg')}")
                    return None
                result = data.get("result") or {}
                lst = result.get("list")
                if not isinstance(lst, list):
                    logger.error(f"❌ result.list가 리스트가 아님: {type(lst)}")
                    return None
                return lst
            except requests.exceptions.Timeout:
                logger.error("⏱️ 요청 타임아웃")
                return None
            except requests.exceptions.RequestException as e:
                logger.error(f"🌐 네트워크 예외: {e}")
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
                logger.error(f"⏰ executions 반영 대기 타임아웃({exec_timeout_sec}s). 부분 체결/전파 지연 가능.")

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
        query_str = urlencode(params_pairs, doseq=True)
        url = f"{self.base_url}{endpoint}?{query_str}"
        headers = self._get_headers(method, endpoint, params=query_str, body="")

        try:
            r = requests.get(url, headers=headers, timeout=5)
            data = r.json()
        except Exception as e:
            logger.error(f"[ERROR] 지갑 조회 실패 (API): {e}")
            return self.load_local_wallet_balance()  # 실패 시 로컬 fallback

        if isinstance(data, dict) and data.get("retCode") != 0:
            logger.error(f"[ERROR] 잔고 조회 실패: {data.get('retMsg')}")
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
            logger.error(f"[ERROR] 로컬 지갑 파일 읽기 오류: {e}")
            return {}

    def save_local_wallet_balance(self, data):
        try:
            with open(self.wallet_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[ERROR] 지갑 저장 실패: {e}")

    def load_orders(self):
        if not os.path.exists(self.orders_file):
            return []
        try:
            with open(self.orders_file, "r", encoding="utf-8") as f:
                content = f.read().strip()
                return json.loads(content) if content else []
        except Exception as e:
            logger.error(f"거래기록 로드 실패: {e}")
            return []

    def save_orders(self, trades):
        try:
            with open(self.orders_file, "w", encoding="utf-8") as f:
                json.dump(trades, f, indent=2)
        except Exception as e:
            logger.error(f"[ERROR] 거래기록 저장 실패: {e}")

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
                        logger.warning("⚠️ orders_file JSON 파싱 실패, 새로 시작")
                        local_orders = []

            # 중복 확인 (execId 또는 id 기준)
            existing_ids = {str(o.get("id")) for o in local_orders}
            if str(trade.get("id")) in existing_ids:
                logger.debug(f"⏩ 이미 존재하는 trade id={trade.get('id')}, 스킵")
                return local_orders

            local_orders.append(trade)

            with open(self.orders_file, "w", encoding="utf-8") as f:
                json.dump(local_orders, f, indent=2, ensure_ascii=False)

            logger.debug(f"📥 신규 trade {trade.get('id')} 저장됨")

        except Exception as e:
            logger.error(f"[ERROR] 거래기록 append 실패: {e}")
            return self.load_orders()
    def update_closes(self, closes, count=None):
        try:
            url = f"{self.base_url}/v5/market/kline"
            params = {
                "category": "linear",
                "symbol": self.symbol,
                "interval": "1",
                "limit": 1000
            }

            all_closes = []
            latest_end = None

            while len(all_closes) < count:
                if latest_end:
                    params["end"] = latest_end

                res = requests.get(url, params=params, timeout=10)
                res.raise_for_status()
                data = res.json().get("result", {}).get("list", [])

                if not data:
                    break

                data = data[::-1]
                closes_chunk = [float(c[4]) for c in data]
                all_closes = closes_chunk + all_closes
                latest_end = int(data[0][0]) - 1

                if len(data) < 1000:
                    break

            all_closes = all_closes[-count:]
            closes.clear()
            closes.extend(all_closes)

            logger.debug(f"📊 캔들 갱신 완료: {len(closes)}개, 최근 종가: {closes[-1]}")
        except Exception as e:
            logger.warning(f"❌ 캔들 요청 실패: {e}")

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
        """
        Bybit에서 지정한 심볼의 레버리지를 설정합니다 (단일모드용, buy/sell 동일).
        이미 설정된 값과 동일할 경우 경고만 출력하고 True 반환.
        """
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
                    logger.debug(f"✅ 레버리지 {leverage}x 설정 완료 | 심볼: {symbol}")
                    return True
                elif ret_code == 110043:
                    logger.debug(f"⚠️ 이미 설정된 레버리지입니다: {leverage}x | 심볼: {symbol}")
                    return True  # 이건 실패 아님
                else:
                    logger.error(f"❌ 레버리지 설정 실패: {data.get('retMsg')} (retCode {ret_code})")
            else:
                logger.error(f"❌ HTTP 오류: {response.status_code} {response.text}")
        except Exception as e:
            logger.error(f"❌ 레버리지 설정 중 예외 발생: {e}")

        return False

    def make_status_log_msg(self, status, price, ma100=None, prev=None,
                            ma_threshold=None, momentum_threshold=None, target_cross=None, closes_num = None):

        if ma100 is not None and prev is not None:

            ma_upper = ma100 * (1 + ma_threshold)
            ma_lower = ma100 * (1 - ma_threshold)


            ma_diff_pct = ((price - ma100) / ma100) * 100  # 현재가가 MA100 대비 몇 % 차이인지


            log_msg = (
                f"\n💹 시세 정보\n"
                f"  • 현재가      : {price:,.1f} "
                f"(MA대비 👉[{ma_diff_pct:+.3f}%]👈)\n"
                f"  • MA100       : {ma100:,.1f}\n"
                f"  • 진입목표(롱/숏) : {ma_lower:,.2f} / {ma_upper:,.2f} "
                f"(±{ma_threshold * 100:.3f}%)\n"
                f"  • 급등락 목표(3분) : {momentum_threshold * 100:.3f}%\n"
                f"  • 목표 크로스: {target_cross}회 / {closes_num} 분)\n"
            )
        else:
            log_msg = ""

        status_list = status.get("positions", [])
        balance = status.get("balance", {})

        total = balance.get("total", 0.0)
        available = balance.get("available", 0.0)
        available_pct = (available / total * 100) if total else 0
        log_msg += (
            f"  💰 자산: 총 {total:.2f} USDT\n"
            f"    사용 가능: {available:.2f} USDT ({available_pct:.1f}%) (레버리지: {self.leverage}x)\n"
        )

        if status_list:
            for position in status_list:
                pos_amt = float(position["position_amt"])
                entry_price = float(position["entryPrice"])
                side = position["position"]

                # 현재가 기준 수익률 / 수익금
                if pos_amt != 0:
                    if side == "LONG":
                        profit_rate = ((price - entry_price) / entry_price) * 100
                        gross_profit = (price - entry_price) * pos_amt
                    else:  # SHORT
                        profit_rate = ((entry_price - price) / entry_price) * 100
                        gross_profit = (entry_price - price) * abs(pos_amt)
                else:
                    profit_rate = 0.0
                    gross_profit = 0.0

                # 수수료 계산 (진입 + 청산 2번)
                position_value = abs(pos_amt) * entry_price
                fee_total = position_value * self.FEE_RATE * 2

                net_profit = gross_profit - fee_total

                log_msg += f"  📈 포지션: {side} ({pos_amt})\n"
                log_msg += f"    평균가: {entry_price:.3f} | 현재가: {price:.3f}\n"
                log_msg += f"    수익률: {profit_rate:.3f}%\n"
                log_msg += f"    수익금: {net_profit:+.3f} USDT (fee {fee_total:.3f} USDT)\n"

                if position["entries"]:
                    for i, (timestamp, qty, entryPrice,t_str) in enumerate(position["entries"], start=1):
                        signed_qty = -qty if position["position"] == "SHORT" else qty
                        log_msg += f"        └ 진입시간 #{i}: {t_str} ({signed_qty:.3f} BTC), 진입가 : {entryPrice:.2f} \n"
                else:
                    log_msg += f"        └ 진입시간: 없음\n"
        else:
            log_msg += "  📉 포지션 없음\n"
        return log_msg.rstrip()

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
                logger.debug(f"응답 JSON 파싱 실패: {r.text[:200]}")
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
            logger.debug(
                f"⌛ 주문 체결 대기중... ({i + 1}/{max_retries}) | 심볼: {symbol} | 주문ID: {order_id[-6:]}"
            )
            time.sleep(sleep_sec)

            # ⏰ 타임아웃: 호출부가 분기할 수 있게 '타임아웃 상태' 반환
        return {"orderId": order_id, "orderStatus": "TIMEOUT"}

    def submit_market_order(self, symbol, order_side, qty, position_idx, reduce_only=False):
        endpoint = "/v5/order/create"
        url = self.base_url + endpoint
        method = "POST"

        payload = {
            "category": "linear",
            "symbol": symbol,
            "side": order_side,  # "Buy" / "Sell"
            "orderType": "Market",
            "qty": str(qty),
            "positionIdx": position_idx,  # 1: Long pos, 2: Short pos
            "reduceOnly": bool(reduce_only),
            "timeInForce": "IOC",
        }
        # Bybit는 JSON 키 정렬한 문자열로 서명 권장
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        headers = self._get_headers(method, endpoint, body=body)
        headers["Content-Type"] = "application/json"

        try:
            r = requests.post(url, headers=headers, data=body, timeout=5)
            if r.status_code != 200:
                logger.error(f"❌ HTTP 오류: {r.status_code} {r.text}")
                return None
            data = r.json()
            if data.get("retCode") == 0:
                return data.get("result", {})
            logger.error(f"❌ 주문 실패: {data.get('retMsg')} (코드 {data.get('retCode')})")
        except Exception as e:
            logger.error(f"❌ 주문 예외: {e}")
        return None

    def open_market(self, symbol, side, price, percent, balance):
        if price is None or balance is None:
            logger.error("❌ 가격 또는 잔고 정보가 누락되었습니다.")
            return None

        total_balance = balance.get("total", 0)
        qty = round(total_balance * self.leverage / price * percent / 100, 3)
        if qty < 0.001:
            logger.warning("❗ 주문 수량이 너무 작습니다. 주문 중단.")
            return None

        if side.lower() == "long":
            order_side, position_idx = "Buy", 1
        elif side.lower() == "short":
            order_side, position_idx = "Sell", 2
        else:
            logger.error(f"❌ 알 수 없는 side 값: {side}")
            return None

        logger.debug(f"📥 {side.upper()} 진입 시도 | 수량: {qty} @ {price:.2f}")
        return self.submit_market_order(symbol, order_side, qty, position_idx, reduce_only=False)

    def close_market(self, symbol, side, qty):
        qty = float(qty)
        if qty < 0.001:
            logger.warning("❗ 청산 수량이 너무 작습니다. 중단.")
            return None

        if side.upper() == "LONG":
            order_side, position_idx = "Sell", 1
        elif side.upper() == "SHORT":
            order_side, position_idx = "Buy", 2
        else:
            logger.error(f"❌ 알 수 없는 side 값: {side}")
            return None

        logger.debug(f"📤 {side.upper()} 포지션 청산 시도 | 수량: {qty}")
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



