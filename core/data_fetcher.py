import requests
from config import BINANCE_SYMBOL, BINANCE_API_URL

def get_real_data(symbol=BINANCE_SYMBOL):
    try:
        url = f"{BINANCE_API_URL}?symbol={symbol}&interval=1m&limit=100"
        res = requests.get(url, timeout=5)
        res.raise_for_status()
        candles = res.json()

        closes = [float(c[4]) for c in candles]
        ma100 = sum(closes) / len(closes)
        price_now = closes[-1]
        price_3min_ago = closes[-4]

        return round(price_now, 3), round(ma100, 3), round(price_3min_ago, 3)

    except Exception as e:
        print(f"❌ 실시간 데이터 가져오기 실패: {e}")
        return None, None, None