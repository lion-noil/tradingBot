# app/main.py
import asyncio
import sys

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI
from core.trade_bot import TradeBot
from controllers.bybit_controller import BybitController
from core.data_fetcher import get_real_data
from asyncio import Queue  # ✅ async 환경에서 적합한 큐
from utils.logger import setup_logger

logger = setup_logger()

app = FastAPI()
manual_queue = Queue()
bot = None
controller = None

async def bot_loop():
    global bot
    while bot.running:
        price_now, ma, prev = get_real_data()
        logger.debug(
            f"💹 현재가: {price_now}, MA100: {ma}, 3분전: {prev} | " +
            (f"📈 포지션: {bot.position.upper()} 진입시간: {bot.position_time.strftime('%H:%M:%S')}"
             if bot.position else "📉 포지션 없음")
        )
        await bot.run_once(price_now, ma, prev)
        await asyncio.sleep(5)

@app.on_event("startup")
async def startup_event():
    global bot, controller
    logger.info("🚀 FastAPI 기반 봇 서버 시작")
    controller = BybitController()
    await controller.init()
    bot = TradeBot(controller, manual_queue)
    asyncio.create_task(bot_loop())

@app.get("/status")
async def status():
    if bot is None:
        return {"error": "Bot not initialized yet"}
    return {
        "position": bot.position,
        "position_time": bot.position_time.strftime('%Y-%m-%d %H:%M:%S') if bot.position_time else None
    }

@app.post("/long")
async def manual_buy():
    await manual_queue.put("long")
    return {"status": "buy triggered"}

@app.post("/short")
async def manual_sell():
    await manual_queue.put("short")
    return {"status": "sell triggered"}

@app.post("/close")
async def manual_close():
    await manual_queue.put("close")
    return {"status": "close triggered"}
