"""OfferUp via Playwright. Results follow the browser's geolocation and only reach ~30 mi."""

import json
import re
import urllib.parse

from ..models import Watch
from . import Listing, Source, local_query, log, money

LINKS_JS = "els => els.map(e => [e.href, e.innerText.split('\\n').map(s => s.trim()).filter(Boolean)])"


class OfferUp(Source):
    name = "offerup"
    label = "OfferUp"
    needs_browser = True
    has_body = True
    detail_cap_key = "offerup_detail_cap"

    def search(self, query: str, watch: Watch) -> list[Listing]:
        page = self.ctx.browser.new_page()
        try:
            page.goto("https://offerup.com/search?" + urllib.parse.urlencode({"q": local_query(query)}), timeout=45000)
            page.wait_for_timeout(4000)
            for _ in range(3):
                page.mouse.wheel(0, 2500)
                page.wait_for_timeout(1200)
            items = page.eval_on_selector_all("a[href*='/item/detail/']", LINKS_JS)
        finally:
            page.close()
        if not items:
            log.warning("offerup returned 0 items for %r", query)
        return [x for x in (self._parse(href, lines) for href, lines in items) if x]

    @staticmethod
    def _parse(href: str, lines: list[str]) -> Listing | None:
        if len(lines) < 2:
            return None
        price_line = next((l for l in lines if l.startswith("$")), None)
        price = money(price_line)
        if not price or price <= 1:
            return None
        title = next(l for l in lines if l != price_line)
        url = href.split("?")[0]
        return Listing(source="offerup", source_id=url.rsplit("/", 1)[-1], title=title, url=url, price_total=price,
                       location=lines[-1] if lines[-1] not in (title, price_line) else "")

    def body(self, listing: Listing) -> str:
        """Item pages 403 plain HTTP, so load them in the browser."""
        page = self.ctx.browser.new_page()
        try:
            page.goto(listing.url, timeout=45000)
            page.wait_for_timeout(2500)
            m = re.search(r'"description":"((?:[^"\\]|\\.)*)"', page.content())
            return json.loads(f'"{m.group(1)}"')[:3000] if m else ""
        except Exception as ex:
            log.warning("offerup detail failed %s: %s", listing.url, ex)
            return ""
        finally:
            page.close()
