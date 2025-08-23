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

# Capabilities (이제는 TEXT_COMMAND만 지원)
DEFAULT_CAPS = ["TEXT_COMMAND"]

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
    if (s.startswith("'") and s.endswith("'")) or (s.startswith('"') and s.endswith('"')):
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
    return None

def _load_bots_config() -> List[Dict[str, Any]]:
    # 1) File 우선
    if LOCAL_BOTS_FILE and os.path.exists(LOCAL_BOTS_FILE):
        with open(LOCAL_BOTS_FILE, encoding="utf-8") as f:
            arr = json.load(f)
        if not isinstance(arr, list):
            raise RuntimeError("LOCAL_BOTS_FILE must contain a JSON array")
        return arr

    # 2) Env JSON
    if LOCAL_BOTS_JSON:
        arr = json.loads(_strip_quotes(LOCAL_BOTS_JSON) or "[]")
        if not isinstance(arr, list):
            raise RuntimeError("LOCAL_BOTS_JSON must be a JSON array")
        return arr

    raise RuntimeError("No bot config. Set LOCAL_BOTS_FILE or LOCAL_BOTS_JSON (JSON array).")

# ─────────────────────────────────────────────────────────────────────
async def handle_text_command(bot: Dict[str, Any], text: str) -> str:
    """텔레그램 명령어를 로컬 서버에 전달하고 결과 반환"""
    status_url = bot.get("local_status_url")  # 예: http://127.0.0.1:8000/status
    base = status_url.rsplit("/", 1)[0] if status_url else None

    if not base:
        return "❌ 이 봇에는 local server가 연결되어 있지 않습니다."

    txt = text.strip().lstrip("/")  # "/status" → "status"
    route = txt.split()[0]          # "/long 20" → "long"
    url = f"{base}/{route}"

    try:
        timeout = httpx.Timeout(connect=3.0, read=10.0, write=5.0, pool=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            if route in ("long", "short"):
                parts = text.split()
                percent = float(parts[1]) if len(parts) > 1 else 10
                r = await client.post(url, json={"percent": percent})
            elif route == "close":
                parts = text.split()
                side = parts[1].upper() if len(parts) > 1 else "LONG"
                r = await client.post(url, json={"side": side})
            else:
                # 기본 GET
                r = await client.get(url, params={"plain": "true"})

            return r.text if r.status_code == 200 else f"❓ 명령 오류: {r.status_code}"
    except Exception as e:
        return f"❓ 처리 실패: {e}"

# ─────────────────────────────────────────────────────────────────────
async def run_client_once(bot: Dict[str, Any]) -> None:
    render_ws_url = bot.get("render_ws_url")
    caps = bot.get("caps") or DEFAULT_CAPS
    auth_token = bot.get("auth_token")

    host, bot_id = _host_and_id_from_url(render_ws_url)
    label = bot.get("name") or bot_id or host or "bot"
    redacted = _redact_token(render_ws_url)

    headers = []
    if auth_token:
        headers.append(("Authorization", f"Bearer {auth_token}"))

    async with websockets.connect(
        render_ws_url,
        ping_interval=None,
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

                if msg.get("type") != "task":
                    if msg.get("type") == "ping":
                        await ws.send(json.dumps({"type": "pong", "ts": int(time.time())}))
                    continue

                cmd = msg.get("command")
                corr = msg.get("correlation_id")
                payload = msg.get("payload") or {}
                log("task.received", label=label, cmd=cmd, corr=corr)

                reply_text = "unsupported"
                if cmd == "TEXT_COMMAND":
                    reply_text = await handle_text_command(bot, payload.get("text", ""))

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

# ─────────────────────────────────────────────────────────────────────
async def heartbeat_task(ws, interval: int, label: str):
    try:
        while True:
            await asyncio.sleep(interval)
            await ws.send(json.dumps({"type": "ping", "ts": int(time.time())}))
            if LOG_HEARTBEAT:
                log("ws.ping", label=label)
    except asyncio.CancelledError:
        return

async def run_bot_supervisor(bot: Dict[str, Any]) -> None:
    label = bot.get("name") or _host_and_id_from_url(bot.get("render_ws_url"))[1] or "bot"
    retry = RETRY_SEC
    while True:
        try:
            await run_client_once(bot)
            retry = RETRY_SEC
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
