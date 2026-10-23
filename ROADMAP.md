# Roadmap

Build steps 1–7 follow the brief: https://claude.ai/artifact/VDZaeNx8XW5fauM7nG1ELY

## After step 6: LLM-assisted watch creation

- "Suggest" button on the new-watch form fills search terms, include regex, exclude terms and a rough
  typical price from a plain description ("Herman Miller Aeron size B"). The user reviews, runs Preview, saves.
- Backend: the LAN Ollama server at `http://192.168.1.197:11434` (reachable from deals-lab, checked 2026-10-23).
  Configure via an optional `OLLAMA_URL` env var; pick the model in Settings.
- Called only on button press, never during scans. Form works by hand when Ollama is off or unset.

## Decisions

- 2026-10-23: watch exclude_terms are regex (per-watch blacklist); GPU-only blacklist terms live in `gpu_import_exclude` for the step 7 importer. `typical_override` wins whenever set and `override_enabled` (default on).
