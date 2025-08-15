
from utils.logger import setup_logger
from strategies.basic_strategy import get_long_entry_reasons, get_short_entry_reasons, get_exit_reasons
from collections import deque
import time
import json
logger = setup_logger()
import asyncio, random
class TradeBot:
    def __init__(self, bybit_websocket_controller, bybit_rest_controller, manual_queue, symbol="BTCUSDT"):

        self.bybit_websocket_controller = bybit_websocket_controller
        self.bybit_rest_controller = bybit_rest_controller
        self.manual_queue = manual_queue
        self.symbol = symbol
        self.running = True
        self.closes = deque(maxlen=7200)

        self.ma100s = None
        self.last_closes_update = 0

        self.status = self.bybit_rest_controller.get_current_position_status()
        self._apply_status(self.status)
        self.target_cross = 5
        self.ma_threshold = None

        # 동시 진입/중복 업데이트 방지
        self._sync_lock = asyncio.Lock()
        self._just_traded_until = 0.0  # 직후 틱 자동진입/중복 실행 방지 쿨다운

        self.price_history = deque(maxlen=4)

    def record_price(self):
        price = self.bybit_websocket_controller.price
        self.price_history.append((time.time(), price))

    def check_price_jump(self, min_sec=0.5, max_sec=2, jump_pct=0.002):
        if len(self.price_history) < 4:
            return None  # 데이터 부족

        now_ts, now_price = self.price_history[-1]
        for ts, past_price in list(self.price_history)[:-1]:
            if min_sec <= now_ts - ts <= max_sec:  # 시간 조건 만족
                change_rate = (now_price - past_price) / past_price
                if abs(change_rate) >= jump_pct:
                    if change_rate > 0:
                        return "UP"  # 급등
                    else:
                        return "DOWN"  # 급락
        return None  # 변화 없음

    async def run_once(self,):
        now = time.time()

        # 1️⃣ 현재 가격 기록
        self.record_price()
        _, latest_price = self.price_history[-1]

        if now - self.last_closes_update >= 60:  # 1분 이상 경과 시
            self.bybit_rest_controller.update_closes(self.closes,count=7200)
            self.ma100s = self.bybit_rest_controller.ma100_list(self.closes)
            self.last_closes_update = now
            self.ma_threshold = self.bybit_rest_controller.find_optimal_threshold(self.closes, self.ma100s, min_thr=0.005, max_thr=0.03,
                                                                 target_cross=self.target_cross)
            new_status = self.bybit_rest_controller.get_current_position_status()
            self._apply_status(new_status)
            self.now_ma100 = self.ma100s[-1]
            self.prev = self.closes[-3]

        # 2️⃣ 급등락 테스트
        change = self.check_price_jump(min_sec=0.5, max_sec=2, jump_pct=self.ma_threshold)
        if change:
            if change == "UP":
                logger.info(" 📈 급등 감지!")
            elif change == "DOWN":
                logger.info(" 📉 급락 감지!")

        percent = 10  # 총자산의 진입비율
        leverage_limit = 20
        exit_ma_threshold = 0.0001  # 청산 기준
        momentum_threshold = self.ma_threshold / 3

        logger.debug(self.bybit_rest_controller.make_status_log_msg(
            self.status, latest_price, self.now_ma100, self.prev, self.ma_threshold,self.target_cross
        ))
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
                await self._execute_and_sync(
                    self.bybit_rest_controller.buy_market_100, self.symbol, latest_price, percent, self.balance
                )
            elif command == "short":
                await self._execute_and_sync(
                    self.bybit_rest_controller.sell_market_100, self.symbol, latest_price, percent, self.balance
                )

            elif command == "close":
                if close_side and close_side in self.pos_dict:
                    pos_amt = float(self.pos_dict[close_side]["position_amt"])
                    entry_price = self.pos_dict[close_side]["entryPrice"]
                    if pos_amt != 0:
                        await self._execute_and_sync(
                            self.bybit_rest_controller.close_position,
                            self.symbol, side=close_side, qty=pos_amt, entry_price=entry_price
                        )
                    else:
                        logger.info(f"❗ 청산할 {close_side} 포지션 없음 (수량 0)")
                else:
                    logger.info(f"❗ 포지션 정보 없음 or 잘못된 side: {close_side}")

        # 4. 자동매매 조건 평가
        if time.monotonic() >= self._just_traded_until:
            ## short 진입 조건
            recent_short_time = self.position_time.get("SHORT")
            short_reasons = get_short_entry_reasons(
                latest_price, self.now_ma100, self.prev, recent_short_time,
                ma_threshold=self.ma_threshold, momentum_threshold=momentum_threshold
            )
            if short_reasons:
                short_reason_msg = (
                        "📌 숏 진입 조건 충족:\n - " +
                        "\n - ".join(short_reasons) +
                        f"\n100평 ±{self.ma_threshold * 100:.3f}%, 급등 ±{momentum_threshold * 100:.3f}% (목표 크로스 {self.target_cross }회)"
                )

                logger.info(short_reason_msg)
                # 포지션 비중 제한 검사 (40% 이상이면 실행 막기)
                short_amt = abs(float(self.pos_dict.get("SHORT", {}).get("position_amt", 0)))
                short_position_value = short_amt * latest_price
                total_balance = self.balance.get("total", 0) or 0
                position_ratio = (short_position_value / total_balance) if total_balance else 0

                if position_ratio >= leverage_limit:
                    logger.info(f"⛔ 숏 포지션 비중 {position_ratio  :.0%} → 총 자산의 {leverage_limit * 100:.0f}% 초과, 추매 차단")
                else:
                    await self._execute_and_sync(
                        self.bybit_rest_controller.sell_market_100, self.symbol, latest_price, percent, self.balance
                    )


            ## long 진입 조건
            recent_long_time = self.position_time.get("LONG")
            long_reasons = get_long_entry_reasons(
                latest_price, self.now_ma100, self.prev, recent_long_time,
                ma_threshold=self.ma_threshold, momentum_threshold=momentum_threshold
            )

            if long_reasons:
                long_reason_msg = (
                        "📌 롱 진입 조건 충족:\n - " +
                        "\n - ".join(long_reasons) +
                        f"\n100평 ±{self.ma_threshold * 100:.3f}%, 급등 ±{momentum_threshold * 100:.3f}% (목표 크로스 {self.target_cross }회)"
                )
                logger.info(long_reason_msg)
                long_amt = abs(float(self.pos_dict.get("LONG", {}).get("position_amt", 0)))
                long_position_value = long_amt * latest_price
                total_balance = self.balance.get("total", 0) or 0
                position_ratio = (long_position_value / total_balance) if total_balance else 0

                if position_ratio >= leverage_limit:
                    logger.info(f"⛔ 롱 포지션 비중 {position_ratio:.0%} → 총 자산의 {leverage_limit * 100:.0f}% 초과, 추매 차단")
                else:
                    await self._execute_and_sync(
                        self.bybit_rest_controller.buy_market_100, self.symbol, latest_price, percent, self.balance
                    )


            ## 청산조건
            for side in ["LONG", "SHORT"]:
                recent_time = self.position_time.get(side)
                if recent_time:
                    entry_price = self.pos_dict[side]["entryPrice"]
                    exit_reasons = get_exit_reasons(
                        side, latest_price, self.now_ma100, recent_time, ma_threshold=exit_ma_threshold
                    )

                    if exit_reasons:
                        pos_amt = abs(float(self.pos_dict[side]["position_amt"]))
                        logger.info(f"📤 자동 청산 사유({side}): {' / '.join(exit_reasons)}")
                        await self._execute_and_sync(
                            self.bybit_rest_controller.close_position,
                            self.symbol, side=side, qty=pos_amt, entry_price=entry_price
                        )


    def _apply_status(self, status):
        """로컬 상태 일괄 갱신(중복 코드 제거)"""
        self.status = status
        self.status_list = status.get("positions", [])
        self.balance = status.get("balance", {})
        self.pos_dict = {p["position"]: p for p in self.status_list}
        self.position_time = {
            "LONG": (self.pos_dict.get("LONG", {}).get("entries") or [[None]])[-1][0]
            if self.pos_dict.get("LONG") and self.pos_dict["LONG"]["entries"] else None,
            "SHORT": (self.pos_dict.get("SHORT", {}).get("entries") or [[None]])[-1][0]
            if self.pos_dict.get("SHORT") and self.pos_dict["SHORT"]["entries"] else None,
        }

    def _extract_fp(self, status):
        """포지션/밸런스 변화 감지용 '지문' 생성"""
        pos_list = status.get("positions", [])
        pos_dict = {p.get("position"): p for p in pos_list}

        long_p = pos_dict.get("LONG", {})
        short_p = pos_dict.get("SHORT", {})

        def entry_time(p):
            entries = p.get("entries") or []
            return entries[0][0] if entries and entries[0] else None

        return (
            float(long_p.get("position_amt") or 0.0),
            float(short_p.get("position_amt") or 0.0),
            long_p.get("entryPrice"),
            short_p.get("entryPrice"),
            entry_time(long_p),
            entry_time(short_p),
            status.get("balance", {}).get("total"),
            long_p.get("updatedTime") or long_p.get("updated_at"),
            short_p.get("updatedTime") or short_p.get("updated_at"),
        )

    async def _refresh_until_change(self, prev_fp, timeout=6.0):
        """REST로 포지션/밸런스 변화가 감지될 때까지 짧게 대기"""
        delay = 0.18
        end = time.monotonic() + timeout
        latest = None
        while time.monotonic() < end:
            latest = self.bybit_rest_controller.get_current_position_status()
            if self._extract_fp(latest) != prev_fp:
                return latest
            await asyncio.sleep(delay + random.random() * 0.08)
            delay = min(delay * 1.7, 1.0)
        return latest or self.bybit_rest_controller.get_current_position_status()

    async def _execute_and_sync(self, fn, *args, **kwargs):
        """
        단일 엔트리포인트:
        1) 주문 실행
        2) 포지션/밸런스 변화 감지까지 대기
        3) 로컬 상태 일괄 갱신
        """
        async with self._sync_lock:
            prev_status = self.status or self.bybit_rest_controller.get_current_position_status()
            prev_fp = self._extract_fp(prev_status)

            result = fn(*args, **kwargs)  # buy/sell/close (동기 가정)

            new_status = await self._refresh_until_change(prev_fp, timeout=6.0)
            self._apply_status(new_status)

            # 같은 루프에서 자동 조건이 바로 또 트리거되지 않도록 짧은 쿨다운
            self._just_traded_until = time.monotonic() + 0.8
            return result


