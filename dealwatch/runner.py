"""Scan run loop: search every source for every enabled watch, store, learn prices, score, alert.

Ported from main() in the GPU scanner; JSON state files are now DB tables. Sending is
delegated to a Notifier, and an alert counts as sent only once the notifier says so,
so a failed send is retried the next time the listing is seen.
"""

import contextlib
import fcntl
import json
import logging
import random
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import llm, recipients as recipients_mod, settings as settings_mod
from .models import Watch
from .scoring import (Match, Rules, Score, classify, effective_typical, is_market_sample, looks_like_bundle, score,
                      seller_ok, typical_price)
from .sources import Context, Listing, Source, registry

log = logging.getLogger("dealwatch.runner")

# eBay first: it feeds the typical prices that score everything else
SOURCE_ORDER = ["ebay", "amazon", "walmart", "offerup", "craigslist", "slickdeals", "rss"]
TIER_ORDER = {"DEAL": 0, "BUNDLE DEAL": 0, "LONG SHOT": 1}
HEALTH_STREAK = 3
BLOCK_COOLDOWN_MIN = (60, 24 * 60)  # after a run that was only rate-limited: wait 1 h, doubling per blocked run, max 24 h
RECHECK_UNAVAILABLE = timedelta(hours=2)


def iso(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


class Notifier:
    """Sends alerts and health warnings.

    alert() gets the recipients still owed this alert (rows with id and chat_id; chat_id None = the owner)
    and returns the ids it delivered to. health() goes to the owner and returns True if delivered.
    """

    def alert(self, alert: dict, recipients: list) -> set[int]:
        log.info("alert pending (no sender): %s %s $%s", alert["tier"], alert["title"][:60], alert["price_total"])
        return set()

    def health(self, text: str) -> bool:
        log.warning("health warning pending (no sender yet): %s", text)
        return False


@dataclass
class Hit:
    watch: Watch
    source: Source
    listing: Listing
    match: Match
    listing_id: int | None = None
    score: Score | None = None
    typical_src: str = ""


@dataclass
class RunResult:
    started_at: str
    finished_at: str | None = None
    skipped: bool = False
    timed_out: bool = False
    parsed: dict = field(default_factory=dict)   # source -> listings parsed
    errors: dict = field(default_factory=dict)   # source -> [messages]
    matches: int = 0
    alerts_new: int = 0
    alerts_sent: int = 0


@contextlib.contextmanager
def scan_lock(path: Path):
    """Cross-process lock, so the scheduler and a CLI scan never overlap. Yields False if held."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def enabled_watches(conn: sqlite3.Connection, ids: list[int] | None = None) -> list[Watch]:
    rows = conn.execute("SELECT * FROM watches WHERE enabled = 1 ORDER BY id").fetchall()
    return [w for w in map(Watch.from_row, rows) if ids is None or w.id in ids]


# ---------- collecting ----------

def collect(ctx: Context, src: Source, watches: list[Watch], deadline: float, result: RunResult,
            conn: sqlite3.Connection | None = None, sleep: bool = True) -> list[Hit]:
    """Search one source for each watch's queries; returns matched listings. Also used by Preview."""
    hits, parsed, fetched = [], 0, 0
    cap = ctx.settings[src.detail_cap_key]
    for w in watches:
        if src.name not in w.sources:
            continue
        started, found, errors, seen = iso(), 0, [], set()
        for i, q in enumerate(w.search_queries):
            if time.monotonic() > deadline:
                result.timed_out = True
                errors.append("run timeout reached")
                break
            try:
                listings = src.search(q, w)
            except Exception as ex:
                log.warning("%s search failed for %r: %s", src.name, q, ex)
                errors.append(f"{q}: {ex}")
                listings = []
            found += len(listings)
            for l in listings:
                if l.source_id in seen:
                    continue
                seen.add(l.source_id)
                if src.name == "ebay" and l.location and "united states" not in l.location.lower():
                    continue
                m = classify(l.title, w, ctx.rules)
                if not m and src.has_body and fetched < cap and looks_like_bundle(l.title, w, ctx.rules):
                    body = _cached_body(conn, l)
                    if body is None:
                        fetched += 1
                        body = src.body(l)
                    l.extra["body"] = body
                    m = classify(l.title, w, ctx.rules, body)
                if m:
                    hits.append(Hit(w, src, l, m))
            if sleep and i < len(w.search_queries) - 1:
                time.sleep(random.uniform(*src.query_delay))
        parsed += found
        result.errors.setdefault(src.name, []).extend(errors)
        if conn is not None:
            conn.execute("INSERT INTO runs (started_at, finished_at, source, watch_id, listings_found, error)"
                         " VALUES (?, ?, ?, ?, ?, ?)", (started, iso(), src.name, w.id, found, "; ".join(errors) or None))
        if result.timed_out:
            break
        if sleep:
            time.sleep(random.uniform(*src.query_delay))
    result.parsed[src.name] = result.parsed.get(src.name, 0) + parsed
    return hits


def _cached_body(conn, l: Listing) -> str | None:
    if conn is None:
        return None
    row = conn.execute("SELECT body FROM listings WHERE source = ? AND source_id = ? AND body IS NOT NULL LIMIT 1",
                       (l.source, l.source_id)).fetchone()
    return row[0] if row else None


# ---------- storing and scoring ----------

def store(conn: sqlite3.Connection, h: Hit, now: str) -> None:
    l = h.listing
    h.listing_id = conn.execute(
        """INSERT INTO listings (source, source_id, watch_id, title, url, price_total, location, distance_mi, seller,
                                 seller_fb_count, seller_fb_pct, condition, body, first_seen, last_seen, available)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
           ON CONFLICT (source, source_id, watch_id) DO UPDATE SET
             title = excluded.title, url = excluded.url, price_total = excluded.price_total,
             location = excluded.location, distance_mi = excluded.distance_mi, seller = excluded.seller,
             seller_fb_count = excluded.seller_fb_count, seller_fb_pct = excluded.seller_fb_pct,
             condition = excluded.condition, body = COALESCE(excluded.body, listings.body),
             last_seen = excluded.last_seen
           RETURNING id""",
        (l.source, l.source_id, h.watch.id, l.title, l.url, l.price_total, l.location, l.distance_mi, l.seller,
         l.seller_fb_count, l.seller_fb_pct, l.condition, l.extra.get("body"), now, now)).fetchone()[0]


def sample_key(h: Hit, now: datetime) -> str | None:
    """price_samples key for a hit, or None if it doesn't count toward this watch's typical price.

    eBay basis: one sample per listing (its current asking price). Store basis: one sample per product per day,
    so the median tracks what the item usually costs over the window and a price drop stands out against it.
    """
    l, w = h.listing, h.watch
    if not l.price_total:
        return None
    if w.typical_basis == "store":
        if h.source.store and h.source.learn_prices and is_market_sample(h.match, l.location, store=True):
            return f"{l.source}:{l.source_id}@{now:%Y-%m-%d}"
        return None
    return l.source_id if h.source.name == "ebay" and is_market_sample(h.match, l.location) else None


def window_days(basis: str, rules: Rules) -> int:
    return rules.store_window_days if basis == "store" else rules.market_window_days


def learn_prices(conn: sqlite3.Connection, hits: list[Hit], rules: Rules, now: datetime) -> dict[int, tuple]:
    """Add each watch's clean price samples to its pool; return watch_id -> (learned median, samples)."""
    for h in hits:
        if key := sample_key(h, now):
            conn.execute("INSERT INTO price_samples (watch_id, source_id, price, first_seen) VALUES (?, ?, ?, ?)"
                         " ON CONFLICT (watch_id, source_id) DO UPDATE SET price = excluded.price",
                         (h.watch.id, key, h.listing.price_total, iso(now)))
    out = {}
    for wid, basis in {(h.watch.id, h.watch.typical_basis) for h in hits}:
        since = iso(now - timedelta(days=window_days(basis, rules)))
        prices = [r[0] for r in conn.execute("SELECT price FROM price_samples WHERE watch_id = ? AND first_seen >= ?",
                                             (wid, since))]
        out[wid] = typical_price(prices, rules)
        if out[wid][0] is not None:
            conn.execute("INSERT INTO market_history (watch_id, day, median, samples) VALUES (?, ?, ?, ?)"
                         " ON CONFLICT (watch_id, day) DO UPDATE SET median = excluded.median, samples = excluded.samples",
                         (wid, now.strftime("%Y-%m-%d"), out[wid][0], out[wid][1]))
    return out


def market(conn: sqlite3.Connection, watch_id: int, rules: Rules, now: datetime | None = None,
           basis: str = "ebay") -> tuple:
    """(learned median or None, sample count, newest sample time) for the UI."""
    since = iso((now or datetime.now(timezone.utc)) - timedelta(days=window_days(basis, rules)))
    rows = conn.execute("SELECT price, first_seen FROM price_samples WHERE watch_id = ? AND first_seen >= ?",
                        (watch_id, since)).fetchall()
    med, n = typical_price([r[0] for r in rows], rules)
    return med, n, max((r[1] for r in rows), default=None)


def pick_price(h: Hit, typical: float | None, rules: Rules) -> float | None:
    """RSS posts mention several prices: take the lowest plausible one, as the scanner did."""
    cands = h.listing.extra.get("price_candidates")
    if not cands:
        return h.listing.price_total
    w, c = h.watch, h.match.count
    ok = []
    for p in cands:
        if w.max_price is not None and p <= w.max_price * c:
            ok.append(p)
        elif typical and rules.longshot_floor * typical * c <= p <= (w.bundle_ratio if h.match.is_bundle else w.deal_ratio) * typical * c:
            ok.append(p)
    return min(ok) if ok else None


def score_hit(h: Hit, typical: float | None, rules: Rules) -> Score | None:
    total = pick_price(h, typical, rules)
    if total is None:
        return None
    h.listing.price_total = total
    l = h.listing
    return score(total, h.match, h.watch, rules, typical, local=h.source.local, store=h.source.store,
                 seller_trusted=seller_ok(l.seller_fb_count, l.seller_fb_pct, rules))


def alert_payload(h: Hit) -> dict:
    l, s = h.listing, h.score
    return {"watch": h.watch.name, "watch_id": h.watch.id, "listing_id": h.listing_id, "tier": s.tier,
            "ratio": s.ratio, "typical": s.typical, "reasons": s.reasons, "count": h.match.count,
            "is_bundle": h.match.is_bundle, "title": l.title, "url": l.url, "price_total": l.price_total,
            "source": h.source.label, "location": l.location, "seller": l.seller, "condition": l.condition,
            "note": l.extra.get("note"), "best_offer": l.extra.get("best_offer", False),
            "typical_src": h.typical_src, "max_price": h.watch.max_price,
            "store_typical": l.extra.get("store_typical"),
            "quiet": s.tier == "LONG SHOT" and h.watch.quiet_longshots}


def handle_alert(conn: sqlite3.Connection, h: Hit, notifier: Notifier, result: RunResult, now: datetime,
                 s: dict | None = None) -> None:
    row = conn.execute("SELECT id, telegram_ok FROM alerts WHERE listing_id = ? AND price_total = ?",
                       (h.listing_id, h.listing.price_total)).fetchone()
    if row and row["telegram_ok"]:
        return  # already alerted at this price
    if s is not None and not _is_the_item(conn, h, s):
        return
    if not _still_available(conn, h, now):
        return
    people = recipients_mod.for_watch(conn, h.watch.id)
    if not people:
        return  # nobody to tell; it alerts if someone is added while the listing is still live
    if row:
        alert_id = row["id"]
    else:
        s = h.score
        alert_id = conn.execute(
            "INSERT INTO alerts (listing_id, watch_id, price_total, tier, ratio, typical_at_alert, reasons, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (h.listing_id, h.watch.id, h.listing.price_total, s.tier, s.ratio, s.typical, json.dumps(s.reasons),
             iso(now))).lastrowid
        result.alerts_new += 1
    done = {r[0] for r in conn.execute("SELECT recipient_id FROM alert_deliveries WHERE alert_id = ?", (alert_id,))}
    owed = [p for p in people if p["id"] not in done]
    try:
        delivered = notifier.alert(alert_payload(h), owed) if owed else set()
    except Exception as ex:
        log.warning("alert send failed: %s", ex)
        delivered = set()
    conn.executemany("INSERT OR IGNORE INTO alert_deliveries (alert_id, recipient_id, sent_at) VALUES (?, ?, ?)",
                     [(alert_id, rid, iso(now)) for rid in delivered])
    if done | delivered >= {p["id"] for p in people}:
        conn.execute("UPDATE alerts SET sent_at = ?, telegram_ok = 1 WHERE id = ?", (iso(now), alert_id))
        result.alerts_sent += 1


def _is_the_item(conn, h: Hit, s: dict) -> bool:
    """Ollama part check. Bundles skip it (bias toward alerting on a whole PC); unavailable = alert, unverified."""
    if h.match.is_bundle or not s.get("llm_check_enabled") or not s.get("ollama_url"):
        return True
    row = conn.execute("SELECT llm_verdict FROM listings WHERE id = ?", (h.listing_id,)).fetchone()
    verdict = row["llm_verdict"] if row else None
    if verdict is None:
        r = llm.check(s["ollama_url"], s["ollama_model"], h.watch.name, h.listing.title)
        if r is None:
            h.listing.extra["note"] = "; ".join(x for x in (h.listing.extra.get("note"), "not checked for parts: Ollama unavailable") if x)
            return True
        verdict, reason = r
        conn.execute("UPDATE listings SET llm_verdict = ?, llm_reason = ? WHERE id = ?", (verdict, reason, h.listing_id))
    if verdict != "item":
        log.info("ollama says %s, not alerting: %s", verdict, h.listing.title[:70])
        return False
    return True


def _still_available(conn, h: Hit, now: datetime) -> bool:
    """Open the listing before alerting: eBay results keep showing sold-out items and Slickdeals keeps expired
    deals. Sources without a check say True. Unavailable ones are rechecked after 2 h."""
    row = conn.execute("SELECT available, checked_at FROM listings WHERE id = ?", (h.listing_id,)).fetchone()
    if row["available"] == 0 and row["checked_at"] and row["checked_at"] > iso(now - RECHECK_UNAVAILABLE):
        return False
    ok = h.source.is_available(h.listing)
    if ok is None:
        return False
    conn.execute("UPDATE listings SET available = ?, checked_at = ? WHERE id = ?", (int(ok), iso(now), h.listing_id))
    if not ok:
        log.info("skipping unavailable listing %s", h.listing.url)
    return ok


# ---------- health ----------

def update_health(conn, s: dict, src: Source, result: RunResult, notifier: Notifier, now: str) -> None:
    health = s.get("health", {})
    hs = health.setdefault(src.name, {"streak": 0, "warned": False, "last_ok": None, "last_error": None})
    errors = result.errors.get(src.name, [])
    if src.health_count(result.parsed.get(src.name, 0)) > 0:
        hs.update(streak=0, warned=False, last_ok=now)
    else:
        hs["streak"] += 1
        hs["last_error"] = errors[-1] if errors else "returned no listings"
    wait = src.min_interval_min
    if hs["streak"] and errors and all("rate-limited" in e for e in errors):
        lo, hi = BLOCK_COOLDOWN_MIN
        wait = max(wait, min(lo * 2 ** (hs["streak"] - 1), hi))
    hs["next_try"] = iso(datetime.fromisoformat(now.replace("Z", "+00:00")) + timedelta(minutes=wait)) if wait else None
    if hs["streak"] >= HEALTH_STREAK and not hs["warned"]:
        retry = f" Backing off; next try {hs['next_try'][11:16]} UTC." if hs["next_try"] and wait > src.min_interval_min else ""
        text = (f"⚠️ Deal Watch: {src.label} has returned nothing for {hs['streak']} runs in a row "
                f"(last error: {hs['last_error']}).{retry} Check the dashboard.")
        try:
            hs["warned"] = notifier.health(text)
        except Exception as ex:
            log.warning("health send failed: %s", ex)
    settings_mod.put(conn, "health", health)
    s["health"] = health


def prune(conn: sqlite3.Connection, s: dict, rules: Rules, now: datetime) -> None:
    conn.execute("DELETE FROM listings WHERE last_seen < ? AND id NOT IN (SELECT listing_id FROM alerts)",
                 (iso(now - timedelta(days=s["listing_retention_days"])),))
    keep = max(rules.market_window_days, rules.store_window_days)
    conn.execute("DELETE FROM price_samples WHERE first_seen < ?", (iso(now - timedelta(days=keep)),))


# ---------- run ----------

def run_scan(conn: sqlite3.Connection, lock_path: Path, notifier: Notifier | None = None, *,
             watch_ids: list[int] | None = None, source_names: list[str] | None = None,
             sources: dict[str, type[Source]] | None = None, sleep: bool = True) -> RunResult:
    result = RunResult(started_at=iso())
    with scan_lock(lock_path) as got:
        if not got:
            log.info("scan already running; skipped")
            result.skipped = True
            return result
        _run(conn, notifier or Notifier(), result, watch_ids, source_names, sources or registry(), sleep)
    return result


def _run(conn, notifier, result, watch_ids, source_names, reg, sleep):
    s = settings_mod.load(conn)
    rules = Rules.from_settings(s)
    watches = enabled_watches(conn, watch_ids)
    ctx = Context(settings=s, rules=rules)
    planned = [n for n in SOURCE_ORDER if n in reg and (source_names is None or n in source_names)
               and any(n in w.sources for w in watches)]
    if source_names is None and watch_ids is None:  # full scans respect each source's cadence and block cooldown
        due = iso()
        waiting = [n for n in planned if ((s.get("health", {}).get(n) or {}).get("next_try") or "") > due]
        if waiting:
            log.info("not due yet: %s", ", ".join(waiting))
        planned = [n for n in planned if n not in waiting]
    names = list(planned)
    deadline = time.monotonic() + s["run_timeout_min"] * 60
    hits: list[Hit] = []
    instances: dict[str, Source] = {}
    browser = None
    try:
        if any(reg[n].needs_browser for n in names):
            try:
                from .sources.browser import Browser
                browser = ctx.browser = Browser(ctx.home)
            except Exception as ex:
                log.error("browser failed to start: %s", ex)
                for n in names:
                    if reg[n].needs_browser:
                        result.errors.setdefault(n, []).append(f"browser failed to start: {ex}")
                names = [n for n in names if not reg[n].needs_browser]
        for n in names:
            src = instances[n] = reg[n](ctx)
            hits += collect(ctx, src, watches, deadline, result, conn, sleep)
            if result.timed_out:
                log.warning("run timeout reached during %s", n)
                break

        now_dt = datetime.now(timezone.utc)
        now = iso(now_dt)
        result.matches = len(hits)
        for h in hits:
            store(conn, h, now)
        learned = learn_prices(conn, hits, rules, now_dt)
        for h in hits:
            med, n = learned.get(h.watch.id, (None, 0))
            typical = effective_typical(med, h.watch)
            h.typical_src = ("your override" if h.watch.override_enabled and h.watch.typical_override is not None
                             else f"{'store prices' if h.watch.typical_basis == 'store' else 'eBay asking'}, {n} samples"
                             if typical is not None else "")
            h.score = score_hit(h, typical, rules)
            sc = h.score
            conn.execute("UPDATE listings SET tier = ?, ratio = ?, reasons = ?, price_total = ? WHERE id = ?",
                         (sc.tier if sc else None, sc.ratio if sc else None, json.dumps(sc.reasons if sc else []),
                          h.listing.price_total, h.listing_id))
        unique = {}
        for h in hits:
            if h.score:
                unique.setdefault((h.listing_id, h.listing.price_total), h)
        for h in sorted(unique.values(), key=lambda h: (TIER_ORDER[h.score.tier], h.score.ratio or 0)):
            handle_alert(conn, h, notifier, result, now_dt, s)
    finally:
        if browser:
            browser.close()

    now = iso()
    for n in planned:
        if n in instances or n in result.errors:  # not reached before a timeout = no verdict
            update_health(conn, s, instances.get(n) or reg[n](ctx), result, notifier, now)
    prune(conn, s, rules, datetime.now(timezone.utc))
    result.finished_at = now
    settings_mod.put(conn, "last_run", {k: v for k, v in result.__dict__.items()})
    log.info("run done: %d matches, %d new alerts, %d sent, parsed %s", result.matches, result.alerts_new,
             result.alerts_sent, result.parsed)
