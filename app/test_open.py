import socket, json, time
from datetime import datetime

# 시분초( + 밀리초까지 )로 매번 다른 signal_id 생성
sid = datetime.now().strftime("test_%H%M%S_%f")[:-3]  # 예: test_long_220512_123

msg = {
    "source": "MT5",
    "symbol": "BTCUSD",
    "action": "ENTRY",
    "side": "LONG",
    "price": 43000,
    "signal_id": sid,
    "ts_ms": int(time.time() * 1000),
}

s = socket.socket()
s.connect(("127.0.0.1", 9010))
s.sendall((json.dumps(msg) + "\n").encode())
s.close()

print("sent signal_id:", sid)
