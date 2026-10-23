"""Typed views of DB rows."""

import json
import sqlite3
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Watch:
    id: int | None
    name: str
    search_queries: list[str]
    include_regex: str | None = None
    exclude_terms: list[str] = field(default_factory=list)  # regex patterns, case-insensitive
    sources: list[str] = field(default_factory=lambda: ["ebay", "craigslist", "offerup", "rss"])
    enabled: bool = True
    deal_ratio: float = 0.60
    local_deal_ratio: float = 0.50
    scam_ratio: float = 0.30
    max_price: float | None = None
    typical_override: float | None = None
    override_enabled: bool = True
    longshot_enabled: bool = True
    quiet_longshots: bool = True
    radius_miles: int | None = None
    bundle_enabled: bool = False
    bundle_regex: str | None = None
    bundle_ratio: float = 0.85
    typical_basis: str = "ebay"  # learn the typical price from "ebay" (used asking prices) or "store" prices
    retail_deal_ratio: float = 0.75

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Watch":
        r = dict(row)
        for k in ("search_queries", "exclude_terms", "sources"):
            r[k] = json.loads(r[k])
        for k in ("enabled", "override_enabled", "longshot_enabled", "quiet_longshots", "bundle_enabled"):
            r[k] = bool(r[k])
        r.pop("created_at", None)
        return cls(**r)
