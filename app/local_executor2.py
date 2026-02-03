# app/local_executor.py
from __future__ import annotations

import asyncio
import json
import os
import time
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional, Set, List
from core.redis_client import redis_client
from bots.trading.trade_executor import TradeExecutor, TradeExecutorDeps
from bots.state.lots import (
    open_lot,
    close_lot_full,
    get_lot_qty_total,
    LotsIndex,
    get_lot_ex_lot_id,
)
from bots.trade_config import TradeConfig, make_mt5_signal_config,make_bybit_config
from utils.logger import setup_logger
# Rest controllers
from controllers.bybit.bybit_rest_controller import BybitRestController
from controllers.mt5.mt5_rest_controller import Mt5RestController

from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))

def _fmt_ts_ms(ts_ms: int) -> str:
    try:
        return datetime.fromtimestamp(int(ts_ms) / 1000, tz=KST).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "-"

def _weighted_avg(items) -> float:
    # items: List[LotCacheItem]
    num = 0.0
    den = 0.0
    for it in items:
        q = float(getattr(it, "qty_total", 0.0) or 0.0)
        p = float(getattr(it, "entry_price", 0.0) or 0.0)
        if q <= 0 or p <= 0:
            continue
        num += q * p
        den += q
    return (num / den) if den > 0 else 0.0

def build_asset_log_with_lots(*, wallet: Dict[str, Any], lots_index: LotsIndex) -> str:
    # bot 스타일 느낌만 유지: wallet + 심볼별 포지션(롱/숏) + 엔트리 라인들
    lines: List[str] = ["\n💼 ASSET STATUS"]

    # wallet 출력(원하면 기존처럼 dict 그대로 or 정렬)
    try:
        w = {k: float(v) for k, v in (wallet or {}).items()}
        lines.append(f"wallet={w}")
    except Exception:
        lines.append(f"wallet={wallet or {}}")

    # lots_index 기준으로 “현재 오픈 포지션이 존재하는 심볼”만 뽑기
    symbols = []
    try:
        symbols = lots_index.list_open_symbols()
    except Exception:
        # (최후수단) 내부 접근도 가능하지만 비추
        symbols = []

    if not symbols:
        lines.append("(open lots 없음)")
        return "\n".join(lines).rstrip()

    for sym in symbols:
        # LONG / SHORT 각각 lots에서 가져옴
        long_items = lots_index.list_open_items(sym, "LONG")
        short_items = lots_index.list_open_items(sym, "SHORT")

        # 심볼 헤더
        lines.append(f"[{sym}]")

        # LONG
        if long_items:
            total_q = sum(float(x.qty_total or 0.0) for x in long_items)
            avg_p = _weighted_avg(long_items)
            lines.append(f"  - 포지션: LONG ({total_q:.3f}, {avg_p:.1f})")
            for i, it in enumerate(long_items, start=1):
                q = float(it.qty_total or 0.0)
                ts = _fmt_ts_ms(int(it.entry_ts_ms or 0))
                px = float(it.entry_price or 0.0)
                lines.append(f"     └#{i} {q:+.3f} : {ts}, {px:.1f}")

        # SHORT
        if short_items:
            total_q = sum(float(x.qty_total or 0.0) for x in short_items)
            avg_p = _weighted_avg(short_items)
            lines.append(f"  - 포지션: SHORT ({total_q:.3f}, {avg_p:.1f})")
            for i, it in enumerate(short_items, start=1):
                q = float(it.qty_total or 0.0)
                ts = _fmt_ts_ms(int(it.entry_ts_ms or 0))
                px = float(it.entry_price or 0.0)
                # SHORT는 출력에서 - 로 보이게(네 예시처럼)
                lines.append(f"     └#{i} {-q:+.3f} : {ts}, {px:.1f}")

        if (not long_items) and (not short_items):
            lines.append("  - 포지션 없음")

    return "\n".join(lines).rstrip()

# =========================
# Settings (ENV) - account-based profile
# =========================

def _env(key: str, default: str = "") -> str:
    return (os.getenv(key) or default).strip()

def _pick_profile_by_account_id(account_id: str) -> str:
    """
    EXEC_ACCOUNT_ID로 선택한 account_id를 가진 profile을 찾는다.
    profiles: EXEC_PROFILES=A1,A2,...
    each: EXEC_{P}_ACCOUNT_ID=...
    """
    aid = (account_id or "").strip()
    if not aid:
        raise RuntimeError("EXEC_ACCOUNT_ID is empty")

    profiles = [p.strip() for p in _env("EXEC_PROFILES", "").split(",") if p.strip()]
    if not profiles:
        raise RuntimeError("EXEC_PROFILES is empty (ex: EXEC_PROFILES=A1,A2)")

    for p in profiles:
        if _env(f"EXEC_{p}_ACCOUNT_ID") == aid:
            return p

    raise RuntimeError(f"No profile matched EXEC_ACCOUNT_ID={aid}. profiles={profiles}")

# 어떤 계정을 돌릴지 (tenant key)
EXEC_ACCOUNT_ID = _env("EXEC_ACCOUNT_ID")
PROFILE = _pick_profile_by_account_id(EXEC_ACCOUNT_ID)

# 공통 runtime
HOST = _env("EXEC_LISTEN_HOST", "127.0.0.1")
ENTRY_TTL_MS = int(_env("EXEC_ENTRY_TTL_MS", "60000"))
DEDUP_TTL_SEC = int(_env("EXEC_DEDUP_TTL_SEC", str(24 * 3600)))
DRY_RUN = (_env("EXEC_DRY_RUN", "0") == "1")

# profile-scoped
DEFAULT_ENGINE = _env(f"EXEC_{PROFILE}_ENGINE", "BYBIT").upper()
PORT = int(_env(f"EXEC_{PROFILE}_LISTEN_PORT", "9009"))

BASE_NS = _env(f"EXEC_{PROFILE}_BASE_NS", "agent")
USER_ID = _env(f"EXEC_{PROFILE}_USER_ID", "local_user")
ACCOUNT_ID = _env(f"EXEC_{PROFILE}_ACCOUNT_ID", "").strip()

# (TradeExecutorDeps.get_entry_percent) - 지금은 config에서 꺼내므로 미사용이면 둬도 됨
DEFAULT_ENTRY_PERCENT = float(_env("EXEC_ENTRY_PERCENT", "10"))
MAX_EFF_LEV = float(_env("EXEC_MAX_EFF_LEV", "0"))

# symbol filters (profile-scoped)
RECEIVE_SYMBOLS = _env(f"EXEC_{PROFILE}_RECEIVE_SYMBOLS", "")
EXECUTE_SYMBOLS = _env(f"EXEC_{PROFILE}_EXECUTE_SYMBOLS", "")

# telegram (profile-scoped token + global chat id)
tg_bot = _env(f"EXEC_{PROFILE}_TELEGRAM_BOT_TOKEN", "")
tg_chat = _env("TELEGRAM_CHAT_ID", "")
enable_tg = bool(tg_bot and tg_chat)

def state_namespace() -> str:
    # lots/assets는 account_id 기준으로 분리
    # (user_id는 표시용/구분용으로 같이 둬도 되지만, account가 핵심 키)
    if ACCOUNT_ID:
        return f"{BASE_NS}:{USER_ID}:{ACCOUNT_ID}"
    return f"{BASE_NS}:{USER_ID}"

STATE_NS = state_namespace()


system_logger = setup_logger(
    "local_executor",
    logger_level=logging.DEBUG,
    console_level=logging.DEBUG,
    file_level=logging.INFO,
    enable_telegram=enable_tg,
    telegram_level=logging.INFO,
    exclude_sig_in_file=False,
    telegram_mode="both",
    telegram_bot_token=tg_bot,
    telegram_chat_id=tg_chat,
)

trading_logger = setup_logger(
    "local_executor_trade",
    logger_level=logging.DEBUG,
    console_level=logging.DEBUG,
    file_level=logging.INFO,
    enable_telegram=enable_tg,
    telegram_level=logging.INFO,
    write_signals_file=True,
    signals_filename="signals.jsonl",
    exclude_sig_in_file=False,
    telegram_mode="both",
    telegram_bot_token=tg_bot,
    telegram_chat_id=tg_chat,
)


# =========================
# Helpers
# =========================
def now_ms() -> int:
    return int(time.time() * 1000)


def parse_symbols(s: str) -> Optional[Set[str]]:
    """
    "BTCUSDT, ETHUSDT" -> {"BTCUSDT","ETHUSDT"}
    empty -> None (no filter)
    """
    if not s:
        return None
    out = set()
    for x in s.split(","):
        t = x.strip().upper()
        if t:
            out.add(t)
    return out or None


def state_namespace() -> str:
    if ACCOUNT_ID:
        return f"{BASE_NS}:{USER_ID}:{ACCOUNT_ID}"
    return f"{BASE_NS}:{USER_ID}"


STATE_NS = state_namespace()

def pick_config_name(engine: str) -> str:
    eng = (engine or "").upper().strip()
    if eng == "BYBIT":
        return "bybit"
    if eng == "MT5":
        return "MT5"

def load_engine_config(engine: str) -> TradeConfig:
    cfg_name = pick_config_name(engine)  # 위에서 만든 함수
    n = (cfg_name or "default").strip()

    # 1) name 기준으로 "의도"를 결정
    if n == "bybit":
        return make_bybit_config().normalized()

    # mt5_signal / mt5 / mt5_trade 등 "mt5 계열"이면 일단 mt5_signal 팩토리로 생성
    if n.startswith("MT5"):
        cfg = make_mt5_signal_config().normalized()
        cfg.name = n
        return cfg.normalized()


def make_event_id(msg: Dict[str, Any]) -> str:
    # 멱등 키: (engine/source + symbol + action + signal_id)
    src = (msg.get("source") or msg.get("engine") or DEFAULT_ENGINE).upper().strip()
    sym = (msg.get("symbol") or "").upper().strip()
    act = (msg.get("action") or "").upper().strip()
    sid = str(msg.get("signal_id") or "")
    return f"{src}|{sym}|{act}|{sid}"
# --- dedup: in-memory (no Redis) ---
_seen: dict[str, int] = {}  # eid -> expire_ms

def dedup_seen(eid: str) -> bool:
    """
    True  -> 이미 처리한 이벤트(중복)
    False -> 처음 보는 이벤트(처리 진행)
    """
    now = now_ms()

    # 가벼운 GC (너무 커지면 일부 청소)
    if len(_seen) > 50000:
        # 랜덤/정렬 없이 앞부분만 조금 청소해도 충분
        for k, exp in list(_seen.items())[:5000]:
            if exp <= now:
                _seen.pop(k, None)

    exp = _seen.get(eid)
    if exp is not None and exp > now:
        return True

    _seen[eid] = now + (DEDUP_TTL_SEC * 1000)
    return False



def entry_expired(ts_ms: int) -> bool:
    if ts_ms <= 0:
        return False
    return (now_ms() - ts_ms) > ENTRY_TTL_MS


# =========================
# Logging
# =========================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("local_executor")

# =========================
# Filters (receive/execute)
# =========================
RX = parse_symbols(RECEIVE_SYMBOLS)
EX = parse_symbols(EXECUTE_SYMBOLS)



def allow_receive(engine: str, symbol: str) -> bool:
    # engine 인자는 무시해도 되지만, 로그 호환 때문에 둠
    sym = (symbol or "").upper().strip()
    if not sym:
        return False
    return True if RX is None else (sym in RX)

def allow_execute(engine: str, symbol: str) -> bool:
    sym = (symbol or "").upper().strip()
    if not sym:
        return False
    return True if EX is None else (sym in EX)


# =========================
# Exec Context per engine
# =========================
@dataclass
class ExecContext:
    engine: str
    state_ns: str
    rest: Any
    trade_executor: TradeExecutor
    lots_index: LotsIndex
    rules_warmed: Set[str]

    asset: Dict[str, Any]                 # ✅ 추가

def make_rest(engine: str):
    eng = (engine or "").upper().strip()
    if eng == "BYBIT":
        return BybitRestController(system_logger=system_logger)
    if eng == "MT5":
        return Mt5RestController(system_logger=system_logger)
    raise ValueError(f"Unknown engine={engine}")


def warmup_symbol_rules(ctx: ExecContext, symbol: str) -> None:
    sym = (symbol or "").upper().strip()
    if not sym or sym in ctx.rules_warmed:
        return
    get_fn = getattr(ctx.rest, "get_symbol_rules", None)
    fetch_fn = getattr(ctx.rest, "fetch_symbol_rules", None)
    try:
        if callable(get_fn):
            get_fn(sym)
        elif callable(fetch_fn):
            fetch_fn(sym)
        ctx.rules_warmed.add(sym)
        log.debug(f"[rules] warmed {ctx.engine} {sym}")
    except Exception as e:
        log.warning(f"[rules] warmup failed {ctx.engine} {sym}: {e}")


def save_asset(state_ns: str, rest: Any, asset: dict, symbol: Optional[str]) -> None:
    """
    lots/assets는 user_id 포함된 STATE_NS로만 저장.
    rest._asset_key / rest.REDIS_ASSET_KEY는 공유 컨트롤러라 충돌 가능성이 있어
    여기서는 강제로 trading:{STATE_NS}:asset 사용.
    """
    try:
        asset_key = f"trading:{state_ns}:asset"
        wallet = asset.get("wallet") or {}
        for ccy, v in wallet.items():
            try:
                redis_client.hset(asset_key, f"wallet.{ccy}", f"{float(v):.10f}")
            except Exception:
                pass

        pos_map = asset.get("positions") or {}

        if symbol:
            sym = str(symbol).upper().strip()
            pos_sym = pos_map.get(sym)
            payload = "[]"
            if pos_sym is not None:
                payload = json.dumps(pos_sym, separators=(",", ":"), ensure_ascii=False, default=str)
            redis_client.hset(asset_key, f"positions.{sym}", payload)
            return

        # ✅ symbol=None이면 전 심볼 저장
        for sym, pos_sym in pos_map.items():
            sym_u = str(sym).upper().strip()
            payload = json.dumps(pos_sym, separators=(",", ":"), ensure_ascii=False, default=str)
            redis_client.hset(asset_key, f"positions.{sym_u}", payload)


    except Exception as e:
        log.warning(f"[save_asset] failed (symbol={symbol}): {e}")


def build_ctx(engine: str) -> ExecContext:
    engine = (engine or DEFAULT_ENGINE).upper().strip()
    cfg = load_engine_config(engine)
    max_eff = float(getattr(cfg, "max_effective_leverage", 0.0) or 0.0)

    rest = make_rest(engine)
    state_ns_engine = f"{STATE_NS}:{engine}"

    lots_index = LotsIndex(namespace=state_ns_engine, redis_cli=redis_client)

    symbols_ctx = sorted(list(EX or RX or set()))
    lots_index.load_from_redis(symbols=symbols_ctx)

    # ✅ ctx.asset를 단일 소스로 사용
    asset: Dict[str, Any] = {"wallet": {}, "positions": {}}

    # asset warmup
    try:
        asset = rest.build_asset(asset=asset, symbol=None) or asset
    except Exception:
        pass
    for s in (symbols_ctx or []):
        try:
            asset = rest.build_asset(asset=asset, symbol=s) or asset
        except Exception:
            pass


    try:
        save_asset(state_ns_engine, rest, asset, None)
    except Exception:
        pass

    ctx = ExecContext(
        engine=engine,
        state_ns=state_ns_engine,
        rest=rest,
        trade_executor=None,   # 아래에서 주입
        lots_index=lots_index,
        rules_warmed=set(),
        asset=asset,
    )

    def _get_entry_percent_for_symbol(symbol: str) -> float:
        sym = (symbol or "").upper().strip()
        m = getattr(cfg, "entry_percent_by_symbol", None) or {}
        v = m.get(sym)
        if v is None:
            v = getattr(cfg, "entry_percent", None)
        if v is None:
            v = DEFAULT_ENTRY_PERCENT
        return max(0.001, float(v))

    deps = TradeExecutorDeps(
        get_asset=lambda: ctx.asset,
        set_asset=lambda a: (ctx.asset.clear(), ctx.asset.update(a or {})),
        get_entry_percent=lambda sym: _get_entry_percent_for_symbol(sym),
        get_max_effective_leverage=lambda: float(max_eff),
        save_asset=lambda a, sym: save_asset(state_ns_engine, rest, a, sym),

        open_lot=lambda *, symbol, side, entry_ts_ms, entry_price, qty_total, entry_signal_id=None,
                        ex_lot_id=None: open_lot(
            namespace=state_ns_engine,
            symbol=symbol,
            side=side,
            entry_ts_ms=entry_ts_ms,
            entry_price=entry_price,
            qty_total=qty_total,
            entry_signal_id=entry_signal_id,
            ex_lot_id=ex_lot_id,
        ),
        close_lot_full=lambda *, lot_id: close_lot_full(namespace=state_ns_engine, lot_id=lot_id),
        get_lot_qty_total=lambda lot_id: get_lot_qty_total(namespace=state_ns_engine, lot_id=lot_id),
        get_lot_ex_lot_id=lambda lot_id: get_lot_ex_lot_id(namespace=state_ns_engine, lot_id=lot_id),

        on_lot_open=lambda sym, side, lot_id, entry_ts_ms, qty_total, entry_price, entry_signal_id,
                           ex_lot_id: lots_index.on_open(
            sym, side, lot_id,
            entry_ts_ms=entry_ts_ms,
            qty_total=qty_total,
            entry_price=entry_price,
            entry_signal_id=entry_signal_id,
            ex_lot_id=int(ex_lot_id or 0),
        ),
        on_lot_close=lambda sym, side, lot_id: lots_index.on_close(sym, side, lot_id),
    )

    trade_executor = TradeExecutor.build(
        rest=rest,
        deps=deps,
        system_logger=system_logger,
        trading_logger=trading_logger,
        taker_fee_rate=0.00055,
        engine_name=engine,   # BYBIT / MT5
    )

    ctx.trade_executor = trade_executor
    return ctx



# 멀티 거래소(엔진) 지원: BYBIT/MT5 둘 다 받을 수 있게 ctx map 구성
CTX_MAP: Dict[str, ExecContext] = {}

def get_ctx(engine: str) -> ExecContext:
    eng = (engine or DEFAULT_ENGINE).upper().strip()
    ctx = CTX_MAP.get(eng)
    if ctx:
        return ctx
    # lazy create
    ctx = build_ctx(eng)
    CTX_MAP[eng] = ctx
    return ctx


# =========================
# Action Handling
# =========================
async def handle_action(msg: Dict[str, Any]) -> None:
    engine = (msg.get("source") or msg.get("engine") or DEFAULT_ENGINE).upper().strip()
    symbol = (msg.get("symbol") or "").upper().strip()

    # 0) 수신 필터
    if not allow_receive(engine, symbol):
        return

    action = (msg.get("action") or "").upper().strip()
    side = (msg.get("side") or "").upper().strip()
    price = msg.get("price")
    signal_id = msg.get("signal_id")
    close_open_signal_id = msg.get("close_open_signal_id")
    ts_ms = int(msg.get("ts_ms") or 0)

    if not action or not symbol or not signal_id:
        return

    # 1) 중복 방지(멱등)
    eid = make_event_id(msg)
    if dedup_seen(eid):
        log.debug(f"[skip] dup {eid}")
        return

    # 2) TTL (ENTRY만)
    if action == "ENTRY" and entry_expired(ts_ms):
        log.debug(f"[skip] expired ENTRY {engine} {symbol} sid={signal_id}")
        return

    # 3) 실행 여부 필터 (받는 건 OK, 실행만 제한)
    exec_ok = allow_execute(engine, symbol)

    # 4) context
    ctx = get_ctx(engine)

    # 5) rules warmup (normalize_qty 스킵 방지)
    warmup_symbol_rules(ctx, symbol)

    # 6) DRY_RUN / execute filter
    if DRY_RUN or (not exec_ok):
        log.debug(
            f"[RX{'/DRY' if DRY_RUN else ''}{'/NOEXEC' if not exec_ok else ''}] "
            f"{engine} {action} {symbol} {side} px={price} sid={signal_id} close_open={close_open_signal_id}"
        )
        return

    # 7) 실행
    if action == "ENTRY":
        if price is None:
            log.warning(f"[skip] ENTRY missing price {engine} {symbol} sid={signal_id}")
            return
        await ctx.trade_executor.open_position(
            symbol,
            side,
            float(price),
            entry_signal_id=str(signal_id),
        )
        system_logger.debug(
            build_asset_log_with_lots(
                wallet=(ctx.asset.get("wallet") or {}),
                lots_index=ctx.lots_index,
            )
        )

        return

    if action == "EXIT":
        if not close_open_signal_id:
            log.warning(f"[skip] EXIT missing close_open_signal_id {engine} {symbol} sid={signal_id}")
            return

        lot_id = ctx.lots_index.find_open_lot_id_by_entry_signal_id(
            symbol,
            side,
            str(close_open_signal_id),
        )
        if not lot_id:
            log.warning(f"[skip] EXIT lot not found {engine} {symbol} open_signal_id={close_open_signal_id}")
            return

        await ctx.trade_executor.close_position(
            symbol,
            side,
            lot_id,
            exit_signal_id=str(signal_id),
        )

        system_logger.debug(
            build_asset_log_with_lots(
                wallet=(ctx.asset.get("wallet") or {}),
                lots_index=ctx.lots_index,
            )
        )
        return

    log.warning(f"[skip] unknown action={action} msg={msg}")


# =========================
# TCP Server
# =========================
async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    addr = writer.get_extra_info("peername")
    log.debug(f"[connect] {addr}")
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                msg = json.loads(line.decode("utf-8").strip())
            except Exception:
                continue
            try:
                await handle_action(msg)
            except Exception as e:
                log.exception(f"[handle_action] error: {e}")
    finally:
        writer.close()
        await writer.wait_closed()
        log.debug(f"[disconnect] {addr}")

def _warmup_all_symbols(ctx: ExecContext) -> None:
    # 가능한 심볼 목록: 실행/수신 필터에 걸린 것 + cfg.symbols
    cfg = load_engine_config(ctx.engine)

    symbols = set()
    symbols.update(EX or set())
    symbols.update(RX or set())
    symbols.update((cfg.symbols or []))

    symbols = sorted({s.upper().strip() for s in symbols if s and s.strip()})
    if not symbols:
        system_logger.debug(f"[rules] warmup skipped: no symbols (engine={ctx.engine})")
        return

    ok, fail = [], []
    for sym in symbols:
        try:
            warmup_symbol_rules(ctx, sym)
            ok.append(sym)
        except Exception as e:
            fail.append((sym, str(e)))

    system_logger.debug(
        f"[rules] warmup done engine={ctx.engine} ok={len(ok)} fail={len(fail)} "
        f"ok_syms={ok[:10]}{'...' if len(ok) > 10 else ''}"
    )
    if fail:
        system_logger.warning(f"[rules] warmup failed details={fail[:10]}")

async def main():
    log.debug("=== Local Executor ===")
    log.debug(f"listen={HOST}:{PORT}")
    log.debug(f"STATE_NS={STATE_NS}  (lots/assets prefix)")
    log.debug(f"DEFAULT_ENGINE={DEFAULT_ENGINE}")
    log.debug(f"DRY_RUN={DRY_RUN} ENTRY_TTL_MS={ENTRY_TTL_MS}")
    log.debug(f"receive={RX} execute={EX}")
    log.debug(f"STATE_NS={STATE_NS}")

    # ✅ 여기서 초기 snapshot
    try:
        ctx = get_ctx(DEFAULT_ENGINE)
    # ✅ 부팅 시 rules warmup 강제 + 확인 로그
        _warmup_all_symbols(ctx)

        system_logger.debug(
            build_asset_log_with_lots(
                wallet=(ctx.asset.get("wallet") or {}),
                lots_index=ctx.lots_index,
            )
        )

    except Exception as e:
        system_logger.warning(f"[asset_report] initial tick failed: {e}")

    server = await asyncio.start_server(handle_client, HOST, PORT)
    addrs = ", ".join(str(sock.getsockname()) for sock in (server.sockets or []))
    log.debug(f"Listening on {addrs}")

    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
