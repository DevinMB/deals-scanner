"""Command-line tools.

  python -m dealwatch.cli search <source> "<query>" [--regex R] [--exclude T ...] [--bundle] [--all]
      Print the listings a source parses for a query, marking which ones match an ad-hoc watch.
  python -m dealwatch.cli watch add "<name>" --query Q [--query Q ...] [--override 800] [--max-price 400] ...
  python -m dealwatch.cli watch list
  python -m dealwatch.cli scan [--watch ID ...] [--source S ...] [--dry-run]
      Run one scan now against the app database (skipped if a scan is already running).
  python -m dealwatch.cli telegram-test
  python -m dealwatch.cli soak-report [--since 2026-10-23T18:45:00Z | --hours 48]
"""

import argparse
import json
import logging
import sys

from . import config, db, telegram
from .models import Watch
from .runner import market, run_scan
from .scoring import Rules, classify
from .settings import load as load_settings
from .sources import Context, registry


def search(args) -> int:
    sources = registry()
    if args.source not in sources:
        print(f"unknown source {args.source!r}; choose from {', '.join(sources)}", file=sys.stderr)
        return 2
    watch = Watch(id=None, name=args.query, search_queries=[args.query], include_regex=args.regex,
                  exclude_terms=args.exclude, bundle_enabled=args.bundle)
    ctx = Context()
    cls = sources[args.source]
    browser = None
    if cls.needs_browser:
        from .sources.browser import Browser
        browser = ctx.browser = Browser(ctx.home)
    try:
        src = cls(ctx)
        listings = src.search(args.query, watch)
        if args.body:
            bodies = {l.source_id: src.body(l) for l in listings[:args.body]}
    finally:
        if browser:
            browser.close()
    shown = 0
    for l in listings:
        m = classify(l.title, watch, ctx.rules, bodies.get(l.source_id, "") if args.body else "")
        if not m and not args.all:
            continue
        shown += 1
        tag = "--" if not m else f"x{m.count}" + (" bundle" if m.is_bundle else "") + (f" flags={m.flags}" if m.flags else "")
        price = f"${l.price_total:,.0f}" if l.price_total is not None else "?"
        seller = f" | {l.seller} ({l.seller_fb_count:g}, {l.seller_fb_pct}%)" if l.seller_fb_count is not None else ""
        print(f"{price:>8}  {tag:<10} {l.title[:90]}")
        print(f"{'':10}{l.location or ''}{seller}  {l.url}")
    print(f"\n{len(listings)} parsed, {shown} shown ({'all' if args.all else 'matching only'})")
    return 0


def watch_add(args) -> int:
    conn = _db()
    wid = conn.execute(
        "INSERT INTO watches (name, search_queries, include_regex, exclude_terms, sources, typical_override,"
        " max_price, bundle_enabled) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (args.name, json.dumps(args.query), args.regex, json.dumps(args.exclude),
         json.dumps(args.sources or list(registry())), args.override, args.max_price, int(args.bundle))).lastrowid
    from . import recipients
    recipients.add_owner_to(conn, wid)
    print(f"added watch {wid}: {args.name}")
    return 0


def watch_list(args) -> int:
    conn = _db()
    rules = Rules.from_settings(load_settings(conn))
    for r in conn.execute("SELECT * FROM watches ORDER BY id"):
        w = Watch.from_row(r)
        med, n, newest = market(conn, w.id, rules)
        override = f"${w.typical_override:,.0f}{'' if w.override_enabled else ' (off)'}" if w.typical_override else "-"
        print(f"{w.id:>3} {'on ' if w.enabled else 'off'} {w.name:<28} learned={f'${med:,.0f}' if med else '-'} "
              f"({n} samples) override={override} max={w.max_price or '-'} sources={','.join(w.sources)}")
    return 0


def scan(args) -> int:
    cfg = config.load()
    notifier = telegram.from_config(cfg, lambda: db.connect(cfg.db_path), dry_run=args.dry_run)
    r = run_scan(_db(), cfg.data_dir / "scan.lock", notifier, watch_ids=args.watch or None,
                 source_names=args.source or None)
    if r.skipped:
        print("a scan is already running")
        return 1
    print(json.dumps(r.__dict__, indent=1))
    return 0


def telegram_test(args) -> int:
    ok, reason = telegram.from_config(config.load()).test()
    print("sent" if ok else f"not sent: {reason}")
    return 0 if ok else 1


def soak_report(args) -> int:
    """How the app has done since a time: scan cadence, per-source results, alerts. For the 48 h soak test."""
    from datetime import datetime, timedelta, timezone
    conn = _db()
    since = args.since or (datetime.now(timezone.utc) - timedelta(hours=args.hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    times = [datetime.fromisoformat(r[0].replace("Z", "+00:00"))
             for r in conn.execute("SELECT started_at FROM runs WHERE started_at >= ? ORDER BY 1", (since,))]
    starts = []  # a scan = a burst of run rows; a quiet gap of 5+ min starts the next one
    for t in times:
        if not starts or t - last > timedelta(minutes=5):
            starts.append(t)
        last = t
    gaps = [f"{a:%m-%d %H:%M}→{b:%H:%M}" for a, b in zip(starts, starts[1:]) if b - a > timedelta(minutes=50)]
    print(f"since {since}: {len(starts)} scans; gaps over 50 min: {gaps or 'none'}")
    for r in conn.execute("SELECT source, COUNT(*), SUM(listings_found > 0), SUM(error IS NOT NULL), MAX(finished_at) "
                          "FROM runs WHERE started_at >= ? GROUP BY source", (since,)):
        print(f"  {r[0]:<11} {r[1]:>4} searches, {r[2]:>4} with results, {r[3]:>4} errors, last {r[4]}")
    for r in conn.execute("SELECT tier, COUNT(*), SUM(telegram_ok) FROM alerts WHERE created_at >= ? GROUP BY tier", (since,)):
        print(f"  alerts {r[0]:<11} {r[1]} created, {r[2]} sent")
    h = load_settings(conn).get("health", {})
    print("  health:", {k: f"streak {v['streak']}" for k, v in h.items()})
    return 0


def _db():
    conn = db.connect(config.load().db_path)
    db.migrate(conn)
    return conn


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="dealwatch")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search", help="print parsed listings from one source")
    s.add_argument("source")
    s.add_argument("query")
    s.add_argument("--regex", help="include_regex for the ad-hoc watch")
    s.add_argument("--exclude", action="append", default=[], help="exclude pattern (repeatable)")
    s.add_argument("--bundle", action="store_true", help="also match inside bundles")
    s.add_argument("--body", type=int, default=0, metavar="N", help="fetch bodies for the first N listings")
    s.add_argument("--all", action="store_true", help="show non-matching listings too")
    s.set_defaults(fn=search)

    w = sub.add_parser("watch", help="add or list watches").add_subparsers(dest="watch_cmd", required=True)
    a = w.add_parser("add")
    a.add_argument("name")
    a.add_argument("--query", action="append", required=True)
    a.add_argument("--regex")
    a.add_argument("--exclude", action="append", default=[])
    a.add_argument("--source", dest="sources", action="append", choices=list(registry()))
    a.add_argument("--override", type=float, help="typical price override")
    a.add_argument("--max-price", type=float)
    a.add_argument("--bundle", action="store_true")
    a.set_defaults(fn=watch_add)
    w.add_parser("list").set_defaults(fn=watch_list)

    sc = sub.add_parser("scan", help="run one scan now")
    sc.add_argument("--watch", type=int, action="append")
    sc.add_argument("--source", action="append", choices=list(registry()))
    sc.add_argument("--dry-run", action="store_true", help="print Telegram messages instead of sending")
    sc.set_defaults(fn=scan)
    sub.add_parser("telegram-test", help="send a Telegram test message").set_defaults(fn=telegram_test)
    so = sub.add_parser("soak-report", help="scan cadence, source results and alerts since a time")
    so.add_argument("--since", help="ISO UTC, e.g. 2026-10-23T18:45:00Z")
    so.add_argument("--hours", type=float, default=48)
    so.set_defaults(fn=soak_report)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
