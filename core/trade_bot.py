
from utils.logger import setup_logger
from strategies.basic_strategy import get_long_entry_reasons, get_short_entry_reasons, get_exit_reasons

logger = setup_logger()

class TradeBot:
    def __init__(self, controller, manual_queue, symbol="BTCUSDT"):
        self.binance = controller
        self.manual_queue = manual_queue
        self.symbol = symbol
        self.position_time = {}  # LONG/SHORT 별 진입시간
        self.running = True


    async def run_once(self,):

        price, ma100, prev = self.binance.get_real_data()

        closes = self.binance.get_ohlc_1m(minutes=1440, ma_window=100)
        ma100s = self.binance.ma100_list(closes)

        target_cross = 6
        optimal_thr = self.binance.find_optimal_threshold(closes, ma100s,target_cross=target_cross)
        ma_threshold = optimal_thr
        momentum_threshold = ma_threshold / 3

        log_msg = (
            f"💹 현재가: {price}, MA100: {ma100}, 3분전: {prev}\n"
            f"100평 ±{ma_threshold * 100:.3f}%, 급등 ±{momentum_threshold * 100:.3f}% (목표 크로스 {target_cross}회)\n"
        )

        status = self.binance.get_current_position_status()
        status_list = status.get("positions", [])
        balance = status.get("balance", {})
        log_msg += self.binance.make_status_log_msg(status)

        logger.debug(log_msg)

        pos_dict = {p["position"]: p for p in status_list}

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

        leverage_limit = 20
        exit_ma_threshold = 0.0002 # 청산 기준

        ## short 진입 조건
        recent_short_time = None
        if "SHORT" in pos_dict and pos_dict["SHORT"]["entries"]:
            recent_short_time = self.position_time['SHORT']
        short_reasons = get_short_entry_reasons(price, ma100, prev, recent_short_time,
                                                ma_threshold=ma_threshold, momentum_threshold=momentum_threshold)
        if short_reasons:
            short_reason_msg = (
                    "📌 숏 진입 조건 충족:\n - " +
                    "\n - ".join(short_reasons) +
                    f"\n100평 ±{ma_threshold * 100:.3f}%, 급등 ±{momentum_threshold * 100:.3f}% (목표 크로스 {target_cross}회)\n"
            )

            logger.info(short_reason_msg)
            # 포지션 비중 제한 검사 (40% 이상이면 실행 막기)
            short_amt = abs(float(pos_dict.get("SHORT", {}).get("position_amt", 0)))
            short_position_value = short_amt * price
            total_balance = balance["total"]
            position_ratio = short_position_value / total_balance

            if position_ratio >= leverage_limit:
                logger.info(f"⛔ 숏 포지션 비중 {position_ratio  :.0%} → 총 자산의 {leverage_limit * 100:.0f}% 초과, 추매 차단")
            else:
                self.binance.sell_market_100(self.symbol, price, percent, balance)

        ## long 진입 조건
        recent_long_time = None
        if "LONG" in pos_dict and pos_dict["LONG"]["entries"]:
            recent_long_time = self.position_time['LONG']
        long_reasons = get_long_entry_reasons(price, ma100, prev, recent_long_time,
                                              ma_threshold=ma_threshold, momentum_threshold=momentum_threshold)

        if long_reasons:
            long_reason_msg = (
                    "📌 롱 진입 조건 충족:\n - " +
                    "\n - ".join(long_reasons) +
                    f"\n100평 ±{ma_threshold * 100:.3f}%, 급등 ±{momentum_threshold * 100:.3f}% (목표 크로스 {target_cross}회)\n"
            )
            logger.info(long_reason_msg)
            long_amt = abs(float(pos_dict.get("LONG", {}).get("position_amt", 0)))
            long_position_value = long_amt * price
            total_balance = balance["total"]
            position_ratio = long_position_value / total_balance

            if position_ratio >= leverage_limit:
                logger.info(f"⛔ 롱 포지션 비중 {position_ratio:.2%} → 총 자산의 {leverage_limit * 100:.0f}% 초과, 추매 차단")
            else:
                self.binance.buy_market_100(self.symbol, price, percent, balance)

        
        ## 청산조건
        for side in ["LONG", "SHORT"]:
            recent_time = self.position_time.get(side)
            if recent_time:
                entry_price = pos_dict[side]["entryPrice"]
                exit_reasons = get_exit_reasons(
                    side,
                    price,
                    ma100,
                    recent_time,
                    ma_threshold=exit_ma_threshold
                )

                if exit_reasons:
                    pos_amt = abs(float(pos_dict[side]["position_amt"]))
                    logger.info(f"📤 자동 청산 사유({side}): {' / '.join(exit_reasons)}")
                    self.binance.close_position(self.symbol, side=side, qty=pos_amt, entry_price=entry_price)
