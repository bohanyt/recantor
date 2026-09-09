# ADR 0002: Recording and access guardrails

- Status: Accepted
- Date: 2026-09-09

## Context

A foundation review before application scaffolding found several reliability details that must be explicit before Recantor implements browser recording or exposes the live workflow to real users.

The original architecture already established the main principle:

> capture -> local spool -> sequenced upload -> durable server write -> ACK

This ADR tightens what each part of that promise means in real browser and multi-user use cases.

## Decision

### 1. Browser local storage is a recovery spool, not the final durability boundary

IndexedDB is the browser recovery spool for unacknowledged audio. It is not equivalent to server-side durable storage.

The web client should:

- request persistent origin storage with `navigator.storage.persist()` when supported;
- inspect whether storage is persistent;
- monitor approximate usage/quota with `navigator.storage.estimate()` where supported;
- persist each archive chunk before considering it locally recoverable;
- keep every unacknowledged chunk until server ACK;
- visibly downgrade recording safety if local persistence fails or available quota becomes unsafe.

The product must not show a healthy/safe recording state if it can no longer write the local recovery spool while the server is also unavailable.

A server ACK remains the strong durability boundary.

### 2. One live capture writer owns a session at a time

A single live session must not have two independent tabs/devices generating overlapping sequence streams at the same time.

Recantor will enforce one active capture writer per live session.

The browser should use same-origin coordination, preferably the Web Locks API where supported, so two tabs do not accidentally become active recorders for the same session.

The server remains authoritative across tabs and devices through a capture lease/epoch or equivalent ownership token.

Recovery upload of already-spooled chunks is distinct from ownership of new live capture. A stale client may reconcile previously captured chunks according to idempotency rules without being allowed to start a competing new audio stream.

### 3. Stop/finalize carries a high-water mark

A successful user-initiated Stop must tell the server what the client believes the final archive sequence is.

The exact wire schema will be defined during Phase 1, but it must carry an equivalent of:

- final sequence/high-water mark;
- final observed monotonic recording time;
- session/capture ownership identity.

A session must not become `COMPLETE` merely because no more chunks happened to arrive.

Before successful finalization, every expected sequence through the declared high-water mark must be either:

- durably accepted; or
- explicitly represented as a gap/failure.

If the browser disappears without a clean Stop, Recantor records an interruption. Later recovery/finalization may use only proven evidence and must not invent a clean tail.

### 4. Lifecycle commands are retry-safe

Start/resume/finalize operations must tolerate network retries and double clicks.

Where a command creates or transitions durable state, use idempotency keys, conditional state transitions, or naturally idempotent resource semantics so a repeated request cannot create duplicate sessions/finalizations.

### 5. Durable ACK requires durable audio and durable acceptance metadata

For the initial filesystem `AudioStorage` adapter, ACK must not be emitted while the only copy exists in process memory, an open buffer, or Redis.

The implementation must use crash-safe file commit semantics appropriate to the platform, such as a temporary file, flush/fsync where available, and atomic rename/replace, plus durable PostgreSQL acceptance metadata.

The database and audio store can fail at different moments, so reconciliation must tolerate orphaned files or incomplete metadata without silently acknowledging data that is not actually recoverable.

### 6. MediaRecorder chunks are ordered media fragments

Do not assume every `MediaRecorder` `dataavailable` blob is independently decodable as a standalone audio file.

Archive chunks are ordered source fragments. Final reconstruction/normalization must use container-aware concatenation/remux/decoding.

Low-latency STT should use a known-decodable audio path, such as the realtime PCM/Web Audio lane or a validated server decoder path, instead of assuming each archive fragment can independently be sent to STT.

### 7. Product API is versioned from the beginning

Version product API routes from the first scaffold:

```text
/api/v1/...
```

Operational health endpoints remain outside the product API version:

```text
/healthz   process/liveness only
/readyz    dependency readiness
```

FastAPI/Pydantic remains the schema source of truth. Phase 0 must prove OpenAPI -> generated TypeScript API types/client usage rather than starting with duplicated handwritten request/response types.

### 8. Live Intelligence must have an access-control baseline before production exposure

Phase 1 may prove reliable recording in a trusted development environment without authentication, but the Live Intelligence product is an authenticated workflow.

Before it is exposed as a production/live-user feature, Recantor must add:

- authenticated session ownership;
- authorization checks on session/audio/transcript access;
- secure session/cookie/token handling appropriate to the chosen auth design;
- a user-visible delete path for owned recordings;
- an explicit retention behavior.

The exact authentication mechanism is intentionally deferred to its own implementation ADR so a public upstream can support normal self-hosted deployments without hard-coding one company's identity provider.

Guest upload/guest record remains a separate capability-token path with expiry and abuse limits.

### 9. Recording privacy is a product behavior

Recantor is not designed for covert recording.

The UI must make active recording state obvious to the operator. Deployers/operators remain responsible for applicable participant notice/consent requirements in their jurisdiction and organization.

Stored meeting audio and transcripts are sensitive data. Production claims require access control plus a documented deletion/retention path; organization-wide retention policy automation can mature later.

### 10. Session kinds are explicit

The server model should distinguish session intent rather than infer it from whichever endpoint happened to create it.

At minimum the design must be able to represent equivalents of:

- authenticated live recording;
- guest/simple browser recording;
- existing-file upload.

They may share processing and transcript models while having different access, ingestion, and lifecycle rules.

## Consequences

### Positive

- accidental duplicate recorders are bounded;
- Stop has a provable completeness boundary;
- browser storage pressure cannot silently masquerade as safe capture;
- server ACK has concrete crash-safety meaning;
- mobile/native clients have a stable versioned API target;
- authentication no longer falls through a gap between the product contract and late organization features;
- raw browser container behavior is not confused with the realtime STT transport.

### Costs

- Phase 1 must implement more explicit recovery and ownership state;
- browser storage capability/status needs UX and tests;
- durable filesystem ACK requires careful write/reconciliation behavior;
- access control becomes a real milestone before production exposure.

These costs directly address expected real-world failure modes and are therefore accepted.

## External platform notes

Relevant browser platform behavior is tracked in `docs/REFERENCES.md`, including the Storage API, Web Locks API, and MediaRecorder timing/lifecycle caveats.
