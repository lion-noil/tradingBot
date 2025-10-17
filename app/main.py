# app/main.py

import asyncio
import sys
from dotenv import load_dotenv
load_dotenv()
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
import os
import logging
import asyncio
from fastapi import FastAPI, Response, HTTPException,Request  # ← Response, HTTPException 추가
from core.trade_bot import TradeBot
from controllers.controller import BybitWebSocketController, BybitRestController
from asyncio import Queue
from utils.logger import setup_logger
from pydantic import BaseModel
from typing import Literal
from services.daily_report import init_daily_report_scheduler, run_daily_report_from_cache

class ManualOrderRequest(BaseModel):
    percent: float = 10  # 기본값: 10%
    symbol: str | None = None  # 여러 심볼 지원용 (선택)

class ManualCloseRequest(BaseModel):
    side: Literal["LONG", "SHORT"]
    symbol: str | None = None

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# system: 사람용 로그 + 텔레그램(원하면 레벨 높게), signals.jsonl 없음
system_logger = setup_logger(
    "system",
    logger_level=logging.DEBUG,
    console_level=logging.DEBUG,
    file_level=logging.INFO,
    enable_telegram=True,
    telegram_level=logging.INFO,
    exclude_sig_in_file=False,     # ✅ SIG도 파일에 포함(혹시 찍히더라도)
    telegram_mode="both",          # ✅ 사람용도, 만약 SIG가 있다면 그것도 함께
)

# 트레이딩 로거: 사람용은 파일, SIG는 텔레그램(+ signals.jsonl)
trading_logger = setup_logger(
    "trading",
    logger_level=logging.DEBUG,
    console_level=logging.DEBUG,
    file_level=logging.INFO,
    enable_telegram=True,
    telegram_level=logging.INFO,
    write_signals_file=True,       # ✅ signals.jsonl 생성
    signals_filename="signals.jsonl",
    exclude_sig_in_file=False,      # ✅ 사람용 파일에서 SIG 제외
    telegram_mode="both",
)

app = FastAPI()
manual_queue = Queue()
bot = None
bybit_websocket_controller = None
bybit_rest_controller = None
scheduler = None

async def bot_loop():
    global bot

    MIN_TICKS =bot.history_num

    while True:
        try:
            # 각 심볼의 가격을 한 번씩 기록
            for sym in bot.symbols:
                bot.record_price(sym)

            # 모든 심볼이 최소 MIN_TICKS 도달했는지 확인
            ready = all(len(bot.price_history[sym]) >= MIN_TICKS for sym in bot.symbols)
            if ready:
                system_logger.debug("✅ 데이터 준비 완료, 메인 루프 시작")
                break

            # 진행 상황 로깅(선택)
            missing = {sym: len(bot.price_history[sym]) for sym in bot.symbols if
                       len(bot.price_history[sym]) < MIN_TICKS}
            system_logger.debug(f"⏳ 데이터 준비 중... (부족: {missing})")

            await asyncio.sleep(0.5)
        except Exception as e:
            system_logger.error(f"❌ bot warmup 오류: {e}")
            await asyncio.sleep(1.0)

        # --- 메인 루프 ---
    while bot.running:
        try:
            await bot.run_once()
            await asyncio.sleep(0.5)
        except Exception as e:
            system_logger.error(f"❌ bot_loop 오류: {e}")
            await asyncio.sleep(10)


@app.on_event("startup")
async def startup_event():
    global bot, bybit_websocket_controller, bybit_rest_controller, scheduler
    system_logger.debug("🚀 FastAPI 기반 봇 서버 시작")

    symbols = ("BTCUSDT", "ETHUSDT","XAUTUSDT")

    bybit_websocket_controller = BybitWebSocketController(
        symbols=symbols,
        system_logger=system_logger
    )
    bybit_websocket_controller = BybitWebSocketController(symbols=symbols, system_logger = system_logger)
    bybit_rest_controller = BybitRestController(system_logger = system_logger)
    bot = TradeBot(
        bybit_websocket_controller,
        bybit_rest_controller,
        manual_queue,
        system_logger=system_logger,
        trading_logger=trading_logger,
        symbols=symbols,
    )

    asyncio.create_task(bot_loop())
    scheduler = init_daily_report_scheduler(lambda: bot, symbols=symbols, system_logger=system_logger)

@app.get("/info")
async def status(symbol: str = "BTCUSDT", plain: bool = True):
    if bot is None:
        raise HTTPException(status_code=503, detail="Bot not initialized yet")

    # 심볼 유효성 체크
    if symbol not in bot.symbols:
        raise HTTPException(status_code=400, detail=f"Unknown symbol: {symbol}. Available: {bot.symbols}")

    status_text = bot.make_status_log_msg(symbol)
    if plain:
        return Response(content=status_text, media_type="text/plain")
    return {"symbol": symbol, "message": status_text}
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

# app/main.py - 수동 트리거용 엔드포인트(원하면)
@app.get("/dailyreport")
async def trigger_daily_report(symbol: str = "BTCUSDT"):
    # 심볼 유효성 체크(보고서도 심볼별)
    if bot is None:
        raise HTTPException(status_code=503, detail="Bot not initialized yet")
    if symbol not in bot.symbols:
        raise HTTPException(status_code=400, detail=f"Unknown symbol: {symbol}. Available: {bot.symbols}")

    try:
        result = run_daily_report_from_cache(lambda: bot, symbol=symbol, system_logger=system_logger)
        return {"status": "ok", "rows": result.get("count")}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)