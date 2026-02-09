import socket, json, time
from datetime import datetime

HOST, PORT = "127.0.0.1", 9009

# ✅ 청산 대상이 되는 "ENTRY의 signal_id"를 여기에 넣어야 함
OPEN_SIGNAL_ID = "test_160836_074"   # <- 네가 열었던 엔트리 신호 id로 바꿔

exit_sid = datetime.now().strftime("test_exit_%H%M%S_%f")[:-3]

msg = {
    "source": "BYBIT",
    "symbol": "SOLUSDT",
    "action": "EXIT",
    "side": "LONG",
    "signal_id": exit_sid,                # EXIT 이벤트 자체 id (매번 달라야 dup 안 걸림)
    "close_open_signal_id": OPEN_SIGNAL_ID,  # ✅ 어떤 포지션(로트) 청산할지
    "ts_ms": int(time.time() * 1000),
}

s = socket.socket()
s.connect((HOST, PORT))
s.sendall((json.dumps(msg) + "\n").encode())
s.close()

print("sent EXIT signal_id:", exit_sid, "close_open_signal_id:", OPEN_SIGNAL_ID)
