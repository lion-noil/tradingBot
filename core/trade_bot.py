
from utils.logger import setup_logger
from strategies.basic_strategy import get_long_entry_reasons, get_short_entry_reasons, get_exit_reasons
from collections import deque
import time
import json
logger = setup_logger()

class TradeBot:
    def __init__(self, controller, bybit_websocket_controller, bybit_rest_controller, manual_queue, symbol="BTCUSDT"):
        self.controller = controller
        self.bybit_websocket_controller = bybit_websocket_controller
        self.bybit_rest_controller = bybit_rest_controller
        self.manual_queue = manual_queue
        self.symbol = symbol
        self.running = True
        self.closes = deque(maxlen=1539)
        self.ma100s = self.bybit_rest_controller.ma100_list(self.closes)
        self.last_closes_update = 0
        self.status = self.controller.get_current_position_status()
        self.balance = self.status.get("balance", {})
        self.status_list = self.status.get("positions", [])
        self.pos_dict = {p["position"]: p for p in self.status_list}
        self.position_time = {
            "LONG": self.pos_dict.get("LONG", {}).get("entries", [[None]])[0][0] if self.pos_dict.get("LONG") and
                                                                               self.pos_dict["LONG"]["entries"] else None,
            "SHORT": self.pos_dict.get("SHORT", {}).get("entries", [[None]])[0][0] if self.pos_dict.get("SHORT") and
                                                                                 self.pos_dict["SHORT"]["entries"] else None,
        }

        self.status = self.bybit_rest_controller.get_full_position_info()

        self.target_cross = 4
        self.ma_threshold = 0.005


    async def run_once(self,):
        now = time.time()

        if now - self.last_closes_update >= 60:  # 1분 이상 경과 시
            self.bybit_rest_controller.update_closes(self.closes,count=1539)
            self.ma100s = self.bybit_rest_controller.ma100_list(self.closes)
            self.last_closes_update = now
            self.ma_threshold = self.controller.find_optimal_threshold(self.closes, self.ma100s, min_thr=0.005, max_thr=0.03,
                                                                 target_cross=self.target_cross)

        price= self.bybit_websocket_controller.price
        ma100 = self.ma100s[-1]
        prev = self.closes[-4]

        percent = 10  # 총자산의 진입비율
        leverage_limit = 20
        exit_ma_threshold = 0.0002  # 청산 기준
        momentum_threshold = self.ma_threshold / 3

        log_msg = (
            f"💹 현재가: {price}, MA100: {ma100:.1f}, 3분전: {prev}\n"
            f"100평 ±{ma_threshold * 100:.3f}%, 급등 ±{momentum_threshold * 100:.3f}% (목표 크로스 {self.target_cross }회)"
        )
        log_msg += self.controller.make_status_log_msg(self.status)
        logger.debug(log_msg)

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
                self.controller.buy_market_100(self.symbol, price, percent, self.balance)
            elif command == "short":
                self.controller.sell_market_100(self.symbol, price, percent, self.balance)
            elif command == "close":
                if close_side and close_side in self.pos_dict:
                    pos_amt = float(self.pos_dict[close_side]["position_amt"])
                    if pos_amt != 0:
                        self.controller.close_position(self.symbol, side=close_side,qty = pos_amt)
                    else:
                        logger.info(f"❗ 청산할 {close_side} 포지션 없음 (수량 0)")
                else:
                    logger.info(f"❗ 포지션 정보 없음 or 잘못된 side: {close_side}")
            self.status = self.controller.get_current_position_status()
            self.status_list = self.status.get("positions", [])
            self.balance = self.status.get("balance", {})
            self.pos_dict = {p["position"]: p for p in self.status_list}
            self.position_time = {
                "LONG": self.pos_dict.get("LONG", {}).get("entries", [[None]])[0][0] if self.pos_dict.get("LONG") and
                                                                                        self.pos_dict["LONG"][
                                                                                            "entries"] else None,
                "SHORT": self.pos_dict.get("SHORT", {}).get("entries", [[None]])[0][0] if self.pos_dict.get("SHORT") and
                                                                                          self.pos_dict["SHORT"][
                                                                                              "entries"] else None,
            }


        # 4. 자동매매 조건 평가
        ## short 진입 조건
        recent_short_time = None
        if "SHORT" in self.pos_dict and self.pos_dict["SHORT"]["entries"]:
            recent_short_time = self.position_time['SHORT']
        short_reasons = get_short_entry_reasons(price, ma100, prev, recent_short_time,
                                                ma_threshold=ma_threshold, momentum_threshold=momentum_threshold)
        if short_reasons:
            short_reason_msg = (
                    "📌 숏 진입 조건 충족:\n - " +
                    "\n - ".join(short_reasons) +
                    f"\n100평 ±{ma_threshold * 100:.3f}%, 급등 ±{momentum_threshold * 100:.3f}% (목표 크로스 {self.target_cross }회)"
            )

            logger.info(short_reason_msg)
            # 포지션 비중 제한 검사 (40% 이상이면 실행 막기)
            short_amt = abs(float(self.pos_dict.get("SHORT", {}).get("position_amt", 0)))
            short_position_value = short_amt * price
            total_balance = self.balance["total"]
            position_ratio = short_position_value / total_balance

            if position_ratio >= leverage_limit:
                logger.info(f"⛔ 숏 포지션 비중 {position_ratio  :.0%} → 총 자산의 {leverage_limit * 100:.0f}% 초과, 추매 차단")
            else:
                self.controller.sell_market_100(self.symbol, price, percent, self.balance)
                self.status = self.controller.get_current_position_status()
                self.status_list = self.status.get("positions", [])
                self.balance = self.status.get("balance", {})
                self.pos_dict = {p["position"]: p for p in self.status_list}
                self.position_time = {
                    "LONG": self.pos_dict.get("LONG", {}).get("entries", [[None]])[0][0] if self.pos_dict.get(
                        "LONG") and
                                                                                            self.pos_dict["LONG"][
                                                                                                "entries"] else None,
                    "SHORT": self.pos_dict.get("SHORT", {}).get("entries", [[None]])[0][0] if self.pos_dict.get(
                        "SHORT") and
                                                                                              self.pos_dict["SHORT"][
                                                                                                  "entries"] else None,
                }


        ## long 진입 조건
        recent_long_time = None
        if "LONG" in self.pos_dict and self.pos_dict["LONG"]["entries"]:
            recent_long_time = self.position_time['LONG']
        long_reasons = get_long_entry_reasons(price, ma100, prev, recent_long_time,
                                              ma_threshold=ma_threshold, momentum_threshold=momentum_threshold)

        if long_reasons:
            long_reason_msg = (
                    "📌 롱 진입 조건 충족:\n - " +
                    "\n - ".join(long_reasons) +
                    f"\n100평 ±{ma_threshold * 100:.3f}%, 급등 ±{momentum_threshold * 100:.3f}% (목표 크로스 {self.target_cross }회)"
            )
            logger.info(long_reason_msg)
            long_amt = abs(float(self.pos_dict.get("LONG", {}).get("position_amt", 0)))
            long_position_value = long_amt * price
            total_balance = self.balance["total"]
            position_ratio = long_position_value / total_balance

            if position_ratio >= leverage_limit:
                logger.info(f"⛔ 롱 포지션 비중 {position_ratio:.2%} → 총 자산의 {leverage_limit * 100:.0f}% 초과, 추매 차단")
            else:
                self.controller.buy_market_100(self.symbol, price, percent, self.balance)
                self.status = self.controller.get_current_position_status()
                self.status_list = self.status.get("positions", [])
                self.balance = self.status.get("balance", {})
                self.pos_dict = {p["position"]: p for p in self.status_list}
                self.position_time = {
                    "LONG": self.pos_dict.get("LONG", {}).get("entries", [[None]])[0][0] if self.pos_dict.get(
                        "LONG") and
                                                                                            self.pos_dict["LONG"][
                                                                                                "entries"] else None,
                    "SHORT": self.pos_dict.get("SHORT", {}).get("entries", [[None]])[0][0] if self.pos_dict.get(
                        "SHORT") and
                                                                                              self.pos_dict["SHORT"][
                                                                                                  "entries"] else None,
                }

        
        ## 청산조건
        for side in ["LONG", "SHORT"]:
            recent_time = self.position_time.get(side)
            if recent_time:
                entry_price = self.pos_dict[side]["entryPrice"]
                exit_reasons = get_exit_reasons(
                    side,
                    price,
                    ma100,
                    recent_time,
                    ma_threshold=exit_ma_threshold
                )

                if exit_reasons:
                    pos_amt = abs(float(self.pos_dict[side]["position_amt"]))
                    logger.info(f"📤 자동 청산 사유({side}): {' / '.join(exit_reasons)}")
                    self.controller.close_position(self.symbol, side=side, qty=pos_amt, entry_price=entry_price)
                    self.status = self.controller.get_current_position_status()
                    self.status_list = self.status.get("positions", [])
                    self.balance = self.status.get("balance", {})
                    self.pos_dict = {p["position"]: p for p in self.status_list}
                    self.position_time = {
                        "LONG": self.pos_dict.get("LONG", {}).get("entries", [[None]])[0][0] if self.pos_dict.get(
                            "LONG") and
                                                                                                self.pos_dict["LONG"][
                                                                                                    "entries"] else None,
                        "SHORT": self.pos_dict.get("SHORT", {}).get("entries", [[None]])[0][0] if self.pos_dict.get(
                            "SHORT") and
                                                                                                  self.pos_dict[
                                                                                                      "SHORT"][
                                                                                                      "entries"] else None,
                    }

