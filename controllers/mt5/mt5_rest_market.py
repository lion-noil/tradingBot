# controllers/mt5/mt5_rest_market.py
from typing import Any, Dict, List

from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))


def _safe_int(x):
    try:
        return int(x)
    except Exception:
        return int(float(x))


class Mt5RestMarketMixin:
    """
    시세/캔들/시장 관련 기능 (BybitRestMarketMixin 포지션)
    현재는 캔들(update_candles)만 구현.
    """

    def update_candles(self, candles: list, symbol: str | None = None, count: int | None = None):
        """
        BybitRestMarketMixin.update_candles 와 같은 스타일로 만든 MT5 버전.

        - 서버 엔드포인트: GET /v5/market/candles/with-gaps
        - 쿼리:
            symbol: 심볼 (예: US100)
            interval: "1" (1분봉) 또는 "D"
            limit: 요청 개수
            end: end_ms (가장 최근 바의 끝 기준)

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

        candles 리스트는 아래 형태의 dict 들로 채워진다:
            {
                "start": ms,
                "open": float 또는 None,
                "high": float 또는 None,
                "low":  float 또는 None,
                "close": float 또는 None,
                "volume": float
            }

        ※ 1분봉 with-gaps 특성상 OHLC 가 None 인 "빈 캔들"도 포함될 수 있음.
        """

        try:
            symbol = symbol or "US100"
            sym = symbol.upper()
            endpoint = "/v5/market/candles/with-gaps"

            target = count if (isinstance(count, int) and count > 0) else 1000
            all_candles: List[Dict[str, Any]] = []
            end_ms: int | None = None  # 페이징용 end 파라미터 (ms)

            while len(all_candles) < target:
                req_limit = min(1000, target - len(all_candles))

                params: Dict[str, Any] = {
                    "symbol": sym,
                    "interval": "1",  # 기본은 1분봉 (필요하면 파라미터로 빼도 됨)
                    "limit": req_limit,
                }
                if end_ms is not None:
                    params["end"] = int(end_ms)

                # Base 에서 제공하는 공통 요청 사용
                data = self._request("GET", endpoint, params=params)

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
                    # 더 이상 페이징할 데이터 없음
                    break

                # rows 는 [[ms,o,h,l,c,vol], ...] 형태
                # 안전하게 정렬
                rows.sort(key=lambda x: x[0])

                chunk: List[Dict[str, Any]] = []
                for c in rows:
                    try:
                        if not isinstance(c, (list, tuple)) or len(c) < 6:
                            continue
                        ts_ms = _safe_int(c[0])

                        o = c[1]
                        h = c[2]
                        l = c[3]
                        close = c[4]
                        vol = c[5]

                        item = {
                            "start": ts_ms,
                            "open": float(o) if o is not None else None,
                            "high": float(h) if h is not None else None,
                            "low": float(l) if l is not None else None,
                            "close": float(close) if close is not None else None,
                            "volume": float(vol or 0.0),
                        }
                        chunk.append(item)
                    except Exception:
                        continue

                if chunk:
                    # Bybit 버전처럼 "옛날 → 최신" 순서 유지:
                    # 새로 가져온 chunk(과거 구간)를 앞에 붙인다.
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

                # 서버가 limit 보다 적게 주면 마지막 페이지일 가능성 있음
                if len(rows) < req_limit:
                    break

            # count 지정 시, 최신 기준으로 잘라내기
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
