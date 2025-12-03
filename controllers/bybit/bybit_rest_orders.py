# controllers/bybit/bybit_rest_orders.py
import os
import json
import time
from datetime import datetime, timezone, timedelta

import requests

KST = timezone(timedelta(hours=9))


class BybitRestOrdersMixin:
    # -------------------------
    # Path helpers (심볼별 로컬 파일 경로)
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
                self.system_logger.error(f"거래기록 로드 실패: {e}")
            return []

    def save_orders(self, symbol: str, trades):
        path = self._fp_orders(symbol)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(trades, f, indent=2)
        except Exception as e:
            if getattr(self, "system_logger", None):
                self.system_logger.error(f"[ERROR] 거래기록 저장 실패: {e}")

    def append_order(self, symbol: str, trade: dict):
        """
        trade 하나를 로컬 파일에 append (중복 방지)
        """
        try:
            local_orders = self.load_orders(symbol)
            existing_ids = {str(o.get("id")) for o in local_orders}
            if str(trade.get("id")) in existing_ids:
                if getattr(self, "system_logger", None):
                    self.system_logger.debug(f"⏩ 이미 존재하는 trade id={trade.get('id')} ({symbol}), 스킵")
                return local_orders

            local_orders.append(trade)
            self.save_orders(symbol, local_orders)
            if getattr(self, "system_logger", None):
                self.system_logger.debug(f"📥 ({symbol}) 신규 trade {trade.get('id')} 저장됨")
            return local_orders
        except Exception as e:
            if getattr(self, "system_logger", None):
                self.system_logger.error(f"[ERROR] 거래기록 append 실패: {e}")
            return self.load_orders(symbol)

    # -------------------------
    # Bybit에서 체결내역 동기화
    # -------------------------
    def sync_orders_from_bybit(self, symbol="BTCUSDT"):
        method = "GET"
        category = "linear"
        limit = 20
        endpoint = "/v5/execution/list"
        params_dict = {
            "category": category,
            "symbol": symbol,
            "limit": limit,
        }
        params_str = "&".join([f"{k}={params_dict[k]}" for k in sorted(params_dict)])
        url = f"{self.base_url}{endpoint}?{params_str}"

        def _fetch_once():
            # 매 호출마다 재서명(타임스탬프 최신화)
            headers = self._get_headers(method, endpoint, params=params_str, body="")
            try:
                resp = requests.get(url, headers=headers, timeout=5)
                # HTTP 레벨 오류
                if resp.status_code != 200:
                    if getattr(self, "system_logger", None):
                        self.system_logger.error(f"❌ HTTP 오류 {resp.status_code}: {resp.text[:200]}")
                    return None
                try:
                    data = resp.json()
                except Exception:
                    if getattr(self, "system_logger", None):
                        self.system_logger.error(f"❌ JSON 파싱 실패: {resp.text[:200]}")
                    return None
                if data.get("retCode") != 0:
                    if getattr(self, "system_logger", None):
                        self.system_logger.error(
                            f"❌ Bybit 오류 retCode={data.get('retCode')}, retMsg={data.get('retMsg')}"
                        )
                    return None
                result = data.get("result") or {}
                lst = result.get("list")
                if not isinstance(lst, list):
                    if getattr(self, "system_logger", None):
                        self.system_logger.error(f"❌ result.list가 리스트가 아님: {type(lst)}")
                    return None
                return lst
            except requests.exceptions.Timeout:
                if getattr(self, "system_logger", None):
                    self.system_logger.error("⏱️ 요청 타임아웃")
                return None
            except requests.exceptions.RequestException as e:
                if getattr(self, "system_logger", None):
                    self.system_logger.error(f"🌐 네트워크 예외: {e}")
                return None

        # 1차 요청
        executions = _fetch_once()
        # (옵션) 실패 시 1회 재시도
        if executions is None:
            if getattr(self, "system_logger", None):
                self.system_logger.debug("↻ 재시도: 서명/타임스탬프 갱신")
            executions = _fetch_once()
            if executions is None:
                # 완전 실패면 기존 로컬 그대로 반환
                return self.load_orders(symbol)

        try:
            local_orders = self.load_orders(symbol)
            existing_ids = {str(order["id"]) for order in local_orders}
            appended = 0
            for e in reversed(executions):
                if e.get("execType") != "Trade" or float(e.get("execQty", 0)) == 0:
                    continue

                exec_id = str(e["execId"])
                if exec_id in existing_ids:
                    continue

                side = e["side"]
                position_side = "LONG" if side == "Buy" else "SHORT"
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
                    "time_str": datetime.fromtimestamp(
                        int(e["execTime"]) / 1000, tz=KST
                    ).strftime("%Y-%m-%d %H:%M:%S"),
                    "fee": float(e.get("execFee", 0)),
                }

                local_orders.append(trade)
                existing_ids.add(exec_id)
                appended += 1

            # ✅ 시간순 정렬 (옛날 → 최신)
            if local_orders:
                local_orders.sort(key=lambda x: x.get("time", 0))

            if appended > 0:
                self.save_orders(symbol, local_orders)
                if getattr(self, "system_logger", None):
                    self.system_logger.debug(f"📥 ({symbol}) 신규 체결 {appended}건 저장됨")
            return local_orders

        except Exception as e:
            if getattr(self, "system_logger", None):
                self.system_logger.error(f"[ERROR] 주문 동기화 실패: {e}")
            return self.load_orders(symbol)

    # -------------------------
    # 특정 orderId로 체결 조회
    # -------------------------
    def get_trade_w_order_id(self, symbol="BTCUSDT", order_id=None):
        if not order_id:
            if getattr(self, "system_logger", None):
                self.system_logger.error("❌ order_id가 필요합니다.")
            return self.load_orders(symbol)

        method = "GET"
        endpoint = "/v5/execution/list"
        params_dict = {
            "category": "linear",
            "symbol": symbol,
            "orderId": order_id,  # orderId 필터링 → limit 불필요
        }

        # 공통 GET 유틸
        def _fetch_once() -> list | None:
            params_str = "&".join([f"{k}={params_dict[k]}" for k in sorted(params_dict)])
            url = f"{self.base_url}{endpoint}?{params_str}"
            headers = self._get_headers(method, endpoint, params=params_str, body="")
            try:
                resp = requests.get(url, headers=headers, timeout=5)
                if resp.status_code != 200:
                    if getattr(self, "system_logger", None):
                        self.system_logger.error(
                            f"❌ HTTP 오류 {resp.status_code}: {resp.text[:200]}"
                        )
                    return None
                try:
                    data = resp.json()
                except Exception:
                    if getattr(self, "system_logger", None):
                        self.system_logger.error(f"❌ JSON 파싱 실패: {resp.text[:200]}")
                    return None
                if data.get("retCode") != 0:
                    if getattr(self, "system_logger", None):
                        self.system_logger.error(
                            f"❌ Bybit 오류 retCode={data.get('retCode')}, retMsg={data.get('retMsg')}"
                        )
                    return None
                result = data.get("result") or {}
                lst = result.get("list")
                if not isinstance(lst, list):
                    if getattr(self, "system_logger", None):
                        self.system_logger.error(
                            f"❌ result.list가 리스트가 아님: {type(lst)}"
                        )
                    return None
                return lst
            except requests.exceptions.Timeout:
                if getattr(self, "system_logger", None):
                    self.system_logger.error("⏱️ 요청 타임아웃")
                return None
            except requests.exceptions.RequestException as e:
                if getattr(self, "system_logger", None):
                    self.system_logger.error(f"🌐 네트워크 예외: {e}")
                return None

        t1 = time.time()
        exec_timeout_sec = 10
        poll_interval_sec = 1

        while True:
            executions = _fetch_once()
            if executions:
                break
            if time.time() - t1 > exec_timeout_sec:
                if getattr(self, "system_logger", None):
                    self.system_logger.error(
                        f"⏰ executions 반영 대기 타임아웃({exec_timeout_sec}s). 부분 체결/전파 지연 가능."
                    )
                break
            time.sleep(poll_interval_sec)
        if not executions:
            return []

        e = executions[0]
        exec_id = str(e["execId"])
        side = e["side"]
        position_side = "LONG" if side == "Buy" else "SHORT"
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
            "time_str": datetime.fromtimestamp(
                int(e["execTime"]) / 1000, tz=KST
            ).strftime("%Y-%m-%d %H:%M:%S"),
            "fee": float(e.get("execFee", 0)),
        }

        return trade

    # -------------------------
    # 엔트리 빌드 (포지션 구성용)
    # -------------------------
    def _build_entries_from_orders(
        self, local_orders: list, symbol: str, direction: str, target_qty: float
    ):
        if not target_qty or target_qty <= 0:
            return []

        # 해당 심볼, 해당 방향(LONG/SHORT), OPEN 체결만 추출
        open_orders = [
            o
            for o in local_orders
            if o.get("symbol") == symbol
            and o.get("side") == direction
            and o.get("type") == "OPEN"
        ]
        # 최신부터 소비하기 위해 시간 내림차순
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
                    "ts_str": datetime.fromtimestamp(
                        ts_ms / 1000, tz=KST
                    ).strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
            remaining -= use_qty

        # 오래된 → 최신 순으로 정렬해 반환
        picked.sort(key=lambda x: x["ts"])
        return picked
