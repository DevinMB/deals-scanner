"""Preview: run a watch's searches once without storing or alerting, and explain every listing.

Runs in a background thread under the scan lock, so a preview never shares the VM's memory
with a scan's browser; it waits for a running scan to finish first.
"""

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field

from . import db, settings as settings_mod
from .config import Config
from .models import Watch
from .runner import SOURCE_ORDER, Hit, RunResult, scan_lock, score_hit
from .scoring import Rules, effective_typical, explain, is_market_sample, looks_like_bundle, typical_price
from .sources import Context, registry

log = logging.getLogger("dealwatch.preview")
MAX_JOBS = 20
LOCK_WAIT = 15 * 60
BODY_CAP = 10


@dataclass
class PreviewJob:
    id: str
    watch: Watch
    status: str = "waiting"  # waiting | running | done | error
    progress: list[str] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    learned: float | None = None
    samples: int = 0
    typical: float | None = None
    error: str | None = None
    started: float = field(default_factory=time.time)

    @property
    def failures(self):
        return [p for p in self.progress if ": failed: " in p]

    @property
    def matches(self):
        return [r for r in self.rows if r["match"]]

    @property
    def alerts(self):
        return [r for r in self.rows if r["tier"]]

    @property
    def excluded_by_reason(self) -> list[tuple[str, list[dict]]]:
        groups: dict[str, list[dict]] = {}
        for r in self.rows:
            if not r["match"]:
                groups.setdefault(r["reason"], []).append(r)
        return sorted(groups.items(), key=lambda kv: -len(kv[1]))


JOBS: dict[str, PreviewJob] = {}


def start(cfg: Config, watch: Watch) -> PreviewJob:
    job = PreviewJob(id=uuid.uuid4().hex[:12], watch=watch)
    JOBS[job.id] = job
    for old in sorted(JOBS.values(), key=lambda j: j.started)[:-MAX_JOBS]:
        JOBS.pop(old.id, None)
    threading.Thread(target=_run, args=(job, cfg), daemon=True).start()
    return job


def _run(job: PreviewJob, cfg: Config) -> None:
    deadline = time.time() + LOCK_WAIT
    try:
        while True:
            with scan_lock(cfg.data_dir / "scan.lock") as got:
                if got:
                    job.status = "running"
                    _search(job, cfg)
                    job.status = "done"
                    return
            if time.time() > deadline:
                raise TimeoutError("a scan has been running for 15 minutes; try again later")
            if not job.progress or job.progress[-1] != "Waiting for the running scan to finish…":
                job.progress.append("Waiting for the running scan to finish…")
            time.sleep(5)
    except Exception as ex:
        log.exception("preview failed")
        job.status, job.error = "error", str(ex)


def _search(job: PreviewJob, cfg: Config) -> None:
    w = job.watch
    conn = db.connect(cfg.db_path)
    try:
        s = settings_mod.load(conn)
    finally:
        conn.close()
    rules = Rules.from_settings(s)
    ctx = Context(settings=s, rules=rules)
    reg = registry()
    names = [n for n in SOURCE_ORDER if n in w.sources and n in reg]
    hits, browser = [], None
    try:
        if any(reg[n].needs_browser for n in names):
            from .sources.browser import Browser
            browser = ctx.browser = Browser(ctx.home)
        for n in names:
            src, seen, bodies = reg[n](ctx), set(), 0
            for q in w.search_queries:
                job.progress.append(f"{src.label}: searching “{q}”…")
                try:
                    listings = src.search(q, w)
                except Exception as ex:
                    job.progress.append(f"{src.label}: failed: {ex}")
                    continue
                job.progress.append(f"{src.label}: {len(listings)} listings")
                for l in listings:
                    if l.source_id in seen:
                        continue
                    seen.add(l.source_id)
                    m, reason = explain(l.title, w, rules)
                    if not m and src.has_body and bodies < BODY_CAP and looks_like_bundle(l.title, w, rules):
                        bodies += 1
                        m, reason = explain(l.title, w, rules, src.body(l))
                    if n == "ebay" and l.location and "united states" not in l.location.lower():
                        m, reason = None, "outside the US"
                    row = {"source": src.label, "title": l.title, "url": l.url, "price": l.price_total,
                           "location": l.location, "match": m, "reason": reason, "tier": None, "ratio": None,
                           "reasons": []}
                    job.rows.append(row)
                    if m:
                        hits.append((Hit(w, src, l, m), row))
    finally:
        if browser:
            browser.close()
    prices = [h.listing.price_total for h, _ in hits
              if h.source.name == "ebay" and h.listing.price_total and is_market_sample(h.match, h.listing.location)]
    job.learned, job.samples = typical_price(prices, rules)
    job.typical = effective_typical(job.learned, w)
    for h, row in hits:
        sc = score_hit(h, job.typical, rules)
        row["price"] = h.listing.price_total
        if sc:
            row.update(tier=sc.tier, ratio=sc.ratio, reasons=sc.reasons)
    job.rows.sort(key=lambda r: (not r["match"], r["tier"] is None, r["ratio"] if r["ratio"] is not None else 9))
