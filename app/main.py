# app/main.py
# 통합 신호봇 진입점. 엔진(bybit/s1/mt5)을 STRATEGY env(또는 --strategy)로 선택.
# 기존 main_only_bybit.py / main_s1.py / main_only_mt.py 의 동작을 1:1 보존하고,
# 차이나는 부분만 ENGINES 레지스트리로 분리 → 새 전략은 레지스트리에 한 항목 추가로 끝.

import sys
import signal, os, asyncio, logging, threading, time
from collections import deque

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI
from asyncio import Queue

from bots.trade_bot import TradeBot
from bots.trade_config import (make_bybit_config, make_s1_config, make_mt5_signal_config,
                               make_s1_mt5_config, make_s2_config, make_s2_mt5_config,
                               make_fx_daily_trend_config, make_fx_daily_rev_config,
                               make_crypto_daily_trend_config, make_crypto_daily_rev_config,
                               make_mt5_daily_trend_config, make_mt5_daily_rev_config,
                               make_s11_trend_config, make_s11_rev_config, make_s11_fade_config,
                               make_s11_mt5_trend_config, make_s11_mt5_fade_config,
                               make_s11_mt5_rev_config,
                               make_s22_trend_config, make_s22_rev_config,
                               make_s22_ewz_config, make_s22_fade_config)
from utils.logger import setup_logger
from utils.local_action_sender import LocalActionSender, Target


def _env(key: str, default: str = "") -> str:
    return (os.getenv(key) or default).strip()


# ─────────────────────────────────────────────────────────────────────────────
# 하트비트: 메인 루프가 살아 도는 동안만 갱신. 행(hang)되면 갱신 멈춤 → healthcheck가
# stale 감지 후 컨테이너 재기동(docker-compose healthcheck + restart: always).
# ─────────────────────────────────────────────────────────────────────────────
_HB_PATH = _env("HEARTBEAT_FILE", "/tmp/hb")


def _heartbeat() -> None:
    try:
        with open(_HB_PATH, "w") as f:
            f.write(str(int(time.time())))
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# 컨트롤러 팩토리 (엔진별 시세/주문 컨트롤러 + URL/키 env 차이를 흡수)
# ─────────────────────────────────────────────────────────────────────────────
def _build_bybit_controllers(symbols, system_logger):
    from controllers.bybit.bybit_ws_controller import BybitWebSocketController
    from controllers.bybit.bybit_rest_controller import BybitRestController
    ws = BybitWebSocketController(
        symbols=symbols, system_logger=system_logger,
        price_ws_url=_env("BYBIT_PRICE_WS_URL", ""),
    )
    rest = BybitRestController(
        system_logger=system_logger,
        trade_base_url=_env("BYBIT_TRADE_REST_URL", ""),
        price_base_url=_env("BYBIT_PRICE_REST_URL", ""),
    )
    return ws, rest


def _build_mt5_controllers(symbols, system_logger):
    from controllers.mt5.mt5_ws_controller import Mt5WebSocketController
    from controllers.mt5.mt5_rest_controller import Mt5RestController
    from utils.symbol_mapper import SymbolAliasMap
    api_key = _env("MT5_API_KEY", "")
    symbol_map = SymbolAliasMap.from_env()
    ws = Mt5WebSocketController(
        symbols=symbols, system_logger=system_logger,
        price_ws_url=_env("MT5_PRICE_WS_URL", ""), api_key=api_key, symbol_map=symbol_map,
    )
    rest = Mt5RestController(
        system_logger=system_logger,
        price_base_url=_env("MT5_PRICE_REST_URL", ""), api_key=api_key, symbol_map=symbol_map,
    )
    return ws, rest


def _build_mt5_controllers_daily(symbols, system_logger):
    """일봉(D1) 채널용 MT5 컨트롤러. WS는 kline.D 구독(ticker 가격은 동일), REST는 호출측에서 interval=D."""
    from controllers.mt5.mt5_ws_controller import Mt5WebSocketController
    from controllers.mt5.mt5_rest_controller import Mt5RestController
    from utils.symbol_mapper import SymbolAliasMap
    api_key = _env("MT5_API_KEY", "")
    symbol_map = SymbolAliasMap.from_env()
    ws = Mt5WebSocketController(
        symbols=symbols, system_logger=system_logger,
        price_ws_url=_env("MT5_PRICE_WS_URL", ""), api_key=api_key, symbol_map=symbol_map,
        kline_interval="D",
    )
    rest = Mt5RestController(
        system_logger=system_logger,
        price_base_url=_env("MT5_PRICE_REST_URL", ""), api_key=api_key, symbol_map=symbol_map,
    )
    return ws, rest


def _build_bybit_controllers_daily(symbols, system_logger):
    """일봉(D1) 크립토(Bybit) 채널용 컨트롤러. 라이브가격=ticker(WS), 캔들=일봉 REST 백필(interval=D).
    WS kline은 D 구독(freshness용, 일봉 tick은 REST 백필만 사용). update_candles가 interval=D 처리."""
    from controllers.bybit.bybit_ws_controller import BybitWebSocketController
    from controllers.bybit.bybit_rest_controller import BybitRestController
    ws = BybitWebSocketController(
        symbols=symbols, system_logger=system_logger,
        price_ws_url=_env("BYBIT_PRICE_WS_URL", ""),
    )
    ws.kline_interval = "D"  # 일봉 kline 구독(1분 채널과 격리)
    rest = BybitRestController(
        system_logger=system_logger,
        trade_base_url=_env("BYBIT_TRADE_REST_URL", ""),
        price_base_url=_env("BYBIT_PRICE_REST_URL", ""),
    )
    return ws, rest


# ─────────────────────────────────────────────────────────────────────────────
# 엔진 레지스트리 — 기존 3개 main의 차이를 그대로 옮긴 스펙.
#   새 전략 추가 = 여기 항목 하나 추가(+ 필요시 config 메이커/컨트롤러 팩토리).
# ─────────────────────────────────────────────────────────────────────────────
ENGINES = {
    "bybit": {
        "name": "BYBIT",
        "make_config": lambda: make_bybit_config(),
        "make_controllers": _build_bybit_controllers,
        "targets_env": "BYBIT_EXECUTOR_TARGETS",
        "targets_fallback_env": None,
        "targets_default": "127.0.0.1:9009,127.0.0.1:9007",
        "signals_file": "signals.jsonl",
        "tg_token_env": "Noil1_TELEGRAM_BOT_TOKEN",
        "tg_token_fallback_env": "Noil1_TELEGRAM_CHAT_ID",
        "publish_config": True,
        "port": 18001,
        "warmup_timeout": None,
        "burst": dict(threshold=5, window_sec=10.0, grace_sec=3, level=logging.WARNING, flush=True),
    },
    "s1": {
        "name": "S1",
        "make_config": lambda: make_s1_config(signal_only=False, entries_disabled=True),  # 🟡 드레인(S11 대체, ≤14d 자연소진)
        "make_controllers": _build_bybit_controllers,
        "targets_env": "S1_EXECUTOR_TARGETS",
        "targets_fallback_env": "BYBIT_EXECUTOR_TARGETS",
        "targets_default": "127.0.0.1:9009",
        "signals_file": "signals_s1.jsonl",
        "tg_token_env": "Noil1_TELEGRAM_BOT_TOKEN",
        "tg_token_fallback_env": "Noil1_TELEGRAM_CHAT_ID",
        "publish_config": False,  # 'bybit' 네임스페이스 공유 → config는 signal-bybit이 소유
        "port": 18003,
        "warmup_timeout": None,
        "burst": dict(threshold=5, window_sec=10.0, grace_sec=3, level=logging.WARNING, flush=True),
    },
    "mt5": {
        "name": "MT5",
        "make_config": lambda: make_mt5_signal_config(),
        "make_controllers": _build_mt5_controllers,
        "targets_env": "MT5_EXECUTOR_TARGETS",
        "targets_fallback_env": None,
        "targets_default": "127.0.0.1:9010",
        "signals_file": "signals.jsonl",
        "tg_token_env": "Noil2_TELEGRAM_BOT_TOKEN",
        "tg_token_fallback_env": "Noil2_TELEGRAM_CHAT_ID",
        "publish_config": True,
        "port": 18013,
        "warmup_timeout": 120.0,  # 2분 내 틱 없는 심볼은 폐장으로 보고 스킵
        "burst": dict(threshold=10, window_sec=10.0, grace_sec=0.2, level=logging.ERROR, flush=False),
    },
    "s1mt5": {
        "name": "S1-MT5",
        "make_config": lambda: make_s1_mt5_config(signal_only=False, entries_disabled=True),  # 🟡 드레인(S11 대체)
        "make_controllers": _build_mt5_controllers,
        "targets_env": "S1MT5_EXECUTOR_TARGETS",
        "targets_fallback_env": "MT5_EXECUTOR_TARGETS",
        "targets_default": "127.0.0.1:9010",
        "signals_file": "signals_s1_mt5.jsonl",
        "tg_token_env": "Noil2_TELEGRAM_BOT_TOKEN",
        "tg_token_fallback_env": "Noil2_TELEGRAM_CHAT_ID",
        "publish_config": False,  # 'mt5' 네임스페이스 공유 → config는 signal-mt5가 소유
        "port": 18014,
        "warmup_timeout": 120.0,
        "burst": dict(threshold=10, window_sec=10.0, grace_sec=0.2, level=logging.ERROR, flush=False),
    },
    "s2": {
        "name": "S2",
        "make_config": lambda: make_s2_config(signal_only=False, entries_disabled=True),  # 🟡 드레인(S11 대체)
        "make_controllers": _build_bybit_controllers,
        "targets_env": "S2_EXECUTOR_TARGETS",
        "targets_fallback_env": "BYBIT_EXECUTOR_TARGETS",
        "targets_default": "127.0.0.1:9009",
        "signals_file": "signals_s2.jsonl",
        "tg_token_env": "Noil1_TELEGRAM_BOT_TOKEN",
        "tg_token_fallback_env": "Noil1_TELEGRAM_CHAT_ID",
        "publish_config": False,  # 'bybit' 네임스페이스 공유
        "port": 18005,
        "warmup_timeout": None,
        "burst": dict(threshold=5, window_sec=10.0, grace_sec=3, level=logging.WARNING, flush=True),
    },
    "s2mt5": {
        "name": "S2-MT5",
        "make_config": lambda: make_s2_mt5_config(signal_only=False, entries_disabled=True),  # 🟡 드레인(S11 대체)
        "make_controllers": _build_mt5_controllers,
        "targets_env": "S2MT5_EXECUTOR_TARGETS",
        "targets_fallback_env": "MT5_EXECUTOR_TARGETS",
        "targets_default": "127.0.0.1:9010",
        "signals_file": "signals_s2_mt5.jsonl",
        "tg_token_env": "Noil2_TELEGRAM_BOT_TOKEN",
        "tg_token_fallback_env": "Noil2_TELEGRAM_CHAT_ID",
        "publish_config": False,
        "port": 18015,
        "warmup_timeout": 120.0,
        "burst": dict(threshold=10, window_sec=10.0, grace_sec=0.2, level=logging.ERROR, flush=False),
    },
    # ── 일봉(D1) FX 채널 (HANDOFF_DAILY_FX) — namespace "fxd", executor-a2 공유, win=90/최대보유30일 ──
    "fxd1": {
        "name": "FXD1",
        "make_config": lambda: make_fx_daily_trend_config(signal_only=False),  # 🔴 LIVE (일봉바게이트+쿨다운persist)
        "make_controllers": _build_mt5_controllers_daily,
        "targets_env": "FXD_EXECUTOR_TARGETS",
        "targets_fallback_env": "MT5_EXECUTOR_TARGETS",
        "targets_default": "127.0.0.1:9010",
        "signals_file": "signals_fxd_trend.jsonl",
        "tg_token_env": "Noil2_TELEGRAM_BOT_TOKEN",
        "tg_token_fallback_env": "Noil2_TELEGRAM_CHAT_ID",
        "publish_config": True,  # 'fxd' 네임스페이스 config 소유
        "port": 18016,
        "warmup_timeout": 120.0,
        "burst": dict(threshold=10, window_sec=10.0, grace_sec=0.2, level=logging.ERROR, flush=False),
    },
    "fxd2": {
        "name": "FXD2",
        "make_config": lambda: make_fx_daily_rev_config(signal_only=False),  # 🔴 LIVE (일봉바게이트+쿨다운persist)
        "make_controllers": _build_mt5_controllers_daily,
        "targets_env": "FXD_EXECUTOR_TARGETS",
        "targets_fallback_env": "MT5_EXECUTOR_TARGETS",
        "targets_default": "127.0.0.1:9010",
        "signals_file": "signals_fxd_rev.jsonl",
        "tg_token_env": "Noil2_TELEGRAM_BOT_TOKEN",
        "tg_token_fallback_env": "Noil2_TELEGRAM_CHAT_ID",
        "publish_config": False,  # 'fxd' 네임스페이스 공유(config는 fxd1이 소유)
        "port": 18017,
        "warmup_timeout": 120.0,
        "burst": dict(threshold=10, window_sec=10.0, grace_sec=0.2, level=logging.ERROR, flush=False),
    },
    # ── 일봉(D1) 크립토(Bybit) 채널 (HANDOFF_DAILY_MT5 §3b) — namespace "cryptod"(별도, 1분과 격리 필수),
    #   executor-a1(9009). 🔴 LIVE. 거래는 Bybit 거래소, 전략 태그 s3/s4로 구분.
    "cryptod1": {
        "name": "CRYPTOD1",
        "make_config": lambda: make_crypto_daily_trend_config(signal_only=False),  # 🔴 LIVE
        "make_controllers": _build_bybit_controllers_daily,
        "targets_env": "CRYPTOD_EXECUTOR_TARGETS",
        "targets_fallback_env": "BYBIT_EXECUTOR_TARGETS",
        "targets_default": "127.0.0.1:9009",
        "signals_file": "signals_cryptod_trend.jsonl",
        "tg_token_env": "Noil1_TELEGRAM_BOT_TOKEN",
        "tg_token_fallback_env": "Noil1_TELEGRAM_CHAT_ID",
        "publish_config": False,  # 'bybit' 네임스페이스 공유 → config는 signal-bybit이 소유
        "port": 18018,
        "warmup_timeout": None,
        "burst": dict(threshold=5, window_sec=10.0, grace_sec=3, level=logging.WARNING, flush=True),
    },
    "cryptod2": {
        "name": "CRYPTOD2",
        "make_config": lambda: make_crypto_daily_rev_config(signal_only=False),  # 🔴 LIVE
        "make_controllers": _build_bybit_controllers_daily,
        "targets_env": "CRYPTOD_EXECUTOR_TARGETS",
        "targets_fallback_env": "BYBIT_EXECUTOR_TARGETS",
        "targets_default": "127.0.0.1:9009",
        "signals_file": "signals_cryptod_rev.jsonl",
        "tg_token_env": "Noil1_TELEGRAM_BOT_TOKEN",
        "tg_token_fallback_env": "Noil1_TELEGRAM_CHAT_ID",
        "publish_config": False,  # 'bybit' 네임스페이스 공유 → config는 signal-bybit이 소유
        "port": 18019,
        "warmup_timeout": None,
        "burst": dict(threshold=5, window_sec=10.0, grace_sec=3, level=logging.WARNING, flush=True),
    },
    # ── 일봉(D1) MT5 비환율 채널 (HANDOFF_DAILY_MT5 §3) — namespace "mt5d"(별도, 1분과 격리 필수),
    #   executor-a2(9010). 🔴 LIVE. 거래는 MT5 거래소, 전략 태그 s3/s4로 구분. 사이징 2% 엄수.
    "mt5d1": {
        "name": "MT5D1",
        "make_config": lambda: make_mt5_daily_trend_config(signal_only=False),  # 🔴 LIVE
        "make_controllers": _build_mt5_controllers_daily,
        "targets_env": "MT5D_EXECUTOR_TARGETS",
        "targets_fallback_env": "MT5_EXECUTOR_TARGETS",
        "targets_default": "127.0.0.1:9010",
        "signals_file": "signals_mt5d_trend.jsonl",
        "tg_token_env": "Noil2_TELEGRAM_BOT_TOKEN",
        "tg_token_fallback_env": "Noil2_TELEGRAM_CHAT_ID",
        "publish_config": False,  # 'mt5' 네임스페이스 공유 → config는 signal-mt5가 소유
        "port": 18020,
        "warmup_timeout": 120.0,
        "burst": dict(threshold=10, window_sec=10.0, grace_sec=0.2, level=logging.ERROR, flush=False),
    },
    "mt5d2": {
        "name": "MT5D2",
        "make_config": lambda: make_mt5_daily_rev_config(signal_only=False),  # 🔴 LIVE
        "make_controllers": _build_mt5_controllers_daily,
        "targets_env": "MT5D_EXECUTOR_TARGETS",
        "targets_fallback_env": "MT5_EXECUTOR_TARGETS",
        "targets_default": "127.0.0.1:9010",
        "signals_file": "signals_mt5d_rev.jsonl",
        "tg_token_env": "Noil2_TELEGRAM_BOT_TOKEN",
        "tg_token_fallback_env": "Noil2_TELEGRAM_CHAT_ID",
        "publish_config": False,  # 'mt5' 네임스페이스 공유 → config는 signal-mt5가 소유
        "port": 18021,
        "warmup_timeout": 120.0,
        "burst": dict(threshold=10, window_sec=10.0, grace_sec=0.2, level=logging.ERROR, flush=False),
    },
    # ── S11 「1분봉책」 (HANDOFF_MASTER v4 §2-A′) — 구 S1/S2 대체. namespace "s11"(Bybit)/"s11m"(MT5) ──
    "s11t": {
        "name": "S11-TREND",
        "make_config": lambda: make_s11_trend_config(signal_only=False),  # 🔴 LIVE
        "make_controllers": _build_bybit_controllers,
        "targets_env": "S11_EXECUTOR_TARGETS",
        "targets_fallback_env": "BYBIT_EXECUTOR_TARGETS",
        "targets_default": "127.0.0.1:9009",
        "signals_file": "signals_s11_trend.jsonl",
        "tg_token_env": "Noil1_TELEGRAM_BOT_TOKEN",
        "tg_token_fallback_env": "Noil1_TELEGRAM_CHAT_ID",
        "publish_config": True,  # 's11' 네임스페이스 config 소유
        "port": 18022,
        "warmup_timeout": None,
        "burst": dict(threshold=5, window_sec=10.0, grace_sec=3, level=logging.WARNING, flush=True),
    },
    "s11r": {
        "name": "S11-REV",
        "make_config": lambda: make_s11_rev_config(signal_only=False),  # 🔴 LIVE
        "make_controllers": _build_bybit_controllers,
        "targets_env": "S11_EXECUTOR_TARGETS",
        "targets_fallback_env": "BYBIT_EXECUTOR_TARGETS",
        "targets_default": "127.0.0.1:9009",
        "signals_file": "signals_s11_rev.jsonl",
        "tg_token_env": "Noil1_TELEGRAM_BOT_TOKEN",
        "tg_token_fallback_env": "Noil1_TELEGRAM_CHAT_ID",
        "publish_config": False,  # 's11' 공유(config는 s11t 소유)
        "port": 18023,
        "warmup_timeout": None,
        "burst": dict(threshold=5, window_sec=10.0, grace_sec=3, level=logging.WARNING, flush=True),
    },
    "s11f": {
        "name": "S11-FADE",
        "make_config": lambda: make_s11_fade_config(signal_only=False),  # 🔴 LIVE
        "make_controllers": _build_bybit_controllers,
        "targets_env": "S11_EXECUTOR_TARGETS",
        "targets_fallback_env": "BYBIT_EXECUTOR_TARGETS",
        "targets_default": "127.0.0.1:9009",
        "signals_file": "signals_s11_fade.jsonl",
        "tg_token_env": "Noil1_TELEGRAM_BOT_TOKEN",
        "tg_token_fallback_env": "Noil1_TELEGRAM_CHAT_ID",
        "publish_config": False,  # 's11' 공유
        "port": 18024,
        "warmup_timeout": None,
        "burst": dict(threshold=5, window_sec=10.0, grace_sec=3, level=logging.WARNING, flush=True),
    },
    "s11mt": {
        "name": "S11M-TREND",
        "make_config": lambda: make_s11_mt5_trend_config(signal_only=False),  # 🔴 LIVE (보수 1% 사이징)
        "make_controllers": _build_mt5_controllers,
        "targets_env": "S11M_EXECUTOR_TARGETS",
        "targets_fallback_env": "MT5_EXECUTOR_TARGETS",
        "targets_default": "127.0.0.1:9010",
        "signals_file": "signals_s11m_trend.jsonl",
        "tg_token_env": "Noil2_TELEGRAM_BOT_TOKEN",
        "tg_token_fallback_env": "Noil2_TELEGRAM_CHAT_ID",
        "publish_config": True,  # 's11m' 네임스페이스 config 소유
        "port": 18025,
        "warmup_timeout": 120.0,
        "burst": dict(threshold=10, window_sec=10.0, grace_sec=0.2, level=logging.ERROR, flush=False),
    },
    "s11mf": {
        "name": "S11M-FADE",
        "make_config": lambda: make_s11_mt5_fade_config(signal_only=False),  # 🔴 LIVE (보수 1% 사이징)
        "make_controllers": _build_mt5_controllers,
        "targets_env": "S11M_EXECUTOR_TARGETS",
        "targets_fallback_env": "MT5_EXECUTOR_TARGETS",
        "targets_default": "127.0.0.1:9010",
        "signals_file": "signals_s11m_fade.jsonl",
        "tg_token_env": "Noil2_TELEGRAM_BOT_TOKEN",
        "tg_token_fallback_env": "Noil2_TELEGRAM_CHAT_ID",
        "publish_config": False,  # 's11m' 공유(config는 s11mt 소유)
        "port": 18026,
        "warmup_timeout": 120.0,
        "burst": dict(threshold=10, window_sec=10.0, grace_sec=0.2, level=logging.ERROR, flush=False),
    },
    # ── S22 「4시간봉책」 (HANDOFF_S22 2026-07-13) — Bybit 8셀, 진입 5%. namespace "s22" ──
    "s22t": {
        "name": "S22-TREND",
        "make_config": lambda: make_s22_trend_config(signal_only=False),  # 🔴 LIVE (BTC·XRP z추세롱)
        "make_controllers": _build_bybit_controllers,
        "targets_env": "S22_EXECUTOR_TARGETS",
        "targets_fallback_env": "BYBIT_EXECUTOR_TARGETS",
        "targets_default": "127.0.0.1:9009",
        "signals_file": "signals_s22_trend.jsonl",
        "tg_token_env": "Noil1_TELEGRAM_BOT_TOKEN",
        "tg_token_fallback_env": "Noil1_TELEGRAM_CHAT_ID",
        "publish_config": True,  # 's22' 네임스페이스 config 소유
        "port": 18027,
        "warmup_timeout": None,
        "burst": dict(threshold=5, window_sec=10.0, grace_sec=3, level=logging.WARNING, flush=True),
    },
    "s22r": {
        "name": "S22-REV",
        "make_config": lambda: make_s22_rev_config(signal_only=False),  # 🔴 LIVE (SOL·XRP z역추롱)
        "make_controllers": _build_bybit_controllers,
        "targets_env": "S22_EXECUTOR_TARGETS",
        "targets_fallback_env": "BYBIT_EXECUTOR_TARGETS",
        "targets_default": "127.0.0.1:9009",
        "signals_file": "signals_s22_rev.jsonl",
        "tg_token_env": "Noil1_TELEGRAM_BOT_TOKEN",
        "tg_token_fallback_env": "Noil1_TELEGRAM_CHAT_ID",
        "publish_config": False,  # 's22' 공유(config는 s22t 소유)
        "port": 18028,
        "warmup_timeout": None,
        "burst": dict(threshold=5, window_sec=10.0, grace_sec=3, level=logging.WARNING, flush=True),
    },
    "s22e": {
        "name": "S22-EWZ",
        "make_config": lambda: make_s22_ewz_config(signal_only=False),  # 🔴 LIVE (ETH롱·숏, SOL롱 — 신규 s14)
        "make_controllers": _build_bybit_controllers,
        "targets_env": "S22_EXECUTOR_TARGETS",
        "targets_fallback_env": "BYBIT_EXECUTOR_TARGETS",
        "targets_default": "127.0.0.1:9009",
        "signals_file": "signals_s22_ewz.jsonl",
        "tg_token_env": "Noil1_TELEGRAM_BOT_TOKEN",
        "tg_token_fallback_env": "Noil1_TELEGRAM_CHAT_ID",
        "publish_config": False,  # 's22' 공유
        "port": 18029,
        "warmup_timeout": None,
        "burst": dict(threshold=5, window_sec=10.0, grace_sec=3, level=logging.WARNING, flush=True),
    },
    "s22f": {
        "name": "S22-FADE",
        "make_config": lambda: make_s22_fade_config(signal_only=False),  # 🔴 LIVE (XRP 48h -15% 페이드)
        "make_controllers": _build_bybit_controllers,
        "targets_env": "S22_EXECUTOR_TARGETS",
        "targets_fallback_env": "BYBIT_EXECUTOR_TARGETS",
        "targets_default": "127.0.0.1:9009",
        "signals_file": "signals_s22_fade.jsonl",
        "tg_token_env": "Noil1_TELEGRAM_BOT_TOKEN",
        "tg_token_fallback_env": "Noil1_TELEGRAM_CHAT_ID",
        "publish_config": False,  # 's22' 공유
        "port": 18030,
        "warmup_timeout": None,
        "burst": dict(threshold=5, window_sec=10.0, grace_sec=3, level=logging.WARNING, flush=True),
    },
    # ── 책 통합 엔진 (2026-07-14) — 셀 패밀리별 컨테이너를 책당 1프로세스로 통합. ──
    #   WS 1개 공유(중복 구독 제거) + 쿨다운 태그별 분리 + 텔레그램 '전체 N'=책 유니버스.
    #   구 채널(s11t/r/f, s22t/r/e/f, s11mt/mf)은 compose에서 retired 프로파일로 봉인(롤백용).
    "s11": {
        "name": "S11-BOOK",
        "make_configs": lambda: [make_s11_trend_config(signal_only=False),   # 첫 항목=primary(config publish 소유)
                                 make_s11_rev_config(signal_only=False),
                                 make_s11_fade_config(signal_only=False)],   # 🔴 LIVE (1분봉책 11셀)
        "make_controllers": _build_bybit_controllers,
        "targets_env": "S11_EXECUTOR_TARGETS",
        "targets_fallback_env": "BYBIT_EXECUTOR_TARGETS",
        "targets_default": "127.0.0.1:9009",
        "signals_file": "signals_s11_book.jsonl",
        "tg_token_env": "Noil1_TELEGRAM_BOT_TOKEN",
        "tg_token_fallback_env": "Noil1_TELEGRAM_CHAT_ID",
        "publish_config": True,  # 's11' 네임스페이스 config 소유(구 s11t 역할 승계)
        "port": 18031,
        "warmup_timeout": None,
        "burst": dict(threshold=5, window_sec=10.0, grace_sec=3, level=logging.WARNING, flush=True),
    },
    "s22": {
        "name": "S22-BOOK",
        "make_configs": lambda: [make_s22_trend_config(signal_only=False),   # 첫 항목=primary
                                 make_s22_rev_config(signal_only=False),
                                 make_s22_ewz_config(signal_only=False),
                                 make_s22_fade_config(signal_only=False)],   # 🔴 LIVE (4시간봉책 8셀)
        "make_controllers": _build_bybit_controllers,
        "targets_env": "S22_EXECUTOR_TARGETS",
        "targets_fallback_env": "BYBIT_EXECUTOR_TARGETS",
        "targets_default": "127.0.0.1:9009",
        "signals_file": "signals_s22_book.jsonl",
        "tg_token_env": "Noil1_TELEGRAM_BOT_TOKEN",
        "tg_token_fallback_env": "Noil1_TELEGRAM_CHAT_ID",
        "publish_config": True,  # 's22' 네임스페이스 config 소유(구 s22t 역할 승계)
        "port": 18032,
        "warmup_timeout": None,
        "burst": dict(threshold=5, window_sec=10.0, grace_sec=3, level=logging.WARNING, flush=True),
    },
    # ── S33 「일봉책」 (2026-07-15) — S3(추세)+S4(역추세) 통합, 유니버스당 1프로세스. ──
    #   태그(s3/s4)·네임스페이스(bybit/mt5/fxd)는 유지 → 열린 포지션·쿨다운·사이징 무손실 승계.
    #   구 cryptod1/2·mt5d1/2·fxd1/2는 compose retired 프로파일로 봉인.
    "s33": {
        "name": "S33-BOOK",
        "make_configs": lambda: [make_crypto_daily_trend_config(signal_only=False),  # 첫 항목=primary
                                 make_crypto_daily_rev_config(signal_only=False)],   # 🔴 LIVE (U1 일봉)
        "make_controllers": _build_bybit_controllers_daily,
        "targets_env": "CRYPTOD_EXECUTOR_TARGETS",
        "targets_fallback_env": "BYBIT_EXECUTOR_TARGETS",
        "targets_default": "127.0.0.1:9009",
        "signals_file": "signals_s33_book.jsonl",
        "tg_token_env": "Noil1_TELEGRAM_BOT_TOKEN",
        "tg_token_fallback_env": "Noil1_TELEGRAM_CHAT_ID",
        "publish_config": False,  # 'bybit' 네임스페이스 공유 → config는 signal-bybit이 소유
        "port": 18034,
        "warmup_timeout": None,
        "burst": dict(threshold=5, window_sec=10.0, grace_sec=3, level=logging.WARNING, flush=True),
    },
    "s33m": {
        "name": "S33M-BOOK",
        "make_configs": lambda: [make_mt5_daily_trend_config(signal_only=False),
                                 make_mt5_daily_rev_config(signal_only=False)],   # 🔴 LIVE (U2 일봉)
        "make_controllers": _build_mt5_controllers_daily,
        "targets_env": "MT5D_EXECUTOR_TARGETS",
        "targets_fallback_env": "MT5_EXECUTOR_TARGETS",
        "targets_default": "127.0.0.1:9010",
        "signals_file": "signals_s33m_book.jsonl",
        "tg_token_env": "Noil2_TELEGRAM_BOT_TOKEN",
        "tg_token_fallback_env": "Noil2_TELEGRAM_CHAT_ID",
        "publish_config": False,  # 'mt5' 네임스페이스 공유 → config는 signal-mt5가 소유
        "port": 18035,
        "warmup_timeout": 120.0,
        "burst": dict(threshold=10, window_sec=10.0, grace_sec=0.2, level=logging.ERROR, flush=False),
    },
    "s33f": {
        "name": "S33F-BOOK",
        "make_configs": lambda: [make_fx_daily_trend_config(signal_only=False),
                                 make_fx_daily_rev_config(signal_only=False)],    # 🔴 LIVE (U3 일봉)
        "make_controllers": _build_mt5_controllers_daily,
        "targets_env": "FXD_EXECUTOR_TARGETS",
        "targets_fallback_env": "MT5_EXECUTOR_TARGETS",
        "targets_default": "127.0.0.1:9010",
        "signals_file": "signals_s33f_book.jsonl",
        "tg_token_env": "Noil2_TELEGRAM_BOT_TOKEN",
        "tg_token_fallback_env": "Noil2_TELEGRAM_CHAT_ID",
        "publish_config": True,  # 'fxd' 네임스페이스 config 소유(구 fxd1 승계)
        "port": 18036,
        "warmup_timeout": 120.0,
        "burst": dict(threshold=10, window_sec=10.0, grace_sec=0.2, level=logging.ERROR, flush=False),
    },
    "s11m": {
        "name": "S11M-BOOK",
        "make_configs": lambda: [make_s11_mt5_trend_config(signal_only=False),  # 첫 항목=primary
                                 make_s11_mt5_rev_config(signal_only=False),   # BTC 역추롱(크립토 CFD)
                                 make_s11_mt5_fade_config(signal_only=False)],  # 🔴 LIVE (S11 MT5 확장판)
        "make_controllers": _build_mt5_controllers,
        "targets_env": "S11M_EXECUTOR_TARGETS",
        "targets_fallback_env": "MT5_EXECUTOR_TARGETS",
        "targets_default": "127.0.0.1:9010",
        "signals_file": "signals_s11m_book.jsonl",
        "tg_token_env": "Noil2_TELEGRAM_BOT_TOKEN",
        "tg_token_fallback_env": "Noil2_TELEGRAM_CHAT_ID",
        "publish_config": True,  # 's11m' 네임스페이스 config 소유(구 s11mt 역할 승계)
        "port": 18033,
        "warmup_timeout": 120.0,
        "burst": dict(threshold=10, window_sec=10.0, grace_sec=0.2, level=logging.ERROR, flush=False),
    },
}


def _select_engine() -> str:
    """엔진 선택: --strategy <name> / --strategy=<name> 인자 > STRATEGY env > 'bybit'."""
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a.startswith("--strategy="):
            return a.split("=", 1)[1].strip().lower()
        if a == "--strategy" and i + 1 < len(argv):
            return argv[i + 1].strip().lower()
    return _env("STRATEGY", "bybit").lower()


ENGINE = _select_engine()
if ENGINE not in ENGINES:
    raise SystemExit(f"[main] 알 수 없는 STRATEGY={ENGINE!r} (가능: {sorted(ENGINES)})")
SPEC = ENGINES[ENGINE]
NAME = SPEC["name"]


class BurstWarningTerminator(logging.Handler):
    """경보 폭주 시 안전 종료. trigger_level/flush는 엔진별로 다름(기존 동작 보존)."""
    def __init__(self, threshold: int, window_sec: float, grace_sec: float,
                 trigger_level: int = logging.WARNING, flush_on_kill: bool = True):
        super().__init__()
        self.threshold = threshold
        self.window_sec = window_sec
        self.grace_sec = grace_sec
        self.trigger_level = trigger_level
        self.flush_on_kill = flush_on_kill
        self._ts = deque()
        self._lock = threading.Lock()
        self._armed = True
        logging.captureWarnings(True)

    def emit(self, record: logging.LogRecord):
        if record.levelno < self.trigger_level or not self._armed:
            return
        now = time.monotonic()
        with self._lock:
            self._ts.append(now)
            cutoff = now - self.window_sec
            while self._ts and self._ts[0] < cutoff:
                self._ts.popleft()
            if len(self._ts) >= self.threshold:
                self._armed = False
                logging.getLogger("system").error(
                    f"🚨 WARNING {len(self._ts)}회/{self.window_sec:.1f}s → 안전 종료 시도"
                )
                self._shutdown()

    def _shutdown(self):
        flush = self.flush_on_kill

        def _kill():
            try:
                if flush:
                    logging.getLogger("system").critical("🧯 종료 직전: 로그/텔레그램 flush 시도")
                    logging.shutdown()
            finally:
                try:
                    os.kill(os.getpid(), signal.SIGINT)
                except Exception:
                    raise SystemExit(1)

        try:
            loop = asyncio.get_running_loop()
            if loop.is_running():
                loop.call_later(self.grace_sec, _kill)
                return
        except RuntimeError:
            pass
        _kill()


# ── 로거 (엔진별 텔레그램 토큰 / signals 파일) ──────────────────────────────
tg_bot = os.getenv(SPEC["tg_token_env"]) or os.getenv(SPEC["tg_token_fallback_env"])
tg_chat = os.getenv("TELEGRAM_CHAT_ID")

system_logger = setup_logger(
    "system",
    logger_level=logging.DEBUG, console_level=logging.DEBUG, file_level=logging.INFO,
    enable_telegram=True, telegram_level=logging.INFO, exclude_sig_in_file=False,
    telegram_mode="both", telegram_bot_token=tg_bot, telegram_chat_id=tg_chat,
)
_b = SPEC["burst"]
system_logger.addHandler(BurstWarningTerminator(
    threshold=_b["threshold"], window_sec=_b["window_sec"], grace_sec=_b["grace_sec"],
    trigger_level=_b["level"], flush_on_kill=_b["flush"],
))

trading_logger = setup_logger(
    "trading",
    logger_level=logging.DEBUG, console_level=logging.DEBUG, file_level=logging.INFO,
    enable_telegram=True, telegram_level=logging.INFO,
    write_signals_file=True, signals_filename=SPEC["signals_file"], exclude_sig_in_file=False,
    telegram_mode="both", telegram_bot_token=tg_bot, telegram_chat_id=tg_chat,
)

app = FastAPI()
manual_queue: Queue = Queue()

bot: TradeBot | None = None
ws_controller = None
rest_controller = None


async def warmup_with_ws_prices(bot: TradeBot, ws, name: str, warmup_timeout):
    MIN_TICKS = bot.jump.history_num
    started_at = time.monotonic()
    while True:
        try:
            elapsed = time.monotonic() - started_at
            missing: dict[str, int] = {}
            for sym in bot.symbols:
                price = ws.get_price(sym)
                exchange_ts = ws.get_last_exchange_ts(sym)
                if price is not None:
                    bot.jump.record_price(sym, price, exchange_ts)
                cur = len(bot.jump.price_history.get(sym, []))
                if cur < MIN_TICKS:
                    if warmup_timeout is not None and elapsed >= warmup_timeout:
                        system_logger.debug(  # 마감 심볼 워밍업 스킵은 정상 → 텔레그램 안 보냄
                            f"[{name}] ⏭ [{sym}] 틱 부족({cur}/{MIN_TICKS}), {elapsed:.0f}s 타임아웃 → 스킵"
                        )
                    else:
                        missing[sym] = cur
            if not missing:
                system_logger.debug(f"✅ [{name}] 데이터 준비 완료, 메인 루프 시작")
                return
            _heartbeat()  # warmup 진행 중에도 살아있음 표시(warmup 행도 healthcheck가 잡게)
            system_logger.debug(f"⏳ [{name}] 데이터 준비 중... (부족: {missing})")
            await asyncio.sleep(0.5)
        except Exception as e:
            system_logger.error(f"❌ [{name}] warmup 오류: {e}")
            await asyncio.sleep(1.0)


async def bot_loop(bot: TradeBot, ws, name: str, warmup_timeout):
    await warmup_with_ws_prices(bot, ws, name, warmup_timeout)
    _heartbeat()  # warmup 통과 직후 1회(메인 루프 진입 표시)
    while True:
        try:
            await bot.run_once()
            _heartbeat()  # run_once가 행되면 여기 도달 못함 → stale → 재기동
            await asyncio.sleep(0.5)
        except Exception as e:
            system_logger.error(f"❌ [{name}] bot_loop 오류: {e}")
            await asyncio.sleep(10)


@app.on_event("startup")
async def startup_event():
    global bot, ws_controller, rest_controller

    _heartbeat()  # 부팅 즉시 1회 → healthcheck가 워밍업 시작 전에 오인 kill 안 하도록
    system_logger.debug(f"🚀 신호 봇 시작 (engine={ENGINE}, name={NAME})")

    # ✅ 책 모드(make_configs): 서브 config 리스트 — 첫 항목=primary, 심볼=합집합(순서보존)
    sub_cfgs = None
    if SPEC.get("make_configs"):
        sub_cfgs = SPEC["make_configs"]()
        cfg = sub_cfgs[0]
        _seen = []
        for _c in sub_cfgs:
            for _s in (getattr(_c, "symbols", []) or []):
                if _s not in _seen:
                    _seen.append(_s)
        symbols = tuple(_seen)
        system_logger.debug(
            f"🔧 {NAME} 책 모드: 서브 {len(sub_cfgs)}개 "
            f"tags={[getattr(c, 'strategy', '?') for c in sub_cfgs]}")
    else:
        cfg = SPEC["make_config"]()
        symbols = tuple(getattr(cfg, "symbols", []) or [])
    if not symbols:
        system_logger.error(f"⚠️ [{NAME}] 거래 심볼이 없습니다 — .env(심볼 env) 확인.")
    system_logger.debug(f"🔧 {NAME} symbols={symbols}, config={cfg.as_dict()}")

    ws_controller, rest_controller = SPEC["make_controllers"](symbols, system_logger)

    raw = _env(SPEC["targets_env"], "")
    if not raw and SPEC.get("targets_fallback_env"):
        raw = _env(SPEC["targets_fallback_env"], "")
    if not raw:
        raw = SPEC["targets_default"]
    targets = [Target(h, int(p)) for t in raw.split(",") if ":" in t
               for h, p in [t.strip().rsplit(":", 1)]]
    local_sender = LocalActionSender(targets=targets, system_logger=system_logger, ping_sec=10)
    local_sender.start()

    bot = TradeBot(
        ws_controller,
        rest_controller,
        manual_queue,
        system_logger=system_logger,
        trading_logger=trading_logger,
        symbols=symbols,
        config=cfg,
        action_sender=local_sender,
        publish_config=SPEC["publish_config"],
        sub_configs=sub_cfgs,  # ✅ 책 모드(None이면 단일 모드)
    )

    asyncio.create_task(bot_loop(bot, ws_controller, NAME, SPEC["warmup_timeout"]))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=SPEC["port"], reload=False)
