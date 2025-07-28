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

    def get_real_data(self, symbol="BTCUSDT"):
        try:
            url = f"{self.BINANCE_API_URL}?symbol={symbol}&interval=1m&limit=100"
            res = requests.get(url, timeout=10)
            res.raise_for_status()
            candles = res.json()

            closes = [float(c[4]) for c in candles]
            ma100 = sum(closes) / len(closes)
            price_now = closes[-1]
            price_3min_ago = closes[-4]

            return round(price_now, 3), round(ma100, 3), round(price_3min_ago, 3)

        except Exception as e:
            print(f"❌ 실시간 데이터 가져오기 실패: {e}")
            return None, None, None
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
            # 최신 100개 주문 가져오기
            new_orders = self.client.futures_get_all_orders(symbol=symbol, limit=100)
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

        log_msg = ""
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

    def get_ohlc_1m(self, symbol="BTCUSDT", minutes=1440, ma_window=100):
        total_needed = minutes + (ma_window - 1)
        url = "https://api.binance.com/api/v3/klines"
        now = int(time.time() * 1000)
        closes = []
        left = total_needed
        end_time = now

        while left > 0:
            get_num = min(left, 1000)
            params = dict(symbol=symbol, interval="1m", limit=get_num, endTime=end_time)
            res = requests.get(url, params=params)
            res.raise_for_status()
            candles = res.json()
            if not candles:
                break

            closes_chunk = [float(c[4]) for c in candles]
            closes = closes_chunk + closes  # 앞에 붙이기(과거→최근순)
            end_time = candles[0][0] - 1  # 다음 요청은 더 과거로 이동
            left -= get_num
        closes = closes[-total_needed:]

        return closes

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



