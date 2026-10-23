"""Command-line tools.

  python -m dealwatch.cli search <source> "<query>" [--regex R] [--exclude T ...] [--bundle] [--all]
      Print the listings a source parses for a query, marking which ones match an ad-hoc watch.
  python -m dealwatch.cli watch add "<name>" --query Q [--query Q ...] [--override 800] [--max-price 400] ...
  python -m dealwatch.cli watch list
  python -m dealwatch.cli scan [--watch ID ...] [--source S ...]
      Run one scan now against the app database (skipped if a scan is already running).
"""

import argparse
import json
import logging
import sys

from . import config, db
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
    r = run_scan(_db(), cfg.data_dir / "scan.lock", watch_ids=args.watch or None, source_names=args.source or None)
    if r.skipped:
        print("a scan is already running")
        return 1
    print(json.dumps(r.__dict__, indent=1))
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
    sc.set_defaults(fn=scan)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
