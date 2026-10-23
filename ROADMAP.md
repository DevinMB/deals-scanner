# Roadmap

Build steps 1–7 follow the brief: https://claude.ai/artifact/VDZaeNx8XW5fauM7nG1ELY

## After step 6: LLM-assisted watch creation

- "Suggest" button on the new-watch form fills search terms, include regex, exclude terms and a rough
  typical price from a plain description ("Herman Miller Aeron size B"). The user reviews, runs Preview, saves.
- Backend: the LAN Ollama server at `http://192.168.1.197:11434` (reachable from deals-lab, checked 2026-10-23).
  Configure via an optional `OLLAMA_URL` env var; pick the model in Settings.
- Called only on button press, never during scans. Form works by hand when Ollama is off or unset.

## After step 6: extra Telegram recipients per watch

- A "People" tab maps a name to a Telegram chat ID (e.g. "Alex → 123456789").
- Each watch has a list of people to alert in addition to the owner's `TELEGRAM_CHAT_ID`.
- Each person must open the bot and press Start once; Telegram doesn't let bots message someone first.
  "Send test message" per person confirms it works. Health warnings go to the owner only.
- Data: `recipients (id, name, chat_id)` + `watch_recipients (watch_id, recipient_id)`; the step 5 sender
  already takes a list of chat IDs, and alert rows record delivery per recipient so one failed chat retries alone.

## Step 8 (last): UI review pass

- Drive every page with Playwright at desktop and phone widths, screenshot each, and clean up layout,
  wording and flow wherever it could be clearer or nicer. Done after the GPU import and soak test.

## Decisions

- 2026-10-23: watch exclude_terms are regex (per-watch blacklist); GPU-only blacklist terms live in `gpu_import_exclude` for the step 7 importer. `typical_override` wins whenever set and `override_enabled` (default on).
- 2026-10-23: first live scan showed parts/accessories outnumbering the item in eBay results (Aeron median
  learned as $80). Added generic accessory detection ("... for/fits/compatible with <item>"), put
  `replacement` and `compatible with` back in the global blacklist, and drop samples under 25% of the
  90th-percentile price before taking the median. Aeron median became $588 on the same data.
