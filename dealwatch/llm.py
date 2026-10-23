"""Optional part/accessory check with a local Ollama model, run only on alert candidates.

Asks whether a listing is the watched item itself or a part, accessory or other product.
Returns None when Ollama is unset or unreachable, so alerting never depends on it.
"""

import json
import logging

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
