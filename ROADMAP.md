# Roadmap

Build steps 1–7 follow the brief: https://claude.ai/artifact/VDZaeNx8XW5fauM7nG1ELY

## After step 6: LLM-assisted watch creation

- "Suggest" button on the new-watch form fills search terms, include regex, exclude terms and a rough
  typical price from a plain description ("Herman Miller Aeron size B"). The user reviews, runs Preview, saves.
- Backend: the LAN Ollama server at `http://192.168.1.197:11434` (reachable from deals-lab, checked 2026-10-23).
  Configure via an optional `OLLAMA_URL` env var; pick the model in Settings.
- Called only on button press, never during scans. Form works by hand when Ollama is off or unset.

## Open decisions

- Global `hard_blacklist` was copied from the GPU scanner and contains GPU-only terms (`laptop`, `mobile`,
  `cable`, `bridge`, `optiplex`, `3060 3070`, brand "for ..." patterns). They would block non-GPU watches
  such as a laptop. Proposed for step 7: keep generic terms global, move GPU-only ones onto the imported GPU watches.
- `typical_override` is used only while a watch has fewer than 6 samples; the learned median wins after that.
