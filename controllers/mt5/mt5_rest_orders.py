# controllers/mt5/mt5_rest_orders.py
import os
import json
import time
from datetime import datetime, timezone, timedelta

import MetaTrader5 as mt5

KST = timezone(timedelta(hours=9))


class Mt5RestOrdersMixin:
    """
    MT5 터미널(로컬 MetaTrader5) 기반 주문/체결 기록 관리

    ✅ 현실적인 운영 전략(중요):
    - 브로커/심볼에 따라 MT5 Python API의 history_deals_get/history_orders_get 결과가
      0이거나 symbol이 비는 경우가 있음(너 지금 케이스).
    - 그래서 "주문 성공 시점에 로컬 파일 기록(=mt5_rest_trade.py에서 기록)"을 진실로 두고,
      sync는 MT5 히스토리가 잡히면 보강하는 형태로 동작하도록 한다.
    """

    # -------------------------
    # 내부: MT5 연결 보장
    # -------------------------
    def _ensure_mt5(self) -> bool:
        if mt5.initialize():
            return True
        if getattr(self, "system_logger", None):
            self.system_logger.error(f"[ERROR] MT5 initialize failed: {mt5.last_error()}")
        return False

    # -------------------------
    # Path helpers
    # -------------------------
    def _fp_orders(self, symbol: str) -> str:
        return f"{symbol}_orders.json"

    # -------------------------
    # 로컬 주문 기록 로드/저장
    # -------------------------
    def load_orders(self, symbol: str):
        path = self._fp_orders(symbol)
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                return json.loads(content) if content else []
        except Exception as e:
            if getattr(self, "system_logger", None):
                self.system_logger.error(f"[MT5] 거래기록 로드 실패: {e}")
            return []

    def save_orders(self, symbol: str, trades):
        path = self._fp_orders(symbol)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(trades, f, indent=2, ensure_ascii=False)
        except Exception as e:
            if getattr(self, "system_logger", None):
                self.system_logger.error(f"[MT5][ERROR] 거래기록 저장 실패: {e}")

    def append_order(self, symbol: str, trade: dict):
        """
        trade 하나를 로컬 파일에 append (중복 방지)
        """
        try:
            local_orders = self.load_orders(symbol)
            existing_ids = {str(o.get("id")) for o in local_orders}
            if str(trade.get("id")) in existing_ids:
                if getattr(self, "system_logger", None):
                    self.system_logger.debug(
                        f"⏩ [MT5] 이미 존재 trade id={trade.get('id')} ({symbol}), 스킵"
                    )
                return local_orders

            local_orders.append(trade)
            self.save_orders(symbol, local_orders)
            if getattr(self, "system_logger", None):
                self.system_logger.debug(f"📥 [MT5] ({symbol}) 신규 trade {trade.get('id')} 저장됨")
            return local_orders
        except Exception as e:
            if getattr(self, "system_logger", None):
                self.system_logger.error(f"[MT5][ERROR] 거래기록 append 실패: {e}")
            return self.load_orders(symbol)

    # -------------------------
    # 내부: 심볼 매칭(브로커 suffix 대응)  ✅ 더 널널하게
    # -------------------------
    def _match_symbol(self, deal_symbol: str, target_symbol: str) -> bool:
        ds = (deal_symbol or "").upper()
        ts = (target_symbol or "").upper()
        if not ds or not ts:
            return False

        # 완전 동일
        if ds == ts:
            return True
        # 접두/접미/포함 (BTCUSDm, BTCUSD.r, BTCUSD-ECN, XBTCUSD 같은 케이스까지)
        if ds.startswith(ts) or ds.endswith(ts) or (ts in ds):
            return True
        return False

    # -------------------------
    # deal -> trade dict 변환
    # -------------------------
    def _deal_to_trade(self, d) -> dict | None:
        """
        MT5 deal(namedtuple) -> 공통 trade dict로 변환
        """
        try:
            dtype = int(getattr(d, "type", -1))
            entry = int(getattr(d, "entry", -1))
            volume = float(getattr(d, "volume", 0.0) or 0.0)

            # ✅ volume 0인 deal은 보통 balance/credit/commission 성격 -> trade로 취급하지 않음
            if volume <= 0:
                return None

            # 방향
            if dtype == mt5.DEAL_TYPE_BUY:
                position_side = "LONG"
            elif dtype == mt5.DEAL_TYPE_SELL:
                position_side = "SHORT"
            else:
                return None

            # OPEN/CLOSE 판정
            if entry in (mt5.DEAL_ENTRY_IN, mt5.DEAL_ENTRY_INOUT):
                trade_type = "OPEN"
            elif entry == mt5.DEAL_ENTRY_OUT:
                trade_type = "CLOSE"
            else:
                trade_type = "OPEN"

            t_msc = getattr(d, "time_msc", None)
            if t_msc is None:
                ts_ms = int(getattr(d, "time", 0) or 0) * 1000
            else:
                ts_ms = int(t_msc)

            price = float(getattr(d, "price", 0.0) or 0.0)
            commission = float(getattr(d, "commission", 0.0) or 0.0)
            swap = float(getattr(d, "swap", 0.0) or 0.0)
            fee = commission + swap

            return {
                "id": str(getattr(d, "ticket", "")),          # deal ticket
                "symbol": str(getattr(d, "symbol", "")),
                "side": position_side,
                "type": trade_type,
                "qty": volume,
                "price": price,
                "time": ts_ms,
                "time_str": datetime.fromtimestamp(ts_ms / 1000, tz=KST).strftime("%Y-%m-%d %H:%M:%S"),
                "fee": float(fee),
                "order_id": str(getattr(d, "order", "")),     # order ticket
                "position_id": str(getattr(d, "position_id", "")),
                "profit": float(getattr(d, "profit", 0.0) or 0.0),
            }
        except Exception:
            return None

    # -------------------------
    # history_deals_get/orders_get 안전 호출 ✅ 네 콘솔 테스트 방식과 동일하게 naive로만
    # -------------------------
    def _history_deals_get_safe(self, date_from: datetime, date_to: datetime):
        deals = mt5.history_deals_get(date_from, date_to)
        if deals is None:
            return []
        return list(deals)

    def _history_orders_get_safe(self, date_from: datetime, date_to: datetime):
        orders = mt5.history_orders_get(date_from, date_to)
        if orders is None:
            return []
        return list(orders)

    # -------------------------
    # MT5에서 체결내역 동기화
    # -------------------------
    def sync_orders_from_mt5(self, symbol: str = "EURUSD", lookback_days: int = 30, debug: bool = True):
        """
        ✅ 동작 원칙
        - 로컬 파일이 기본(진실)
        - MT5 history_deals_get에서 '실거래 deal(volume>0, BUY/SELL)'이 잡히면 로컬에 병합
        - MT5에서 아무것도 안 잡히면 로컬을 그대로 반환 (0으로 덮어쓰지 않음)
        """
        sym = (symbol or "").upper()
        if not sym:
            return []

        local_orders = self.load_orders(sym)
        existing_ids = {str(o.get("id")) for o in local_orders}

        if not self._ensure_mt5():
            return local_orders

        # ✅ naive(local) datetime 사용 (너 콘솔 테스트와 동일)
        date_to = datetime.now()
        date_from = date_to - timedelta(days=int(lookback_days))

        deals = self._history_deals_get_safe(date_from, date_to)

        if debug:
            print(f"[DEBUG] history_deals_get total={len(deals)} range={date_from.isoformat()} ~ {date_to.isoformat()}")
            # 최근 10개 원본 스냅샷(필터 전) - 실제로 symbol이 뭔지 확인용
            if deals:
                ds = sorted(deals, key=lambda d: int(getattr(d, "time_msc", 0) or 0), reverse=True)[:10]
                for d in ds:
                    print(
                        "[DEBUG] raw_deal:",
                        "ticket=", getattr(d, "ticket", None),
                        "symbol=", repr(getattr(d, "symbol", None)),
                        "type=", getattr(d, "type", None),
                        "entry=", getattr(d, "entry", None),
                        "volume=", getattr(d, "volume", None),
                        "price=", getattr(d, "price", None),
                        "order=", getattr(d, "order", None),
                        "time_msc=", getattr(d, "time_msc", None),
                    )

        # ✅ 심볼 필터
        deals_sym = [d for d in deals if self._match_symbol(getattr(d, "symbol", ""), sym)]
        if debug:
            print(f"[DEBUG] filtered deals for {sym} => {len(deals_sym)}")

        appended = 0

        if deals_sym:
            deals_sym.sort(key=lambda d: int(getattr(d, "time_msc", 0) or int(getattr(d, "time", 0) or 0) * 1000))
            for d in deals_sym:
                trade = self._deal_to_trade(d)
                if not trade:
                    continue
                if str(trade["id"]) in existing_ids:
                    continue
                local_orders.append(trade)
                existing_ids.add(str(trade["id"]))
                appended += 1

        if appended > 0:
            local_orders.sort(key=lambda x: x.get("time", 0))
            self.save_orders(sym, local_orders)
            if getattr(self, "system_logger", None):
                self.system_logger.debug(f"📥 [MT5] ({sym}) 신규 deal {appended}건 저장됨")
        else:
            if debug:
                print("[DEBUG] no new mt5 deals appended. keep local_orders as-is.")

        return local_orders

    # -------------------------
    # 특정 orderId(ticket)로 체결 조회
    # -------------------------
    def get_trade_w_order_id(self, symbol: str = "EURUSD", order_id=None, debug: bool = True):
        """
        ✅ 우선순위:
        1) 로컬 파일에서 order_id 매칭 찾아서 반환
        2) MT5 history_deals_get에서 (deal.order == oid) OR (deal.ticket == oid) 찾아서 반환
           - 기본은 짧은 윈도우(60분) → 실패 시 확장
           - naive local datetime 기준(네 환경에서 검증됨)
        """
        import time
        from datetime import datetime, timedelta

        sym = (symbol or "").upper()
        oid = str(order_id) if order_id is not None else ""

        if not oid:
            if getattr(self, "system_logger", None):
                self.system_logger.error("[MT5] ❌ order_id가 필요합니다.")
            return []

        # ✅ int 변환(가능하면)
        try:
            oid_int = int(float(oid))
        except Exception:
            oid_int = None

        # 1) local 먼저
        try:
            local_orders = self.load_orders(sym)
            for x in reversed(local_orders):
                if str(x.get("order_id", "")) == oid:
                    return x
        except Exception:
            pass

        if not self._ensure_mt5():
            return []

        # ✅ 서버 시간 기준 dt_to 만들기(tick 기반) - 안정화
        def _get_dt_to():
            now = datetime.now()
            try:
                tick = mt5.symbol_info_tick(sym)
                if tick and getattr(tick, "time", 0):
                    tick_dt = datetime.fromtimestamp(int(tick.time))
                    if tick_dt >= now - timedelta(minutes=2):
                        return tick_dt
            except Exception:
                pass
            return now

        # ✅ deals에서 oid 매칭 (order/ticket 둘 다)
        def _find_once(minutes: int):
            date_to = _get_dt_to()
            date_from = date_to - timedelta(minutes=minutes)

            deals = self._history_deals_get_safe(date_from, date_to)
            matched = []

            for d in deals:
                if not self._match_symbol(getattr(d, "symbol", ""), sym):
                    continue

                # ✅ 여기 핵심: order OR ticket 매칭
                if oid_int is not None:
                    d_order = int(getattr(d, "order", 0) or 0)
                    d_ticket = int(getattr(d, "ticket", 0) or 0)
                    if d_order != oid_int and d_ticket != oid_int:
                        continue
                else:
                    # 숫자 아닌 oid면 문자열 비교(드문 케이스)
                    if str(getattr(d, "order", "")) != oid and str(getattr(d, "ticket", "")) != oid:
                        continue

                trade = self._deal_to_trade(d)
                if trade:
                    matched.append(trade)

            if matched:
                matched.sort(key=lambda x: x.get("time", 0))
                return matched[0]
            return None

        t1 = time.time()
        exec_timeout_sec = 10
        poll_interval_sec = 1

        # ✅ 기본은 짧게, 필요 시 확장
        windows = (60, 6 * 60, 24 * 60, 30 * 24 * 60)  # 1h → 6h → 1d → 30d

        while True:
            for w in windows:
                got = _find_once(w)
                if got:
                    return got

            if time.time() - t1 > exec_timeout_sec:
                if debug:
                    print(f"[DEBUG] get_trade_w_order_id timeout. sym={sym} oid={oid}")
                break

            time.sleep(poll_interval_sec)

        return []

    # -------------------------
    # 엔트리 빌드 (포지션 구성용)
    # -------------------------
    def _build_entries_from_orders(self, local_orders: list, symbol: str, direction: str, target_qty: float):
        if not target_qty or target_qty <= 0:
            return []

        sym = (symbol or "").upper()
        if not sym:
            return []

        open_orders = [
            o for o in (local_orders or [])
            if self._match_symbol(o.get("symbol", ""), sym)
            and o.get("side") == direction
            and o.get("type") == "OPEN"
        ]
        open_orders.sort(key=lambda x: x.get("time", 0), reverse=True)

        remaining = float(target_qty)
        picked = []
        for o in open_orders:
            if remaining <= 1e-12:
                break

            this_qty = float(o.get("qty", 0.0) or 0.0)
            use_qty = min(this_qty, remaining)

            ts_ms = int(o.get("time", 0) or 0)
            picked.append(
                {
                    "ts": ts_ms,
                    "qty": use_qty,
                    "price": float(o.get("price", 0.0) or 0.0),
                    "ts_str": datetime.fromtimestamp(ts_ms / 1000, tz=KST).strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
            remaining -= use_qty

        picked.sort(key=lambda x: x["ts"])
        return picked

