# controllers/controller.py

import requests
from binance.client import Client
from binance.enums import *
import hmac, hashlib
import threading
from websocket import WebSocketApp
from dotenv import load_dotenv
import os
from datetime import datetime
import time
from utils.logger import setup_logger
logger = setup_logger()
load_dotenv()
import json


class CoinFuturesController:
    def __init__(self):
        self.client = Client(
            os.getenv("BINANCE_API_KEY"),
            os.getenv("BINANCE_API_SECRET")
        )
        # 선물 테스트넷 URL (USDⓢ-M)
        self.client.FUTURES_URL = "https://testnet.binancefuture.com/fapi"
        self.positions_file = "positions.json"
        self.orders_file = "orders.json"
        self.BINANCE_API_URL = "https://api.binance.com/api/v3/klines"
        # controllers에서 한 번 상위로(app) 올라감

    def load_local_positions(self):
        if not os.path.exists(self.positions_file):
            return []
        try:
            with open(self.positions_file, "r", encoding="utf-8") as f:
                content = f.read().strip()
                return json.loads(content) if content else []
        except Exception as e:
            logger.error(f"[ERROR] 로컬 포지션 파일 읽기 오류:{e}")
            return []
    def save_local_positions(self, positions):
        try:
            with open(self.positions_file, "w", encoding="utf-8") as f:
                json.dump(positions, f, indent=2)
        except Exception as e:
            logger.error(f"[ERROR] 로컬 포지션 저장 실패:{e}")
    def get_full_position_info(self, symbol="BTCUSDT"):

        new_positions = self.client.futures_position_information(symbol=symbol)
        new_positions = [p for p in new_positions if float(p["positionAmt"]) != 0]

        local_positions = self.load_local_positions()

        # 실시간 변동 필드 제외
        def clean_position(pos):
            ignore_keys = {
                "markPrice", "unRealizedProfit", "notional",
                "initialMargin", "maintMargin", "positionInitialMargin",
                "updateTime", "adl", "openOrderInitialMargin",
                "bidNotional", "askNotional", "isolatedWallet",
                "isolatedMargin"
            }
            return {k: v for k, v in pos.items() if k not in ignore_keys}

        cleaned_local = [clean_position(p) for p in local_positions]
        cleaned_new = [clean_position(p) for p in new_positions]
        if json.dumps(cleaned_local, sort_keys=True) != json.dumps(cleaned_new, sort_keys=True):
            logger.debug(f"포지션 변경 감지됨 → 로컬 파일 업데이트")

            self.save_local_positions(new_positions)

        return new_positions
    def sync_orders_from_binance(self, symbol="BTCUSDT"):
        try:
            # 최신 30개 주문 가져오기
            new_orders = self.client.futures_get_all_orders(symbol=symbol, limit=30)
            local_orders = self.load_orders()

            # 이미 저장된 주문 ID 목록
            existing_ids = {str(order["id"]) for order in local_orders}

            appended = 0
            for o in new_orders:
                # 체결된 주문만 저장 (실제 거래가 없는 주문 제외)
                if o["status"] != "FILLED" or float(o["executedQty"]) == 0:
                    continue

                order_id = str(o["orderId"])
                if order_id in existing_ids:
                    continue

                # 진입/청산 판단 (포지션 방향 + 주문 방향 조합)
                position_side = o.get("positionSide", "BOTH")  # 기본값 보정
                side = o["side"]

                if (position_side == "LONG" and side == "BUY") or \
                        (position_side == "SHORT" and side == "SELL"):
                    trade_type = "OPEN"
                else:
                    trade_type = "CLOSE"

                # avgPrice가 0일 경우 방어 코드
                try:
                    avg_price = float(o["avgPrice"])
                except (ValueError, TypeError):
                    avg_price = 0.0

                trade = {
                    "id": order_id,  # 고유 ID
                    "symbol": o["symbol"],
                    "side": position_side,  # LONG / SHORT
                    "type": trade_type,  # OPEN / CLOSE
                    "qty": float(o["executedQty"]),
                    "price": avg_price,
                    "time": int(o["time"]),

                    # 추가 정보
                    "orderSide": side,  # BUY / SELL
                    "reduceOnly": o.get("reduceOnly", False),
                    "closePosition": o.get("closePosition", False),
                    "status": o["status"],  # FILLED
                    "orderType": o["type"],  # MARKET / LIMIT 등
                    "cumQuote": float(o.get("cumQuote", 0)),  # 체결된 총 USDT
                    "clientOrderId": o.get("clientOrderId", ""),
                    "source": "binance"
                }

                local_orders.append(trade)
                appended += 1

            if appended > 0:
                self.save_orders(local_orders)
                logger.debug(f"📥 신규 주문 {appended}건 저장됨")

            return local_orders  # 전체 저장된 주문 리스트를 리턴

        except Exception as e:
            logger.error(f"[ERROR] 바이낸스 주문 동기화 실패: {e}")
            return self.load_orders()  # 실패 시 기존 것이라도 리턴

    def get_current_position_status(self, symbol="BTCUSDT"):
        posinfo_list = self.get_full_position_info(symbol)
        all_orders = self.sync_orders_from_binance(symbol)

        results = []

        for pos in posinfo_list or []:
            side = pos["positionSide"]
            remaining_qty = abs(float(pos["positionAmt"]))
            direction = side
            entry_price = float(pos["entryPrice"])

            price_now = float(pos.get("markPrice", entry_price))  # 기본값 방어

            # 수익률 계산
            if direction.upper() == "SHORT":
                profit_rate = (entry_price - price_now) / entry_price * 100
            else:
                profit_rate = (price_now - entry_price) / entry_price * 100
            unrealized_profit = profit_rate / 100 * abs(remaining_qty) * entry_price

            open_orders = [
                o for o in all_orders
                if o["symbol"] == symbol and o["side"] == direction and o["type"] == "OPEN"
            ]
            open_orders.sort(key=lambda x: x["time"], reverse=True)

            entry_logs = []  # (time, qty, price)
            for order in open_orders:
                order_qty = float(order["qty"])

                if remaining_qty == 0:
                    break

                used_qty = min(order_qty, remaining_qty)
                price = float(order["price"])
                order_time = order["time"]
                entry_logs.append((order_time, used_qty, price))
                remaining_qty -= used_qty

            results.append({
                "position": direction,
                "position_amt": pos["positionAmt"],
                "entryPrice": entry_price,
                "entries": entry_logs,  # 리스트 of (timestamp, qty, price)
                "profit_rate": profit_rate,
                "unrealized_profit": unrealized_profit,
                "current_price": price_now
            })

        balances = self.client.futures_account_balance()

        for b in balances:
            if b["asset"] == "USDT":
                total = float(b["balance"])
                avail = float(b["availableBalance"])
                upnl = float(b["crossUnPnl"])
                break
        else:
            total = avail = upnl = 0.0
            logger.warning("❗ USDT 잔액 정보 없음")

        # 레버리지 조회
        try:
            all_positions = self.client.futures_account()["positions"]
            for pos in all_positions:
                if pos["symbol"] == symbol:
                    leverage = int(pos["leverage"])
        except Exception as e:
            logger.warning(f"❗ 레버리지 조회 실패: {e}")
            leverage = 0

            # 합쳐서 반환
        return {
            "balance": {
                "total": total,
                "available": avail,
                "unrealized_pnl": upnl,
                "leverage": leverage
            },
            "positions": results
        }

    def make_status_log_msg(self, status):
        status_list = status.get("positions", [])
        balance = status.get("balance", {})

        total = balance.get("total", 0.0)
        available = balance.get("available", 0.0)
        upnl = balance.get("unrealized_pnl", 0.0)
        leverage = balance.get("leverage", 0)

        log_msg = "\n"
        log_msg += f"  💰 자산: 총 {total:.2f} USDT\n    사용 가능: {available:.2f}\n    미실현 손익: {upnl:+.2f} (레버리지: {leverage}x)\n"

        if status_list:
            for position in status_list:
                log_msg += f"  📈 포지션: {position['position']} ({position['position_amt']})\n"
                log_msg += f"    평균가: {position['entryPrice']:.3f}\n"
                log_msg += f"    수익률: {position['profit_rate']:.3f}%\n"
                log_msg += f"    수익금: {position['unrealized_profit']:+.3f} USDT\n"
                if position["entries"]:
                    for i, (timestamp, qty, entryPrice) in enumerate(position["entries"], start=1):
                        t_str = datetime.fromtimestamp(timestamp / 1000).strftime("%Y-%m-%d %H:%M:%S")
                        signed_qty = -qty if position["position"] == "SHORT" else qty
                        log_msg += f"        └ 진입시간 #{i}: {t_str} ({signed_qty:.3f} BTC), 진입가 : {entryPrice:.2f} \n"
                else:
                    log_msg += f"        └ 진입시간: 없음\n"
        else:
            log_msg += "  📉 포지션 없음\n"
        return log_msg.rstrip()

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


    def buy_market_100(self, symbol="BTCUSDT", price = None, percent=10,balance=None):
        try:
            if price is None or balance is None:
                logger.error("❌ 가격 또는 잔고 정보가 누락되었습니다.")
                return None
            leverage = balance.get('leverage', 1)
            if leverage <= 0:
                logger.warning("❗ 유효하지 않은 레버리지 값. 기본값 1배 적용.")
                leverage = 1

            total_balance = balance.get('total', 0)
            qty = round(total_balance * leverage / price * percent / 100, 3)
            if qty < 0.001:
                logger.warning("❗ 주문 수량이 너무 작습니다. 매수 중단.")
                return None

            logger.debug(f"🟩 롱 진입 시작 | 수량: {qty} @ 현재가 {price:.2f}")

            order = self.client.futures_create_order(
                symbol=symbol,
                side=SIDE_BUY,
                type=FUTURE_ORDER_TYPE_MARKET,
                quantity=qty,
                positionSide="LONG"
            )

            order_id = order.get("orderId")
            avg_price = order.get("avgFillPrice", price)
            logger.info(
                f"✅ 롱 진입 완료\n"
                f" | 주문ID: {order_id}\n"
                f" | 진입가: {avg_price}\n"
                f" | 수량: {qty}"
            )
            status = self.get_current_position_status()
            logger.info(self.make_status_log_msg(status) + '\n')


            return order  # 성공 시 주문 정보 리턴

        except Exception as e:
            logger.error(f"❌ 롱 진입 실패: {e}")
            return None

    def sell_market_100(self, symbol="BTCUSDT", price=None, percent=10, balance=None):
        try:
            if price is None or balance is None:
                logger.error("❌ 가격 또는 잔고 정보가 누락되었습니다.")
                return None

            leverage = balance.get('leverage', 1)
            if leverage <= 0:
                logger.warning("❗ 유효하지 않은 레버리지 값. 기본값 10배 적용.")
                leverage = 10

            total_balance = balance.get('total', 0)
            qty = round(total_balance * leverage / price * percent / 100, 3)
            if qty < 0.001:
                logger.warning("❗ 주문 수량이 너무 작습니다. 매도 중단.")
                return None

            logger.debug(f"🟥 숏 진입 시작 | 수량: {qty} @ 현재가 {price:.2f}")

            order = self.client.futures_create_order(
                symbol=symbol,
                side=SIDE_SELL,
                type=FUTURE_ORDER_TYPE_MARKET,
                quantity=qty,
                positionSide="SHORT"
            )

            order_id = order.get("orderId")
            avg_price = order.get("avgFillPrice", price)  # 테스트넷에서는 avgFillPrice 없을 수 있음
            logger.info(
                f"✅ 숏 진입 완료\n"
                f" | 주문ID: {order_id}\n"
                f" | 진입가: {avg_price}\n"
                f" | 수량: {qty}"
            )

            status = self.get_current_position_status()
            logger.info(self.make_status_log_msg(status) + '\n')


            return order  # 성공 시 주문 정보 리턴

        except Exception as e:
            logger.error(f"❌ 숏 진입 실패: {e}")
            return None

    def close_position(self, symbol="BTCUSDT", side=None, qty=None, entry_price=None):
        try:
            if not side or not qty or not entry_price:
                logger.error(f"❌ 청산 요청 실패: side, qty 또는 entry_price가 제공되지 않음")
                return
            qty = abs(float(qty))

            # 1. 현재가(청산가) 가져오기 (ticker price 사용)
            close_price = float(self.client.futures_symbol_ticker(symbol=symbol)["price"])

            # 2. 수익금/수익률 계산
            if side == "LONG":
                profit = (close_price - entry_price) * qty
                profit_rate = ((close_price - entry_price) / entry_price) * 100
            else:  # SHORT
                profit = (entry_price - close_price) * qty
                profit_rate = ((entry_price - close_price) / entry_price) * 100

            logger.debug(
                f"📉 {side} 포지션 청산 시도 | 수량: {qty}@ 현재가 {close_price:.2f}"
            )

            order = self.client.futures_create_order(
                symbol=symbol,
                side=SIDE_SELL if side == "LONG" else SIDE_BUY,
                type=FUTURE_ORDER_TYPE_MARKET,
                quantity=qty,
                positionSide=side
            )

            logger.info(
                f"✅ {side} 포지션 청산 완료\n"
                f" | 주문ID: {order.get('orderId')}\n"
                f" | 평균진입가: {entry_price:.2f}\n"
                f" | 청산시도가: {close_price:.2f}\n"
                f" | 수익금: {profit:.2f}\n"
                f" | 수익률: {profit_rate:.2f}%"
            )

            status = self.get_current_position_status()
            logger.info(self.make_status_log_msg(status) + '\n')

        except Exception as e:
            logger.error(f"❌ 포지션 청산 실패 ({side}): {e}")

    def ma100_list(self, closes):
        ma100s = []
        for i in range(99, len(closes)):
            ma = sum(closes[i - 99:i + 1]) / 100
            ma100s.append(ma)
        return ma100s  # len = len(closes) - 99

    def count_cross(self, closes, ma100s, threshold):
        count = 0
        last_state = None  # "above", "below", "in"
        closes = list(closes)  # 🔧 deque → list로 변환

        for price, ma in zip(closes[99:], ma100s):
            upper = ma * (1 + threshold)
            lower = ma * (1 - threshold)

            if price > upper:
                state = "above"
            elif price < lower:
                state = "below"
            else:
                state = "in"

            # 아래에서 위로 upper 크로스
            if last_state in ("below", "in") and state == "above":
                count += 1

            # 위에서 아래로 lower 크로스
            if last_state in ("above", "in") and state == "below":
                count += 1

            last_state = state

        return count

    def find_optimal_threshold(self, closes, ma100s, min_thr=0.002, max_thr=0.05, target_cross=4):
        left, right = min_thr, max_thr
        optimal = max_thr
        for _ in range(10):  # 충분히 반복
            mid = (left + right) / 2
            crosses = self.count_cross(closes, ma100s, mid)
            if crosses > target_cross:
                left = mid  # threshold를 키워야 cross가 줄어듦
            else:
                optimal = mid
                right = mid
        return max(optimal, min_thr)

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
        last_state = None  # "above", "below", "in"
        closes = list(closes)  # 🔧 deque → list로 변환

        for price, ma in zip(closes[99:], ma100s):
            upper = ma * (1 + threshold)
            lower = ma * (1 - threshold)

            if price > upper:
                state = "above"
            elif price < lower:
                state = "below"
            else:
                state = "in"

            # 아래에서 위로 upper 크로스
            if last_state in ("below", "in") and state == "above":
                count += 1

            # 위에서 아래로 lower 크로스
            if last_state in ("above", "in") and state == "below":
                count += 1

            last_state = state

        return count

    def find_optimal_threshold(self, closes, ma100s, min_thr=0.002, max_thr=0.05, target_cross=4):
        left, right = min_thr, max_thr
        optimal = max_thr
        for _ in range(10):  # 충분히 반복
            mid = (left + right) / 2
            crosses = self.count_cross(closes, ma100s, mid)
            if crosses > target_cross:
                left = mid  # threshold를 키워야 cross가 줄어듦
            else:
                optimal = mid
                right = mid
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
            """불변 비교를 위한 핵심 필드만 필터링"""
            return {
                "symbol": pos.get("symbol"),
                "side": pos.get("side"),
                "size": str(pos.get("size")),
                "avgPrice": str(pos.get("avgPrice")),
                "leverage": str(pos.get("leverage")),
                # 필요한 경우 추가 가능
            }

        cleaned_local = [clean_position(p) for p in local_positions]
        cleaned_new = [clean_position(p) for p in new_positions]

        if json.dumps(cleaned_local, sort_keys=True) != json.dumps(cleaned_new, sort_keys=True):
            logger.debug("📌 포지션 변경 감지됨 → 로컬 파일 업데이트")
            self.save_local_positions(new_positions)

        return new_positions

    def sync_orders_from_bybit(self, symbol="BTCUSDT"):
        method = "GET"
        endpoint = "/v5/order/history"
        category = "linear"
        limit = 30
        params = f"category={category}&symbol={symbol}&limit={limit}"
        url = f"{self.base_url}{endpoint}?{params}"

        timestamp = str(int(time.time() * 1000))
        sign = self._generate_signature(timestamp, method, params=params)

        headers = {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-SIGN": sign,
            "X-BAPI-RECV-WINDOW": self.recv_window
        }

        try:
            response = requests.get(url, headers=headers)
            data = response.json()
            new_orders = data.get("result", {}).get("list", [])

            local_orders = self.load_orders()
            existing_ids = {str(order["id"]) for order in local_orders}

            appended = 0
            for o in new_orders:
                if o["orderStatus"] != "Filled" or float(o.get("cumExecQty", 0)) == 0:
                    continue

                order_id = str(o["orderId"])
                if order_id in existing_ids:
                    continue

                # 진입/청산 판단
                reduce_only = o.get("reduceOnly", False)
                is_close = o.get("isClose", False)
                side = o["side"]  # "Buy" or "Sell"
                trade_type = "CLOSE" if reduce_only or is_close else "OPEN"
                position_side = "LONG" if side == "Buy" else "SHORT"

                try:
                    avg_price = float(o.get("avgPrice") or o["price"])
                except (ValueError, TypeError):
                    avg_price = 0.0

                trade = {
                    "id": order_id,
                    "symbol": o["symbol"],
                    "side": position_side,  # LONG / SHORT
                    "type": trade_type,  # OPEN / CLOSE
                    "qty": float(o["cumExecQty"]),
                    "price": avg_price,
                    "time": int(o["createdTime"]),
                    "orderSide": side,
                    "reduceOnly": reduce_only,
                    "closePosition": is_close,
                    "status": o["orderStatus"],
                    "orderType": o["orderType"],
                    "cumQuote": float(o.get("cumExecValue", 0)),
                    "clientOrderId": o.get("orderLinkId", ""),
                    "source": "bybit"
                }

                local_orders.append(trade)
                appended += 1

            if appended > 0:
                self.save_orders(local_orders)
                logger.debug(f"📥 신규 주문 {appended}건 저장됨")

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
            price_now = float(pos.get("markPrice", entry_price)) or entry_price
            leverage = int(pos.get("leverage", 0))

            # 수익률 계산
            if direction == "SHORT":
                profit_rate = (entry_price - price_now) / entry_price * 100
            else:
                profit_rate = (price_now - entry_price) / entry_price * 100

            # 미실현 손익
            try:
                unrealized_profit = float(pos.get("unrealisedPnl", 0.0))
            except:
                unrealized_profit = profit_rate / 100 * position_amt * entry_price

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
                entry_logs.append((order_time, used_qty, price))
                remaining_qty -= used_qty
                if abs(remaining_qty) < 1e-8:
                    break

            results.append({
                "position": direction,
                "position_amt": position_amt,
                "entryPrice": entry_price,
                "entries": entry_logs,
                "profit_rate": profit_rate,
                "unrealized_profit": unrealized_profit,
                "current_price": price_now
            })


        # 지갑 잔고 조회
        try:
            balance_info = self.get_wallet_balance("USDT")
            total = float(balance_info.get("coin_equity", 0.0))  # ✅ 수정됨
            avail = float(balance_info.get("available_balance", 0.0))  # ✅
            upnl = float(balance_info.get("coin_unrealized_pnl", 0.0))  # ✅ 수정됨
        except Exception as e:
            logger.warning(f"❗ USDT 잔액 정보 조회 실패: {e}")
            total = avail = upnl = 0.0

        return {
            "balance": {
                "total": total,
                "available": avail,
                "unrealized_pnl": upnl,
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
            "available_balance": float(account_data.get("totalAvailableBalance", 0)),
            "coin_unrealized_pnl": float(coin_data.get("unrealisedPnl", 0))
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

    def update_closes(self, closes, count=1440):
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
        return [
            sum(closes_list[i - 99:i + 1]) / 100
            for i in range(99, len(closes_list))
        ]

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
                    logger.warning(f"⚠️ 이미 설정된 레버리지입니다: {leverage}x | 심볼: {symbol}")
                    return True  # 이건 실패 아님
                else:
                    logger.error(f"❌ 레버리지 설정 실패: {data.get('retMsg')} (retCode {ret_code})")
            else:
                logger.error(f"❌ HTTP 오류: {response.status_code} {response.text}")
        except Exception as e:
            logger.error(f"❌ 레버리지 설정 중 예외 발생: {e}")

        return False

    def make_status_log_msg(self, status):
        status_list = status.get("positions", [])
        balance = status.get("balance", {})

        total = balance.get("total", 0.0)
        available = balance.get("available", 0.0)
        upnl = balance.get("unrealized_pnl", 0.0)
        log_msg = ""
        log_msg += f"  💰 자산: 총 {total:.2f} USDT\n    사용 가능: {available:.2f}\n    미실현 손익: {upnl:+.2f} (레버리지: {self.leverage}x)\n"

        if status_list:
            for position in status_list:
                log_msg += f"  📈 포지션: {position['position']} ({position['position_amt']})\n"
                log_msg += f"    평균가: {position['entryPrice']:.3f}\n"
                log_msg += f"    수익률: {position['profit_rate']:.3f}%\n"
                log_msg += f"    수익금: {position['unrealized_profit']:+.3f} USDT\n"
                if position["entries"]:
                    for i, (timestamp, qty, entryPrice) in enumerate(position["entries"], start=1):
                        t_str = datetime.fromtimestamp(timestamp / 1000).strftime("%Y-%m-%d %H:%M:%S")
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




