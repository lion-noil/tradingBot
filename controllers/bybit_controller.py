# controllers/bybit_controller.py
from utils.logger import setup_logger
from playwright.async_api import async_playwright
logger = setup_logger()
class BybitController:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.page = None

    async def init(self):
        try:
            self.playwright = await async_playwright().start()
            # 🚨 CDP만 사용, subprocess 없이
            self.browser = await self.playwright.chromium.connect_over_cdp("http://localhost:9222")
            context = self.browser.contexts[0]
            self.page = context.pages[0]
            print("CDP 연결 성공zZZ")
        except Exception as e:
            print("❌ CDP 연결 실패:", e)
            raise

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

    async def close_position_market(self, price=None, ma=None):
        try:
            logger.info("📉 포지션 청산 시작")
            await self.page.click('td._OFunc a[data="B"]')
            await self.page.wait_for_selector('div.GmCfm.Show >> a._OK', timeout=3000)
            await self.page.click('div.GmCfm.Show >> a._OK')
            logger.info(f"✅ 청산 완료 @ {price} (MA100: {ma})")
        except Exception as e:
            logger.error(f"❌ 청산 오류: {e}")

    async def close(self):
        await self.browser.close()
        await self.playwright.stop()
