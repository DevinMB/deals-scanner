-- Toggle for typical_override: when on and set, the override beats the learned median.
ALTER TABLE watches ADD COLUMN override_enabled INTEGER NOT NULL DEFAULT 1;
