"""Slickdeals keyword search RSS: community-posted deals from any store (plain HTTP).

These are already deal prices, so they're never used to learn a typical price.
"""

import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from ..models import Watch
from . import Listing, Source, http_get, log, money

MAX_AGE = timedelta(days=7)  # keyword search returns threads from months ago; old deals are almost always gone

# "... $66.25 at Amazon", "... Instore @ Costco. $49.99 on website", "... at Best Buy (+ free shipping)"
STORE = re.compile(r"(?:\bat|@)\s+([A-Z][\w&'\- ]{1,30}?)(?=\s*[(\[,+.]|\s*$|\s+(?:for|w/|with|via|and)\b)")


class Slickdeals(Source):
    name = "slickdeals"
    label = "Slickdeals"
    local = False
    store = True
    learn_prices = False

    def search(self, query: str, watch: Watch) -> list[Listing]:
        raw = http_get("https://slickdeals.net/newsearch.php", q=query, searcharea="deals", searchin="first", rss="1").content
        out = []
        for e in ET.fromstring(raw).iter("item"):
            title = html.unescape(e.findtext("title", "") or "")
            link = (e.findtext("link", "") or "").split("?")[0]
            price = money(title)
            if not price or not link:
                continue
            try:
                posted = parsedate_to_datetime(e.findtext("pubDate", ""))
            except (TypeError, ValueError):
                posted = None
            if posted and datetime.now(timezone.utc) - posted > MAX_AGE:
                continue
            store = STORE.search(title)
            out.append(Listing(source=self.name, source_id=link.rstrip("/").rsplit("/", 1)[-1][:120], title=title,
                               url=link, price_total=price, location=store.group(1).strip() if store else "",
                               extra={"note": "community-posted deal; check it's still live"}))
        return out

    def is_available(self, listing: Listing) -> bool | None:
        """The thread page says "isExpiredDeal":"Yes" once members mark the deal dead."""
        try:
            page = http_get(listing.url).text
        except Exception as ex:
            log.warning("slickdeals check failed %s: %s", listing.url, ex)
            return None
        return '"isExpiredDeal":"Yes"' not in page
