-- Store sources (Amazon, Walmart, Slickdeals): where the typical price comes from, and the store deal level.
ALTER TABLE watches ADD COLUMN typical_basis TEXT NOT NULL DEFAULT 'ebay';  -- 'ebay' | 'store'
ALTER TABLE watches ADD COLUMN retail_deal_ratio REAL NOT NULL DEFAULT 0.75;
