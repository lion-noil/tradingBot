# controllers/mt5/mt5_rest_base.py
import json
import time
from typing import Any, Dict, Optional
import requests


class Mt5RestBase:
    """
    MT5 REST Base (Bybit 스타일 유지)

    - price_base_url: ONLINE (시세/캔들)  ✅ 필수
    - trade_base_url: LOCAL  (주문/계좌 REST)  ✅ 옵션 (지금은 터미널 API로 거래하니 없어도 됨)
    - base_url: 기존 코드 호환용 (기본은 price로 두는 걸 추천)
    """

    def __init__(
            self,
            system_logger=None,
            *,
            trade_base_url: str | None = None,
            price_base_url: str | None = None,
            api_key: str | None = None,
            api_secret: str | None = None,
            symbol_map=None,
    ):
        self.system_logger = system_logger
        self.price_base_url = price_base_url
        self.trade_base_url = trade_base_url
        self.api_key = api_key
        self._symbol_rules: dict[str, dict] = {}
        self.symbol_map = symbol_map  # SymbolAliasMap | None
        # ✅ 가격 API 지속-장애 격상: transient는 DEBUG로 조용하지만, "실패가 시작된 시점"부터
        #   NET_ALERT_AFTER_SEC 이상 실패가 지속되면 ERROR 1회(텔레그램) + 복구 시 INFO 1회.
        #   ⚠️ 기준은 '첫 실패 후 경과'지 '마지막 성공 후 경과'가 아님 — 일봉 봇은 REST를 시간당
        #   1회만 써서 성공 간격이 원래 1시간이라, 성공 기준이면 blip 1번이 "1시간 장애"로 오발됨
        #   (2026-07-10 fxd2 오발 실측: 3598초 경보 → 2초 뒤 복구).
        self.NET_ALERT_AFTER_SEC = 300.0
        self._net_fail_since: float | None = None   # 연속 실패 시작 시각(성공 시 리셋)
        self._net_alerted: bool = False
        # 터널/원진 일시장애로 나오는 HTTP 상태(cloudflare 5xx 계열) — price는 transient 취급
        self.TRANSIENT_HTTP = {502, 503, 504, 520, 521, 522, 524, 530}

    def _note_price_net_fail(self, desc: str) -> None:
        """가격 API 일시 실패 공통 처리: 평소 DEBUG, 실패 지속(첫 실패 후 임계 초과) 시 ERROR 1회 격상."""
        if not self.system_logger:
            return
        now = time.time()
        if self._net_fail_since is None:
            self._net_fail_since = now
        down_sec = now - self._net_fail_since
        if down_sec >= self.NET_ALERT_AFTER_SEC and not self._net_alerted:
            self._net_alerted = True
            self.system_logger.error(
                f"🚨 [MT5 REST] 가격 API 실패 {int(down_sec)}초째 지속 — "
                f"서버/터널 점검 필요 (마지막 오류: {desc[:120]})")
        else:
            self.system_logger.debug(f"[MT5 REST] {desc[:200]}")

    def _broker_sym(self, symbol: str) -> str:
        """Canonical → broker symbol. No-op if no mapping set."""
        s = (symbol or "").upper().strip()
        if s and self.symbol_map:
            return self.symbol_map.to_broker(s)
        return s

    # -------------------------
    # URL / 헤더 빌더
    # -------------------------
    def _build_url(self, endpoint: str, *, use: str = "price") -> str:
        """
        use: "price" | "trade"
        """
        if use not in ("trade", "price"):
            raise ValueError("use must be 'trade' or 'price'")

        if use == "price":
            base = self.price_base_url
        else:
            if not self.trade_base_url:
                raise RuntimeError("MT5_TRADE_REST_URL is not configured (trade REST disabled)")
            base = self.trade_base_url

        return base.rstrip("/") + endpoint

    def _get_headers(self, *, use: str = "price") -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    # -------------------------
    # 공통 요청
    # -------------------------
    def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        body_dict: Optional[Dict[str, Any]] = None,
        timeout: float = 5.0,
        *,
        use: str = "price",
    ) -> Dict[str, Any]:
        """
        use="price" : 시세/캔들(ONLINE)
        use="trade" : 주문/계좌(LOCAL REST)  (지금은 거의 안 쓸 예정)
        """
        url = self._build_url(endpoint, use=use)

        try:
            if method.upper() == "GET":
                resp = requests.get(
                    url,
                    headers=self._get_headers(use=use),
                    params=params,
                    timeout=timeout,
                )
            else:
                body = json.dumps(body_dict or {}, separators=(",", ":"))
                resp = requests.post(
                    url,
                    headers=self._get_headers(use=use),
                    params=params,
                    data=body,
                    timeout=timeout,
                )
        except requests.RequestException as e:
            if self.system_logger:
                # 일시적 네트워크 오류(타임아웃/502/503/530/DNS/연결끊김)의 시세 조회(use=price)는
                # 다음 tick에 자동 재시도되므로 무해 → DEBUG(텔레 억제). 재시작 직후 캔들 풀백필이
                # 서버를 점유해 가격조회가 잠깐 밀리는 콜드스타트 버스트가 대표 사례.
                # 주문/계좌(use=trade)는 실주문 관련이라 transient여도 ERROR 유지.
                es = str(e)
                transient = any(t in es for t in (
                    "502", "503", "530", "Max retries", "resolve", "Connection",
                    "timed out", "RemoteDisconnected", "Bad Gateway", "Tunnel"))
                if transient and use == "price":
                    # ✅ 평소 DEBUG, 지속 장애(임계 초과)면 1회 격상 — _note_price_net_fail
                    self._note_price_net_fail(f"네트워크 예외(use={use}): {es}")
                else:
                    self.system_logger.error(f"[MT5 REST] 네트워크 예외(use={use}): {e}")
            raise

        if resp.status_code != 200:
            if self.system_logger:
                # ✅ 터널/원진 일시장애 상태코드(502/503/530 등)의 price 조회는 transient 취급:
                #   평소 DEBUG + 지속 장애면 1회 격상. 그 외(4xx, trade 등)는 기존 WARNING.
                if use == "price" and resp.status_code in self.TRANSIENT_HTTP:
                    self._note_price_net_fail(f"HTTP {resp.status_code} use={use}")
                else:
                    self.system_logger.warning(
                        f"[MT5 REST] HTTP {resp.status_code} use={use} {resp.text[:200]}"
                    )
            resp.raise_for_status()

        # ✅ 가격 API 정상 응답 → 연속실패 타이머 리셋(+격상 경보 났었으면 복구 알림 1회)
        if use == "price":
            self._net_fail_since = None
            if self._net_alerted:
                self._net_alerted = False
                if self.system_logger:
                    self.system_logger.info("✅ [MT5 REST] 가격 API 복구됨")

        try:
            return resp.json()
        except Exception:
            if self.system_logger:
                self.system_logger.error(
                    f"[MT5 REST] JSON 파싱 실패(use={use}): {resp.text[:200]}"
                )
            raise