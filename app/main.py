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

    # 🟢 웜업 단계: 최신가, ma100, ma_threshold 준비될 때까지 대기
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

    _, latest_price = bot.price_history[-1]
    status_text = bot.bybit_rest_controller.make_status_log_msg(
        bot.status, latest_price, bot.now_ma100, bot.prev,
        bot.ma_threshold,bot.momentum_threshold, bot.target_cross, bot.closes_num,bot.exit_ma_threshold
    )
    min_sec = 0.5
    max_sec = 2
    jump_state, min_dt, max_dt = bot.check_price_jump(min_sec, max_sec)

    extra_line = (
        f"\n⏱️ 감시 구간(±{bot.ma_threshold * 100:.3f}%)\n"
        f"  • 체크 구간 : {min_sec:.1f}초 ~ {max_sec:.1f}초\n"
    )
    if jump_state is True:
        extra_line += f"  • 상태      : 👀 감시 중\n"
    if min_dt is not None and max_dt is not None:
        extra_line += f"  • 데이터간격 : 최소 {min_dt:.3f}s / 최대 {max_dt:.3f}s\n"


    status_text = f"{status_text}{extra_line}"
    if plain:
        return Response(content=status_text, media_type="text/plain")
    return {
        "symbol": symbol,
        "latest_price": latest_price,
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