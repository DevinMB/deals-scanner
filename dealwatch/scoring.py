"""Deal scoring, ported from classify(), tier() and update_market() in the GPU scanner.

Pure functions: no I/O. A Watch replaces the scanner's per-model config, and Rules
carries the global settings (blacklists, seller thresholds, market window).
"""

import functools
import json
import re
import statistics
from dataclasses import dataclass
from pathlib import Path

from .models import Watch

DEFAULTS = json.loads((Path(__file__).parent / "defaults.json").read_text())

# "seat pads for herman miller aeron", "casters fit aeron": a part for the item, not the item.
# A price right after ("for $400") is a sale, not an accessory.
ACCESSORY_BEFORE = re.compile(r"\b(for|fits?|compatible with|works with)\s+(?![$\d])(?:\S+\s+){0,2}$")

COUNT_WORDS = [(re.compile(r"\b(8\s*x|x\s*8|eight)\s*$"), 8), (re.compile(r"\b(quad|4\s*x|x\s*4|four)\s*$"), 4),
               (re.compile(r"\b(dual|2\s*x|x\s*2|two|pair of)\s*$"), 2)]


def _rx(patterns):
    return re.compile("|".join(f"(?:{p})" for p in patterns))


@dataclass(frozen=True)
class Rules:
    hard: re.Pattern
    soft: re.Pattern
    longshot_floor: float
    min_seller_feedback: int
    min_seller_positive_pct: float
    market_window_days: int
    min_market_samples: int
    cheap_outlier_fraction: float
    bundle: dict
    filler_words: list[str]

    @classmethod
    def from_settings(cls, s: dict | None = None) -> "Rules":
        s = {**DEFAULTS, **(s or {})}
        return cls(hard=_rx(s["hard_blacklist"]), soft=_rx(s["soft_flags"]), longshot_floor=s["longshot_floor"],
                   min_seller_feedback=s["min_seller_feedback"], min_seller_positive_pct=s["min_seller_positive_pct"],
                   market_window_days=s["market_window_days"], min_market_samples=s["min_market_samples"],
                   cheap_outlier_fraction=s["cheap_outlier_fraction"],
                   bundle={k: re.compile(v) for k, v in s["bundle"].items() if not k.startswith("_")},
                   filler_words=s["filler_words"])


@dataclass(frozen=True)
class Match:
    count: int
    is_bundle: bool
    flags: list[str]


@dataclass(frozen=True)
class Score:
    tier: str  # "DEAL", "BUNDLE DEAL" or "LONG SHOT"
    ratio: float | None
    typical: float | None
    reasons: list[str]


def find_match(watch: Watch, text: str) -> int | None:
    """Start of the watch's match in text, or None. Default rule: every word of the first search query."""
    if watch.include_regex:
        m = re.search(watch.include_regex, text, re.I)
        return m.start() if m else None
    words = watch.search_queries[0].lower().split() if watch.search_queries else []
    starts = []
    for w in words:
        m = re.search(rf"(?<!\w){re.escape(w)}(?!\w)", text)
        if not m:
            return None
        starts.append(m.start())
    return min(starts) if starts else None


def classify(title: str, watch: Watch, rules: Rules, body: str = "") -> Match | None:
    """None (ignore) or a Match. Body text is read only for bundle watches, to find the item inside."""
    t = title.lower()
    if rules.hard.search(t):
        return None
    b = rules.bundle
    pos, src = None, None
    # Only trust the description when the title is a bundle that doesn't name some other item
    use_body = (watch.bundle_enabled and body and (b["word_regex"].search(t) or _bundle_phrase(watch, b).search(t)
                or b["spec_regex"].search(t)) and not b["other_item_regex"].search(t))
    for text in (t, body.lower() if use_body else ""):
        p = find_match(watch, text) if text else None
        if p is not None and not (watch.exclude_terms and _excludes(tuple(watch.exclude_terms)).search(text)):
            pos, src = p, text
            break
    if pos is None or _is_accessory(watch, src, pos):
        return None
    filler = set(rules.filler_words) | set(watch.name.lower().split())
    for q in watch.search_queries:
        filler |= set(q.lower().split())
    # Strip brand/name words between a quantity word and the item ("2x nvidia rtx 3090")
    before = re.sub(r"(?:\s|(?<!\w)(?:" + "|".join(map(re.escape, filler)) + r"))+$", "", src[max(0, pos - 30):pos])
    count = next((n for rx, n in COUNT_WORDS if rx.search(before)), 1)
    is_bundle = (bool(_bundle_phrase(watch, b).search(t) or (b["spec_regex"].search(t) and not b["single_item_regex"].search(t)))
                 and not b["not_bundle_regex"].search(t))
    if src is not t:  # item only mentioned in the body: almost always a whole system
        is_bundle = True
    if is_bundle and not watch.bundle_enabled:
        return None
    return Match(count=count, is_bundle=is_bundle, flags=sorted(set(rules.soft.findall(t)) - {""}))


def _is_accessory(watch: Watch, text: str, pos: int) -> bool:
    starts = [pos]
    if not watch.include_regex and watch.search_queries:  # also check each query word ("pads for aeron")
        for w in watch.search_queries[0].lower().split():
            starts += [m.start() for m in re.finditer(rf"(?<!\w){re.escape(w)}(?!\w)", text)]
    return any(ACCESSORY_BEFORE.search(text[max(0, p - 40):p]) for p in starts)


@functools.lru_cache(maxsize=512)
def _excludes(patterns: tuple[str, ...]) -> re.Pattern:
    """A watch's own blacklist: regex patterns (plain words work as-is), matched case-insensitively."""
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.I)


def _bundle_phrase(watch: Watch, b: dict) -> re.Pattern:
    return re.compile(watch.bundle_regex) if watch.bundle_regex else b["phrase_regex"]


def looks_like_bundle(title: str, watch: Watch, rules: Rules) -> bool:
    """Worth fetching the listing body: a bundle watch, and the title reads like a whole system."""
    t, b = title.lower(), rules.bundle
    return bool(watch.bundle_enabled and not rules.hard.search(t)
                and (b["word_regex"].search(t) or _bundle_phrase(watch, b).search(t) or b["spec_regex"].search(t)))


def seller_ok(fb_count: float | None, fb_pct: float | None, rules: Rules) -> bool:
    """Sources without seller feedback (None) count as trusted, as in the scanner."""
    if fb_count is None:
        return True
    return fb_count >= rules.min_seller_feedback and (fb_pct or 0) >= rules.min_seller_positive_pct


def score(total: float, m: Match, watch: Watch, rules: Rules, typical: float | None, *,
          local: bool, seller_trusted: bool = True) -> Score | None:
    """Tier for an all-in price, or None. typical is the per-item price (see effective_typical)."""
    if watch.max_price is not None and total <= watch.max_price * m.count:
        ratio = total / (typical * m.count) if typical else None
        return Score("DEAL", ratio, typical, [])
    if not typical:
        return None
    ratio = total / (typical * m.count)
    limit = watch.bundle_ratio if m.is_bundle else watch.local_deal_ratio if local else watch.deal_ratio
    longshot_limit = watch.bundle_ratio if m.is_bundle else watch.deal_ratio
    if ratio > longshot_limit or ratio < rules.longshot_floor:
        return None
    reasons = []
    if ratio > limit:
        reasons.append(f"only {round((1 - ratio) * 100)}% below eBay typical — may just be normal local pricing")
    if ratio < watch.scam_ratio:
        reasons.append(f"price is {round((1 - ratio) * 100)}% below typical — scam/broken risk")
    if m.flags:
        reasons.append("title mentions: " + ", ".join(m.flags))
    if not seller_trusted:
        reasons.append("seller has little or mixed feedback")
    if reasons:
        return Score("LONG SHOT", ratio, typical, reasons) if watch.longshot_enabled else None
    return Score("BUNDLE DEAL" if m.is_bundle else "DEAL", ratio, typical, [])


def is_market_sample(m: Match | None, location: str) -> bool:
    """Clean, single-item, US listing: counts toward the watch's typical price."""
    return bool(m and not m.is_bundle and not m.flags and m.count == 1 and "united states" in (location or "").lower())


def typical_price(prices: list[float], rules: Rules) -> tuple[float | None, int]:
    """Median of in-window asking prices with the top and bottom 10% trimmed; None below min samples.

    Samples under cheap_outlier_fraction of the 90th percentile are dropped first: parts and accessories
    that slip past the filters are cheap next to the item, and for some items they outnumber it.
    """
    vals = sorted(prices)
    if vals:
        p90 = vals[int((len(vals) - 1) * 0.9)]
        vals = [v for v in vals if v >= rules.cheap_outlier_fraction * p90]
    if len(vals) < rules.min_market_samples:
        return None, len(vals)
    k = len(vals) // 10
    return round(statistics.median(vals[k:len(vals) - k] if k else vals)), len(vals)


def effective_typical(learned: float | None, watch: Watch) -> float | None:
    """The watch's override when set and switched on, else the learned median (None = never alert)."""
    if watch.override_enabled and watch.typical_override is not None:
        return watch.typical_override
    return learned
