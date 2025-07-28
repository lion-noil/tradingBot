# app/main.py

import asyncio
import sys
from dotenv import load_dotenv
load_dotenv()
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI
from core.trade_bot import TradeBot
from controllers.controller import CoinFuturesController,BybitWebSocketController, BybitRestController
from asyncio import Queue
from utils.logger import setup_logger
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
bybit_websocket_controller = None
bybit_rest_controller = None

async def bot_loop():
    global bot
    while bot.running:
        try:
            await bot.run_once()
            await asyncio.sleep(10)

        except Exception as e:
            logger.error(f"❌ bot_loop 오류: {e}")
            await asyncio.sleep(10)


@app.on_event("startup")
async def startup_event():
    global bot, controller,bybit_websocket_controller, bybit_rest_controller
    logger.info("🚀 FastAPI 기반 봇 서버 시작")
    controller = CoinFuturesController()
    bybit_websocket_controller = BybitWebSocketController()
    bybit_rest_controller = BybitRestController()
    bot = TradeBot(controller, bybit_websocket_controller, bybit_rest_controller, manual_queue)
    asyncio.create_task(bot_loop())

    status = controller.get_current_position_status()
    logger.info(controller.make_status_log_msg(status) + '\n')
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