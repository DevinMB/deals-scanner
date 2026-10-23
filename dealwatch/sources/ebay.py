"""eBay via Playwright: plain HTTP and sold-listing searches are blocked."""

import random
import re
import urllib.parse

from ..models import Watch
from . import Listing, Source, log, money

EXTRACT_JS = """() => [...document.querySelectorAll('li.s-card')].map(c => {
  const q = s => c.querySelector(s);
  const rows = [...c.querySelectorAll('.s-card__attribute-row')].map(r => r.innerText.trim());
  const sec = q('.su-card-container__attributes__secondary');
  return {
    id: c.getAttribute('data-listingid'),
    title: (q('.s-card__title .su-styled-text') || {}).innerText || '',
    cond: [...c.querySelectorAll('.s-card__subtitle')].map(e => e.innerText.trim()).join(' · '),
    prices: [...c.querySelectorAll('.s-card__price')].map(e => e.innerText.trim()),
    rows, seller: sec ? sec.innerText.replace(/\\n/g, ' ') : '',
  };
})"""

UNAVAILABLE = re.compile(r"out of stock|this listing (has|was) ended|no longer available|listing was ended|bidding has ended")


class Ebay(Source):
    name = "ebay"
    label = "eBay"
    local = False
    needs_browser = True

    def __init__(self, ctx):
        super().__init__(ctx)
        self._page = None

    @property
    def page(self):
        if self._page is None:
            self._page = self.ctx.browser.new_page()
            self._page.goto("https://www.ebay.com/", timeout=45000)
            self._page.wait_for_timeout(2500)
        return self._page

    def search(self, query: str, watch: Watch) -> list[Listing]:
        # Buy It Now, newly listed first, US only (eBay blocks bots from sold-listing searches)
        params = {"_nkw": query, "_ipg": "120", "LH_BIN": "1", "_sop": "10", "LH_PrefLoc": "1"}
        url = "https://www.ebay.com/sch/i.html?" + urllib.parse.urlencode(params)
        cards = []
        for _ in range(2):  # 0 cards usually means a soft block; going via the homepage clears it
            self.page.goto(url, timeout=45000)
            self.page.wait_for_timeout(2500 + random.randint(0, 1500))
            cards = self.page.evaluate(EXTRACT_JS)
            if cards:
                break
            self.page.goto("https://www.ebay.com/", timeout=45000)
            self.page.wait_for_timeout(4000)
        if not cards:
            log.warning("ebay returned 0 cards for %r (possible block)", query)
        return [x for x in map(self._parse, cards) if x]

    @staticmethod
    def _parse(c: dict) -> Listing | None:
        if not c["id"] or not c["title"] or len(c["prices"]) != 1:  # skip price ranges / multi-variation
            return None
        if c["title"].strip().lower() == "shop on ebay":  # placeholder card eBay mixes into results
            return None
        price = money(c["prices"][0])
        if price is None or re.search(r"parts|not working", c["cond"].lower()):
            return None
        ship = 0.0
        for r in c["rows"]:
            if "delivery" in r.lower() or "shipping" in r.lower():
                ship = money(r) or 0.0
        fb = re.search(r"([\d.]+)% positive \(([\d.]+)([KM]?)\)", c["seller"])
        return Listing(
            source="ebay", source_id=c["id"], title=c["title"], url=f"https://www.ebay.com/itm/{c['id']}",
            price_total=round(price + ship, 2), condition=c["cond"] or None,
            location=next((r for r in c["rows"] if r.startswith("Located in")), ""),
            seller=re.sub(r"^\w{3}-\d\d \d\d:\d\d\s+", "", c["seller"].strip()) or None,
            seller_fb_pct=float(fb.group(1)) if fb else None,
            seller_fb_count=float(fb.group(2)) * {"": 1, "K": 1e3, "M": 1e6}[fb.group(3)] if fb else 0,
            extra={"best_offer": any("best offer" in r.lower() for r in c["rows"])})

    def is_available(self, listing: Listing) -> bool | None:
        """Search results keep showing sold-out listings at old prices. None = couldn't check."""
        try:
            self.page.goto(listing.url, timeout=45000)
            self.page.wait_for_timeout(2500)
            text = self.page.inner_text("body")[:20000].lower()
        except Exception as ex:
            log.warning("ebay verify failed %s: %s", listing.url, ex)
            return None
        return not UNAVAILABLE.search(text)
