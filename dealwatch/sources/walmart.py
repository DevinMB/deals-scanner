"""Walmart search via the page's embedded __NEXT_DATA__ JSON (plain HTTP)."""

import json
import random
import re
import time

from ..models import Watch
from . import Listing, Source, http_get, log

NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


class Walmart(Source):
    name = "walmart"
    label = "Walmart"
    local = False
    store = True

    def search(self, query: str, watch: Watch) -> list[Listing]:
        m = None
        for attempt in range(2):  # Walmart's bot check ("Robot or human?") comes and goes; back off and retry once
            page = http_get("https://www.walmart.com/search", q=query).text
            if m := NEXT_DATA.search(page):
                break
            if attempt == 0:
                time.sleep(random.uniform(20, 30))
        if not m:
            blocked = "Robot or human" in page
            raise RuntimeError("Walmart bot check (rate-limited)" if blocked else "Walmart page had no product data")
        stacks = json.loads(m.group(1))["props"]["pageProps"]["initialData"]["searchResult"].get("itemStacks") or []
        out = []
        for it in (i for s in stacks for i in s.get("items", []) if i.get("__typename") == "Product"):
            price = it.get("price")
            if not isinstance(price, (int, float)) or price <= 0:  # unpriced marketplace variants show 0
                continue
            if (it.get("availabilityStatusV2") or {}).get("value") not in (None, "IN_STOCK"):
                continue
            out.append(Listing(source=self.name, source_id=str(it["usItemId"]), title=it.get("name") or "",
                               url="https://www.walmart.com" + it.get("canonicalUrl", "").split("?")[0],
                               price_total=float(price), seller=it.get("sellerName") or None,
                               extra={"sponsored": bool(it.get("isSponsoredFlag"))}))
        if not out:
            log.warning("walmart returned 0 priced items for %r", query)
        return out
