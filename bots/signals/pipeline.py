# bots/signals/pipeline.py
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("Asia/Seoul")


def _to_str(x: Any) -> str:
    if isinstance(x, (bytes, bytearray)):
        return x.decode("utf-8", errors="replace")
    return str(x)


def _hset_compat(redis_client: Any, key: str, field: str, value: str) -> None:
    hset = getattr(redis_client, "hset", None)
    if not callable(hset):
        raise AttributeError("redis_client has no hset()")
    try:
        hset(key, field, value)
        return
    except TypeError:
        pass
    hset(name=key, key=field, value=value)


def _hget_compat(redis_client: Any, key: str, field: str) -> Optional[str]:
    hget = getattr(redis_client, "hget", None)
    if not callable(hget):
        raise AttributeError("redis_client has no hget()")
    try:
        res = hget(key, field)
    except TypeError:
        res = hget(name=key, key=field)
    if res is None:
        return None
    return _to_str(res)


def _hdel_compat(redis_client: Any, key: str, fields: Sequence[str]) -> None:
    if not fields:
        return
    hdel = getattr(redis_client, "hdel", None)
    if not callable(hdel):
        raise AttributeError("redis_client has no hdel()")
    try:
        hdel(key, *fields)
        return
    except TypeError:
        pass
    hdel(key, list(fields))


def _zadd_compat(redis_client: Any, key: str, score: float, member: str) -> None:
    zadd = getattr(redis_client, "zadd", None)
    if not callable(zadd):
        raise AttributeError("redis_client has no zadd()")
    try:
        zadd(key, {member: score})
        return
    except TypeError:
        pass
    try:
        zadd(key, score, member)
        return
    except TypeError:
        pass
    zadd(key, [(member, score)])


def _zrem_compat(redis_client: Any, key: str, members: Sequence[str]) -> None:
    if not members:
        return
    zrem = getattr(redis_client, "zrem", None)
    if not callable(zrem):
        raise AttributeError("redis_client has no zrem()")
    try:
        zrem(key, *members)
        return
    except TypeError:
        pass
    zrem(key, list(members))


def _zrangebyscore_compat(
    redis_client: Any,
    key: str,
    min_score: Union[int, float, str],
    max_score: Union[int, float, str],
    *,
    start: int = 0,
    num: int = 1000,
) -> List[str]:
    fn = getattr(redis_client, "zrangebyscore", None)
    if not callable(fn):
        execute = getattr(redis_client, "execute", None) or getattr(redis_client, "execute_command", None)
        if not callable(execute):
            raise AttributeError("redis_client has no zrangebyscore()/execute()")
        res = execute("ZRANGEBYSCORE", key, min_score, max_score, "LIMIT", start, num)
        return [_to_str(x) for x in (res or [])]

    try:
        res = fn(key, min_score, max_score, start=start, num=num)
        return [_to_str(x) for x in (res or [])]
    except TypeError:
        pass

    try:
        res = fn(key, min_score, max_score, start, num)
        return [_to_str(x) for x in (res or [])]
    except TypeError:
        pass

    res = fn(key, min_score, max_score)
    res = [_to_str(x) for x in (res or [])]
    return res[start : start + num]


def _pipeline_or_none(redis_client: Any) -> Optional[Any]:
    p = getattr(redis_client, "pipeline", None)
    if callable(p):
        try:
            return p(transaction=True)
        except TypeError:
            try:
                return p()
            except TypeError:
                return None
    return None


# ------------------------- latest helpers -------------------------
def _latest_index(symbol: str, kind: Any, side: Any) -> str:
    return f"{symbol}|{kind}|{side}"


def _pack_latest(ts_ms: int, record_field: str) -> str:
    # value: "<ts_ms>|<record_field>"
    return f"{int(ts_ms)}|{record_field}"


def _unpack_latest(v: str) -> Tuple[Optional[int], Optional[str]]:
    if not v:
        return (None, None)
    parts = v.split("|", 1)
    if len(parts) != 2:
        return (None, None)
    ts_s, record_field = parts
    try:
        return (int(ts_s), record_field)
    except Exception:
        return (None, record_field)


def upload_signal(redis_client: Any, sig: Dict[str, Any], namespace: str) -> Tuple[str, int]:
    if not namespace:
        raise ValueError("namespace is required (e.g. 'bybit', 'mt5')")
    if redis_client is None:
        return ("", 0)

    symbol = sig.get("symbol")
    ts_iso = sig.get("ts")
    if not symbol or not ts_iso:
        raise ValueError("sig must include 'symbol' and 'ts'")

    day = ts_iso[:10]

    sid_src = f"{symbol}|{ts_iso}|{sig.get('kind')}|{sig.get('side')}|{sig.get('price')}"
    sid = hashlib.sha1(sid_src.encode("utf-8")).hexdigest()
    record_field = f"{day}|{sid}"

    extra = sig.get("extra") or {}
    if "ts_ms" not in extra:
        extra["ts_ms"] = int(time.time() * 1000)
        sig["extra"] = extra
    ts_ms = int(extra["ts_ms"])

    suffix = (int(sid[:3], 16) % 1000) / 1000.0  # 0.000 ~ 0.999
    score = ts_ms + suffix

    value = json.dumps(sig, ensure_ascii=False, separators=(",", ":"))

    key_main = f"trading:{namespace}:signal"
    key_time = f"{key_main}:time"
    key_latest = f"{key_main}:latest"

    # ✅ latest는 (symbol,kind,side)별로 저장 + value에 ts_ms 포함
    latest_index = _latest_index(symbol, sig.get("kind"), sig.get("side"))
    latest_value = _pack_latest(ts_ms, record_field)

    pipe = _pipeline_or_none(redis_client)
    if pipe is not None:
        try:
            _hset_compat(pipe, key_main, record_field, value)
            try:
                pipe.zadd(key_time, {record_field: score})
            except Exception:
                pass
            _hset_compat(pipe, key_latest, latest_index, latest_value)
            pipe.execute()
        finally:
            # 파이프가 zadd를 지원 못하는 케이스 대비
            _zadd_compat(redis_client, key_time, score, record_field)
        return (record_field, ts_ms)

    _hset_compat(redis_client, key_main, record_field, value)
    _zadd_compat(redis_client, key_time, score, record_field)
    _hset_compat(redis_client, key_latest, latest_index, latest_value)
    return (record_field, ts_ms)


def prune_signals(
    redis_client: Any,
    namespace: str,
    *,
    keep_days: int = 10,
    batch: int = 1000,
) -> int:
    if redis_client is None:
        return 0
    if keep_days <= 0:
        return 0

    key_main = f"trading:{namespace}:signal"
    key_time = f"{key_main}:time"

    now_ms = int(time.time() * 1000)
    cutoff = now_ms - keep_days * 24 * 60 * 60 * 1000

    old_fields = _zrangebyscore_compat(redis_client, key_time, "-inf", cutoff, start=0, num=batch)
    if not old_fields:
        return 0

    _hdel_compat(redis_client, key_main, old_fields)
    _zrem_compat(redis_client, key_time, old_fields)
    return len(old_fields)


_LAST_PRUNE_MS_BY_NS: Dict[str, int] = {}


def maybe_prune_signals(
    redis_client: Any,
    namespace: str,
    *,
    keep_days: int = 10,
    interval_sec: int = 60,
    batch: int = 1000,
    max_rounds: int = 5,
) -> int:
    if redis_client is None:
        return 0

    now_ms = int(time.time() * 1000)
    last_ms = _LAST_PRUNE_MS_BY_NS.get(namespace, 0)

    if now_ms - last_ms < interval_sec * 1000:
        return 0

    _LAST_PRUNE_MS_BY_NS[namespace] = now_ms

    total = 0
    for _ in range(max_rounds):
        n = prune_signals(redis_client, namespace, keep_days=keep_days, batch=batch)
        total += n
        if n == 0:
            break
    return total


def log_and_upload_signal(
    trading_logger: Any,
    redis_client: Any,
    sig_dict: Dict[str, Any],
    namespace: str,
    *,
    keep_days: Optional[int] = None,
    prune_interval_sec: int = 24 * 60 * 60,
) -> None:
    if trading_logger:
        trading_logger.info("SIG " + json.dumps(sig_dict, ensure_ascii=False))
    if redis_client is None:
        return

    upload_signal(redis_client, sig_dict, namespace=namespace)

    if keep_days is not None:
        maybe_prune_signals(
            redis_client,
            namespace,
            keep_days=keep_days,
            interval_sec=prune_interval_sec,
        )


def build_log_upload(
    trading_logger: Any,
    redis_client: Any,
    sig_dict: Dict[str, Any],
    symbol: str,
    namespace: str,
    *,
    keep_days: Optional[int] = None,
) -> Dict[str, Any]:
    if not isinstance(sig_dict, dict):
        raise TypeError(f"build_log_upload expects dict, got {type(sig_dict)}")

    # copy to avoid mutating caller's dict
    d: Dict[str, Any] = dict(sig_dict)

    # 최소 필드 보정 (없으면 채움)
    if symbol and not d.get("symbol"):
        d["symbol"] = symbol
    if not d.get("ts"):
        d["ts"] = datetime.now(_TZ).isoformat()
    if namespace and not d.get("engine"):
        d["engine"] = namespace

    log_and_upload_signal(
        trading_logger,
        redis_client,
        d,
        namespace,
        keep_days=keep_days,
    )
    return d


# ------------------------- read latest (1 roundtrip) -------------------------
def get_latest_ts_ms(redis_client: Any, namespace: str, symbol: str, kind: str, side: str) -> Optional[int]:
    """
    HGET 한번으로 최신 ts_ms 반환.
    """
    if redis_client is None:
        return None
    key_latest = f"trading:{namespace}:signal:latest"
    idx = _latest_index(symbol, kind, side)
    try:
        v = _hget_compat(redis_client, key_latest, idx)
        ts_ms, _ = _unpack_latest(v or "")
        return ts_ms
    except Exception:
        return None


def get_latest_record_field(redis_client: Any, namespace: str, symbol: str, kind: str, side: str) -> Optional[str]:
    """
    HGET 한번으로 최신 record_field("YYYY-MM-DD|sid") 반환.
    """
    if redis_client is None:
        return None
    key_latest = f"trading:{namespace}:signal:latest"
    idx = _latest_index(symbol, kind, side)
    try:
        v = _hget_compat(redis_client, key_latest, idx)
        _, record_field = _unpack_latest(v or "")
        return record_field
    except Exception:
        return None

def get_latest_entry_ts_ms(redis_client: Any, namespace: str, symbol: str, side: str) -> Optional[int]:
    """
    trading:{ns}:signal:latest 에서 (symbol, ENTRY, side)의 최신 ts_ms를 HGET 1번으로 가져온다.
    value 포맷: "<ts_ms>|<record_field>"
    """
    if redis_client is None:
        return None
    key_latest = f"trading:{namespace}:signal:latest"
    idx = f"{symbol}|ENTRY|{side}"
    try:
        v = redis_client.hget(key_latest, idx)
        if not v:
            return None
        if isinstance(v, (bytes, bytearray)):
            v = v.decode("utf-8", errors="replace")
        else:
            v = str(v)
        ts_s = v.split("|", 1)[0]
        return int(ts_s) if ts_s else None
    except Exception:
        return None
