"""One-click import of the GPU scanner's 17 models as watches.

Each model becomes a bundle-enabled watch: its regex as the include pattern, its excludes plus
the GPU-only blacklist as exclude patterns, and its fallback_median as an override that is on.
Re-importing updates watches with the same name in place, keeping their history.
"""

import json
import re
import sqlite3
from pathlib import Path

from . import recipients
from .scoring import DEFAULTS

GPU = json.loads((Path(__file__).parent / "gpu_import.json").read_text())


def watch_rows() -> list[dict]:
    rows = []
    for m in GPU["models"]:
        excludes = [re.escape(x.strip()) if x.strip() else x for x in m.get("exclude", [])]
        rows.append({
            "name": m["name"], "search_queries": [m["query"]], "include_regex": m["regex"],
            "exclude_terms": excludes + DEFAULTS["gpu_import_exclude"],
            "sources": ["ebay", "craigslist", "offerup", "rss"], "enabled": 1,
            "deal_ratio": GPU["deal_ratio"], "local_deal_ratio": GPU["local_deal_ratio"], "scam_ratio": GPU["scam_ratio"],
            "typical_override": m["fallback_median"], "override_enabled": 1,
            "bundle_enabled": 1, "bundle_ratio": GPU["pc_ratio"],
        })
    return rows


def run(conn: sqlite3.Connection) -> tuple[int, int]:
    """Returns (created, updated)."""
    created = updated = 0
    for r in watch_rows():
        vals = {k: json.dumps(v) if isinstance(v, list) else v for k, v in r.items()}
        row = conn.execute("SELECT id FROM watches WHERE name = ? COLLATE NOCASE", (r["name"],)).fetchone()
        if row:
            conn.execute(f"UPDATE watches SET {', '.join(f'{k} = ?' for k in vals)} WHERE id = ?", [*vals.values(), row[0]])
            updated += 1
        else:
            wid = conn.execute(f"INSERT INTO watches ({', '.join(vals)}) VALUES ({', '.join('?' * len(vals))})",
                               list(vals.values())).lastrowid
            recipients.add_owner_to(conn, wid)
            created += 1
    return created, updated
