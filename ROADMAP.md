# Roadmap

Build steps 1–7 follow the brief: https://claude.ai/artifact/VDZaeNx8XW5fauM7nG1ELY

## After step 6: LLM-assisted watch creation

- "Suggest" button on the new-watch form fills search terms, include regex, exclude terms and a rough
  typical price from a plain description ("Herman Miller Aeron size B"). The user reviews, runs Preview, saves.
- Backend: the LAN Ollama server at `http://192.168.1.197:11434` (reachable from deals-lab, checked 2026-10-23).
  Configure via an optional `OLLAMA_URL` env var; pick the model in Settings.
- Called only on button press, never during scans. Form works by hand when Ollama is off or unset.

## Step 8 (last): UI review pass

- Drive every page with Playwright at desktop and phone widths, screenshot each, and clean up layout,
  wording and flow wherever it could be clearer or nicer. Done after the GPU import and soak test.

## Decisions

- 2026-10-23: watch exclude_terms are regex (per-watch blacklist); GPU-only blacklist terms live in `gpu_import_exclude` for the step 7 importer. `typical_override` wins whenever set and `override_enabled` (default on).
- 2026-10-23: first live scan showed parts/accessories outnumbering the item in eBay results (Aeron median
  learned as $80). Added generic accessory detection ("... for/fits/compatible with <item>"), put
  `replacement` and `compatible with` back in the global blacklist, and drop samples under 25% of the
  90th-percentile price before taking the median. Aeron median became $588 on the same data.
- 2026-10-23: Ollama part check added (user's choice): alert candidates are asked "item or part?" before sending,
  verdict cached per listing, bundles skipped, Ollama down = alert marked "not checked". Default model
  qwen3.5:4b, picked on 27 labeled titles from the first scans (27/27 at ~2 s; qwen3:4b 22/27; the 9B 23/27).
  Price is left out of the prompt: with it, small models called real cheap deals "parts".
- 2026-10-23: GPU import brings fallback_median over as typical_override, switched on (user's choice).
- 2026-10-23: Telegram recipients built (user asked): Settings → Telegram recipients (you = "Me", chat from .env;
  add people by "Find people who messaged the bot" or chat ID; rename, test, remove), per-watch "Send alerts to"
  picker defaulting to you, delivery tracked per person so a failed chat retries alone. Health warnings: owner only.
