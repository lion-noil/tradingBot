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
    ?쒖꽭/罹붾뱾/?쒖옣 愿??湲곕뒫 (媛寃⑹? URL ?쒕쾭濡?怨꾩냽 諛쏅뒗 踰꾩쟾)
    - update_candles: ONLINE(price) ?쒕쾭 ?ъ슜
    """

    def update_candles(self, candles: list, symbol: str | None = None, count: int | None = None,
                       interval: str = "1"):
        """
        - ?쒕쾭 ?붾뱶?ъ씤?? GET /v5/market/candles/with-gaps
        - ?묐떟:
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
            sym = self._broker_sym(symbol or "US100")
            endpoint = "/v5/market/candles/with-gaps"

            target = count if (isinstance(count, int) and count > 0) else 1000
            all_candles: List[Dict[str, Any]] = []
            end_ms: Optional[int] = None

            seen_starts: set[int] = set()  # 以묐났 ?쒓굅??

            while len(all_candles) < target:
                req_limit = min(1000, target - len(all_candles))

                params: Dict[str, Any] = {
                    "symbol": sym,
                    "interval": interval,
                    "limit": req_limit,
                }
                if end_ms is not None:
                    params["end"] = int(end_ms)

                # ??ONLINE(price) ?쒕쾭濡??몄텧 (Mixin???곕뒗 ?곸쐞 ?대옒?ㅺ? _request ?쒓났?댁빞 ??
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

                # ?덉쟾 ?뺣젹
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
                    # 怨쇨굅 chunk瑜??욎뿉 遺숈뿬 "?쏅궇?믪턀?? ?좎?
                    all_candles = chunk + all_candles
                else:
                    break

                # ?섏씠吏? nextCursor ?ъ슜
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
                        f"?뱤 [MT5] ({sym}) 罹붾뱾 媛깆떊 ?꾨즺: {len(candles)}媛? "
                        f"last OHLC=({last['open']}, {last['high']}, {last['low']}, {last['close']}), "
                        f"vol={last['volume']}"
                    )
                else:
                    self.system_logger.debug(f"?뱤 [MT5] ({sym}) 罹붾뱾 媛깆떊: 寃곌낵 ?놁쓬")

        except Exception as e:
            if getattr(self, "system_logger", None):
                es = str(e)
                # 일시적 서버/터널/네트워크 오류(502/503/530/DNS/연결끊김)는 캔들 버퍼가 직전
                # 데이터를 유지하므로 무해(MA 유효, 현재가는 WS) -> DEBUG(텔레 억제). 그 외만 WARNING.
                transient = any(t in es for t in (
                    "502","503","530","Max retries","resolve","Connection",
                    "timed out","RemoteDisconnected","Bad Gateway","Tunnel"))
                log = self.system_logger.debug if transient else self.system_logger.warning
                log(f"❌ [MT5] ({symbol}) 캔들 요청 실패: {es[:200]}")

