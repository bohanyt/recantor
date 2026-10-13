# Meeting essence contracts (#70)

Python package: `recantor.meeting_essence` (Pydantic contracts, validation, prompt loader, bounded stage provider protocol).

Legacy Codex helper module `#68` remains `recantor.meeting_intelligence` (single `.py` module). `#74` owns any layered→v1 adapter using exact helper wire keys.

## Read order

1. GitHub #69 (product SOP and adversarial fixtures)
2. `docs/meeting-intelligence/SOP.md`
3. `apps/api/src/recantor/meeting_essence/contracts.py`

## Validate public fixtures

From `apps/api` with the project virtual environment:

```powershell
uv run python -m recantor.meeting_essence tests/fixtures/meeting_intelligence/s01_s06_example.json
uv run python -m recantor.meeting_essence tests/fixtures/meeting_intelligence/informational_example.json
```

Or:

```powershell
uv run recantor-meeting-validate tests/fixtures/meeting_intelligence/s01_s06_example.json
```

Successful runs print `schema_identity`, prompt SHA-256 values, `prompt_cache_key`, and `validation=OK`.

## Tests (no database)

```powershell
uv run pytest tests/test_meeting_essence_contracts.py -q
```

Structural / schema validation does **not** claim live model extraction accuracy. Gold fixtures mark `structural_check_status=completed` only; semantic forward/backward checks default to `not_started` until `#71+` runs them with an explicit `semantic_check_method`.

### Snapshot hashing

- Segment `content_hash` = SHA-256 of the segment's exact UTF-8 bytes (preserves line endings, outer whitespace, and codepoint offsets; CRLF and LF produce different hashes).
- `snapshot_content_hash` = SHA-256 of canonical JSON (sorted keys, compact separators) over contract version, session id, publication boundary, canonicalized `meeting_metadata` (sorted keys), ordered segment fingerprints (id, sequence, timing, language, created_at, content_hash), and coverage flags.
- `captured_at` is intentionally excluded so unchanged transcript snapshots stay stable across re-capture timing updates.
- `snapshot_id` = `snap_` + first 24 hex chars of SHA-256(`session_id` + `:` + `snapshot_content_hash`).
