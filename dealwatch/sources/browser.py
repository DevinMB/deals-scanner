"""One shared Chromium per run, set up the way the GPU scanner learned works against eBay/OfferUp."""

import re

from . import UA


class Browser:
    def __init__(self, home: dict):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(channel="chromium",
                                                args=["--disable-blink-features=AutomationControlled"])
        # OfferUp results follow the browser's location, so pin it to home
        self.ctx = self.browser.new_context(user_agent=UA, locale="en-US", viewport={"width": 1366, "height": 900},
                                            geolocation={"latitude": home["lat"], "longitude": home["lon"]},
                                            permissions=["geolocation"])
        self.ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        self.ctx.route(re.compile(r"\.(png|jpe?g|webp|gif|woff2?|mp4)(\?|$)"), lambda r: r.abort())

    def new_page(self):
        return self.ctx.new_page()

    def close(self):
        try:
            self.browser.close()
        finally:
            self._pw.stop()
