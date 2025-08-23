import os
import sys
import json
import time
import random
import asyncio
import contextlib
import logging
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse, parse_qsl, urlencode

import websockets  # pip install websockets
import httpx       # pip install httpx
from dotenv import load_dotenv  # pip install python-dotenv

load_dotenv()

# ─────────────────────────────────────────────────────────────────────
# ENV (MULTI-ONLY — single mode removed)
LOCAL_BOTS_JSON: Optional[str] = os.getenv("LOCAL_BOTS_JSON")  # JSON array (quoted in .env)
LOCAL_BOTS_FILE: Optional[str] = os.getenv("LOCAL_BOTS_FILE")  # path to a JSON file

# Heartbeat & reconnect
HEARTBEAT_SEC = int(os.getenv("HEARTBEAT_SEC", "15"))
RETRY_SEC = float(os.getenv("RETRY_SEC", "3"))
MAX_RETRY_SEC = float(os.getenv("MAX_RETRY_SEC", "30"))

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_HEARTBEAT = os.getenv("LOG_HEARTBEAT", "0") == "1"

# Default capabilities supported by this local bridge
DEFAULT_CAPS = ["STATUS_QUERY"]

# ─────────────────────────────────────────────────────────────────────
# Logging setup
logger = logging.getLogger("local-ws-bridge")
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_handler)


def log(event: str, **fields):
    kv = " ".join(f"{k}={repr(v)}" for k, v in fields.items() if v is not None)
    logger.info(f"{event} {kv}")


def _strip_quotes(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    s = v.strip()
    if (s.startswith("'''") and s.endswith("'''")) or (s.startswith('"""') and s.endswith('"""')):
        return s[3:-3].strip()
    if (s.startswith("'") and s.endsWith("'")) or (s.startswith('"') and s.endswith('"')):
        return s[1:-1].strip()
    return s


def _redact_token(url: Optional[str]) -> Optional[str]:
    if not url:
        return url
    try:
        p = urlparse(url)
        q = dict(parse_qsl(p.query, keep_blank_values=True))
        if "token" in q:
            q["token"] = "***"
        new_q = urlencode(q, doseq=True)
        return p._replace(query=new_q).geturl()
    except Exception:
        return url


def _host_and_id_from_url(url: Optional[str]):
    if not url:
        return None, None
    p = urlparse(url)
    parts = [s for s in p.path.split("/") if s]
    bot_id = None
    if "ws" in parts:
        idx = parts.index("ws")
        if idx + 1 < len(parts):
            bot_id = parts[idx + 1]
    return p.hostname, bot_id


def _validate_bot_dict(b: Dict[str, Any]) -> Optional[str]:
    if not isinstance(b, dict):
        return "bot entry must be an object"
    if not b.get("render_ws_url"):
        return "render_ws_url is required"
    caps = b.get("caps") or DEFAULT_CAPS
    if "STATUS_QUERY" in caps and not b.get("local_status_url"):
        return "local_status_url is required when STATUS_QUERY is enabled"
    return None


def _load_bots_config() -> List[Dict[str, Any]]:
    """Load list of bot configs (MULTI ONLY).
    Each item shape:
      {
        "name": "label for logs",                                 # optional
        "render_ws_url": "wss://.../ws/<bot_id>?token=...",       # required
        "local_status_url": "http://127.0.0.1:8000/status?plain=true",  # required if STATUS_QUERY
        "caps": ["STATUS_QUERY"],                                  # optional
        "auth_token": "..."                                       # optional; sent as Authorization: Bearer ...
      }
    """
    # 1) File has highest priority
    if LOCAL_BOTS_FILE and os.path.exists(LOCAL_BOTS_FILE):
        try:
            with open(LOCAL_BOTS_FILE, encoding="utf-8") as f:
                arr = json.load(f)
            if not isinstance(arr, list):
                raise RuntimeError("LOCAL_BOTS_FILE must contain a JSON array")
            # validate
            errs = [(_validate_bot_dict(b), i) for i, b in enumerate(arr)]
            bad = [(e, i) for e, i in errs if e]
            if bad:
                raise RuntimeError("; ".join([f"item[{i}]: {e}" for e, i in bad]))
            log("config.multi.file", path=LOCAL_BOTS_FILE, bots=len(arr))
            return arr
        except Exception as e:
            raise RuntimeError(f"Failed to load LOCAL_BOTS_FILE: {e}")

    # 2) Env JSON next
    if LOCAL_BOTS_JSON:
        try:
            arr = json.loads(_strip_quotes(LOCAL_BOTS_JSON) or "[]")
            if not isinstance(arr, list):
                raise RuntimeError("LOCAL_BOTS_JSON must be a JSON array")
            errs = [(_validate_bot_dict(b), i) for i, b in enumerate(arr)]
            bad = [(e, i) for e, i in errs if e]
            if bad:
                raise RuntimeError("; ".join([f"item[{i}]: {e}" for e, i in bad]))
            log("config.multi.env", bots=len(arr))
            return arr
        except Exception as e:
            raise RuntimeError(f"Failed to load LOCAL_BOTS_JSON: {e}")

    # 3) Nothing configured → hard fail (single mode removed)
    raise RuntimeError("No bot config. Set LOCAL_BOTS_FILE or LOCAL_BOTS_JSON (JSON array).")


# ─────────────────────────────────────────────────────────────────────
async def http_get_status(status_url: Optional[str]) -> str:
    if not status_url:
        return "LOCAL_STATUS_URL not set for this bot"
    try:
        timeout = httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(status_url)
            r.raise_for_status()
            return r.text if r.text else json.dumps(r.json(), ensure_ascii=False)
    except Exception as e:
        return f"처리 오류: {e}"


async def heartbeat_task(ws: websockets.WebSocketClientProtocol, interval: int, label: str) -> None:
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                await ws.send(json.dumps({"type": "ping", "ts": int(time.time())}))
                if LOG_HEARTBEAT:
                    log("ws.ping", label=label)
            except Exception:
                return
    except asyncio.CancelledError:
        return


async def run_client_once(bot: Dict[str, Any]) -> None:
    render_ws_url = bot.get("render_ws_url")
    status_url = bot.get("local_status_url")
    caps = bot.get("caps") or DEFAULT_CAPS
    auth_token = bot.get("auth_token")  # optional Authorization header

    host, bot_id = _host_and_id_from_url(render_ws_url)
    label = bot.get("name") or bot_id or host or "bot"
    redacted = _redact_token(render_ws_url)

    headers = []
    if auth_token:
        headers.append(("Authorization", f"Bearer {auth_token}"))

    async with websockets.connect(
        render_ws_url,
        ping_interval=None,  # app-level heartbeat only
        close_timeout=5,
        max_size=2 * 1024 * 1024,
        extra_headers=headers or None,
    ) as ws:
        await ws.send(json.dumps({"type": "hello", "caps": caps}))
        log("ws.connected", label=label, url=redacted, caps=caps)

        hb = asyncio.create_task(heartbeat_task(ws, HEARTBEAT_SEC, label))
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue

                mtype = msg.get("type")
                if mtype != "task":
                    if mtype == "ping":
                        await ws.send(json.dumps({"type": "pong", "ts": int(time.time())}))
                    continue

                cmd = msg.get("command")
                corr = msg.get("correlation_id")
                log("task.received", label=label, cmd=cmd, corr=corr)
                reply_text = "unsupported"

                if cmd == "STATUS_QUERY":
                    t0 = time.perf_counter()
                    reply_text = await http_get_status(status_url)
                    ms = round((time.perf_counter() - t0) * 1000)
                    log("task.status.done", label=label, corr=corr, ms=ms)

                if corr:
                    await ws.send(json.dumps({
                        "type": "result",
                        "correlation_id": corr,
                        "text": reply_text,
                    }))
                    log("task.replied", label=label, corr=corr, size=len(reply_text or ""))
        finally:
            hb.cancel()
            with contextlib.suppress(Exception):
                await hb


async def run_bot_supervisor(bot: Dict[str, Any]) -> None:
    label = bot.get("name") or _host_and_id_from_url(bot.get("render_ws_url"))[1] or "bot"
    retry = RETRY_SEC
    while True:
        try:
            await run_client_once(bot)
            retry = RETRY_SEC
        except (websockets.exceptions.ConnectionClosed, ConnectionError) as e:
            log("ws.closed", label=label, reason=str(e), next_retry_sec=round(retry, 1))
        except Exception as e:
            log("ws.error", label=label, error=str(e), next_retry_sec=round(retry, 1))
        await asyncio.sleep(retry + random.uniform(0, 0.5 * retry))
        retry = min(MAX_RETRY_SEC, max(RETRY_SEC, retry * 1.6))


async def main() -> None:
    bots = _load_bots_config()
    log("startup", bots=len(bots))
    await asyncio.gather(*(run_bot_supervisor(b) for b in bots))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Interrupted. Bye ✨")
