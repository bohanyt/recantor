# Meeting essence contracts (#70)

Python package: `recantor.meeting_essence` (Pydantic contracts, validation, prompt loader, serial offline engine, bounded stage provider protocol).

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

Structural / schema validation does **not** claim live model extraction accuracy. The offline engine (`recantor-meeting-engine-run`) uses a **deterministic fixture provider**. With independent annotations it checks each page as `offline-fixture-annotation-v1`. Without them it stays structural and partial. A custom provider may instead complete `source-aware-binding-v1` from the source text and claims on the request; that is still behavioral evidence, not live model accuracy. Overview text is checked and does not replace topic, action, or open-item detail. Relationship checks require both endpoint records and the cited source span, hash, and time; a matching type/from/to triple with different evidence does not pass.

```powershell
uv run recantor-meeting-engine-run tests/fixtures/meeting_intelligence/s01_s06_example.json
```

Engine runs print `schema_identity`, `prompt_cache_key`, `snapshot_content_hash`, and a JSON trace of stage wire audits (requested/observed model remain null on deterministic runs).

### Snapshot hashing

- Segment `content_hash` = SHA-256 of the segment's exact UTF-8 bytes (preserves line endings, outer whitespace, and codepoint offsets; CRLF and LF produce different hashes).
- `snapshot_content_hash` = SHA-256 of canonical JSON (sorted keys, compact separators) over contract version, session id, publication boundary, canonicalized `meeting_metadata` (sorted keys), ordered segment fingerprints (id, sequence, timing, language, created_at, content_hash), and coverage flags.
- `captured_at` is intentionally excluded so unchanged transcript snapshots stay stable across re-capture timing updates.
- `snapshot_id` = `snap_` + first 24 hex chars of SHA-256(`session_id` + `:` + `snapshot_content_hash`).
