# Codex Instructions

This repository is public.

- Do not commit local runtime state, task queues, logs, credentials, personal paths, private operator notes, real Codex prompts, JSONL transcripts, session ids, thread ids, or usage-limit messages.
- Keep `README.md`, `docs/`, `examples/`, `tests/`, and source fixtures public-safe and sanitized.
- Treat `.private/`, `.codex-batch-runner/`, `*.local.md`, and `*.local.plist` as private/local-only unless explicitly instructed otherwise.
- `.codex-batch-runner/` is runtime state. Do not use it for project roadmap, task dashboard, proposal, or operator planning documents.
- If `.private/AGENTS.md` exists, read and apply it for local operator instructions. If it is missing, continue with this file only.
