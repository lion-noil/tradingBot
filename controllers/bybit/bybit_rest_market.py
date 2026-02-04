# controllers/bybit/bybit_rest_market.py

import time
from datetime import timezone, timedelta

import requests

KST = timezone(timedelta(hours=9))


def _safe_int(x):
    try:
        return int(x)
    except Exception:
        return int(float(x))


class BybitRestMarketMixin:
    # -------------------------
    # 캔들 업데이트 (가격용, 메인넷)
    # -------------------------
    def update_candles(self, candles, symbol=None, count=None):
        try:
            symbol = symbol
            # ✅ 가격용 REST URL (메인넷)
            url = f"{self.price_base_url}/v5/market/kline"

            target = count if (isinstance(count, int) and count > 0) else 1000
            all_candles = []
            latest_end = None  # ms

            while len(all_candles) < target:
                req_limit = min(1000, target - len(all_candles))
                params = {
                    "category": "linear",
                    "symbol": symbol,
                    "interval": "1",
                    "limit": req_limit,
                }
                if latest_end is not None:
                    params["end"] = latest_end

                res = requests.get(url, params=params, timeout=10)
                res.raise_for_status()

                data = res.json()
                if not isinstance(data, dict):
                    raise RuntimeError(f"unexpected JSON root: {type(data).__name__}")

                ret_code = data.get("retCode", 0)
                if ret_code != 0:
                    raise RuntimeError(
                        f"bybit error retCode={ret_code}, retMsg={data.get('retMsg')}"
                    )

                result = data.get("result", {})
                raw_list = result.get("list") or []
                if not raw_list:
                    break

                raw_list = raw_list[::-1]

                chunk = []
                for c in raw_list:
                    try:
                        if not isinstance(c, (list, tuple)) or len(c) < 5:
                            continue
                        chunk.append(
                            {
                                "start": _safe_int(c[0]),
                                "open": float(c[1]),
                                "high": float(c[2]),
                                "low": float(c[3]),
                                "close": float(c[4]),
                            }
                        )
                    except Exception:
                        continue

                if chunk:
                    all_candles = chunk + all_candles
                    latest_end = _safe_int(raw_list[0][0]) - 1
                else:
                    break

                if len(raw_list) < req_limit:
                    break

            if isinstance(count, int) and count > 0:
                all_candles = all_candles[-count:]

            candles.clear()
            candles.extend(all_candles)

            last = candles[-1] if candles else None
            if getattr(self, "system_logger", None):
                if last:
                    self.system_logger.debug(
                        f"📊 ({symbol}) 캔들 갱신 완료: {len(candles)}개, "
                        f"last OHLC=({last['open']}, {last['high']}, {last['low']}, {last['close']})"
                    )
                else:
                    self.system_logger.debug(f"📊 ({symbol}) 캔들 갱신: 결과 없음")

        except Exception as e:
            if getattr(self, "system_logger", None):
                self.system_logger.warning(f"❌ ({symbol}) 캔들 요청 실패: {e}")


