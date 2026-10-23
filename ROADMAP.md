# Roadmap

Build steps 1–7 follow the brief: https://claude.ai/artifact/VDZaeNx8XW5fauM7nG1ELY

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
- 2026-10-23: Ollama Suggest built (user asked): on the watch form, Suggest proposes search terms, whole-word
  excludes and a rough price as tickable chips; Apply merges them in, nothing is overwritten. qwen3.5:4b (~2.5 s)
  beat the 9B, which invented models and copied the prompt example. Suggestions still need a human glance.
- 2026-10-23: GPU import moved off the dashboard into Settings; user trimmed watches to RTX 3090, Aeron, X100V.
- 2026-10-23: Store sources added (user asked): Amazon (shared browser), Walmart (page JSON over HTTP), Slickdeals
  keyword RSS. Per watch: "Typical price from" eBay or Amazon/Walmart prices (one sample per product per day,
  30-day window; Slickdeals never counts since its prices are already deals), and a store deal level (default 25%
  below). Prices compared per listing (user's choice), so store watches should name the pack size.
  Amazon and Walmart block intermittently: both detect the block page, back off and retry once; Walmart flagged
  this IP after a night of testing.
