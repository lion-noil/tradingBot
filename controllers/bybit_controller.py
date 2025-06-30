from playwright.async_api import async_playwright
import asyncio
from utils.logger import logger


class BybitController:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

    async def init(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.connect_over_cdp("http://localhost:9222")
        self.context = self.browser.contexts[0]
        self.page = self.context.pages[0]

    async def _select_100_percent_market(self):
        await self.page.click('text="100%"')
        await self.page.click('text="시장가"')

    async def _confirm_order(self):
        await self.page.click('div.GmCfm.Show >> a._OK')

    async def buy_market_100(self, price=None, ma=None):
        try:
            logger.info("🟩 매수 시작")
            await self._select_100_percent_market()
            await self.page.click('text="매수 / Long"')
            await self._confirm_order()
            logger.info(f"✅ 매수 완료 @ {price} (MA100: {ma})")
        except Exception as e:
            logger.error(f"❌ 매수 오류: {e}")

    async def sell_market_100(self, price=None, ma=None):
        try:
            logger.info("🟥 매도 시작")
            await self._select_100_percent_market()
            await self.page.click('text="매도 / Short"')
            await self._confirm_order()
            logger.info(f"✅ 매도 완료 @ {price} (MA100: {ma})")
        except Exception as e:
            logger.error(f"❌ 매도 오류: {e}")

    async def close_position_market(self):
        try:
            logger.info("📉 포지션 청산 시작")
            await self.page.click('td._OFunc a[data="B"]')
            await self.page.wait_for_selector('div.GmCfm.Show >> a._OK', timeout=3000)
            await self.page.click('div.GmCfm.Show >> a._OK')
            logger.info("✅ 청산 완료")
        except Exception as e:
            logger.error(f"❌ 청산 오류: {e}")

    async def close(self):
        await self.browser.close()
        await self.playwright.stop()
