# controllers/mt5/mt5_rest_market.py
from __future__ import annotations

from typing import Any, Dict, List, Optional
from datetime import timezone, timedelta
import time
KST = timezone(timedelta(hours=9))


def _safe_int(x):
    try:
        return int(x)
    except Exception:
        return int(float(x))


class Mt5RestMarketMixin:
    """
    시세/캔들/시장 관련 기능 (가격은 URL 서버로 계속 받는 버전)
    - update_candles: ONLINE(price) 서버 사용
    """

    def update_candles(self, candles: list, symbol: str | None = None, count: int | None = None):
        """
        - 서버 엔드포인트: GET /v5/market/candles/with-gaps
        - 응답:
            {
              "retCode": 0,
              "retMsg": "OK",
              "result": {
                "symbol": "US100",
                "list": [[ms, o, h, l, c, vol], ...],
                "nextCursor": 1710000000000
              }
            }
        """
        try:
            symbol = symbol or "US100"
            sym = symbol.upper()
            endpoint = "/v5/market/candles/with-gaps"

            target = count if (isinstance(count, int) and count > 0) else 1000
            all_candles: List[Dict[str, Any]] = []
            end_ms: Optional[int] = None

            seen_starts: set[int] = set()  # 중복 제거용

            while len(all_candles) < target:
                req_limit = min(1000, target - len(all_candles))

                params: Dict[str, Any] = {
                    "symbol": sym,
                    "interval": "1",
                    "limit": req_limit,
                }
                if end_ms is not None:
                    params["end"] = int(end_ms)

                # ✅ ONLINE(price) 서버로 호출 (Mixin을 쓰는 상위 클래스가 _request 제공해야 함)
                data = self._request("GET", endpoint, params=params, use="price")

                if not isinstance(data, dict):
                    raise RuntimeError(f"unexpected JSON root: {type(data).__name__}")

                ret_code = data.get("retCode", 0)
                if ret_code != 0:
                    ret_msg = data.get("retMsg")
                    raise RuntimeError(f"mt5 candles error retCode={ret_code}, retMsg={ret_msg}")

                result = data.get("result", {}) or {}
                rows = result.get("list") or []

                if not isinstance(rows, list):
                    raise RuntimeError(f"'list' is {type(rows).__name__}, not list")

                if not rows:
                    break

                # 안전 정렬
                rows.sort(key=lambda x: x[0])

                chunk: List[Dict[str, Any]] = []
                for c in rows:
                    try:
                        if not isinstance(c, (list, tuple)) or len(c) < 6:
                            continue

                        ts_ms = _safe_int(c[0])
                        if ts_ms in seen_starts:
                            continue

                        o, h, l, close, vol = c[1], c[2], c[3], c[4], c[5]

                        item = {
                            "start": ts_ms,
                            "open": float(o) if o is not None else None,
                            "high": float(h) if h is not None else None,
                            "low": float(l) if l is not None else None,
                            "close": float(close) if close is not None else None,
                            "volume": float(vol or 0.0),
                        }
                        chunk.append(item)
                        seen_starts.add(ts_ms)
                    except Exception:
                        continue

                if chunk:
                    # 과거 chunk를 앞에 붙여 "옛날→최신" 유지
                    all_candles = chunk + all_candles
                else:
                    break

                # 페이징: nextCursor 사용
                next_cursor = result.get("nextCursor")
                if next_cursor is None:
                    break
                try:
                    end_ms = int(next_cursor)
                except Exception:
                    break

                if len(rows) < req_limit:
                    break

            if isinstance(count, int) and count > 0:
                all_candles = all_candles[-count:]

            candles.clear()
            candles.extend(all_candles)

            last = candles[-1] if candles else None
            if getattr(self, "system_logger", None):
                if last:
                    self.system_logger.debug(
                        f"📊 [MT5] ({sym}) 캔들 갱신 완료: {len(candles)}개, "
                        f"last OHLC=({last['open']}, {last['high']}, {last['low']}, {last['close']}), "
                        f"vol={last['volume']}"
                    )
                else:
                    self.system_logger.debug(f"📊 [MT5] ({sym}) 캔들 갱신: 결과 없음")

        except Exception as e:
            if getattr(self, "system_logger", None):
                self.system_logger.warning(f"❌ [MT5] ({symbol}) 캔들 요청 실패: {e}")
