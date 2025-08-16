
from utils.logger import setup_logger
from strategies.basic_strategy import get_long_entry_reasons, get_short_entry_reasons, get_exit_reasons
from collections import deque
import time
import json
import math
logger = setup_logger()
import asyncio, random
class TradeBot:
    def __init__(self, bybit_websocket_controller, bybit_rest_controller, manual_queue, symbol="BTCUSDT"):

        self.bybit_websocket_controller = bybit_websocket_controller
        self.bybit_rest_controller = bybit_rest_controller
        self.manual_queue = manual_queue
        self.symbol = symbol
        self.running = True
        self.closes_num = 7200
        self.closes = deque(maxlen=self.closes_num)

        self.ma100s = None
        self.last_closes_update = 0
        self.target_cross = 5
        self.ma_threshold = None

        # 동시 진입/중복 업데이트 방지
        self._sync_lock = asyncio.Lock()
        self._just_traded_until = 0.0  # 직후 틱 자동진입/중복 실행 방지 쿨다운

        self.price_history = deque(maxlen=4)

    def record_price(self):
        ts = time.time()
        price = getattr(self.bybit_websocket_controller, "price", None)

        # 1) 값 유효성 검사
        if not isinstance(price, (int, float)):
            logger.debug("skip record_price: non-numeric price=%r", price)
            return
        if not (price > 0):  # 0 또는 음수 방지
            logger.debug("skip record_price: non-positive price=%r", price)
            return
        # float NaN/Inf 방지
        if math.isnan(price) or math.isinf(price):
            logger.debug("skip record_price: NaN/Inf price=%r", price)
            return

        # 2) 타임스탬프 단조 증가(간헐적 시계 역전/동일 ts 방지)
        if self.price_history and ts <= self.price_history[-1][0]:
            ts = self.price_history[-1][0] + 1e-6
        self.price_history.append((ts, float(price)))

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
            self.bybit_rest_controller.update_closes(self.closes,count=self.closes_num)
            self.ma100s = self.bybit_rest_controller.ma100_list(self.closes)
            self.last_closes_update = now
            self.ma_threshold = self.bybit_rest_controller.find_optimal_threshold(self.closes, self.ma100s, min_thr=0.005, max_thr=0.03,
                                                                 target_cross=self.target_cross)

            self.bybit_rest_controller.get_full_position_info(self.symbol)
            self.bybit_rest_controller.sync_orders_from_bybit()
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

            if command in ("long", "short"):
                await self._execute_and_sync(
                    self.bybit_rest_controller.open_market,
                    self.status,
                    self.symbol,
                    command,  # "long" or "short"
                    latest_price,
                    percent,
                    self.balance
                )

            elif command == "close":
                if close_side and close_side in self.pos_dict:
                    pos_amt = float(self.pos_dict[close_side]["position_amt"])
                    if pos_amt != 0:
                        await self._execute_and_sync(
                            self.bybit_rest_controller.close_market,
                            self.status,
                            self.symbol,
                            side=close_side,  # "LONG" or "SHORT"
                            qty=pos_amt
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
                        f"\n100평 ±{self.ma_threshold * 100:.3f}%, 급등 ±{momentum_threshold * 100:.3f}% (목표 크로스 {self.target_cross }회 / ({self.closes_num} 분봉))"
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
                        self.bybit_rest_controller.open_market,
                        self.status,
                        self.symbol,
                        "short",  # "long" or "short"
                        latest_price,
                        percent,
                        self.balance
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
                        f"\n100평 ±{self.ma_threshold * 100:.3f}%, 급등 ±{momentum_threshold * 100:.3f}% (목표 크로스 {self.target_cross }회 / ({self.closes_num} 분봉))"
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
                        self.bybit_rest_controller.open_market,
                        self.status,
                        self.symbol,
                        "long",  # "long" or "short"
                        latest_price,
                        percent,
                        self.balance
                    )


            ## 청산조건
            for side in ["LONG", "SHORT"]:
                recent_time = self.position_time.get(side)
                if recent_time:
                    exit_reasons = get_exit_reasons(
                        side, latest_price, self.now_ma100, recent_time, ma_threshold=exit_ma_threshold
                    )

                    if exit_reasons:
                        pos_amt = abs(float(self.pos_dict[side]["position_amt"]))
                        logger.info(f"📤 자동 청산 사유({side}): {' / '.join(exit_reasons)}")
                        await self._execute_and_sync(
                            self.bybit_rest_controller.close_market,
                            self.status,
                            self.symbol,
                            side=side,  # "LONG" or "SHORT"
                            qty=pos_amt
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

    async def _execute_and_sync(self, fn, prev_status, *args, **kwargs):
        async with self._sync_lock:
            # 1) 주문 실행
            result = fn(*args, **kwargs)  # place_market_order / close_position 등
            order_id = result.get("orderId")

            # 2) 체결 확인
            filled = None
            if order_id:
                filled = self.bybit_rest_controller.wait_order_fill(self.symbol, order_id)

            orderStatus = (filled or {}).get("orderStatus", "").upper()

            if orderStatus == "FILLED":
                self._log_fill(filled, prev_status=prev_status)

                self.bybit_rest_controller.get_full_position_info(self.symbol)
                trade = self.bybit_rest_controller.get_trade_w_order_id(self.symbol,order_id)
                self.bybit_rest_controller.append_order(trade)
                now_status = self.bybit_rest_controller.get_current_position_status(symbol=self.symbol)
                self._apply_status(now_status)

            elif orderStatus in ("CANCELLED", "REJECTED"):
                logger.warning(f"⚠️ 주문 {order_id[-6:]} 상태: {orderStatus} (체결 없음)")
                # 이미 취소/거절 상태 → 추가 취소 API 호출 불필요
            elif orderStatus == "TIMEOUT":
                logger.warning(f"⚠️ 주문 {order_id[-6:]} 체결 대기 타임아웃 → 취소 시도")
                try:
                    cancel_res = self.bybit_rest_controller.cancel_order(self.symbol, order_id)
                    logger.warning(f"🗑️ 단일 주문 취소 결과: {cancel_res}")
                except Exception as e:
                    logger.error(f"단일 주문 취소 실패: {e}")
            else:
                # 예상치 못한 상태(New/PartiallyFilled 등) → 정책에 따라 취소할지, 더 기다릴지
                logger.warning(f"ℹ️ 주문 {order_id[-6:]} 상태: {orderStatus or 'UNKNOWN'} → 정책에 따라 처리")


            # 같은 루프에서 자동 조건이 바로 또 트리거되지 않도록 짧은 쿨다운
            self._just_traded_until = time.monotonic() + 0.8
            return result

    def _classify_intent(self, filled: dict) -> str:
        side = (filled.get("side") or "").upper()  # BUY/SELL
        pos = int(filled.get("positionIdx") or 0)  # 1/2
        ro = bool(filled.get("reduceOnly"))  # True/False
        if ro:  # 청산
            if pos == 1 and side == "SELL":  return "LONG_CLOSE"
            if pos == 2 and side == "BUY":   return "SHORT_CLOSE"
        else:  # 진입
            if pos == 1 and side == "BUY":   return "LONG_OPEN"
            if pos == 2 and side == "SELL":  return "SHORT_OPEN"

    def _log_fill(self, filled: dict, prev_status: dict | None = None):
        side,intent = self._classify_intent(filled).split("_") # LONG_OPEN / SHORT_OPEN / LONG_CLOSE / SHORT_CLOSE ...
        order_tail = (filled.get("orderId") or "")[-6:] or "UNKNOWN"
        avg_price = float(filled.get("avgPrice") or 0.0)  # 이번 체결가 (청산가)
        exec_qty = float(filled.get("cumExecQty") or filled.get("qty") or 0.0)
        fee = float(filled.get("cumExecFee") or 0.0)  # USDT, 보통 음수


        # 진입(OPEN): 기본 로그
        if not intent.endswith("CLOSE"):
            logger.info(
                f"✅ {side} 주문 체결 완료\n"
                f" | 주문ID(뒷6자리): {order_tail}\n"
                f" | 평균진입가: {avg_price:.2f}\n"
                f" | 체결수량: {exec_qty}"
            )
            return

        # 청산(CLOSE): prev_status에서 평균진입가 자동 해석
        entry_price = self._extract_entry_price_from_prev(filled, prev_status)

        # 손익 계산
        if (side,intent) == ("LONG","CLOSE"):
            profit_gross = (avg_price - entry_price) * exec_qty
        else:  # SHORT_CLOSE
            profit_gross = (entry_price - avg_price) * exec_qty

        profit_net = profit_gross + fee
        profit_rate = (profit_gross / entry_price) * 100 if entry_price else 0.0

        logger.info(
            f"✅ {side} 포지션 청산 완료\n"
            f" | 주문ID: {order_tail}\n"
            f" | 평균진입가: {entry_price:.2f}\n"
            f" | 청산가: {avg_price:.2f}\n"
            f" | 체결수량: {exec_qty}\n"
            f" | 수익금(수수료 제외): {profit_net:.2f}\n"
            f" | 수익금(총): {profit_gross:.2f}, 수수료: {fee:.2f}\n"
            f" | 수익률: {profit_rate:.2f}%"
        )

    def _extract_entry_price_from_prev(self, filled: dict, prev_status: dict | None) -> float | None:
        """
        prev_status 예:
        {
          'balance': {...},
          'positions': [
            {'position': 'LONG', 'position_amt': 0.025, 'entryPrice': 117421.7,
             'entries': [(1755328360633, 0.025, 117393.4, '2025-08-16 16:12:40')]}
          ]
        }
        """
        if not prev_status:
            return None

        # filled의 포지션 방향 파악
        pos_idx = int(filled.get("positionIdx") or 0)  # 1: LONG, 2: SHORT
        side_key = "LONG" if pos_idx == 1 else "SHORT"

        positions = prev_status.get("positions") or []
        # 1) 우선 entryPrice 필드
        for p in positions:
            if (p.get("position") or "").upper() == side_key:
                ep = p.get("entryPrice") or p.get("avgPrice")
                if ep is not None:
                    try:
                        return float(ep)
                    except Exception:
                        pass
                # 2) 없으면 entries 마지막 체결가로 폴백
                entries = p.get("entries") or []
                if entries:
                    try:
                        # (ts, qty, price, time_str)
                        return float(entries[-1][2])
                    except Exception:
                        pass
        return None