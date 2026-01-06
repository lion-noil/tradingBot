# bots/signals/pipeline.py
from __future__ import annotations
import hashlib
import time
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from typing import Any, Dict
_TZ = ZoneInfo("Asia/Seoul")

def upload_signal(redis_client, sig: Dict[str, Any], namespace: str) -> None:
    if not namespace:
        raise ValueError("namespace is required (e.g. 'bybit', 'mt5')")
    if redis_client is None:
        return


    symbol = sig.get("symbol")
    ts_iso = sig.get("ts")
    if not symbol or not ts_iso:
        raise ValueError("sig must include 'symbol' and 'ts'")
    day = ts_iso[:10]
    sid_src = f"{symbol}|{ts_iso}|{sig.get('kind')}|{sig.get('side')}|{sig.get('price')}"
    sid = hashlib.sha1(sid_src.encode("utf-8")).hexdigest()
    field = f"{day}|{sid}"

    extra = sig.get("extra") or {}
    if "ts_ms" not in extra:
        extra["ts_ms"] = int(time.time() * 1000)
        sig["extra"] = extra

    value = json.dumps(sig, ensure_ascii=False, separators=(",", ":"))

    key_ns = f"trading:{namespace}:signal"
    redis_client.hset(key_ns, field, value)


def log_and_upload_signal(
    trading_logger: Any,
    redis_client: Any,
    sig_dict: Dict[str, Any],
    namespace: str
) -> None:
    if trading_logger:
        trading_logger.info("SIG " + json.dumps(sig_dict, ensure_ascii=False))
    if redis_client is None:
        return

    upload_signal(redis_client, sig_dict, namespace=namespace)

def build_signal_dict(sig, symbol: str, namespace: str) -> Dict[str, Any]:
    d: Dict[str, Any] = {
        "kind": sig.kind,
        "side": sig.side,
        "symbol": symbol,
        "ts": datetime.now(_TZ).isoformat(),
        "price": sig.price,
        "ma100": sig.ma100,
        "ma_delta_pct": sig.ma_delta_pct,
        "thresholds": sig.thresholds,
        "reasons": sig.reasons,
        "engine": namespace,
    }

    extra = getattr(sig, "extra", None)
    if isinstance(extra, dict) and extra:
        d["extra"] = extra
    return d

def build_log_upload(trading_logger, redis_client, sig, symbol: str, namespace: str):
    sig_dict = build_signal_dict(sig, symbol, namespace=namespace)
    log_and_upload_signal(trading_logger, redis_client, sig_dict, namespace)
    return sig_dict