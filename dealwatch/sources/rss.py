"""Reddit and deal-site RSS/Atom feeds (plain HTTP).

Feeds aren't searchable, so each run fetches every feed once and search() matches
the cached items against the watch. Prices are parsed from text near the item
mention, so every listing carries a "verify price" note.
"""

import html
import re
import time
import xml.etree.ElementTree as ET

import httpx

from ..models import Watch
from ..scoring import find_match, classify
from . import Listing, Source, http_get, log, money

FEED_SLEEP = 15  # unauthenticated Reddit RSS rate-limits hard
RETRY_AFTER_429 = 45
ATOM = {"a": "http://www.w3.org/2005/Atom"}


def rss_items(raw: bytes):
    root = ET.fromstring(raw)
    for e in root.findall(".//a:entry", ATOM):  # Atom (Reddit)
        link = e.find("a:link", ATOM)
        yield (e.findtext("a:id", "", ATOM), e.findtext("a:title", "", ATOM),
               link.get("href") if link is not None else "", e.findtext("a:content", "", ATOM))
    for e in root.iter("item"):  # RSS 2.0
        yield (e.findtext("guid") or e.findtext("link", ""), e.findtext("title", ""), e.findtext("link", ""),
               e.findtext("description", ""))


def prices_near(text: str, watch: Watch) -> list[float]:
    """Prices mentioned within ~120 chars after each mention of the watch's item."""
    out, rest, offset = [], text, 0
    while (p := find_match(watch, rest)) is not None:
        window = text[offset + p: offset + p + 120]
        out += [x for x in (money(s) for s in re.findall(r"\$\s?[\d,]{3,6}(?:\.\d\d)?", window)) if x]
        offset += p + 1
        rest = text[offset:]
    return out


class Rss(Source):
    name = "rss"
    label = "Reddit + deal RSS"

    def __init__(self, ctx):
        super().__init__(ctx)
        self._items: list[tuple] | None = None
        self.failed_feeds: list[str] = []

    def _fetch_all(self) -> list[tuple]:
        items, feeds = [], self.ctx.settings["rss_feeds"]
        for i, feed in enumerate(feeds):
            try:
                try:
                    raw = http_get(feed["url"]).content
                except httpx.HTTPStatusError as ex:
                    if ex.response.status_code != 429:
                        raise
                    log.info("rss rate-limited on %s, retrying in %ss", feed["name"], RETRY_AFTER_429)
                    time.sleep(RETRY_AFTER_429)
                    raw = http_get(feed["url"]).content
                for gid, title, link, body in rss_items(raw):
                    items.append((feed, gid, html.unescape(title), link, body))
            except Exception as ex:
                log.warning("rss failed %s: %s", feed["name"], ex)
                self.failed_feeds.append(feed["name"])
            if i < len(feeds) - 1:
                time.sleep(FEED_SLEEP)
        if feeds and len(self.failed_feeds) == len(feeds):
            raise RuntimeError("every RSS feed failed: " + ", ".join(self.failed_feeds))
        return items

    def health_count(self, parsed: int) -> int:
        return len(self._items or [])  # feed items fetched, not matches: a quiet feed isn't a failure

    def search(self, query: str, watch: Watch) -> list[Listing]:
        if self._items is None:
            self._items = []  # if every feed fails, report it once rather than refetching per watch
            self._items = self._fetch_all()
        out = []
        for feed, gid, title, link, body in self._items:
            tl = title.lower()
            if feed["used"]:  # r/hardwareswap style: only look at what they HAVE
                if "[h]" not in tl:
                    continue
                tl = tl.split("[h]", 1)[1].split("[w]", 1)[0]
            if not classify(tl, watch, self.ctx.rules):
                continue
            text = html.unescape(re.sub(r"<[^>]+>", " ", body or "")).lower()
            prices = prices_near(tl + " " + text, watch) or [p for p in [money(title)] if p]
            out.append(Listing(source=self.name, source_id=gid, title=title, url=link,
                               price_total=min(prices) if prices else None, location=feed["name"],
                               extra={"price_candidates": prices, "note": "price parsed from post text — verify"}))
        return out
