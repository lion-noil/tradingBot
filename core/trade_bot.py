
from utils.logger import setup_logger
from strategies.basic_strategy import get_long_entry_reasons, get_short_entry_reasons, get_exit_reasons
import requests
logger = setup_logger()

class TradeBot:
    def __init__(self, controller, manual_queue, symbol="BTCUSDT"):
        self.binance = controller
        self.manual_queue = manual_queue
        self.symbol = symbol
        self.position_time = {}  # LONG/SHORT 별 진입시간
        self.BINANCE_API_URL = "https://api.binance.com/api/v3/klines"
        self.running = True

    def get_real_data(self, symbol="BTCUSDT"):
        try:
            url = f"{self.BINANCE_API_URL}?symbol={symbol}&interval=1m&limit=100"
            res = requests.get(url, timeout=5)
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

    def get_current_position_status(self, symbol="BTCUSDT"):
        posinfo_list = self.binance.get_full_position_info(symbol)
        all_orders = self.binance.sync_orders_from_binance(symbol)

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

        balances = self.binance.client.futures_account_balance()

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
            all_positions = self.binance.client.futures_account()["positions"]
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

    async def run_once(self, price, ma100, prev, status_list, balance):

        pos_dict = {p["position"]: p for p in status_list}

        has_long = "LONG" in pos_dict and float(pos_dict["LONG"]["position_amt"]) != 0
        has_short = "SHORT" in pos_dict and float(pos_dict["SHORT"]["position_amt"]) != 0

        # 2. 진입시간 최신화 (entries가 있으면 첫 엔트리의 timestamp를 진입시간으로)
        self.position_time = {
            "LONG": pos_dict.get("LONG", {}).get("entries", [[None]])[0][0] if pos_dict.get("LONG") and
                                                                               pos_dict["LONG"]["entries"] else None,
            "SHORT": pos_dict.get("SHORT", {}).get("entries", [[None]])[0][0] if pos_dict.get("SHORT") and
                                                                                 pos_dict["SHORT"]["entries"] else None,
        }

        # 3. 수동 명령 처리
        if not self.manual_queue.empty():
            command_data = await self.manual_queue.get()

            if isinstance(command_data, dict):
                command = command_data.get("command")
                percent = command_data.get("percent", 10)  # 기본값 10%
                close_side = command_data.get("side")
            else:
                command = command_data
                percent = 10

            if command == "long":
                self.binance.buy_market_100(self.symbol, price, percent, balance)
            elif command == "short":
                self.binance.sell_market_100(self.symbol, price, percent, balance)
            elif command == "close":
                if close_side and close_side in pos_dict:
                    pos_amt = float(pos_dict[close_side]["position_amt"])
                    if pos_amt != 0:
                        self.binance.close_position(self.symbol, side=close_side,qty = pos_amt)
                    else:
                        logger.info(f"❗ 청산할 {close_side} 포지션 없음 (수량 0)")
                else:
                    logger.info(f"❗ 포지션 정보 없음 or 잘못된 side: {close_side}")



        # 4. 자동매매 조건 평가
        percent = 10 # 총자산의 진입비율
        ma_threshold = 0.002
        momentum_threshold = ma_threshold/2

        ## short 진입 조건
        
        recent_short_time = None
        if "SHORT" in pos_dict and pos_dict["SHORT"]["entries"]:
            recent_short_time = self.position_time['SHORT']
        short_reasons = get_short_entry_reasons(price, ma100, prev, recent_short_time,
                                                ma_threshold=ma_threshold, momentum_threshold=momentum_threshold)
        if short_reasons:
            logger.info("📌 숏 진입 조건 충족:\n - " + "\n - ".join(short_reasons))
            self.binance.sell_market_100(self.symbol, price, percent, balance)

        ## long 진입 조건
        recent_long_time = None
        if "LONG" in pos_dict and pos_dict["LONG"]["entries"]:
            recent_long_time = self.position_time['LONG']
        long_reasons = get_long_entry_reasons(price, ma100, prev, recent_long_time,
                                              ma_threshold=ma_threshold, momentum_threshold=momentum_threshold)
        if long_reasons:
            logger.info("📌 롱 진입 조건 충족:\n - " + "\n - ".join(long_reasons))
            self.binance.buy_market_100(self.symbol, price, percent, balance)
        
        ## 청산조건
        for side in ["LONG", "SHORT"]:
            recent_time = self.position_time.get(side)
            if recent_time:
                exit_reasons = get_exit_reasons(side, price, ma100, recent_time)
                if exit_reasons:
                    pos_amt = float(pos_dict[side]["position_amt"])
                    logger.info(f"📤 자동 청산 사유({side}): {' / '.join(exit_reasons)}")
                    self.binance.close_position(self.symbol, side=side, qty=pos_amt)
