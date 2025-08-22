import os, json, asyncio, time
import websockets  # pip install websockets
import requests    # pip install requests
from dotenv import load_dotenv
load_dotenv()
    # 환경변수: 예) wss://telewebhook.onrender.com/ws/pricing-bot?token=changeme1
RENDER_WS_URL = os.getenv("RENDER_WS_URL")

def get_status_text():
    # 너의 로컬 봇이 제공하는 /status?plain=true 호출
    url = os.getenv("LOCAL_STATUS_URL", "http://127.0.0.1:8000/status?plain=true")
    r = requests.get(url, timeout=5)
    r.raise_for_status()
    return r.text

async def main():
    if not RENDER_WS_URL:
        print("RENDER_WS_URL env not set"); return
    caps = ["STATUS_QUERY"]
    while True:
        try:
            async with websockets.connect(RENDER_WS_URL, ping_interval=20, ping_timeout=20) as ws:
                # capabilities 등록
                await ws.send(json.dumps({"type":"hello","caps":caps}))
                print("WS connected to Render.")

                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("type") != "task":
                        continue
                    cmd = msg.get("command")
                    corr = msg.get("correlation_id")
                    reply_text = "unknown command"

                    try:
                        if cmd == "STATUS_QUERY":
                            reply_text = get_status_text()
                        else:
                            reply_text = "unsupported"
                    except Exception as e:
                        reply_text = f"처리 오류: {e}"

                    await ws.send(json.dumps({
                        "type":"result",
                        "correlation_id": corr,
                        "text": reply_text
                    }))
        except Exception as e:
            print("WS error:", e, "— retry in 3s")
            await asyncio.sleep(3)

if __name__ == "__main__":
    asyncio.run(main())
