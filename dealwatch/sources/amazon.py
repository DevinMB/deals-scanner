"""Amazon search via the shared Playwright browser (plain HTTP gets a 503)."""

import random
import re
import urllib.parse

from ..models import Watch
from . import Listing, Source, log, money

EXTRACT_JS = """() => [...document.querySelectorAll('[data-component-type=s-search-result]')].map(c => ({
  asin: c.dataset.asin,
  title: ((c.querySelector('[data-cy=title-recipe]') || {}).innerText || ''),
  price: (c.querySelector('[data-cy=price-recipe] .a-price:not(.a-text-price) .a-offscreen') || {}).textContent || '',
  priceText: ((c.querySelector('[data-cy=price-recipe]') || {}).innerText || ''),
}))"""


class AmazonBlocked(RuntimeError):
    pass


class Amazon(Source):
    name = "amazon"
    label = "Amazon"
    local = False
    store = True
    needs_browser = True
    query_delay = (5, 10)

    def search(self, query: str, watch: Watch) -> list[Listing]:
        page = self.ctx.browser.new_page()
        url = "https://www.amazon.com/s?" + urllib.parse.urlencode({"k": query})
        try:
            for attempt in range(2):  # captchas come and go: back off, warm up on the homepage, retry once
                page.goto(url, timeout=45000)
                page.wait_for_timeout(2500 + random.randint(0, 1500))
                blocked = bool(page.query_selector("form[action*=validateCaptcha]")) or page.title().lower().startswith("sorry")
                if not blocked:
                    break
                if attempt == 0:
                    page.wait_for_timeout(random.uniform(20, 30) * 1000)
                    page.goto("https://www.amazon.com/", timeout=45000)
                    page.wait_for_timeout(3000)
            if blocked:
                raise AmazonBlocked(f"Amazon captcha (rate-limited) for {query!r}")
            cards = page.evaluate(EXTRACT_JS)
        finally:
            page.close()
        if not cards:
            log.warning("amazon returned 0 results for %r", query)
        return [x for x in map(self._parse, cards) if x]

    @staticmethod
    def _parse(c: dict) -> Listing | None:
        lines = [l.strip() for l in c["title"].split("\n") if l.strip()]
        sponsored = bool(lines) and lines[0].lower() == "sponsored"
        title = " ".join(lines[1:] if sponsored else lines)
        price = money(c["price"])
        if not c["asin"] or not title or not price:
            return None
        typical = re.search(r"(?:Typical|List):\s*\$([\d,]+(?:\.\d\d)?)", c["priceText"])
        return Listing(source="amazon", source_id=c["asin"], title=title, url=f"https://www.amazon.com/dp/{c['asin']}",
                       price_total=price, extra={"sponsored": sponsored,
                                                 "store_typical": float(typical.group(1).replace(",", "")) if typical else None})
