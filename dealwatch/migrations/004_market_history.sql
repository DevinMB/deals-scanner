-- One learned-median snapshot per watch per day, for the price-history chart
-- (price_samples only keeps the 14-day market window).
CREATE TABLE market_history (
    watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
    day      TEXT    NOT NULL,
    median   REAL    NOT NULL,
    samples  INTEGER NOT NULL,
    PRIMARY KEY (watch_id, day)
);

-- Pending alerts have no sent_at; the Alerts page needs a time for them.
ALTER TABLE alerts ADD COLUMN created_at TEXT;
