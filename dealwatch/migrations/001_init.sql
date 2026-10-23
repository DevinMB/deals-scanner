-- Initial schema. List-valued columns hold JSON arrays as TEXT.

CREATE TABLE watches (
    id                INTEGER PRIMARY KEY,
    name              TEXT    NOT NULL,
    enabled           INTEGER NOT NULL DEFAULT 1,
    search_queries    TEXT    NOT NULL DEFAULT '[]',
    include_regex     TEXT,
    exclude_terms     TEXT    NOT NULL DEFAULT '[]',
    sources           TEXT    NOT NULL DEFAULT '["ebay","craigslist","offerup","rss"]',
    deal_ratio        REAL    NOT NULL DEFAULT 0.60,
    local_deal_ratio  REAL    NOT NULL DEFAULT 0.50,
    scam_ratio        REAL    NOT NULL DEFAULT 0.30,
    max_price         REAL,
    typical_override  REAL,
    longshot_enabled  INTEGER NOT NULL DEFAULT 1,
    quiet_longshots   INTEGER NOT NULL DEFAULT 1,
    radius_miles      INTEGER,
    bundle_enabled    INTEGER NOT NULL DEFAULT 0,
    bundle_regex      TEXT,
    bundle_ratio      REAL    NOT NULL DEFAULT 0.85,
    created_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Key/value store; value is JSON.
CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE listings (
    id              INTEGER PRIMARY KEY,
    source          TEXT    NOT NULL,
    source_id       TEXT    NOT NULL,
    watch_id        INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
    title           TEXT    NOT NULL,
    url             TEXT    NOT NULL,
    price_total     REAL,
    location        TEXT,
    distance_mi     REAL,
    seller          TEXT,
    seller_fb_count INTEGER,
    seller_fb_pct   REAL,
    condition       TEXT,
    first_seen      TEXT    NOT NULL,
    last_seen       TEXT    NOT NULL,
    available       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (source, source_id, watch_id)
);
CREATE INDEX listings_watch_last_seen ON listings (watch_id, last_seen);

CREATE TABLE price_samples (
    id         INTEGER PRIMARY KEY,
    watch_id   INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
    source_id  TEXT    NOT NULL,
    price      REAL    NOT NULL,
    first_seen TEXT    NOT NULL,
    UNIQUE (watch_id, source_id)
);
CREATE INDEX price_samples_watch_seen ON price_samples (watch_id, first_seen);

-- Dedup: a listing re-alerts only when its price changes.
CREATE TABLE alerts (
    id               INTEGER PRIMARY KEY,
    listing_id       INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    watch_id         INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
    price_total      REAL    NOT NULL,
    tier             TEXT    NOT NULL,
    ratio            REAL,
    typical_at_alert REAL,
    reasons          TEXT    NOT NULL DEFAULT '[]',
    sent_at          TEXT,
    telegram_ok      INTEGER NOT NULL DEFAULT 0,
    UNIQUE (listing_id, price_total)
);
CREATE INDEX alerts_sent_at ON alerts (sent_at);

CREATE TABLE runs (
    id             INTEGER PRIMARY KEY,
    started_at     TEXT    NOT NULL,
    finished_at    TEXT,
    source         TEXT,
    watch_id       INTEGER REFERENCES watches(id) ON DELETE SET NULL,
    listings_found INTEGER,
    error          TEXT
);
CREATE INDEX runs_source_started ON runs (source, started_at);
