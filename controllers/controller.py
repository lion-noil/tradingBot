# controllers/controller.py

import requests
from binance.client import Client
from binance.enums import *
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

    def get_full_position_info(self, symbol="BTCUSDT"):
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

        return cleaned_new

    def sync_orders_from_bybit(self, symbol="BTCUSDT"):

        ####
        method = "GET"
        category = "linear"
        limit = 5
        endpoint = "/v5/execution/list"
        params_dict = {
            "category": category,
            "symbol": symbol,
            "limit": limit
        }
        params_str = "&".join([f"{k}={params_dict[k]}" for k in sorted(params_dict)])
        url = f"{self.base_url}{endpoint}?{params_str}"

        headers = self._get_headers(method, endpoint, params=params_str, body="")

        ####
        try:
            response = requests.get(url, headers=headers)
            data = response.json()
            executions = data.get("result", {}).get("list", [])

            local_orders = self.load_orders()
            existing_ids = {str(order["id"]) for order in local_orders}

            appended = 0
            for e in executions:
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

            if appended > 0:
                # ⭐ 저장 전 정렬 (time, id 기준)
                local_orders.sort(key=lambda o: (int(o["time"]), str(o["id"])))
                self.save_orders(local_orders)
                logger.debug(f"📥 신규 체결 {appended}건 저장됨 (시간순 정렬 완료)")

            return local_orders

        except Exception as e:
            logger.error(f"[ERROR] 주문 동기화 실패: {e}")
            return self.load_orders()

    def get_current_position_status(self, symbol="BTCUSDT"):
        posinfo_list = self.get_full_position_info(symbol)
        all_orders = self.sync_orders_from_bybit(symbol)

        results = []

        for pos in posinfo_list or []:
            position_amt = abs(float(pos.get("size", 0)))
            if position_amt == 0:
                continue

            side = pos.get("side", "").upper()
            direction = "LONG" if side == "BUY" else "SHORT"

            # 진입가 / 현재가
            entry_price = float(pos.get("avgPrice", 0)) or 0.0
            leverage = int(pos.get("leverage", 0))

            # 진입 주문 로그 추출 (sync_orders_from_bybit 사용)
            remaining_qty = position_amt
            open_orders = [
                o for o in all_orders
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


        # 지갑 잔고 조회
        try:
            balance_info = self.get_wallet_balance("USDT")
            total = float(balance_info.get("coin_equity", 0.0))  # ✅ 수정됨
            avail = float(balance_info.get("available_balance", 0.0))  # ✅
        except Exception as e:
            logger.warning(f"❗ USDT 잔액 정보 조회 실패: {e}")
            total = avail = upnl = 0.0

        return {
            "balance": {
                "total": total,
                "available": avail,
                "leverage": leverage if results else 0  # 포지션 없으면 0
            },
            "positions": results
        }

    def get_wallet_balance(self, coin="USDT"):
        method = "GET"
        endpoint = "/v5/account/wallet-balance"
        account_type = "UNIFIED"
        params = f"accountType={account_type}&coin={coin}"

        url = f"{self.base_url}{endpoint}?{params}"

        timestamp = str(int(time.time() * 1000))
        sign = self._generate_signature(timestamp, method, params=params)

        headers = {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-SIGN": sign,
            "X-BAPI-RECV-WINDOW": self.recv_window
        }

        response = requests.get(url, headers=headers)
        data = response.json()
        if data["retCode"] != 0:
            raise Exception(f"잔고 조회 실패: {data['retMsg']}")

        account_data = data["result"]["list"][0]
        coin_data = next((c for c in account_data["coin"] if c["coin"] == coin), {})



        # 첫 번째 코인 정보 반환
        return {
            # 계정 요약 정보 (전체 기준)
            "coin_equity": float(coin_data.get("equity", 0)),
            "available_balance": float(account_data.get("totalAvailableBalance", 0))
        }

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
                            ma_threshold=None, target_cross=None):
        # ==============================
        #  시세 및 조건 범위
        # ==============================
        if ma100 is not None and prev is not None:

            ma_upper = ma100 * (1 + ma_threshold)
            ma_lower = ma100 * (1 - ma_threshold)


            ma_diff_pct = ((price - ma100) / ma100) * 100  # 현재가가 MA100 대비 몇 % 차이인지


            log_msg = (
                f"\n💹 시세 정보\n"
                f"  • 현재가      : {price:,.1f} "
                f"(MA대비 {ma_diff_pct:+.3f}%)\n"
                f"  • MA100       : {ma100:,.1f}\n"
                f"  • 진입목표(롱/숏) : {ma_lower:,.2f} / {ma_upper:,.2f} "
                f"(±{ma_threshold * 100:.3f}%)\n"
                f"  • 목표 크로스: {target_cross}회\n"
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

    def buy_market_100(self,symbol="BTCUSDT", price=None, percent=10, balance=None):
        try:
            if price is None or balance is None:
                logger.error("❌ 가격 또는 잔고 정보가 누락되었습니다.")
                return None

            if self.leverage <= 0:
                logger.warning("❗ 유효하지 않은 레버리지 값. 기본값 1배 적용.")

            total_balance = balance.get('total', 0)
            qty = round(total_balance * self.leverage / price * percent / 100, 3)
            if qty < 0.001:
                logger.warning("❗ 주문 수량이 너무 작습니다. 매수 중단.")
                return None

            logger.debug(f"🟩 롱 진입 시작 | 수량: {qty} @ 현재가 {price:.2f}")


            endpoint = "/v5/order/create"
            url = self.base_url + endpoint
            method = "POST"

            payload = {
                "category": "linear",
                "symbol": symbol,
                "side": "Buy",
                "orderType": "Market",
                "qty": str(qty),
                "positionIdx": 1,
                "timeInForce": "IOC"
            }
            body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
            headers = self._get_headers(method, endpoint, body=body)
            response = requests.post(url, headers=headers, data=body)


            if response.status_code == 200:
                data = response.json()
                if data.get("retCode") == 0:
                    result = data.get("result", {})
                    logger.info(
                        f"✅ 롱 진입 완료\n"
                        f" | 주문ID: {result.get('orderId')}\n"
                        f" | 수량: {qty}"
                    )
                    return result
                else:
                    logger.error(f"❌ 주문 실패: {data.get('retMsg')}")
                    return None
            else:
                logger.error(f"❌ HTTP 오류: {response.status_code} {response.text}")
                return None

        except Exception as e:
            logger.error(f"❌ 롱 진입 실패: {e}")
            return None

    def sell_market_100(self, symbol="BTCUSDT", price=None, percent=10, balance=None):
        try:
            if price is None or balance is None:
                logger.error("❌ 가격 또는 잔고 정보가 누락되었습니다.")
                return None

            total_balance = balance.get('total', 0)
            qty = round(total_balance * self.leverage / price * percent / 100, 3)
            if qty < 0.001:
                logger.warning("❗ 주문 수량이 너무 작습니다. 매도 중단.")
                return None

            logger.debug(f"🟥 숏 진입 시작 | 수량: {qty} @ 현재가 {price:.2f}")

            endpoint = "/v5/order/create"
            url = self.base_url + endpoint
            method = "POST"

            payload = {
                "category": "linear",
                "symbol": symbol,
                "side": "Sell",
                "orderType": "Market",
                "qty": str(qty),
                "positionIdx": 2,  # 숏 포지션
                "timeInForce": "IOC"
            }

            body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
            headers = self._get_headers(method, endpoint, body=body)
            response = requests.post(url, headers=headers, data=body)

            if response.status_code == 200:
                data = response.json()
                if data.get("retCode") == 0:
                    result = data.get("result", {})
                    logger.info(
                        f"✅ 숏 진입 완료\n"
                        f" | 주문ID: {result.get('orderId')}\n"
                        f" | 수량: {qty}"
                    )
                    return result
                else:
                    logger.error(f"❌ 주문 실패: {data.get('retMsg')}")
                    return None
            else:
                logger.error(f"❌ HTTP 오류: {response.status_code} {response.text}")
                return None

        except Exception as e:
            logger.error(f"❌ 숏 진입 실패: {e}")
            return None

    def close_position(self, symbol="BTCUSDT", side=None, qty=None, entry_price=None):
        try:
            if not side or not qty or not entry_price:
                logger.error(f"❌ 청산 요청 실패: side, qty 또는 entry_price가 제공되지 않음")
                return
            qty = abs(float(qty))

            # 현재가 조회 (Bybit Ticker API 사용)
            ticker_endpoint = f"/v5/market/tickers?category=linear&symbol={symbol}"
            ticker_url = self.base_url + ticker_endpoint
            response = requests.get(ticker_url)
            close_price = float(response.json()["result"]["list"][0]["lastPrice"])

            # 수익금 계산
            if side == "LONG":
                profit = (close_price - entry_price) * qty
                profit_rate = ((close_price - entry_price) / entry_price) * 100
                close_side = "Sell"
                positionIdx = 1
            else:  # SHORT
                profit = (entry_price - close_price) * qty
                profit_rate = ((entry_price - close_price) / entry_price) * 100
                close_side = "Buy"
                positionIdx = 2

            logger.debug(
                f"📉 {side} 포지션 청산 시도 | 수량: {qty}@ 현재가 {close_price:.2f}"
            )

            endpoint = "/v5/order/create"
            url = self.base_url + endpoint
            method = "POST"

            payload = {
                "category": "linear",
                "symbol": symbol,
                "side": close_side,
                "orderType": "Market",
                "qty": str(qty),
                "positionIdx": positionIdx,
                "reduceOnly": True,
                "timeInForce": "IOC"
            }

            body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
            headers = self._get_headers(method, endpoint, body=body)
            response = requests.post(url, headers=headers, data=body)

            if response.status_code == 200:
                data = response.json()
                if data.get("retCode") == 0:
                    logger.info(
                        f"✅ {side} 포지션 청산 완료\n"
                        f" | 주문ID: {data['result'].get('orderId')}\n"
                        f" | 평균진입가: {entry_price:.2f}\n"
                        f" | 청산시도가: {close_price:.2f}\n"
                        f" | 수익금: {profit:.2f}\n"
                        f" | 수익률: {profit_rate:.2f}%"
                    )
                    return data
                else:
                    logger.error(f"❌ 청산 실패: {data.get('retMsg')}")
            else:
                logger.error(f"❌ HTTP 오류: {response.status_code} {response.text}")

        except Exception as e:
            logger.error(f"❌ 포지션 청산 실패 ({side}): {e}")




