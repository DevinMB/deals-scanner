"""Settings: defaults.json overlaid with rows from the settings table (values are JSON)."""

import json
import sqlite3

from .scoring import DEFAULTS


def load(conn: sqlite3.Connection) -> dict:
    stored = {k: json.loads(v) for k, v in conn.execute("SELECT key, value FROM settings")}
    return {**DEFAULTS, **stored}


def put(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, json.dumps(value)))
