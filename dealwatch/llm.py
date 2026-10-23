"""Optional part/accessory check with a local Ollama model, run only on alert candidates.

Asks whether a listing is the watched item itself or a part, accessory or other product.
Returns None when Ollama is unset or unreachable, so alerting never depends on it.
"""

import json
import logging
import re

import httpx

log = logging.getLogger("dealwatch.llm")

# Price is deliberately left out: deals are cheap by definition, and small models read "cheap" as "part".
PROMPT = """A shopper wants to buy this item: {item}
A marketplace listing is titled: "{title}"

Judge from the title's wording only. Is the listing the item itself (a complete {item} that could be used \
as-is, possibly with extras), or is it a part, accessory, replacement piece, compatible/"alternative"/knock-off \
product, or something else entirely?
Reply with JSON: {{"verdict": "item" | "part" | "other", "reason": "<at most 12 words>"}}"""

SCHEMA = {"type": "object", "properties": {"verdict": {"type": "string", "enum": ["item", "part", "other"]},
                                           "reason": {"type": "string"}}, "required": ["verdict", "reason"]}


def check(url: str | None, model: str, item: str, title: str, timeout: float = 60) -> tuple[str, str] | None:
    """(verdict, reason), or None if the check couldn't run."""
    if not url:
        return None
    try:
        r = httpx.post(url.rstrip("/") + "/api/chat", timeout=timeout, json={
            "model": model, "stream": False, "think": False, "format": SCHEMA, "options": {"temperature": 0},
            "messages": [{"role": "user", "content": PROMPT.format(item=item, title=title)}]})
        r.raise_for_status()
        out = json.loads(r.json()["message"]["content"])
        verdict = out.get("verdict")
        if verdict not in ("item", "part", "other"):
            raise ValueError(f"unexpected verdict {verdict!r}")
        return verdict, str(out.get("reason", ""))[:200]
    except Exception as ex:
        log.warning("ollama check failed: %s", ex)
        return None


SUGGEST_PROMPT = """I want a marketplace alert (eBay, Craigslist, OfferUp) for buying: {item}

Fill in:
- search_terms: 1 or 2 short searches that a seller's title for a complete {item} would contain (lowercase, 2-4 words).
- exclude: up to 12 words that show a listing is NOT a complete {item}. Two kinds only:
  1. neighbouring models that share its name (other generations, sizes, variants), written exactly as sellers do;
  2. part or accessory words (e.g. "case", "battery", "strap", "manual" for a camera).
  Never list a word a genuine complete {item} listing might use: no brand or family names, no condition words, no
  generic descriptors (e.g. "ergonomic", "office", "body", "kit", "bundle", "pro", "plus"), and nothing that is part of
  "{item}" itself.
- typical_price_usd: roughly what a used, complete {item} sells for in the US now, or null if unsure.
- note: one short, specific tip for buying this item used, or "".

Example for "Fujifilm X100V": {{"search_terms": ["fujifilm x100v"], "exclude": ["x100vi", "x100f", "x100s",
"x100t", "case", "lens hood", "battery", "grip", "manual", "box only"], "typical_price_usd": 1400,
"note": "Check the shutter count and that the leaf shutter fires cleanly."}}
Reply with JSON only."""

SUGGEST_SCHEMA = {"type": "object", "properties": {
    "search_terms": {"type": "array", "items": {"type": "string"}},
    "exclude": {"type": "array", "items": {"type": "string"}},
    "typical_price_usd": {"type": ["number", "null"]},
    "note": {"type": "string"}}, "required": ["search_terms", "exclude", "typical_price_usd", "note"]}


def suggest(url: str | None, model: str, item: str, timeout: float = 90) -> tuple[dict | None, str]:
    """Suggested watch settings for an item description: (dict, "") or (None, reason)."""
    if not url:
        return None, "Set the Ollama URL in Settings first."
    try:
        r = httpx.post(url.rstrip("/") + "/api/chat", timeout=timeout, json={
            "model": model, "stream": False, "think": False, "format": SUGGEST_SCHEMA, "options": {"temperature": 0.2},
            "messages": [{"role": "user", "content": SUGGEST_PROMPT.format(item=item)}]})
        r.raise_for_status()
        out = json.loads(r.json()["message"]["content"])
    except Exception as ex:
        log.warning("ollama suggest failed: %s", ex)
        return None, f"Couldn't get a suggestion from {model}: {type(ex).__name__}"
    name = item.lower()
    terms = [t.strip().lower() for t in out.get("search_terms", []) if isinstance(t, str) and t.strip()][:2]
    # Whole-word patterns ("x100" must not block "x100v"); drop any that would block the item's own name
    excludes = []
    for x in out.get("exclude", []):
        x = " ".join(x.lower().split()) if isinstance(x, str) else ""
        pattern = rf"\b{re.escape(x)}\b"
        if x and not re.search(pattern, name) and not any(re.search(pattern, t) for t in terms) and pattern not in excludes:
            excludes.append(pattern)
    price = out.get("typical_price_usd")
    price = round(price) if isinstance(price, (int, float)) and 1 <= price <= 1_000_000 else None
    return {"search_terms": terms or [item.lower()], "exclude": excludes[:15], "typical_price": price,
            "note": str(out.get("note", ""))[:200]}, ""
