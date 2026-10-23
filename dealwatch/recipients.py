"""Telegram recipients and which watches alert them."""

import sqlite3


def all(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM recipients ORDER BY is_owner DESC, name COLLATE NOCASE").fetchall()


def owner_id(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT id FROM recipients WHERE is_owner = 1").fetchone()[0]


def for_watch(conn: sqlite3.Connection, watch_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT r.* FROM recipients r JOIN watch_recipients wr ON wr.recipient_id = r.id "
                        "WHERE wr.watch_id = ? ORDER BY r.is_owner DESC, r.name COLLATE NOCASE", (watch_id,)).fetchall()


def set_for_watch(conn: sqlite3.Connection, watch_id: int, recipient_ids: list[int]) -> None:
    conn.execute("DELETE FROM watch_recipients WHERE watch_id = ?", (watch_id,))
    conn.executemany("INSERT OR IGNORE INTO watch_recipients (watch_id, recipient_id) VALUES (?, ?)",
                     [(watch_id, r) for r in recipient_ids])


def add_owner_to(conn: sqlite3.Connection, watch_id: int) -> None:
    """Default for watches made outside the form (GPU import, CLI): alert the owner."""
    conn.execute("INSERT OR IGNORE INTO watch_recipients (watch_id, recipient_id) VALUES (?, ?)",
                 (watch_id, owner_id(conn)))
