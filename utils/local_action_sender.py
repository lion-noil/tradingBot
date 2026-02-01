# utils/local_action_sender.py
import asyncio, json
from typing import Any, Dict, Optional

class LocalActionSender:
    def __init__(self, host: str = "127.0.0.1", port: int = 9009):
        self.host = host
        self.port = port
        self._writer: Optional[asyncio.StreamWriter] = None
        self._lock = asyncio.Lock()

    async def _ensure_conn(self):
        if self._writer is not None:
            return
        _, writer = await asyncio.open_connection(self.host, self.port)
        self._writer = writer

    async def send(self, payload: Dict[str, Any]):
        async with self._lock:
            try:
                await self._ensure_conn()
                assert self._writer is not None
                line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
                self._writer.write(line)
                await self._writer.drain()
            except Exception:
                # receiver 꺼져있어도 봇이 죽지 않게
                try:
                    if self._writer:
                        self._writer.close()
                        await self._writer.wait_closed()
                except Exception:
                    pass
                self._writer = None
