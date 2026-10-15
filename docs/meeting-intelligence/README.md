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

## Development API (#74, trusted-dev gate)

Public #68 source `3384ac2d50cb6078a632413bd75573ff030ad581` is the adopted bridge. This API sits on reviewed #73 `36ea771005df831b9fd27ffcc00df45f5cc74688`. Live Codex, Groq, Windows containment acceptance, and privacy gates stay with #67. Semantic quality stays with #76. Nothing here closes #69.

Operation version `meeting-essence.operation/v1`. The deterministic provider is explicit configuration only. A normal process leaves the provider unconfigured and does not fabricate a summary.

Seed the public canonical session on an **owned** database, then drive HTTP. Do not point this at shared `recantor_test`. Create a fresh absent database (example name `recantor_issue74_round9`) on the owned cluster at `127.0.0.1:55472` before seeding; drop/recreate if a prior run left poisoned host-cap rows.

```powershell
$env:MEETING_ESSENCE_DEV_API_ENABLED="1"
$env:RECANTOR_MEETING_ESSENCE_PROVIDER="deterministic"
$env:RECANTOR_MEETING_ESSENCE_FIXTURE="tests/fixtures/meeting_intelligence/s01_s06_example.json"
$env:RECANTOR_PG_OWNED="1"
$env:DATABASE_URL="postgresql+psycopg://recantor_test@127.0.0.1:55472/recantor_issue74_round9"
uv run python tests/meeting_essence_fixture_command.py
```

Start the API and worker with the **same** environment (deterministic provider is explicit; no pytest injection). **Submit HTTP derive before** starting the offline worker so the job exists in the ledger.

On **Windows**, `uvicorn` CLI uses a Proactor loop and breaks async psycopg against PostgreSQL. Use the selector-loop helper instead:

```powershell
# Terminal 1 — API (from apps/api)
$env:MEETING_ESSENCE_DEV_API_ENABLED="1"
$env:RECANTOR_MEETING_ESSENCE_PROVIDER="deterministic"
$env:RECANTOR_MEETING_ESSENCE_FIXTURE="tests/fixtures/meeting_intelligence/s01_s06_example.json"
$env:RECANTOR_PG_OWNED="1"
$env:DATABASE_URL="postgresql+psycopg://recantor_test@127.0.0.1:55472/recantor_issue74_round9"
uv run python tests/meeting_essence_dev_api_serve.py --host 127.0.0.1 --port 8000

# POST derive (session UUID from seed output), then terminal 2 — offline worker (same env)
uv run python tests/meeting_essence_offline_worker.py
```

On Linux/macOS, `uv run uvicorn recantor.main:app --host 127.0.0.1 --port 8000` is fine when the default loop matches psycopg.

On Linux with Redis, the Celery entry in `recantor.meeting_essence.scheduling.tasks` uses the same factory import path as the API. Local Windows proof uses the offline worker above; hosted Linux CI still requires Redis for broker-backed wakes.

With the API process using the same environment, #75 calls:

- `POST /api/v1/sessions/{session_id}/meeting-essence/derivations` with `client_request_id` and optional `operation_version`
- `GET .../derivations/{dispatch_id}` for queue, source, audio, STT, checks, current artifact, and prior useful artifact
- `GET .../artifacts/{artifact_id}/results/{layer}?limit=64&cursor=` for `overview`, `topics`, `actions`, `open_items`, `records`, `relations`
- `GET .../artifacts/{artifact_id}/records/{record_id}/evidence`
- `POST .../derivations/{dispatch_id}/cancel` and `.../retry`

A Celery worker started in that same environment imports `scheduling.tasks` and uses this factory. The same snapshot with a completed artifact is a cache hit and does not call the provider again. Integration proof without injected providers: seed command → HTTP derive → offline worker → status/results/evidence; `uv run pytest tests/test_meeting_essence_api.py` mirrors that path in-process.

OpenAPI stays generated and gitignored:

```powershell
cd apps/api
uv run python -c "import json; from recantor.main import app; json.dump(app.openapi(), open('openapi.json','w'), indent=2)"
$env:OPENAPI_INPUT="openapi.json"
pnpm --dir ../web run generate:api
pnpm --dir ../web exec tsc --noEmit
```

Do not commit `openapi.json` or `apps/web/src/api/generated/`.

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
