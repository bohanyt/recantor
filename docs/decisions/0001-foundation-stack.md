# ADR 0001: Foundation stack

- Status: Accepted
- Date: 2026-09-09

## Context

Recantor needs to support reliable browser recording, resumable uploads, concurrent sessions, realtime transcript updates, Python-based speech/diarization tooling, background jobs, and future native mobile recorder clients.

The project is expected to be maintained by both humans and coding agents. The stack therefore prioritizes:

- widespread documentation and community knowledge;
- explicit type/contracts;
- low conceptual surprise;
- mature libraries;
- straightforward local development;
- compatibility with the Python ML/audio ecosystem;
- a small number of infrastructure primitives;
- replaceable provider/deployment boundaries.

Recantor does not need SSR-heavy content rendering, Kubernetes, Kafka, GraphQL, or a large microservice topology to satisfy its initial requirements.

## Decision

### Web

Use:

- React
- TypeScript
- Vite
- Tailwind CSS
- TanStack Query
- Dexie / IndexedDB

Rationale:

- React/TypeScript is widely documented and familiar to human/AI maintainers.
- Vite keeps the web app a conventional SPA and avoids introducing a second server-side application framework.
- Tailwind is mainstream and works well with a small tokenized design system; Recantor should keep organization branding in CSS variables/configuration rather than hard-coded product forks.
- TanStack Query provides a standard boundary for server state rather than inventing custom caching/fetch state.
- Dexie provides a readable browser recovery spool over raw IndexedDB APIs.

Do not use Next.js as the primary application framework unless a later product requirement genuinely needs its server-rendering/server-runtime capabilities.

### Backend/API

Use:

- Python
- FastAPI
- Pydantic

Rationale:

- speech recognition, diarization, speaker embeddings, PyTorch, NeMo, faster-whisper, and related tooling are Python-first;
- FastAPI/Pydantic gives explicit typed API schemas and generated OpenAPI;
- keeping ordinary product orchestration in Python avoids adding Node backend glue around Python ML services.

The API begins as a modular monolith.

### Runtime baseline

For the initial application scaffold:

- use Node.js 24 LTS for the web/tooling runtime;
- use Python 3.13 for the main FastAPI application;
- record/pin runtime expectations in repository metadata and CI.

GPU/ML services are separate runtime boundaries and may pin a different supported Python minor later if a selected CUDA/ML dependency requires it. That exception must stay local to the service rather than silently changing the whole application runtime.

### API contracts

FastAPI/Pydantic is the source of truth for HTTP API schemas.

Generate OpenAPI and derive TypeScript API types/client bindings from it rather than maintaining duplicate handwritten request/response interfaces.

Future mobile/native clients should use the same documented server contracts.

Product API routes start versioned under `/api/v1`. Operational liveness/readiness endpoints remain outside that product version namespace as defined by ADR 0002.

### Database

Use PostgreSQL with SQLAlchemy 2 and Alembic.

Rationale:

- concurrent users/sessions are normal;
- relational constraints are valuable for idempotency and lifecycle correctness;
- PostgreSQL is operationally mature and well documented;
- Alembic provides explicit schema migration history.

SQLite remains acceptable for isolated local tools/tests where appropriate, but it is not the intended production server database.

### Background jobs

Use Celery with Redis initially.

Rationale:

- mature Python ecosystem;
- familiar retry/routing/concurrency patterns;
- sufficient for the expected initial workload;
- avoids building a job system.

Jobs must remain idempotent/recoverable. Redis is not the durable source of truth for meeting state.

If future scale/operational evidence shows Celery/Redis is inadequate, replace it behind task/domain boundaries through a later ADR.

### Realtime delivery

Use WebSocket for ephemeral live client updates where it improves UX.

WebSocket is not a persistence mechanism. A reconnecting client must be able to recover canonical state through HTTP/API reads.

### Browser recording durability

Use browser local persistent storage through Dexie/IndexedDB for unacknowledged chunks.

Use sequence-numbered idempotent server ingestion and delete local chunks only after durable server ACK.

Browser storage remains a recovery spool whose persistence/quota must be observed as defined by ADR 0002; it is not equivalent to server durability.

Realtime audio processing may use Web Audio/PCM/WebSocket separately, but it must not replace the durable archive lane.

### Large uploads

Use Uppy with tus/tusd rather than implementing resumable upload semantics in Recantor.

Recantor owns session authorization, completion/reconciliation, limits, and downstream processing. tus infrastructure owns resumable byte transfer.

### Audio

Use FFmpeg as the standard media normalization boundary.

Do not assume browser or uploaded media share one codec/container.

### STT

Primary external provider: Groq Whisper-compatible speech-to-text.

Local fallback: faster-whisper/CTranslate2 behind a Recantor-owned `STTProvider` boundary.

The API/product layer must not be coupled directly to Groq response shapes or local model implementation details.

### Diarization

Keep diarization behind an owned provider/service boundary.

Exact online/offline models are deferred until benchmark work. Python is the intended runtime because the strongest candidate ecosystems are Python/PyTorch-based.

### Audio storage

Define an `AudioStorage` boundary.

Initial self-hosted deployments may use local filesystem storage. Later S3-compatible/object storage must be possible without changing recording/session semantics.

Raw audio is not stored in PostgreSQL blobs.

### Deployment

Use Docker Compose as the baseline self-hosted orchestration.

Use Caddy as the default/simple reverse proxy and HTTPS/static-serving option.

Caddy is replaceable downstream; Recantor must not depend on Caddy-specific product behavior.

### Package/tooling baseline

Use:

- `pnpm` for JavaScript/TypeScript package management;
- `uv` for Python dependency/environment management;
- `pytest` for backend tests;
- `Vitest` for frontend unit/component tests where appropriate;
- `Playwright` for real browser behavior;
- GitHub Actions for public upstream CI.

The Phase 0 scaffold should choose conventional lint/format tooling and wire it into CI rather than leaving formatting policy implicit. Exact package versions belong in lockfiles/repository metadata.

## Consequences

### Positive

- mainstream, extensively documented technologies;
- good compatibility with AI coding agents and human contributors;
- one primary product backend language;
- direct access to Python ML/audio ecosystem;
- typed API boundary through Pydantic/OpenAPI/TypeScript generation;
- production-capable relational persistence from the beginning;
- minimal distributed infrastructure;
- natural path from browser client to future native clients;
- frontend branding can be changed through configuration/design tokens rather than organization-specific code forks.

### Costs

- two application languages (TypeScript and Python) remain necessary;
- Celery/Redis introduces operational complexity compared with purely in-process jobs;
- dual durable/realtime recording lanes may add browser/audio implementation complexity;
- local GPU services require separate runtime/deployment concerns;
- PostgreSQL/Docker Compose is heavier than a single-process SQLite prototype.

These costs are accepted because they address expected real product requirements rather than speculative scale.

## Rejected alternatives

### Next.js as the main frontend/backend

Rejected for the initial architecture because Recantor is primarily a realtime SPA and already requires Python for ML/audio workloads. Adding a Next.js server runtime would create overlapping backend responsibilities without a current requirement for SSR/server components.

### Node/TypeScript backend with Python ML microservices

Rejected initially because the product orchestration itself interacts heavily with speech/audio/ML concerns. It would require an extra runtime boundary for little benefit.

### Rust/Go backend

Rejected initially because their performance benefits do not address the first bottlenecks, while they increase distance from the ML ecosystem and reduce contributor/agent familiarity relative to the selected stack.

### SQLite production server database

Rejected because concurrent users and asynchronous workers are expected from the beginning.

### Custom resumable upload protocol

Rejected because tus already solves creation, offsets, resumption, and retry semantics with mature implementations.

### Kafka

Rejected because current workloads do not justify the operational burden. Celery/Redis plus durable database reconciliation is sufficient until proven otherwise.

### Kubernetes

Rejected because the initial self-hosted target is well served by Docker Compose. Kubernetes may be added only if deployment requirements demand it.

### Microservice-per-feature architecture

Rejected. Recantor starts as a modular monolith, with separate services/processes only for clear resource/runtime boundaries such as GPU inference and tusd.

## Revisit triggers

Revisit this ADR when measured evidence shows one of the following:

- Celery/Redis cannot provide required throughput/reliability despite correct configuration;
- API scaling characteristics require a different runtime topology;
- a product requirement needs server rendering or an integrated web server runtime;
- storage requirements demand an object-store-first implementation;
- native client development reveals a contract problem not solvable by normal API evolution;
- deployment scale genuinely requires orchestration beyond Compose.
