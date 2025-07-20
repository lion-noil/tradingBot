# controllers/binance_controller.py

from binance.client import Client
from binance.enums import *
from dotenv import load_dotenv
import os
from utils.logger import setup_logger
logger = setup_logger()
load_dotenv()
import json

class BinanceFuturesController:
    def __init__(self):
        self.client = Client(
            os.getenv("BINANCE_API_KEY"),
            os.getenv("BINANCE_API_SECRET")
        )
        # 선물 테스트넷 URL (USDⓢ-M)
        self.client.FUTURES_URL = "https://testnet.binancefuture.com/fapi"
        self.positions_file = "positions.json"
        self.orders_file = "orders.json"
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
            logger.info(f"포지션 변경 감지됨 → 로컬 파일 업데이트")

            self.save_local_positions(new_positions)

        return new_positions

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
                logger.info(f"📥 신규 주문 {appended}건 저장됨")

            return local_orders  # 전체 저장된 주문 리스트를 리턴

        except Exception as e:
            logger.error(f"[ERROR] 바이낸스 주문 동기화 실패: {e}")
            return self.load_orders()  # 실패 시 기존 것이라도 리턴

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

            logger.info(f"🟩 롱 진입 시작 | 수량: {qty} @ 현재가 {price:.2f}")

            order = self.client.futures_create_order(
                symbol=symbol,
                side=SIDE_BUY,
                type=FUTURE_ORDER_TYPE_MARKET,
                quantity=qty,
                positionSide="LONG"
            )

            order_id = order.get("orderId")
            avg_price = order.get("avgFillPrice", price)  # 테스트넷은 avgFillPrice 없을 수 있음
            logger.info(f"✅ 롱 진입 완료 | 주문ID: {order_id}, 진입가: {avg_price}")

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

            logger.info(f"🟥 숏 진입 시작 | 수량: {qty} @ 현재가 {price:.2f}")

            order = self.client.futures_create_order(
                symbol=symbol,
                side=SIDE_SELL,
                type=FUTURE_ORDER_TYPE_MARKET,
                quantity=qty,
                positionSide="SHORT"
            )

            order_id = order.get("orderId")
            avg_price = order.get("avgFillPrice", price)  # 테스트넷에서는 avgFillPrice 없을 수 있음
            logger.info(f"✅ 숏 진입 완료 | 주문ID: {order_id}, 진입가: {avg_price}")

            return order  # 성공 시 주문 정보 리턴

        except Exception as e:
            logger.error(f"❌ 숏 진입 실패: {e}")
            return None

    def close_position(self, symbol="BTCUSDT", side=None, qty=None):
        try:
            if not side or not qty:
                logger.error(f"❌ 청산 요청 실패: side 또는 qty가 제공되지 않음")
                return
            qty = abs(float(qty))

            logger.info(f"📉 {side} 포지션 청산 시도 | 수량: {qty}")

            order = self.client.futures_create_order(
                symbol=symbol,
                side=SIDE_SELL if side == "LONG" else SIDE_BUY,
                type=FUTURE_ORDER_TYPE_MARKET,
                quantity=qty,
                positionSide=side
            )


            logger.info(f"✅ {side} 포지션 청산 완료 | 주문ID: {order.get('orderId')}")

        except Exception as e:
            logger.error(f"❌ 포지션 청산 실패 ({side}): {e}")

