"""
import plotly.graph_objects as go
from datetime import datetime, timedelta
import plotly.io as pio

def plot_ma_bands_web(closes, ma100s, threshold):
    closes = list(closes)
    ma100s = list(ma100s)

    now = datetime.now()
    times = [now - timedelta(minutes=len(closes)-i) for i in range(len(closes))]

    # None 값 방어 처리
    upper = [ma * (1 + threshold) if ma is not None else None for ma in ma100s]
    lower = [ma * (1 - threshold) if ma is not None else None for ma in ma100s]

    fig = go.Figure()

    fig.add_trace(go.Scatter(x=times, y=closes, mode="lines", name="Close"))
    fig.add_trace(go.Scatter(x=times, y=ma100s, mode="lines", name="MA100"))
    fig.add_trace(go.Scatter(x=times, y=upper, mode="lines", name="Upper", line=dict(dash="dot", color="green")))
    fig.add_trace(go.Scatter(x=times, y=lower, mode="lines", name="Lower", line=dict(dash="dot", color="red")))

    fig.update_layout(
        title="Close vs MA100 ± Threshold",
        xaxis_title="Time",
        yaxis_title="Price",
        hovermode="x unified"
    )

    # 브라우저로 바로 띄우기
    pio.renderers.default = "browser"
    fig.show()

# 사용 예시
plot_ma_bands_web(closes, ma100s, threshold=optimal)
"""

import os, json, requests, sys
from dotenv import load_dotenv

load_dotenv()

TOKEN          = os.getenv("TELEGRAM_BOT_TOKEN")
BASE           = os.getenv("NGROK_URL", "").rstrip("/")
SECRET_PATH    = os.getenv("WEBHOOK_SECRET", "")
HEADER_SECRET  = os.getenv("TG_HEADER_SECRET", "")
FORCE_RENEW    = (os.getenv("FORCE_RENEW", "false").lower() in ("1","true","yes"))

assert TOKEN, "TELEGRAM_BOT_TOKEN 이 필요합니다."

def find_ngrok_https():
    """NGROK_URL이 없으면 로컬 4040 API에서 https public_url을 자동 탐지"""
    try:
        resp = requests.get("http://127.0.0.1:4040/api/tunnels", timeout=2).json()
        for t in resp.get("tunnels", []):
            pub = t.get("public_url") or ""
            if pub.startswith("https://"):
                return pub.rstrip("/")
    except Exception:
        pass
    raise RuntimeError("NGROK_URL이 없고 4040 API에서도 https 터널을 찾지 못했습니다.")

def desired_webhook_url():
    base = BASE or find_ngrok_https()
    assert base.startswith("https://"), "NGROK_URL은 반드시 https여야 합니다."
    url = f"{base}/telegram/webhook"
    if SECRET_PATH:
        url += f"/{SECRET_PATH}"
    return url

def get_webhook_info():
    r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getWebhookInfo", timeout=8)
    r.raise_for_status()
    return r.json()

def delete_webhook(drop_pending=True):
    params = {"drop_pending_updates": "true" if drop_pending else "false"}
    r = requests.get(f"https://api.telegram.org/bot{TOKEN}/deleteWebhook", params=params, timeout=8)
    return r.status_code, r.text

def set_webhook(url):
    payload = {
        "url": url,
        "drop_pending_updates": True,                 # 필요시 False로 바꾸세요
        "allowed_updates": ["message", "channel_post"]
    }
    if HEADER_SECRET:
        payload["secret_token"] = HEADER_SECRET
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/setWebhook", json=payload, timeout=8)
    return r.status_code, r.text

def main():
    want = desired_webhook_url()
    print(f"[want] {want}")

    info = get_webhook_info()
    curr = (info.get("result") or {}).get("url") or ""
    print(f"[current] {curr}")

    if FORCE_RENEW or (curr != want):
        print("[action] deleteWebhook → setWebhook")
        code_d, text_d = delete_webhook(drop_pending=True)
        print(f"[deleteWebhook] {code_d} {text_d}")

        code_s, text_s = set_webhook(want)
        print(f"[setWebhook] {code_s} {text_s}")

        info2 = get_webhook_info()
        print("[getWebhookInfo after set]",
              json.dumps(info2, ensure_ascii=False, indent=2))
        ok = info2.get("ok") and ((info2.get("result") or {}).get("url") == want)
        print("[result]", "OK" if ok else "CHECK NEEDED")
        sys.exit(0 if ok else 1)
    else:
        print("[action] 이미 동일 URL. 변경 없음.")
        print("[getWebhookInfo]", json.dumps(info, ensure_ascii=False, indent=2))
        sys.exit(0)

if __name__ == "__main__":
    main()
