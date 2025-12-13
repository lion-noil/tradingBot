# controllers/mt5/mt5_rest_account.py

import json
from datetime import timezone, timedelta

import MetaTrader5 as mt5

from core.redis_client import redis_client


class Mt5RestAccountMixin:
    """
    BybitRestAccountMixin의 역할을 MT5용으로 옮긴 버전.

    - get_account_balance(): MT5 계좌 balance/equity/free_margin 등 조회
    - get_positions(symbol): 특정 심볼 포지션 조회
    - getNsav_asset(asset, symbol=None, save_redis=True): 자산 + 포지션 구성 & Redis 저장

    ✅ Bybit과 호환을 위해:
    - (Mt5RestOrdersMixin이 함께 믹스인 되어 있으면)
      load_orders + _build_entries_from_orders로 entries를 구성한다.
    """

    REDIS_ASSET_KEY = "trading:mt5_signal:asset"

    def _asset_key(self):
        return self.REDIS_ASSET_KEY

    # -------------------------
    # 내부: MT5 연결 보장
    # -------------------------
    def _ensure_mt5(self):
        if mt5.initialize():
            return True
        if getattr(self, "system_logger", None):
            self.system_logger.error(f"[ERROR] MT5 initialize failed: {mt5.last_error()}")
        return False

    def _json_or_empty_list(self, inpobj):
        if inpobj is None:
            return "[]"
        return json.dumps(inpobj, separators=(",", ":"), ensure_ascii=False)

    # -------------------------
    # 계좌(자산) 조회
    # -------------------------
    def get_account_balance(self):
        try:
            if not self._ensure_mt5():
                return None

            acc = mt5.account_info()
            if acc is None:
                if getattr(self, "system_logger", None):
                    self.system_logger.error(f"[ERROR] account_info() failed: {mt5.last_error()}")
                return None

            return {
                "currency": getattr(acc, "currency", None),
                "balance": float(getattr(acc, "balance", 0.0) or 0.0),
                "equity": float(getattr(acc, "equity", 0.0) or 0.0),
                "margin": float(getattr(acc, "margin", 0.0) or 0.0),
                "free_margin": float(getattr(acc, "margin_free", 0.0) or 0.0),
                "leverage": int(getattr(acc, "leverage", 0) or 0),
            }

        except Exception as e:
            if getattr(self, "system_logger", None):
                self.system_logger.error(f"[ERROR] 계좌 조회 실패: {e}")
            return None

    # -------------------------
    # 특정 심볼 포지션 조회
    # -------------------------
    def get_positions(self, symbol: str = None):
        try:
            if not self._ensure_mt5():
                return []

            if symbol:
                rows = mt5.positions_get(symbol=symbol) or []
            else:
                rows = mt5.positions_get() or []

            return list(rows)

        except Exception as e:
            if getattr(self, "system_logger", None):
                self.system_logger.error(f"[ERROR] positions_get 실패: {e}")
            return []

    # -------------------------
    # (선택) 심볼 규칙 조회: 최소랏/스텝/최대랏 등
    # -------------------------
    def get_symbol_rules(self, symbol: str):
        try:
            if not self._ensure_mt5():
                return None

            info = mt5.symbol_info(symbol)
            if info is None:
                if getattr(self, "system_logger", None):
                    self.system_logger.error(f"[ERROR] symbol_info({symbol}) failed: {mt5.last_error()}")
                return None

            return {
                "symbol": symbol,
                "volume_min": float(getattr(info, "volume_min", 0.0) or 0.0),
                "volume_step": float(getattr(info, "volume_step", 0.0) or 0.0),
                "volume_max": float(getattr(info, "volume_max", 0.0) or 0.0),
                "trade_contract_size": float(getattr(info, "trade_contract_size", 0.0) or 0.0),
                "trade_tick_size": float(getattr(info, "trade_tick_size", 0.0) or 0.0),
                "trade_tick_value": float(getattr(info, "trade_tick_value", 0.0) or 0.0),
            }

        except Exception as e:
            if getattr(self, "system_logger", None):
                self.system_logger.error(f"[ERROR] 심볼 룰 조회 실패({symbol}): {e}")
            return None

    # -------------------------
    # 자산 + 포지션 구성 & Redis 저장
    # -------------------------
    def getNsav_asset(self, asset, symbol: str = None, save_redis: bool = True):
        # ---- 1) account wallet ----
        result = self.get_account_balance()

        if result and save_redis:
            try:
                ccy = result.get("currency") or "ACC"
                prev = float((asset.get("wallet") or {}).get(ccy) or 0.0)
                newv = float(result.get("balance") or 0.0)

                if prev != newv:
                    redis_client.hset(self._asset_key(), f"wallet.{ccy}", f"{newv:.10f}")
                    asset.setdefault("wallet", {})
                    asset["wallet"][ccy] = newv

                # (선택) equity/free_margin도 저장하고 싶으면 켜도 됨
                redis_client.hset(self._asset_key(), f"equity.{ccy}", f"{float(result.get('equity') or 0.0):.10f}")
                redis_client.hset(self._asset_key(), f"free_margin.{ccy}", f"{float(result.get('free_margin') or 0.0):.10f}")

            except Exception as e:
                if getattr(self, "system_logger", None):
                    self.system_logger.error(f"[WARN] Redis 저장 실패(wallet): {e}")

        if not symbol:
            return asset

        sym = symbol.upper()
        asset.setdefault("positions", {})
        asset["positions"].setdefault(sym, {"LONG": None, "SHORT": None})

        # ---- 2) positions ----
        rows = self.get_positions(symbol=sym) or []

        long_pos, short_pos = None, None

        for p in rows:
            volume = float(getattr(p, "volume", 0.0) or 0.0)
            if volume == 0:
                continue

            price_open = float(getattr(p, "price_open", 0.0) or 0.0)
            ptype = int(getattr(p, "type", -1))

            if ptype == mt5.POSITION_TYPE_BUY:
                if long_pos is None:
                    long_pos = {"qty": volume, "avg_price": price_open}
                else:
                    tot = long_pos["qty"] + volume
                    if tot > 0:
                        long_pos["avg_price"] = (long_pos["avg_price"] * long_pos["qty"] + price_open * volume) / tot
                    long_pos["qty"] = tot

            elif ptype == mt5.POSITION_TYPE_SELL:
                if short_pos is None:
                    short_pos = {"qty": volume, "avg_price": price_open}
                else:
                    tot = short_pos["qty"] + volume
                    if tot > 0:
                        short_pos["avg_price"] = (short_pos["avg_price"] * short_pos["qty"] + price_open * volume) / tot
                    short_pos["qty"] = tot

        # ---- 3) entries build (✅ Bybit 방식 그대로) ----
        # Mt5RestOrdersMixin이 같이 믹스인되어 있으면:
        # - load_orders(symbol)
        # - _build_entries_from_orders(local_orders, symbol, "LONG"/"SHORT", qty)
        local_orders = []
        can_build_entries = hasattr(self, "load_orders") and hasattr(self, "_build_entries_from_orders")

        if can_build_entries:
            try:
                local_orders = self.load_orders(sym) or []
            except Exception:
                local_orders = []

        if long_pos is not None:
            if can_build_entries:
                try:
                    long_pos["entries"] = self._build_entries_from_orders(local_orders, sym, "LONG", long_pos["qty"])
                except Exception:
                    long_pos["entries"] = []
            else:
                long_pos["entries"] = []

        if short_pos is not None:
            if can_build_entries:
                try:
                    short_pos["entries"] = self._build_entries_from_orders(local_orders, sym, "SHORT", short_pos["qty"])
                except Exception:
                    short_pos["entries"] = []
            else:
                short_pos["entries"] = []

        asset["positions"][sym]["LONG"] = long_pos
        asset["positions"][sym]["SHORT"] = short_pos

        # ---- 4) redis save ----
        if save_redis:
            try:
                redis_client.hset(
                    self._asset_key(),
                    f"positions.{sym}",
                    self._json_or_empty_list(asset["positions"][sym]),
                )
            except Exception as e:
                if getattr(self, "system_logger", None):
                    self.system_logger.error(f"[WARN] Redis 저장 실패({sym}): {e}")

        return asset


if __name__ == "__main__":
    from pprint import pprint

    # ✅ 프로젝트 환경(app/config.py)이 .env를 로드하고 가드도 실행하도록
    from app import config as cfg

    if not getattr(cfg, "ENABLE_MT5", True):
        raise RuntimeError("ENABLE_MT5=0 입니다. .env에서 ENABLE_MT5=1로 켜주세요.")

    print("\n[0-1] redis ping test")
    try:
        print("redis ping:", redis_client.ping())
    except Exception as e:
        print("redis ping failed:", e)

    TEST_SYMBOL = getattr(cfg, "MT5_TEST_SYMBOL", None) or "BTCUSD"

    # ✅ Orders mixin까지 같이 붙여야 entries가 채워짐
    try:
        from controllers.mt5.mt5_rest_orders import Mt5RestOrdersMixin
    except Exception:
        Mt5RestOrdersMixin = object

    class _Tester(Mt5RestAccountMixin, Mt5RestOrdersMixin):
        system_logger = None

    t = _Tester()

    print("\n[0] CONFIG SNAPSHOT")
    print("ENABLE_MT5:", getattr(cfg, "ENABLE_MT5", None))
    print("TEST_SYMBOL:", TEST_SYMBOL)
    print("REDIS_ASSET_KEY:", t.REDIS_ASSET_KEY)

    print("\n[1] MT5 initialize / account info")
    acc = t.get_account_balance()
    pprint(acc)

    print("\n[2] symbol rules")
    rules = t.get_symbol_rules(TEST_SYMBOL)
    pprint(rules)

    print("\n[3] positions (symbol)")
    pos = t.get_positions(TEST_SYMBOL)
    print(f"positions count = {len(pos)}")
    if pos:
        try:
            pprint(pos[0]._asdict())
        except Exception:
            pprint(pos[0])

    print("\n[4] getNsav_asset + redis save test (with entries)")
    asset = {}
    out = t.getNsav_asset(asset, symbol=TEST_SYMBOL, save_redis=True)
    pprint(out)

    # entries 확인용 출력
    p = ((out.get("positions") or {}).get(TEST_SYMBOL.upper()) or {})
    if p.get("LONG"):
        print("\n[CHECK] LONG entries:")
        pprint(p["LONG"].get("entries"))
    if p.get("SHORT"):
        print("\n[CHECK] SHORT entries:")
        pprint(p["SHORT"].get("entries"))

    print("\nDONE")
