-- Ollama "is this the item or a part?" verdict per listing, so each listing is asked once.
ALTER TABLE listings ADD COLUMN llm_verdict TEXT;
ALTER TABLE listings ADD COLUMN llm_reason TEXT;
