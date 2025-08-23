import os
import json
import asyncio
import time
from typing import Optional
import random
import contextlib
from urllib.parse import urlparse, parse_qsl, urlencode  # NEW
import logging, sys  # NEW

import websockets  # pip install websockets
import httpx       # pip install httpx
from dotenv import load_dotenv  # pip install python-dotenv

load_dotenv()

# ─────────────────────────────────────────────────────────────────────
# ENV
#   RENDER_WS_URL: e.g. wss://telewebhook.onrender.com/ws/pricing-bot?token=changeme1
#   LOCAL_STATUS_URL: e.g. http://127.0.0.1:8000/status?plain=true
RENDER_WS_URL: Optional[str] = os.getenv("RENDER_WS_URL")
LOCAL_STATUS_URL: Optional[str] = os.getenv("LOCAL_STATUS_URL")

# App-level heartbeat (seconds)
HEARTBEAT_SEC = int(os.getenv("HEARTBEAT_SEC", "15"))
# Initial reconnect delay (seconds). Will back off with jitter up to MAX_RETRY_SEC
RETRY_SEC = float(os.getenv("RETRY_SEC", "3"))
MAX_RETRY_SEC = float(os.getenv("MAX_RETRY_SEC", "30"))

# Logging controls
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()  # NEW
LOG_HEARTBEAT = os.getenv("LOG_HEARTBEAT", "0") == "1"  # NEW

# Caps this local bot supports
CAPS = ["STATUS_QUERY"]

# ─────────────────────────────────────────────────────────────────────
# Logging setup (간단 logfmt 스타일)  # NEW
logger = logging.getLogger("wsbot")
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_handler)

def log(event: str, **fields):  # NEW
    kv = " ".join(f"{k}={repr(v)}" for k, v in fields.items() if v is not None)
    logger.info(f"{event} {kv}")

def _redact_token(url: Optional[str]) -> Optional[str]:  # NEW
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

def _bot_info_from_url(url: Optional[str]) -> tuple[Optional[str], Optional[str]]:  # NEW
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

# ─────────────────────────────────────────────────────────────────────
async def get_status_text() -> str:
    """Call your local status endpoint asynchronously.
    Returns plain text. Never raises: converts errors to a message string."""
    if not LOCAL_STATUS_URL:
        return "LOCAL_STATUS_URL env not set"

    try:
        # Separate connect/read timeouts for clarity (connect, read)
        timeout = httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(LOCAL_STATUS_URL)
            r.raise_for_status()
            # Prefer text; fall back to JSON pretty if content-type misleads
            return r.text if r.text else json.dumps(r.json(), ensure_ascii=False)
    except Exception as e:
        return f"처리 오류: {e}"

async def heartbeat_task(ws: websockets.WebSocketClientProtocol, interval: int) -> None:
    """Application-level heartbeat to keep some PaaS proxies happy.
    Sends a tiny JSON ping every `interval` seconds."""
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                await ws.send(json.dumps({"type": "ping", "ts": int(time.time())}))
                if LOG_HEARTBEAT:  # NEW
                    log("ws.ping")
            except Exception:
                # Let outer loop handle reconnection
                return
    except asyncio.CancelledError:
        # Normal on disconnect
        return

async def run_client_once() -> None:
    if not RENDER_WS_URL:
        logger.error("RENDER_WS_URL env not set")  # NEW
        await asyncio.sleep(5)
        return

    host, bot_id = _bot_info_from_url(RENDER_WS_URL)  # NEW
    redacted = _redact_token(RENDER_WS_URL)  # NEW

    # Disable websockets' built-in ping frames (we use app-level heartbeat)
    # Some proxies ignore control frames but keep app data flowing.
    async with websockets.connect(
        RENDER_WS_URL,
        ping_interval=None,
        close_timeout=5,
        max_size=2 * 1024 * 1024,  # 2MB safety
    ) as ws:
        # Register capabilities
        await ws.send(json.dumps({"type": "hello", "caps": CAPS}))
        log("ws.connected", host=host, bot_id=bot_id, url=redacted)  # NEW

        # Start heartbeat
        hb = asyncio.create_task(heartbeat_task(ws, HEARTBEAT_SEC))

        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    # Ignore non-JSON frames
                    continue

                mtype = msg.get("type")
                if mtype != "task":
                    # Optional: respond to server pings if any
                    if mtype == "ping":
                        await ws.send(json.dumps({"type": "pong", "ts": int(time.time())}))
                    continue

                # Handle task
                cmd = msg.get("command")
                corr = msg.get("correlation_id")
                log("task.received", cmd=cmd, corr=corr)  # NEW
                reply_text = "unsupported"

                if cmd == "STATUS_QUERY":
                    t0 = time.perf_counter()  # NEW
                    reply_text = await get_status_text()
                    ms = round((time.perf_counter() - t0) * 1000)  # NEW
                    log("task.status.done", corr=corr, ms=ms)  # NEW

                # Send result (always attempt to reply if correlation exists)
                if corr:
                    await ws.send(json.dumps({
                        "type": "result",
                        "correlation_id": corr,
                        "text": reply_text,
                    }))
                    log("task.replied", corr=corr, size=len(reply_text or ""))  # NEW
        finally:
            hb.cancel()
            with contextlib.suppress(Exception):
                await hb

# ─────────────────────────────────────────────────────────────────────
async def main() -> None:
    retry = RETRY_SEC
    while True:
        try:
            await run_client_once()
            # If run_client_once returns normally, reset backoff
            retry = RETRY_SEC
        except (websockets.exceptions.ConnectionClosed, ConnectionError) as e:
            logger.warning(f"WS closed: {e}. Reconnecting in {retry:.1f}s…")  # NEW
        except Exception as e:
            logger.error(f"WS error: {e}. Reconnecting in {retry:.1f}s…")  # NEW

        # Exponential backoff with jitter up to MAX_RETRY_SEC
        await asyncio.sleep(retry + random.uniform(0, 0.5 * retry))
        retry = min(MAX_RETRY_SEC, max(RETRY_SEC, retry * 1.6))

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Interrupted. Bye ✨")
