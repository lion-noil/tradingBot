# bots/trade_bot.py
import asyncio
import time
from typing import List
from bots.state.signals import OpenSignalsIndex, record_and_index_signal
from .trade_config import TradeConfig
from strategies.s1_reversion import S1Params
from core.engines import CandleEngine, IndicatorEngine, JumpDetector
from core.redis_client import redis_client
from .market.indicators import IndicatorState, bind_refresher
from .market.jump_reporting import JumpService
from .market.market_sync import MarketSync, MarketSyncConfig
from .state.bot_state import BotState
from .reporting.reporting import (
    build_market_status_log,
    extract_market_status_summary,
    should_log_update_market,
)

from .reporting.status_reporter import StatusReporter, StatusReporterDeps
from .trading.signal_processor import SignalProcessor, SignalProcessorDeps, TradeAction

class TradeBot:
    def __init__(
            self,
            ws_controller,
            rest_controller,
            manual_queue,
            action_sender=None,
            system_logger=None,
            trading_logger=None,
            symbols=("BTCUSDT",),
            config: TradeConfig | None = None,
            publish_config: bool = True,  # False면 config를 Redis에 브로드캐스트 안 함(네임스페이스 공유 시 충돌 방지)
    ):
        self.ws = ws_controller
        self.rest = rest_controller
        self.manual_queue = manual_queue
        self.action_sender = action_sender
        self.system_logger = system_logger
        self.trading_logger = trading_logger
        self.symbols: List[str] = list(symbols)

        # config
        self.config = (TradeConfig().normalized() if config is None else config.normalized())
        self.namespace: str = getattr(self.config, "name", None) or "bybit"
        if publish_config:
            self.config.to_redis(redis_client, publish=True)

        # engines
        self.candle = CandleEngine(candles_num=self.config.candles_num)
        self.indicator = IndicatorEngine(
            min_thr=self.config.indicator_min_thr,
            max_thr=self.config.indicator_max_thr,
            target_cross=self.config.target_cross,
        )
        self.jump = JumpDetector(history_num=10, polling_interval=0.5)

        self._apply_config(self.config)
        # state
        self.state = BotState(
            symbols=self.symbols,
            min_ma_threshold=self.config.min_ma_threshold,
        )
        self.state.init_defaults()

        self.jump_service = JumpService(self.jump, self.symbols, system_logger=self.system_logger)

        # indicator binding
        self.ind_state = IndicatorState(
            ma100s=self.state.ma100s,
            now_ma100_map=self.state.now_ma100,
            ma_threshold_map=self.state.ma_threshold,
            thr_quantized_map=self.state.thr_quantized,
            momentum_threshold_map=self.state.momentum_threshold,
            prev3_candle_map=self.state.prev3_candle,
            ma_check_enabled_map=self.state.ma_check_enabled,
            min_ma_threshold=self.state.min_ma_threshold,
        )

        self._refresh_indicators_fn = bind_refresher(
            self.candle,
            self.indicator,
            self.ind_state,
            system_logger=self.system_logger,
            redis_client=redis_client,
            namespace=self.namespace,
        )

        # market sync
        self.market = MarketSync(
            ws=self.ws,
            rest=self.rest,
            candle_engine=self.candle,
            refresh_indicators=self._refresh_indicators_fn,
            cfg=MarketSyncConfig(
                ws_stale_sec=self.ws_stale_sec,
                ws_global_stale_sec=self.ws_global_stale_sec,
                candles_num=self.config.candles_num,
            ),
            system_logger=self.system_logger,
            on_price=lambda sym, px, ex_ts: self.jump.record_price(sym, px, ex_ts),
            jump_service=self.jump_service,
            get_ma_threshold=lambda s: self.state.ma_threshold.get(s),
        )

        # bootstrap
        self.market.bootstrap(symbols=self.symbols)
        self._last_scaleout_ts_ms: dict[tuple[str, str], int] = {}
        self._last_exit_ts_ms: dict[tuple[str, str], int] = {}  # ✅ S1 쿨다운용(구; exit 기준)
        self._last_entry_ts_ms: dict[tuple[str, str], int] = {}  # ✅ S1 v2 진입기준 쿨다운용
        # 심볼별 피드 stale 상태(장 마감 추정). 전이 시 1회만 로그하기 위한 플래그.
        self._feed_stale: dict[str, bool] = {}
        # WS 링크(전역 heartbeat) 끊김 알림용. 끊김은 전 종목 동시 발생이라 심볼별이 아니라
        # 링크 단위로 디바운스 후 텔레그램 1회만 경보한다. 정상 장 마감(특정 심볼만 조용)은
        # 전역 heartbeat가 살아있어 여기 안 걸린다.
        self._ws_link_down_since: float | None = None  # monotonic, 링크 stale 시작 시각
        self._ws_link_alerted: bool = False            # 현재 끊김 구간에 대해 경보 보냈는지
        self._warmup_last_scaleout_ts()

        self.open_signals_index = OpenSignalsIndex()
        self.open_signals_index.load_from_redis(
            namespace=self.namespace,
            symbols=self.symbols,
        )

        if (getattr(self.config, "strategy", "basic") or "basic").lower() == "s1":
            self._warmup_s1_last_exit()

        # signal processor
        self.signal_processor = SignalProcessor(
            system_logger=self.system_logger,
            deps=SignalProcessorDeps(
                get_now_ma100=lambda s: self.state.now_ma100.get(s),
                get_prev3_candle=lambda s: self.state.prev3_candle.get(s),
                get_ma_threshold=lambda s: (
                    self.state.ma_threshold.get(s)
                    if self.state.ma_check_enabled.get(s, True)
                    else None
                ),
                get_momentum_threshold=lambda s: self.state.momentum_threshold.get(s),

                get_position_max_hold_sec=lambda: self.config.position_max_hold_sec,
                get_near_touch_window_sec=lambda: self.config.near_touch_window_sec,
                get_open_signal_items=lambda sym, side: [
                    it for it in self.open_signals_index.list_open(
                        namespace=self.namespace, symbol=sym, side=side.upper(), newest_first=True)
                    if (len(it) < 4 or (it[3] or "").upper() != "S1")  # S1 포지션은 basic 로직에서 제외
                ],

                get_last_scaleout_ts_ms=lambda sym, side: self._last_scaleout_ts_ms.get(
                    ((sym or "").upper(), (side or "").upper())),
                set_last_scaleout_ts_ms=lambda sym, side, ts_ms: self._last_scaleout_ts_ms.__setitem__(
                    ((sym or "").upper(), (side or "").upper()), int(ts_ms)),

                log_signal=lambda sym, side, kind, price, sig: record_and_index_signal(
                    namespace=self.namespace,
                    open_index=self.open_signals_index,
                    sym=sym,
                    side=side,
                    kind=kind,
                    price=price,
                    payload=sig,
                    engine=self.namespace,
                    system_logger=self.system_logger,
                    trading_logger=self.trading_logger,
                ),

                # ✅ S1 전용 deps (strategy="s1"일 때만 사용; basic은 호출 안 함)
                get_recent_closes=lambda s: [
                    c["close"] for c in self.candle.get_candles(s) if c.get("close") is not None
                ],
                get_open_s1_positions=lambda sym, side: self.open_signals_index.list_open_s1(
                    namespace=self.namespace, symbol=sym, side=(side or "").upper()
                ),
                get_last_exit_ts_ms=lambda sym, side: self._last_exit_ts_ms.get(
                    ((sym or "").upper(), (side or "").upper())),
                set_last_exit_ts_ms=lambda sym, side, ts_ms: self._last_exit_ts_ms.__setitem__(
                    ((sym or "").upper(), (side or "").upper()), int(ts_ms)),
                get_last_entry_ts_ms=lambda sym, side: self._last_entry_ts_ms.get(
                    ((sym or "").upper(), (side or "").upper())),
                set_last_entry_ts_ms=lambda sym, side, ts_ms: self._last_entry_ts_ms.__setitem__(
                    ((sym or "").upper(), (side or "").upper()), int(ts_ms)),
            ),
            strategy=getattr(self.config, "strategy", "basic"),
            basic_long_enabled=bool(getattr(self.config, "basic_long_enabled", True)),
            s1_params=S1Params(
                win=int(getattr(self.config, "s1_win", 10080)),
                k1=float(getattr(self.config, "s1_k1", 2.5)),
                b=float(getattr(self.config, "s1_b", 2.0)),
                cooldown_sec=int(getattr(self.config, "s1_cooldown_sec", 12 * 3600)),
            ),
            # ✅ S1 v2: 심볼별 파라미터/동시보유캡/최대보유
            s1_params_by_symbol={
                str(sym).upper(): S1Params(
                    win=int(getattr(self.config, "s1_win", 10080)),
                    k1=float(d.get("k1", 2.5)), b=float(d.get("b", 2.0)),
                    cooldown_sec=int(d.get("cooldown_sec", 12 * 3600)),
                )
                for sym, d in (getattr(self.config, "s1_params_by_symbol", {}) or {}).items()
            },
            s1_maxc_by_symbol={
                str(sym).upper(): int(d.get("max_concurrent", 1))
                for sym, d in (getattr(self.config, "s1_params_by_symbol", {}) or {}).items()
            },
            s1_max_hold_sec=int(getattr(self.config, "s1_max_hold_sec", 14 * 24 * 3600)),
        )

        # reporter
        self.reporter = StatusReporter(
            system_logger=self.system_logger,
            deps=StatusReporterDeps(
                get_symbols=lambda: self.symbols,
                get_jump_state=lambda: self.jump_service.get_state_map(),
                get_ma_threshold=lambda: self.state.ma_threshold,
                get_now_ma100=lambda: self.state.now_ma100,
                get_price=lambda s, now_ts: self.market.get_price(s, now_ts),
                get_ma_check_enabled=lambda: self.state.ma_check_enabled,
                get_min_ma_threshold=lambda: self.state.min_ma_threshold,
            ),
            build_fn=build_market_status_log,
            extract_fn=extract_market_status_summary,
            should_fn=should_log_update_market,
        )

    def _warmup_last_scaleout_ts(self, *, lookback_sec: int = 30 * 60, count: int = 2000):
        key = f"trading:{self.namespace}:signals"
        now_ms = int(time.time() * 1000)
        min_ms = now_ms - int(lookback_sec) * 1000

        # 최신부터 역순으로 긁기
        # stream id는 "ms-seq" 형태라 대략 ms와 비슷
        min_id = f"{min_ms}-0"
        rows = redis_client.xrevrange(key, max="+", min=min_id, count=count) or []

        def decode_fields(fields: dict) -> dict[str, str]:
            out = {}
            for k, v in (fields or {}).items():
                if isinstance(k, (bytes, bytearray)):
                    k = k.decode("utf-8", "ignore")
                if isinstance(v, (bytes, bytearray)):
                    v = v.decode("utf-8", "ignore")
                out[str(k)] = str(v)
            return out

        for sid, fields in rows:
            is_scaleout = False  # ✅ 매 루프마다 초기화

            f = decode_fields(fields)
            kind = (f.get("kind") or "").upper()
            if kind != "EXIT":
                continue
            rj = f.get("reasons_json") or ""
            if "SCALE_OUT" in rj:
                is_scaleout = True

            sym = (f.get("symbol") or "").upper()
            side = (f.get("side") or "").upper()
            ts_ms = f.get("ts_ms")
            if ts_ms is None:
                continue

            try:
                ts_ms_i = int(ts_ms)
            except Exception:
                continue

            if not is_scaleout:
                continue

            if sym and side:
                k = (sym, side)
                # 최신부터 읽으니까, 처음 발견한 게 최신임
                if k not in self._last_scaleout_ts_ms:
                    self._last_scaleout_ts_ms[k] = ts_ms_i

    def _warmup_s1_last_exit(self, *, count: int = 5000):
        """S1 쿨다운 복원: 쿨다운 기간만큼 거슬러 올라가 (sym,side)별 최신 EXIT ts 적재."""
        key = f"trading:{self.namespace}:signals"
        now_ms = int(time.time() * 1000)
        look_ms = (int(getattr(self.config, "s1_cooldown_sec", 12 * 3600)) + 60) * 1000
        rows = redis_client.xrevrange(key, max="+", min=f"{now_ms - look_ms}-0", count=count) or []
        for _sid, fields in rows:
            f = {}
            for fk, fv in (fields or {}).items():
                if isinstance(fk, (bytes, bytearray)): fk = fk.decode("utf-8", "ignore")
                if isinstance(fv, (bytes, bytearray)): fv = fv.decode("utf-8", "ignore")
                f[str(fk)] = str(fv)
            if (f.get("kind") or "").upper() != "EXIT":
                continue
            if "S1" not in (f.get("reasons_json") or ""):  # 공유 네임스페이스: S1 청산만(basic 제외)
                continue
            sym = (f.get("symbol") or "").upper()
            side = (f.get("side") or "").upper()
            ts = f.get("ts_ms")
            if not (sym and side and ts):
                continue
            try:
                ts_i = int(ts)
            except Exception:
                continue
            ek = (sym, side)
            if ek not in self._last_exit_ts_ms:  # newest-first → 처음이 최신
                self._last_exit_ts_ms[ek] = ts_i

    def _apply_config(self, cfg: TradeConfig) -> None:
        self.ws_stale_sec = cfg.ws_stale_sec
        self.ws_global_stale_sec = cfg.ws_global_stale_sec
        self.entry_percent = cfg.entry_percent
        # 피드 게이트 임계(플래핑 방지). ws_stale_sec보다 길게.
        self.feed_gate_stale_sec = float(getattr(cfg, "feed_gate_stale_sec", 120.0))
        # WS 링크 끊김을 텔레그램 경보로 올리기 전 대기(초). 짧은 깜빡임/재접속은 알림 안 함.
        self.ws_link_alert_after_sec = float(getattr(cfg, "ws_link_alert_after_sec", 180.0))

    def _feed_is_fresh(self, symbol: str) -> bool:
        """이 심볼의 시세 피드가 살아있는지(= 장이 열려있는지) 판정.

        ⚠️ 전역 heartbeat가 아니라 '이 심볼이 직접 갱신된 시각'만 본다.
        ws_is_fresh()는 전역 recv(하트비트)로도 fresh를 주기 때문에, US 지수처럼
        해당 심볼만 마감돼 틱이 끊겨도 다른 심볼/하트비트에 묻혀 'fresh'로 통과한다.
        여기서는 심볼별 recv(monotonic)만 보므로 마감 심볼을 정확히 stale로 잡는다.
        monotonic 기반이라 broker 서버 타임존 오프셋 문제도 없다.
        """
        get_recv = getattr(self.ws, "get_last_recv_time", None)
        if callable(get_recv):
            recv = get_recv(symbol)  # 심볼별 monotonic 수신시각
            if recv is not None:
                return (time.monotonic() - float(recv)) <= self.feed_gate_stale_sec

        # 폴백: 거래소 ts(epoch). recv를 못 쓰는 컨트롤러용.
        get_ex = getattr(self.ws, "get_last_exchange_ts", None)
        ex = get_ex(symbol) if callable(get_ex) else None
        if ex is None:
            # 한 번도 들어온 적 없는 심볼 → 아직 거래 금지(보수적으로 stale 취급)
            return False
        ex = float(ex)
        ex = ex / 1000.0 if ex > 1e12 else ex  # ms→sec 정규화
        return (time.time() - ex) <= self.feed_gate_stale_sec

    def _ws_link_alive(self) -> bool:
        """WS 소켓 자체가 살아있는지(특정 심볼과 무관). 전역 recv(heartbeat 포함)만 본다.

        heartbeat 프레임도 전역 recv를 갱신하므로, 장 마감으로 특정 심볼 틱만 끊겨도
        소켓이 살아있으면 fresh로 나온다. 따라서 '전역이 stale = 소켓/연결 자체가 죽음
        = 진짜 끊김'으로 해석할 수 있다. (per-symbol stale은 장 마감일 수 있어 구분됨)
        판단 근거가 없으면(초기/미지원 컨트롤러) 과경보 방지를 위해 살아있다고 가정한다.
        """
        get_recv = getattr(self.ws, "get_last_recv_time", None)
        if callable(get_recv):
            g = get_recv(None)  # 전역 monotonic 수신시각(heartbeat 포함)
            if g is not None:
                return (time.monotonic() - float(g)) <= self.ws_global_stale_sec

        get_frame = getattr(self.ws, "get_last_frame_time", None)
        if callable(get_frame):
            fr = get_frame()
            if fr is not None:
                return (time.monotonic() - float(fr)) <= self.ws_global_stale_sec

        return True

    def _check_ws_link(self) -> None:
        """WS 링크 끊김을 감지해 디바운스 후 텔레그램 1회 경보(복구 시 1회).

        끊김은 전 종목에 동시 영향 → 심볼별이 아니라 링크 단위로 1회만 알린다.
        WARNING 레벨이라 텔레그램 필터(SIG or WARNING+)를 통과한다.
        """
        now = time.monotonic()
        if self._ws_link_alive():
            if self._ws_link_alerted and self.system_logger:
                self.system_logger.warning("✅ WS 시세 피드 복구 → 신호 처리 재개")
            self._ws_link_down_since = None
            self._ws_link_alerted = False
            return

        # 링크 stale
        if self._ws_link_down_since is None:
            self._ws_link_down_since = now
        down_for = now - self._ws_link_down_since
        if (not self._ws_link_alerted) and down_for >= self.ws_link_alert_after_sec:
            self._ws_link_alerted = True
            if self.system_logger:
                self.system_logger.warning(
                    f"⚠️ WS 시세 피드 끊김 {int(down_for)}s 지속 (전 종목 신호 보류) — 연결 점검 필요"
                )

    async def run_once(self):
        loop = asyncio.get_running_loop()
        # WS 링크 끊김 감지(전역, 1회/사이클). per-symbol 게이트와 별개로 동작.
        self._check_ws_link()
        for symbol in self.symbols:
            try:
                now = time.time()
                # market.tick()은 WS stale 시 동기 REST 백필(블로킹 requests ×N)을 수행한다.
                # 이벤트 루프에서 직접 돌리면 그동안 루프가 얼어 안전브레이커(loop.call_later)까지
                # 못 떠 영구 hang이 됨 → executor 스레드로 빼서 루프가 항상 살아있게 한다.
                # tick은 run_once에서만 순차 await로 호출되므로 동시 tick이 없고(캔들 엔진 유일 접근자),
                # WS 컨트롤러는 자체 락으로 thread-safe → 추가 락 불필요.
                price = await loop.run_in_executor(None, self.market.tick, symbol, now)

                # ✅ 세션/피드 게이트: 이 심볼의 시세 피드가 stale면(장 마감 등)
                #    신호 생성 자체를 건너뛴다. tick()/get_price()는 장 마감 후에도
                #    마지막 캐시 가격을 그대로 반환하므로, 이 게이트가 없으면 죽은
                #    가격으로 ENTRY/EXIT가 발생해 거래소가 10018(Market closed)로 거절한다.
                if not self._feed_is_fresh(symbol):
                    if not self._feed_stale.get(symbol):
                        self._feed_stale[symbol] = True
                        if self.system_logger:
                            self.system_logger.debug(  # 마감/피드정지는 정상 → 텔레그램 안 보냄(파일만)
                                f"[{symbol}] ⏸️ 시세 피드 stale(장 마감 추정) → 신호 처리 보류"
                            )
                    continue
                if self._feed_stale.get(symbol):
                    self._feed_stale[symbol] = False
                    if self.system_logger:
                        self.system_logger.debug(f"[{symbol}] ▶️ 시세 피드 복구 → 신호 처리 재개")  # 텔레그램 안 보냄

                actions: List[TradeAction] = await self.signal_processor.process_symbol(symbol, price)

                for act in actions:
                    if self.action_sender is not None:
                        await self.action_sender.send({
                            "ts_ms": int(time.time() * 1000),
                            "symbol": act.symbol,
                            "action": act.action,
                            "side": (act.side or "").upper() if act.side else None,
                            "price": act.price,
                            "signal_id": act.signal_id,
                            "close_open_signal_id": getattr(act, "close_open_signal_id", None),
                            # ✅ executor 실행 게이트용: 전략명 + signal_only(미검증 전략은 신호만, 실주문 X)
                            "strategy": (getattr(self.config, "strategy", "basic") or "basic"),
                            "signal_only": bool(getattr(self.config, "signal_only", False)),
                        })

            except Exception as e:
                if self.system_logger:
                    self.system_logger.exception(f"[{symbol}] run_once error: {e}")
                continue

        self.reporter.tick(time.time())
