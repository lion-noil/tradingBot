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


class _RateLimited(Exception):
    """Bybit rate-limit(retCode=10006) 또는 HTTP 429. 일시적·자동복구 → 텔레 억제."""
    pass


class BybitRestMarketMixin:
    def _kline_get(self, url, params, *, max_retries: int = 5):
        """캔들 1페이지 요청. rate-limit(10006/429)은 지수 백오프로 조용히 재시도.
        시작 시 여러 서비스가 동시에 대량 백필해도 텔레 스팸 없이 흡수."""
        delay = 0.5
        for attempt in range(max_retries + 1):
            res = requests.get(url, params=params, timeout=10)
            if res.status_code == 429:
                if attempt < max_retries:
                    time.sleep(delay); delay = min(delay * 2, 8.0); continue
                raise _RateLimited("HTTP 429 Too Many Requests")
            res.raise_for_status()
            data = res.json()
            if not isinstance(data, dict):
                raise RuntimeError(f"unexpected JSON root: {type(data).__name__}")
            ret_code = data.get("retCode", 0)
            if ret_code == 10006:  # Too many visits (rate limit)
                if attempt < max_retries:
                    time.sleep(delay); delay = min(delay * 2, 8.0); continue
                raise _RateLimited(f"retCode=10006 {data.get('retMsg')}")
            if ret_code != 0:
                raise RuntimeError(f"bybit error retCode={ret_code}, retMsg={data.get('retMsg')}")
            return data
        raise _RateLimited("rate-limit retries exhausted")
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

                data = self._kline_get(url, params)  # rate-limit 백오프 재시도 내장

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

                time.sleep(0.12)  # 페이지 간 예의상 간격 (rate-limit 예방)

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

        except _RateLimited as e:
            # rate-limit은 일시적 → 다음 캔들 주기에 자동 복구. 텔레 억제(INFO).
            if getattr(self, "system_logger", None):
                self.system_logger.info(f"⏳ ({symbol}) 캔들 rate-limit, 다음 주기 재시도: {e}")
        except Exception as e:
            if getattr(self, "system_logger", None):
                self.system_logger.warning(f"❌ ({symbol}) 캔들 요청 실패: {e}")


