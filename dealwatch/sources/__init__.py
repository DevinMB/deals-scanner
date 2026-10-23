"""Marketplace sources. Each is a plugin with search(query, watch) -> list[Listing].

Ported from the GPU scanner's check_* functions. Sources only fetch and parse;
deciding what is a deal (and when to fetch a listing body) is the run loop's job.
"""

import logging
import math
import re
from dataclasses import dataclass, field

import httpx

from ..models import Watch
from ..scoring import DEFAULTS, Rules

log = logging.getLogger("dealwatch.sources")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/141.0.0.0 Safari/537.36")


@dataclass
class Listing:
    source: str
    source_id: str
    title: str
    url: str
    price_total: float | None
    location: str = ""
    distance_mi: float | None = None
    seller: str | None = None
    seller_fb_count: float | None = None  # None = source has no seller feedback
    seller_fb_pct: float | None = None
    condition: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class Context:
    """What a source needs from the app: settings, scoring rules and (for Playwright sources) a browser."""
    settings: dict = field(default_factory=lambda: dict(DEFAULTS))
    rules: Rules = field(default_factory=Rules.from_settings)
    browser: "object | None" = None  # dealwatch.sources.browser.Browser

    @property
    def home(self) -> dict:
        return self.settings["home"]

    def radius(self, watch: Watch) -> float:
        return watch.radius_miles or self.settings["radius_miles"]


class Source:
    name: str = ""
    label: str = ""
    local: bool = True        # private-sale source: uses local_deal_ratio
    needs_browser: bool = False

    def __init__(self, ctx: Context):
        self.ctx = ctx

    def search(self, query: str, watch: Watch) -> list[Listing]:
        raise NotImplementedError

    def body(self, listing: Listing) -> str:
        """Listing description, for bundle matching. Empty when the source has none."""
        return ""


def http_get(url: str, **params) -> httpx.Response:
    r = httpx.get(url, params=params or None, headers={"User-Agent": UA}, timeout=25, follow_redirects=True)
    r.raise_for_status()
    return r


def money(s: str | None) -> float | None:
    m = re.search(r"\$\s?([\d,]+(?:\.\d\d)?)", s or "")
    return float(m.group(1).replace(",", "")) if m else None


def miles(home: dict, lat: float, lon: float) -> float:
    p1, p2 = math.radians(home["lat"]), math.radians(lat)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon - home["lon"]) / 2) ** 2)
    return 3959 * 2 * math.asin(math.sqrt(a))


def registry() -> dict[str, type[Source]]:
    from .craigslist import Craigslist
    from .rss import Rss
    return {s.name: s for s in (Craigslist, Rss)}
