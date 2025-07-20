# test_balance.py

from binance.client import Client
from dotenv import load_dotenv
import os

load_dotenv()

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")

client = Client(API_KEY, API_SECRET)

# ✅ 정확한 테스트넷 선물 API URL 설정
client.FUTURES_URL = "https://testnet.binancefuture.com/fapi"

import datetime

def print_futures_positions_detail():
    try:
        positions = client.futures_position_information()
        print("\n📊 현재 포지션 상세:")
        has_pos = False

        for pos in positions:
            amt = float(pos["positionAmt"])
            if amt != 0.0:
                has_pos = True
                symbol = pos["symbol"]
                entry = float(pos["entryPrice"])
                unreal = float(pos["unRealizedProfit"])
                direction = "롱" if amt > 0 else "숏"

                # 진입 시점 찾기 (최근 체결 내역 중 진입 방향 체결)
                trades = client.futures_account_trades(symbol=symbol)
                # 포지션에 영향 준 마지막 체결 찾기
                entry_trade = None
                for t in reversed(trades):
                    # 롱은 BUY, 숏은 SELL일 때 포지션 증가
                    if (amt > 0 and t["side"] == "BUY") or (amt < 0 and t["side"] == "SELL"):
                        entry_trade = t
                        break
                if entry_trade:
                    ts = int(entry_trade["time"]) // 1000
                    dt = datetime.datetime.fromtimestamp(ts)
                    entry_time_str = dt.strftime('%Y-%m-%d %H:%M:%S')
                else:
                    entry_time_str = "N/A"

                print(
                    f"  - {symbol} | {direction} | 수량: {amt} | 진입가: {entry} | "
                    f"진입시각: {entry_time_str} | 미실현PnL: {unreal:,.4f}"
                )
        if not has_pos:
            print("  (현재 보유 포지션 없음)")
    except Exception as e:
        print("❌ 포지션 상세 조회 실패:", e)

if __name__ == "__main__":
    print_futures_positions_detail()


