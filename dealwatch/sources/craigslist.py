"""Craigslist via its JSON search API (plain HTTP)."""

import html
import re

from ..models import Watch
from . import Listing, Source, http_get, local_query, log, miles

API = "https://sapi.craigslist.org/web/v8/postings/search/full"


class Craigslist(Source):
    name = "craigslist"
    label = "Craigslist"
    has_body = True

    def search(self, query: str, watch: Watch) -> list[Listing]:
        # OR queries ("a | b | c") only work with single-word terms; a multi-word term silently breaks the query.
        h = self.ctx.home
        radius = self.ctx.radius(watch)
        d = http_get(API, batch="1-0-360-0-0", cc="US", lang="en", searchPath="sss", query=local_query(query),
                     lat=h["lat"], lon=h["lon"], search_distance=radius).json()["data"]
        if not d.get("items") or not isinstance(d.get("decode"), dict):  # decode is 0 when nothing matched
            return []
        return [x for x in (self._parse(it, d["decode"]["minPostingId"], radius) for it in d["items"]) if x]

    def _parse(self, it: list, base: int, radius: float) -> Listing | None:
        # Items are positional arrays: [id offset, ?, ?, price, "?~lat~lon", ..., [tag, values...]..., title]
        price, title = it[3], it[-1]
        if not isinstance(title, str) or price is None or price <= 1:
            return None
        parts = dict((x[0], x[1:]) for x in it if isinstance(x, list) and x and isinstance(x[0], int))
        if 13 not in parts or 6 not in parts:
            return None
        try:
            _, lat, lon = it[4].split("~")
            dist = miles(self.ctx.home, float(lat), float(lon))
        except (ValueError, AttributeError):
            dist = None
        if dist is not None and dist > radius:
            return None
        return Listing(source=self.name, source_id=str(base + it[0]), title=title,
                       url=f"https://www.craigslist.org/view/d/{parts[6][0]}/{parts[13][0]}",
                       price_total=float(price), distance_mi=round(dist, 1) if dist is not None else None,
                       location=f"{dist:.0f} mi from {self.ctx.home['label']}" if dist is not None else "")

    def body(self, listing: Listing) -> str:
        try:
            page = http_get(listing.url).text
        except Exception as ex:
            log.warning("craigslist detail failed %s: %s", listing.url, ex)
            return ""
        m = re.search(r'<section id="postingbody">(.*?)</section>', page, re.S)
        return html.unescape(re.sub(r"<[^>]+>", " ", m.group(1)))[:3000] if m else ""
