import websocket
import json
import time
import hmac
import hashlib
import threading
import os
import requests
from dotenv import load_dotenv

# 🔐 실계정 API 키 로드
load_dotenv()
API_KEY = os.getenv("BYBIT_TEST_API_KEY")
API_SECRET = os.getenv("BYBIT_TEST_API_SECRET")

# ✅ 실계정 REST / WS URL
BASE_URL = "https://api.bybit.com"
WS_URL = "wss://stream.bybit.com/v5/private"

# demo REST / WS URL
BASE_URL = "https://api-demo.bybit.com"
WS_URL = "wss://stream-demo.bybit.com/v5/private"

# 📦 전역 변수
latest_position_data = None
latest_wallet_data = None
lock = threading.Lock()

# 🔑 WebSocket 서명 생성
def generate_ws_signature(api_secret, expires):
    message = f"GET/realtime{expires}"
    return hmac.new(
        api_secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

# 🕒 서버 시간
def get_server_time():
    try:
        res = requests.get(f"{BASE_URL}/v5/market/time")
        return int(res.json()["result"]["timeNano"]) // 1_000_000
    except:
        return int(time.time() * 1000)

# 🔌 WebSocket 콜백
def on_open(ws):
    print("🟢 WebSocket opened")
    expires = get_server_time() + 10_000
    signature = generate_ws_signature(API_SECRET, expires)
    auth_msg = {
        "op": "auth",
        "args": [API_KEY, expires, signature]
    }
    ws.send(json.dumps(auth_msg))

def on_message(ws, message):
    global latest_position_data, latest_wallet_data
    msg = json.loads(message)
    print("📩 MESSAGE:", json.dumps(msg, indent=2))

    if msg.get("op") == "auth" and msg.get("success"):
        print("✅ WebSocket 인증 성공")
        ws.send(json.dumps({
            "op": "subscribe",
            "args": ["position", "wallet"]
        }))
        print("📡 Subscribed to position & wallet")

    topic = msg.get("topic")
    if topic == "position":
        with lock:
            latest_position_data = msg["data"]
    elif topic == "wallet":
        with lock:
            latest_wallet_data = msg["data"]

def on_error(ws, error):
    print("❌ WebSocket ERROR:", error)

def on_close(ws, code, msg):
    print("🔌 WebSocket CLOSED", code, msg)

def get_server_timestamp():
    try:
        res = requests.get(f"{BASE_URL}/v5/market/time")
        return str(int(res.json()["result"]["timeNano"]) // 1_000_000)
    except:
        return str(int(time.time() * 1000))  # fallback

# 🌐 REST 포지션
def get_rest_position(symbol="BTCUSDT", category="linear"):
    url = f"{BASE_URL}/v5/position/list"
    timestamp = get_server_timestamp()
    params = {
        "api_key": API_KEY,
        "timestamp": timestamp,
        "category": category,
        "symbol": symbol,
        # "recv_window": 60000,  # 필요시 사용
    }
    param_str = '&'.join([f"{k}={v}" for k, v in sorted(params.items())])
    sign = hmac.new(
        API_SECRET.encode("utf-8"),
        param_str.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    params["sign"] = sign

    print("\n📡 REST 요청 중...")
    res = requests.get(url, params=params)
    print("🌐 REST 응답:")
    try:
        print(json.dumps(res.json(), indent=2))
    except:
        print("응답 파싱 실패:", res.text)

# 🕰️ 1초마다 데이터 출력
def print_stream_loop():
    while True:
        with lock:
            if latest_position_data:
                print("\n📤 최신 포지션 정보:")
                print(json.dumps(latest_position_data, indent=2))

            if latest_wallet_data:
                print("\n💰 최신 지갑 정보:")
                print(json.dumps(latest_wallet_data, indent=2))

        time.sleep(1)

# ▶️ 실행부
if __name__ == "__main__":
    print("🟢 Mainnet REST + WebSocket 시작")

    # 1. REST 확인
    get_rest_position()

    # 2. WebSocket 연결
    ws = websocket.WebSocketApp(
        WS_URL,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    ws_thread = threading.Thread(target=ws.run_forever)
    ws_thread.daemon = True
    ws_thread.start()

    # 3. 실시간 출력 루프
    print_thread = threading.Thread(target=print_stream_loop)
    print_thread.daemon = True
    print_thread.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("👋 종료 요청됨")
        ws.close()
