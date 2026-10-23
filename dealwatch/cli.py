"""Command-line tools.

  python -m dealwatch.cli search <source> "<query>" [--regex R] [--exclude T ...] [--bundle] [--all]

Prints the listings a source parses for a query, marking which ones match the ad-hoc watch.
"""

import argparse
import logging
import sys

from .models import Watch
from .scoring import classify
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
    args = p.parse_args(argv)
    return search(args)


if __name__ == "__main__":
    sys.exit(main())
