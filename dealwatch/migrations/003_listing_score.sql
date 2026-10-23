-- Latest score and cached detail text per listing, for the UI and to avoid refetching bodies.
ALTER TABLE listings ADD COLUMN tier TEXT;
ALTER TABLE listings ADD COLUMN ratio REAL;
ALTER TABLE listings ADD COLUMN reasons TEXT NOT NULL DEFAULT '[]';
ALTER TABLE listings ADD COLUMN body TEXT;
ALTER TABLE listings ADD COLUMN checked_at TEXT;
CREATE INDEX listings_source_id ON listings (source, source_id);
