# controllers/bybit_controller.py
from playwright.sync_api import sync_playwright
from utils.logger import setup_logger

logger = setup_logger()

class BybitController:
    def __init__(self):
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.connect_over_cdp("http://localhost:9222")
        self.context = self.browser.contexts[0]
        self.page = self.context.pages[0]

    def _select_100_percent_market(self):
        self.page.click('text="100%"', timeout=2000)
        self.page.click('text="시장가"', timeout=2000)

    def _confirm_order(self):
        self.page.click('div.GmCfm.Show >> a._OK', timeout=2000)

    def buy_market_100(self, price_now=None, ma=None):
        try:
            logger.info("🟩 매수 시작")
            self._select_100_percent_market()
            self.page.click('text="매수 / Long"', timeout=2000)
            self._confirm_order()
            msg = "✅ 매수 완료"
            if price_now and ma:
                msg += f" @{price_now} (MA100: {ma})"
            logger.info(msg)
        except Exception as e:
            logger.error(f"❌ 매수 오류: {e}")

    def sell_market_100(self, price_now=None, ma=None):
        try:
            logger.info("🟥 매도 시작")
            self._select_100_percent_market()
            self.page.click('text="매도 / Short"', timeout=2000)
            self._confirm_order()
            msg = "✅ 매도 완료"
            if price_now and ma:
                msg += f" @{price_now} (MA100: {ma})"
            logger.info(msg)
        except Exception as e:
            logger.error(f"❌ 매도 오류: {e}")

    def close_position_market(self, price_now=None, ma=None):
        try:
            logger.info("📉 포지션 청산 시작")
            self.page.click('td._OFunc a[data="B"]', timeout=2000)
            self.page.wait_for_selector('div.GmCfm.Show >> a._OK', timeout=3000)
            self.page.click('div.GmCfm.Show >> a._OK', timeout=2000)
            msg = "✅ 청산 완료"
            if price_now and ma:
                msg += f" @{price_now} (MA100: {ma})"
            logger.info(msg)
        except Exception as e:
            logger.error(f"❌ 청산 오류: {e}")

    def close(self):
        self.browser.close()
        self.playwright.stop()
