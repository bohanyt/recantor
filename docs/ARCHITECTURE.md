# Architecture

## Status

This document defines the initial architecture contract for Recantor. It is intentionally conservative: a modular monolith for product logic, separate workers only where runtime/resource boundaries justify them, and explicit durability before AI processing.

Changes that violate the invariants in this document require an ADR.

## Architectural goals

Recantor should:

- reliably capture long meetings from desktop browsers;
- support multiple concurrent recording sessions;
- tolerate temporary network/provider/worker failures without losing acknowledged audio;
- provide low-latency live transcription when infrastructure is healthy;
- degrade into delayed processing rather than lost recording;
- support responsive mobile web without pretending mobile browser lifecycle guarantees exist;
- allow future Android/iOS recorder clients without replacing the backend;
- be straightforward for humans and coding agents to understand and operate;
- remain self-hostable with ordinary infrastructure.

## Non-negotiable invariants

### Capture is independent from intelligence

The durable recording path must not depend on:

- Groq;
- local GPU inference;
- diarization;
- an LLM;
- live WebSocket delivery;
- summary generation.

### Server sessions are canonical

Every recording belongs to a server-created session with a stable identifier and explicit lifecycle.

The browser may cache state for recovery, but it does not own canonical session truth.

### Acknowledgement means durable

A server ACK for an audio chunk means Recantor has durably accepted that exact session/sequence payload according to the configured storage durability contract.

Do not acknowledge merely because bytes entered process memory or a transient queue.

### Ingestion is idempotent

The tuple `(session_id, sequence)` identifies a browser-recording chunk. Retrying it must be safe.

Where integrity matters, store and compare a content hash so a conflicting payload with an already accepted sequence is rejected rather than silently overwritten.

### Unknown continuity is a gap

When audio continuity cannot be proven, persist/report a gap interval or interruption event.

## System overview

```text
                           Recantor

     Browser / future native clients / file uploads
                         |
               HTTPS + WebSocket
                         |
                         v
                 +---------------+
                 |  FastAPI API  |
                 | modular mono. |
                 +-------+-------+
                         |
          +--------------+-------------------+
          |              |                   |
          v              v                   v
     PostgreSQL       Audio storage       Redis
   canonical state    canonical audio   queues/coord.
          |                                  |
          |                         +--------+--------+
          |                         |                 |
          |                         v                 v
          |                    Celery workers     realtime
          |                         |              delivery
          |               +---------+---------+
          |               |         |         |
          |               v         v         v
          |             Groq    local STT  diarization
          |                         |
          |                      GPU pool
          |
          +-------------------------------> derived exports

Guest large upload:
Browser -> Uppy -> tus/tusd -> audio storage -> Recantor completion hook/job
```

## Initial technology boundaries

### Web application

- React
- TypeScript
- Vite
- TanStack Query for server state
- Dexie over IndexedDB for local durable recording spool
- Uppy for large/resumable guest uploads
- browser MediaRecorder/Web Audio APIs as appropriate

The web application is an SPA. It should not contain authoritative business state that cannot be reconstructed from server/session state plus unacknowledged local recording chunks.

### API application

- Python
- FastAPI
- Pydantic
- SQLAlchemy 2
- Alembic

The API begins as a modular monolith. Do not split ordinary product modules into network services without a proven operational need.

Conceptual modules:

```text
api/
  auth
  sessions
  recording_ingest
  uploads
  transcripts
  summaries
  speakers
  exports
  realtime
  health
```

### Background workers

Celery workers consume Redis-backed queues.

Initial logical workload classes:

- `stt_live` — latency-sensitive transcription work;
- `stt_repair` — retries/backfill/reprocessing;
- `finalize` — session reconciliation and exports;
- `summary` — rolling/final meeting intelligence;
- `diarization` — speaker processing where asynchronous execution is appropriate.

These names are conceptual; queue names may differ in implementation.

Queue priority must not be the only fairness mechanism. Worker concurrency, provider limits, per-session scheduling, and backpressure should prevent one session from monopolizing the service.

### PostgreSQL

PostgreSQL owns durable structured state.

Expected entities include:

- users/accounts;
- sessions;
- recording chunks / accepted sequence metadata;
- interruption/gap events;
- transcript segments;
- speakers / speaker aliases;
- summary snapshots / meeting state;
- upload records;
- export records;
- provider attempts / processing status where operationally useful.

Raw audio blobs do not belong in PostgreSQL.

### Redis

Redis is ephemeral infrastructure for:

- Celery broker/result coordination as configured;
- rate limiting;
- short-lived locks;
- pub/sub or transient realtime fanout;
- temporary coordination.

Redis is not the sole owner of a session, transcript, recording, or summary.

### Audio storage

Recantor owns an `AudioStorage` boundary.

Initial implementation may use local filesystem storage on the server. The interface must permit later S3-compatible storage without changing product/domain semantics.

Conceptual layout:

```text
sessions/<session-id>/
  raw/
    00000001.webm
    00000002.webm
  normalized/
  final/
  exports/
```

The exact layout is an implementation detail; database metadata remains the index and state model.

### FFmpeg

FFmpeg normalizes uploaded/container audio into formats required by STT/final processing.

Never assume all browser or uploaded media arrives in the same codec/container.

### STT providers

Product logic calls an owned interface such as:

```text
STTProvider.transcribe(...)
```

Initial implementations:

- Groq provider using Whisper-compatible speech-to-text endpoints;
- local provider backed by faster-whisper/CTranslate2.

Provider-specific request/response shapes are normalized at the adapter boundary.

Provider attempt metadata should make operational behavior inspectable, for example:

- provider;
- model;
- latency;
- retry count;
- error category;
- timestamps.

### Local GPU STT

Local GPU inference is a separate runtime/service boundary because it has distinct CUDA/model/resource requirements.

A deployment with multiple GPUs should treat them as separate worker capacity rather than assuming VRAM is pooled.

The initial operational model may run one warm model worker per GPU and route work across healthy workers.

### Diarization

Diarization is a provider/service boundary because model choice may evolve independently from the product API.

Live diarization may use an online model/algorithm while finalization can use a more accurate offline pass.

Live speaker labels are provisional. Finalization may reconcile them.

Speaker identification against enrolled voice embeddings is a separate feature from diarization and must remain optional.

## Browser recording architecture

Browser recording has two concerns with different reliability requirements:

1. **archive lane** — must preserve recoverable audio;
2. **realtime lane** — optimized for low-latency VAD/STT/UI and may degrade temporarily.

A target design is:

```text
                           microphone
                               |
                 +-------------+-------------+
                 |                           |
                 v                           v
            archive lane                realtime lane
            MediaRecorder              Web Audio/PCM
                 |                           |
          local IndexedDB                    | WebSocket
                 |                           v
      sequenced HTTP upload             server VAD/STT
                 |                           |
        durable server write                v
                 |                    provisional text
                ACK                          |
                 |                           v
      delete local spool item          browser updates
```

The realtime lane must never be the only copy of audio required to recover the meeting.

A simpler implementation may initially derive more processing from the durable archive path if latency remains acceptable, but the durability invariant remains the same.

## Chunk protocol

A live-recording upload should carry enough metadata to support idempotency and recovery, conceptually:

```json
{
  "session_id": "...",
  "sequence": 42,
  "client_started_at": "...",
  "monotonic_start_ms": 82000,
  "monotonic_end_ms": 84500,
  "content_type": "audio/webm",
  "sha256": "..."
}
```

The exact wire shape will be specified before implementation.

Do not assume MediaRecorder `timeslice` values equal exact audio duration. Sequence and observed/monotonic timing matter more than an assumed fixed chunk length.

## Session lifecycle

A session state model should be explicit and persisted.

Conceptually:

```text
CREATED
   |
   +--> RECORDING ------------------+
   |        |                        |
   |        v                        |
   |   INTERRUPTED                   |
   |        |                        |
   |        +--> RECOVERING ---------+
   |
   +--> UPLOADING
            |
            v
       PROCESSING
            |
            v
       FINALIZING
        /       \
       v         v
   COMPLETE    FAILED
```

Not every session type traverses every state.

Subsystem health/degradation should be modeled separately from the main lifecycle when useful. A recording can be healthy while live STT is degraded.

### Terminal audio completeness

Lifecycle completion and audio completeness are separate consumer-facing facts. `state == COMPLETE` means Recantor has fully accounted for the declared final sequence boundary; it does **not** by itself mean the result contains gap-free audio.

When a recording first becomes `COMPLETE`, Recantor persists `audio_completeness` in the same database transaction that proves there are no unaccounted expected sequences:

- `full` — the final sequence boundary is non-zero and every expected sequence through it is durably present;
- `partial` — the final sequence boundary is non-zero and at least one expected sequence is represented by an explicit sequence-loss gap; an all-gap boundary is therefore `partial`, not `empty`;
- `empty` — the declared final sequence boundary is zero, so no audio sequence was captured.

Wall-clock-only interruption/liveness evidence does not by itself downgrade `full`. Liveness evidence is not proof of audio loss; only a sequence interval that must be satisfied by an explicit gap changes the audio completeness category.

`audio_completeness` is terminal evidence. Retry of an already-`COMPLETE` finalize preserves the persisted value rather than recomputing it from mutable read-time state. Historical rows are backfilled once by migration; normal API reads do not derive the field.

`FinalizeSessionResponse.complete` is retained as a compatibility signal meaning **the declared boundary is fully accounted for**. Downstream consumers must inspect `audio_completeness` before deciding whether a finished session is eligible for transcription, summarization, export, or any other operation that assumes actual audio exists or continuity is gap-free.

## Heartbeats and recovery

The live client sends periodic liveness/recording status.

Loss of heartbeat does not immediately mean the user intentionally stopped. The server should use a grace/recovery policy and retain the ability to resume the same session.

On reconnect:

1. client fetches canonical session state;
2. client and server compare accepted chunk sequences;
3. client resends missing/unacknowledged chunks;
4. conflicts are rejected explicitly;
5. visible gap state is updated if continuity cannot be recovered.

Exact timeout values are configuration/tuning, not architecture invariants.

## Live transcription

### Canonical transcript segments and reconnect

Committed transcript text is durable PostgreSQL state, independent from any WebSocket or Redis delivery path. The first canonical transcript contract stores immutable committed segments with:

- a stable segment identifier;
- the owning `session_id`;
- a session-local monotonically increasing `sequence` used as a publication/reconnect cursor;
- an opaque producer key used to make downstream worker retries idempotent;
- `start_ms` and `end_ms` on the recording timeline;
- canonical text and optional language metadata;
- a durable creation timestamp.

Transcript `sequence` is not the same namespace as recording-chunk sequence. It orders committed transcript publication for one session. Timeline placement remains explicit in `start_ms` / `end_ms` so later STT, reconciliation, and diarization work do not have to infer time from commit order.

Sequence allocation is serialized by locking only the owning session row. This prevents duplicate per-session publication sequences without globally serializing unrelated meetings. The database also enforces uniqueness for `(session_id, sequence)` and `(session_id, producer_key)`.

Retrying a producer key with the same canonical payload returns the already-committed segment. Reusing that producer key with different text, timing, or language is a conflict rather than an overwrite. Later final-transcript reconciliation may introduce explicit revision/supersession semantics; it must not silently mutate a retry identity into different evidence.

Normal HTTP reads are the recovery path for canonical transcript state. `GET /api/v1/sessions/{session_id}/transcript` returns segments after an `after_sequence` cursor with a bounded page size. Future WebSocket delivery may notify clients about new transcript segments, but reconnecting clients must recover through this canonical read model rather than depending on WebSocket history.

The initial low-latency STT approach should use voice activity / endpoint detection to create utterance-sized transcription work rather than sending arbitrary fixed windows without speech awareness.

Initial benchmark defaults may start near:

- minimum useful utterance around 1.5 s;
- silence commit around 0.4–0.7 s;
- hard maximum utterance around 8 s.

These are tuning starting points, not permanent contracts. Real office audio and provider limits must drive final values.

Groq is the primary live provider. Local STT remains warm enough to serve as fallback where deployment resources permit.

## Provider routing and degradation

Conceptual routing:

```text
NORMAL
  Groq primary
  local GPU capacity warm

DEGRADED
  Groq slow/rate-limited/transient failure
  -> bounded retries / local fallback according to policy

LOCAL
  external provider unavailable
  -> local GPU pool
```

Do not race providers by default unless measurements prove the latency benefit is worth duplicated compute/cost.

Provider failure does not remove audio from the processing backlog.

## Rolling meeting intelligence

Summary work consumes canonical transcript events, not raw browser text state.

Do not regenerate the entire meeting on every utterance. Maintain structured meeting state and periodically fold new evidence into it.

Conceptual state:

```text
current topic
key points
decisions
action items
open questions
```

Each summary update should be attributable to a transcript range/version so later reconciliation is possible.

Summary processing is lower priority than recording and live STT.

## Finalization

Finalization reconciles durable evidence after recording/upload completion.

Responsibilities may include:

- ensure all accepted chunks are accounted for;
- persist the terminal audio-completeness classification before downstream consumers run;
- process pending STT work;
- reconstruct/normalize final audio where required;
- run final/offline diarization;
- reconcile speaker labels;
- optionally identify enrolled speakers;
- create final transcript ordering;
- produce final meeting intelligence;
- generate exports;
- persist completion/failure state.

Finalization jobs must be restartable/idempotent.

## Large file uploads

Guest and existing-recording uploads use tus-compatible resumable upload rather than a custom protocol.

Target flow:

```text
Browser
  -> create Recantor guest/upload session
  -> Uppy/tus upload
  -> tusd durable storage
  -> completion notification/reconciliation
  -> Recantor processing queue
```

Guest access uses high-entropy capability/session tokens, expiration, size/duration limits, and abuse controls.

## Realtime client updates

WebSocket is appropriate for ephemeral server-to-client live state such as:

- provisional transcript updates;
- summary updates;
- session health;
- processing progress.

WebSocket delivery is not durable state. Reconnecting clients must recover current truth through normal API reads.

## API contracts

FastAPI/Pydantic is the source of truth for HTTP schemas.

The web client should consume generated TypeScript types/client bindings from OpenAPI once scaffolding exists.

Do not manually maintain duplicated API interfaces across Python and TypeScript when generation can enforce parity.

Future native clients should consume the same documented protocol/API contracts.

## Concurrency model

Concurrent sessions must be isolated at every layer.

No process-global recorder/transcript object may implicitly represent "the current meeting".

Every task/event must include or resolve a session identifier.

Concurrency is bounded deliberately:

- API ingestion remains lightweight;
- CPU/GPU heavy work leaves request handlers;
- provider concurrency respects configured rate limits;
- local GPU workers expose explicit capacity;
- long-running finalization cannot starve live work;
- queue depth and processing latency are observable.

## Failure behavior

### Groq failure

- continue capture;
- persist STT backlog;
- retry/fallback according to policy;
- expose degraded live transcript state.

### Local GPU failure

- mark worker unhealthy;
- route elsewhere when possible;
- do not lose queued source audio.

### Redis restart/loss

- durable session/audio/transcript truth remains in PostgreSQL/audio storage;
- reconciliation must be able to rediscover unfinished durable work where necessary.

This means job state cannot exist only as an unrecoverable Redis message.

### API/server restart

- acknowledged chunks remain present;
- clients reconnect/reconcile;
- unfinished processing is rediscoverable/retryable.

### Browser network loss

- continue local spool while browser execution/capture remains alive;
- retry uploads after reconnect;
- show sync state to the user.

### Browser close/crash

- already acknowledged audio remains safe;
- unacknowledged IndexedDB data may be recoverable if the browser preserves origin storage;
- server marks the session interrupted after liveness policy;
- returning client can resume/finalize;
- unknown missing time becomes a visible gap.

### Mobile browser suspension/lock

- do not claim reliability beyond browser/OS guarantees;
- use Screen Wake Lock when available while actively recording;
- surface capability warning;
- native mobile recorder is the long-term persistent-capture solution.

## Security baseline

- HTTPS is required for microphone APIs and production transport.
- Secrets remain server-side.
- Guest upload/session identifiers must be unguessable.
- Validate content type and actual media handling; do not trust filenames alone.
- Apply upload limits and rate limits.
- Do not expose filesystem paths as public identifiers.
- Treat voice embeddings/voiceprints as sensitive data when speaker identification is introduced.
- Public upstream configuration must not contain deployment secrets.

## Observability

At minimum, make the following measurable before production claims:

- active recording sessions;
- accepted/rejected chunk counts;
- client sync backlog;
- detected gaps/interruption events;
- STT queue depth and age;
- summary queue depth and age;
- provider latency/error/rate-limit counts;
- local GPU worker health/capacity;
- finalization age/failures;
- disk/storage consumption.

Logs should carry stable correlation fields such as `session_id`, job/task ID, provider, and chunk/utterance sequence where applicable.

## Deployment baseline

Initial self-hosted deployment:

```text
Caddy
  |
  +-- web static SPA
  +-- FastAPI API

Docker Compose
  +-- api
  +-- worker-live
  +-- worker-default
  +-- postgres
  +-- redis
  +-- tusd
  +-- optional local-stt-gpu0
  +-- optional local-stt-gpu1
  +-- optional diarization worker/service
```

The exact process split should stay minimal until measurements justify more workers/services.

Caddy is a deployment convenience, not a product dependency; downstream deployments may use another reverse proxy.

## Repository shape

Initial intended monorepo shape:

```text
recantor/
  apps/
    web/
    api/
  services/
    stt-local/
    diarization/
  infra/
  docs/
    decisions/
  AGENTS.md
  README.md
```

Do not create empty architecture for its own sake. Directories should appear when the first real implementation requires them.

## Architecture evolution rule

Prefer extending explicit boundaries over replacing the system:

- browser -> native Android/iOS should reuse session/ingest contracts;
- local filesystem -> S3-compatible storage should replace an adapter;
- Groq -> another STT vendor should add/replace a provider;
- diarization model changes should stay behind its boundary;
- organization-specific reskins should remain deployment/configuration concerns.

If a proposed feature requires rewriting the reliable capture path, first prove why the existing contract cannot support it.