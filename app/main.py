# app/main.py

import asyncio
import sys
from dotenv import load_dotenv
load_dotenv()
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
import os
import asyncio
from fastapi import FastAPI, Response, HTTPException,Request  # ← Response, HTTPException 추가
from core.trade_bot import TradeBot
from controllers.controller import BybitWebSocketController, BybitRestController
from asyncio import Queue
from utils.logger import setup_logger
from pydantic import BaseModel
from typing import Literal

class ManualOrderRequest(BaseModel):
    percent: float = 10  # 기본값: 10%
class ManualCloseRequest(BaseModel):
    side: Literal["LONG", "SHORT"]

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
logger = setup_logger()

app = FastAPI()
manual_queue = Queue()
bot = None
bybit_websocket_controller = None
bybit_rest_controller = None

async def bot_loop():
    global bot

    while True:
        bot.record_price()
        if (
                bot.price_history
                and len(bot.price_history) == bot.price_history.maxlen
        ):
            logger.debug("✅ 데이터 준비 완료, 메인 루프 시작")
            break
        logger.debug("⏳ 데이터 준비 중...")
        await asyncio.sleep(0.5)

    while bot.running:
        try:
            await bot.run_once()
            await asyncio.sleep(0.5)

        except Exception as e:
            logger.error(f"❌ bot_loop 오류: {e}")
            await asyncio.sleep(10)


@app.on_event("startup")
async def startup_event():
    global bot, bybit_websocket_controller, bybit_rest_controller
    logger.debug("🚀 FastAPI 기반 봇 서버 시작")
    bybit_websocket_controller = BybitWebSocketController()
    bybit_rest_controller = BybitRestController()
    bot = TradeBot(bybit_websocket_controller, bybit_rest_controller, manual_queue)
    asyncio.create_task(bot_loop())

@app.get("/info")
async def status(symbol: str = "BTCUSDT", plain: bool = True):
    if bot is None:
        raise HTTPException(status_code=503, detail="Bot not initialized yet")
    if not bot.price_history:
        raise HTTPException(status_code=503, detail="Price history not ready")

    status_text = bot.make_status_log_msg()
    if plain:
        return Response(content=status_text, media_type="text/plain")
    return {
        "symbol": symbol,
        "message": status_text
    }
"""
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
"""
if __name__ == "__main__":

    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000 , reload=False)