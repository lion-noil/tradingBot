# app/main.py

import asyncio
import sys
from dotenv import load_dotenv
load_dotenv()
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI
from core.trade_bot import TradeBot
from controllers.binance_controller import BinanceFuturesController
from asyncio import Queue
from utils.logger import setup_logger
from datetime import datetime
from pydantic import BaseModel
from typing import Literal
class ManualOrderRequest(BaseModel):
    percent: float = 10  # 기본값: 10%
class ManualCloseRequest(BaseModel):
    side: Literal["LONG", "SHORT"]

logger = setup_logger()

app = FastAPI()
manual_queue = Queue()
bot = None
controller = None

async def bot_loop():
    global bot
    while bot.running:
        try:
            price_now, ma, prev = bot.get_real_data()
            status = bot.get_current_position_status()  # balance + positions 포함
            status_list = status.get("positions", [])
            balance = status.get("balance", {})

            # 잔고 정보
            total = balance.get("total", 0.0)
            available = balance.get("available", 0.0)
            upnl = balance.get("unrealized_pnl", 0.0)
            leverage = balance.get("leverage", 0)
            # 로그 시작
            log_msg = f"💹 현재가: {price_now}, MA100: {ma}, 3분전: {prev} \n"
            log_msg += f"💰 자산: 총 {total:.2f} USDT / 사용 가능: {available:.2f} / 미실현 손익: {upnl:+.2f} (레버리지: {leverage}x)\n"

            if status_list:
                for status in status_list:

                    log_msg += f"  📈 포지션: {status['position']} ({status['position_amt']}) /"
                    log_msg += f" 평균가: {status['entryPrice']:.3f} /"
                    log_msg += f" 수익률: {status['profit_rate']:.3f}% /"
                    log_msg += f" 수익금: {status['unrealized_profit']:+.3f} USDT\n"

                    if status["entries"]:
                        for i, (timestamp, qty, entryPrice) in enumerate(status["entries"], start=1):
                            t_str = datetime.fromtimestamp(timestamp / 1000).strftime("%Y-%m-%d %H:%M:%S")
                            signed_qty = -qty if status["position"] == "SHORT" else qty
                            log_msg += f"        └ 진입시간 #{i}: {t_str} ({signed_qty:.3f} BTC), 진입가 : {entryPrice:.2f} \n"
                    else:
                        log_msg += f"        └ 진입시간: 없음\n"

            else:
                log_msg += "📉 포지션 없음\n"
            logger.debug(log_msg.rstrip())

            await bot.run_once(price_now, ma, prev, status_list,balance)
            await asyncio.sleep(5)

        except Exception as e:
            logger.error(f"❌ bot_loop 오류: {e}")
            await asyncio.sleep(5)


@app.on_event("startup")
async def startup_event():
    global bot, controller
    logger.info("🚀 FastAPI 기반 봇 서버 시작")
    controller = BinanceFuturesController()  # ✅ 동기 방식, await 필요 없음
    bot = TradeBot(controller, manual_queue)
    asyncio.create_task(bot_loop())

@app.get("/status")
async def status():
    if bot is None:
        return {"error": "Bot not initialized yet"}
    return bot.get_current_position_status()

@app.post("/long")
async def manual_buy(request: ManualOrderRequest):
    await manual_queue.put({"command": "long", "percent": request.percent})
    return {"status": f"buy triggered with {request.percent}%"}

@app.post("/short")
async def manual_sell(request: ManualOrderRequest):
    await manual_queue.put({"command": "short", "percent": request.percent})
    return {"status": f"sell triggered with {request.percent}%"}
@app.post("/close")
async def manual_close(request: ManualCloseRequest):
    await manual_queue.put({"command": "close", "side": request.side})
    return {"status": f"close triggered for {request.side}"}

if __name__ == "__main__":

    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)