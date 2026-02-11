# utils/local_action_sender.py
import asyncio, json, time
from dataclasses import dataclass
from typing import Any, Dict, Optional, List

@dataclass(frozen=True)
class Target:
    host: str
    port: int

class _SingleConnSender:
    """
    단일 (host,port) 연결 담당:
    - send() 실패해도 예외 밖으로 안 던짐
    - 연결/끊김 로그는 상태변경 때만 1회
    - ping loop로 항상 연결을 만들고 유지/감시
    """
    def __init__(self, host: str, port: int, system_logger=None, ping_sec: float = 10.0):
        self.host = host
        self.port = port
        self.system_logger = system_logger
        self.ping_sec = float(ping_sec)

        self._writer: Optional[asyncio.StreamWriter] = None
        self._lock = asyncio.Lock()
        self._connected = False

        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    def _tag(self) -> str:
        return f"{self.host}:{self.port}"

    def start(self):
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._ping_loop())

    async def stop(self):
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except Exception:
                pass
        await self._close()

    async def _ensure_conn(self):
        if self._writer is not None:
            return
        _, w = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port),
            timeout=1.0,  # ✅ 여기!
        )
        self._writer = w
        if not self._connected:
            self._connected = True
            if self.system_logger:
                self.system_logger.debug(f"[sender] connected {self._tag()}")

    async def _close(self):
        w = self._writer
        self._writer = None
        if w:
            try:
                w.close()
                await w.wait_closed()
            except Exception:
                pass
        if self._connected:
            self._connected = False
            if self.system_logger:
                self.system_logger.debug(f"[sender] disconnected {self._tag()}")

    async def send(self, payload: Dict[str, Any]):
        async with self._lock:
            try:
                await self._ensure_conn()
                assert self._writer is not None
                line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
                self._writer.write(line)
                await self._writer.drain()
            except Exception:
                # receiver 꺼져도 봇이 죽지 않게: 여기서만 끊김 처리
                await self._close()

    async def _ping_loop(self):
        backoff = 0.5
        backoff_max = 10.0
        while not self._stop.is_set():
            try:
                await self.send({"type": "PING", "ts_ms": int(time.time() * 1000)})
                backoff = 0.5
                await asyncio.sleep(self.ping_sec)
            except asyncio.CancelledError:
                raise
            except Exception:
                # send 내부에서 close 처리됨. 재시도만.
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.7, backoff_max)

class LocalActionSender:
    """
    멀티 타겟 브로드캐스트 sender.
    - targets로 여러 포트 등록 가능(9009, 9008)
    - start() 호출 시 각 타겟별 ping loop 시작
    - send()는 모든 타겟에 병렬 전송(한쪽 실패가 다른쪽에 영향 X)
    """
    def __init__(self, targets: List[Target], system_logger=None, ping_sec: float = 10.0):
        self._senders = [
            _SingleConnSender(t.host, t.port, system_logger=system_logger, ping_sec=ping_sec)
            for t in (targets or [])
        ]

    def start(self):
        for s in self._senders:
            s.start()

    async def stop(self):
        await asyncio.gather(*[s.stop() for s in self._senders], return_exceptions=True)

    async def send(self, payload: Dict[str, Any]):
        await asyncio.gather(*[s.send(payload) for s in self._senders], return_exceptions=True)
