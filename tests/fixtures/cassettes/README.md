# Cassettes

JSON Lines pre-recorded LLM responses consumed by
`hostlens.agent.backends.playback.PlaybackBackend` so integration tests
exercise the Agent loop without burning Anthropic API tokens. One file
per scenario; one record per line. Cassette-miss fails fast (no
fallback to real API).

## File format

Each line is a self-contained JSON object:

```json
{"request": {...}, "response": {...}, "tools_schema_hash": "<sha256-hex>"}
```

- `request`: the matching key payload. `PlaybackBackend` hashes
  `{"model", "messages", "tools_count"}` (SHA-256, `sort_keys=True`).
  `system`, `max_tokens`, full `tools`, and `timeout` are NOT in the
  key (see spec for trade-off).
- `response`: a valid `MessageResponse` payload (`id`, `model`, `role`,
  `content[]`, `stop_reason`, `usage`); validated at load time.
- `tools_schema_hash` (optional, lint-only): SHA-256 of tools schema at
  record time; `--check-schema-drift` warns on drift, never blocks.

## Recording flow (manual, M2)

M2 has no recorder. To add a record:

1. Capture a real `messages.create` request + response.
2. Build the JSON object above; scrub all secrets (API keys, bearer
   tokens, JWTs, IPv4, `/Users/...` paths, hostnames, `key=value`
   credentials).
3. Append the line to the cassette file.
4. Run `python scripts/cassette_lint.py`; exit 0 required before commit.

Filename convention: `<verb>_<entity>_<purpose>.jsonl`. Do not reuse
filenames across unrelated scenarios — the request-key hash domain is
per-file, so collisions are silent.

## What `cassette_lint.py` checks

- Each record validates against `MessageResponse`.
- No line contains a pattern matched by `hostlens.core.redact`.
- `--check-schema-drift --current-tools-hash <hex>` warns on drift.
