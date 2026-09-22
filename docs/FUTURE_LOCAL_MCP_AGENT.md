# Future: Local MCP gateway for Recantor

Status: **PROPOSED / future feature**

This document records a future integration direction. It is not a current product capability, does not change the existing roadmap order, and must not be described as working until repository evidence proves it.

## Goal

Expose a small, stable Model Context Protocol (MCP) surface in front of Recantor so an external agent can request transcription, inspect job state, retrieve canonical transcript data, and request exports without learning Recantor's internal API, storage layout, queue implementation, or STT provider details.

A primary intended consumer is an enterprise assistant such as a Microsoft 365 Copilot / Copilot Studio agent, but the MCP surface should remain vendor-neutral and reusable by any compatible MCP client.

The desired deployment shape is:

```text
enterprise agent / MCP client
          |
          | HTTPS + MCP Streamable transport
          v
+---------------------------+
| Recantor MCP gateway      |
| narrow agent-facing API   |
+-------------+-------------+
              |
              | Recantor-owned HTTP/domain contracts
              v
+---------------------------+
| Recantor                   |
| API + PostgreSQL + jobs    |
+-------------+--------------+
              |
       +------+------+
       |             |
       v             v
   local STT       Groq STT
 faster-whisper    provider
 / CTranslate2
       |
       v
 configured NVIDIA GPU workers
```

The MCP gateway is an orchestration/access surface. It does not become a new transcript source of truth.

## Why this fits the existing architecture

Recantor already owns the important boundaries:

- canonical transcript state lives in PostgreSQL-backed Recantor models;
- existing-recording upload/transcription is a product workflow;
- exports are derived from canonical transcript truth;
- STT is behind the Recantor-owned `STTProvider` boundary;
- Groq is the current external provider;
- faster-whisper/CTranslate2 is the planned local provider;
- local GPU inference is already a separate runtime/service boundary;
- one configured GPU should initially be treated as one worker-capacity lane rather than pooled VRAM.

The MCP layer should therefore call existing Recantor contracts instead of duplicating transcription, job, export, or provider logic.

## Important boundary: local server vs cloud agent

A cloud-hosted agent cannot call a loopback-only URL such as `http://localhost:8000` on the Recantor machine.

For a cloud client such as Copilot Studio, the MCP endpoint must be reachable from that client over HTTPS. Current Copilot Studio MCP integration uses the Streamable transport and expects an MCP server URL that Copilot Studio can reach.

A deployment may satisfy that with an approved reverse proxy, gateway, ingress, or other organization-managed network path. The public Recantor repository must not hard-code one organization's domain, tunnel, tenant, or identity provider.

Conceptually:

```text
cloud agent
    |
    v
https://<deployment>/mcp
    |
    v
reverse proxy / approved gateway
    |
    v
Recantor MCP process
    |
    v
Recantor API / domain services
```

Do not expose PostgreSQL, Redis, filesystem storage, or unrestricted internal Recantor endpoints merely to make MCP reachable.

## Proposed MCP process boundary

Prefer a small Python MCP process or mountable ASGI component that lives beside the existing API but remains a narrow adapter.

Initial preference:

```text
Recantor deployment
  api
  web
  worker
  reconciler
  tusd
  local-stt      # future Phase 4 service
  mcp            # future thin agent-facing adapter
```

The MCP component should not become a second product backend. Product/domain logic remains in Recantor.

If the MCP SDK/runtime can be mounted cleanly into the existing FastAPI deployment without weakening isolation or lifecycle behavior, that may be considered later. A separate process is easier to reason about initially.

## Minimal tool contract

Keep the first MCP surface intentionally small.

### `recantor_health()`

Returns agent-safe readiness information.

Example conceptual result:

```json
{
  "status": "ready",
  "transcription_available": true,
  "local_stt_available": true,
  "external_stt_available": true
}
```

Do not return secrets, internal storage keys, connection strings, host paths, Redis details, or provider credentials.

### `start_transcription(...)`

Starts the existing-recording workflow from a bounded source reference.

The preferred long-term input is a source reference or short-lived HTTPS media URL, not raw video/audio bytes embedded in MCP JSON.

Conceptual input:

```json
{
  "source_url": "https://example.invalid/short-lived-media",
  "filename": "meeting.mp4",
  "language_hint": "auto"
}
```

Conceptual result:

```json
{
  "job_id": "...",
  "status": "queued"
}
```

This tool must feed the normal Recantor upload/media-processing/canonical-transcript path. It must not create a parallel transcript model.

Remote-source ingest requires explicit SSRF, size, duration, content-type, timeout, redirect, and authorization controls before production use.

### `get_transcription_status(job_id)`

Returns stable user-facing processing state without exposing Celery/Redis internals.

Example states may map to product language such as:

- queued;
- preparing media;
- transcribing;
- complete;
- failed.

### `get_transcript(job_id, cursor?, limit?)`

Returns bounded canonical transcript data in timeline order.

Large transcripts must be paginated/bounded. Do not return an unbounded full meeting merely because MCP permits a large response.

### `get_export(job_id, format)`

Requests or retrieves a derived export using Recantor's existing canonical export semantics.

Initial formats:

- TXT
- JSON/JSONL
- VTT
- SRT

The MCP layer must not invent export content independently from Recantor.

## Optional later tools

Only after the minimal surface is proven:

- `search_transcripts(query, ...)`
- `list_recent_transcriptions(...)`
- `cancel_transcription(job_id)` if safe cancellation semantics exist
- source-specific helpers such as resolving an enterprise document/video reference

Do not add write/delete/admin tools simply because MCP makes them convenient.

## Enterprise file-source direction

A useful future workflow is:

```text
user uploads recording to enterprise file storage
        |
        v
agent resolves file/reference
        |
        | small reference only
        v
Recantor MCP
        |
        | server-to-server media transfer
        v
Recantor existing-recording pipeline
        |
        v
canonical transcript + exports
```

For Microsoft deployments, SharePoint/OneDrive via Microsoft Graph is one possible source adapter. That integration should pass a stable item reference or short-lived download reference to Recantor rather than routing large media through the language-model context.

Keep source-provider integration separate from Recantor transcript semantics. A future deployment can use SharePoint, S3, another document system, or direct upload without changing the canonical transcript model.

## Local GPU STT direction

The local MCP feature becomes substantially more useful when paired with Roadmap Phase 4 local GPU STT.

Initial local provider direction remains:

- faster-whisper / CTranslate2;
- one warm worker-capacity lane per configured GPU;
- explicit health/capacity reporting;
- provider timing/error instrumentation;
- Groq retained as a proven external provider/fallback until benchmark evidence justifies a different routing policy.

Do not assume two 8 GB GPUs behave like one 16 GB GPU. Prefer independent worker lanes first.

A future router may support policies such as:

```text
local-first:
  healthy local worker -> local STT
  local saturated/unavailable -> Groq

external-first:
  Groq -> primary
  Groq unavailable/constrained -> local STT
```

The correct default must be selected from measured latency, accuracy, concurrency, cost, and operational evidence rather than preference.

## Language benchmark target

The local STT benchmark should include deployment-relevant speech rather than only English public samples.

At minimum test:

- Indonesian;
- English;
- Japanese;
- Javanese;
- Indonesian/Javanese code-switching with ordinary workplace English technical terms.

Whisper exposes language identifiers for Indonesian, Japanese, Javanese, and Sundanese, but language availability is not an accuracy guarantee. Javanese and code-switched speech require real target-audio evaluation.

Support an optional language hint such as:

```text
auto
id
en
ja
jw
```

Default to `auto` until evidence shows a forced hint is beneficial for a specific workflow.

Record at least:

- wall-clock transcription time;
- realtime factor;
- VRAM use;
- model and compute type;
- transcript error/quality against a small human-verified reference;
- provider/model failures;
- queue wait vs inference time.

## Security and privacy requirements

Meeting audio and transcripts are sensitive data. MCP does not weaken the existing access/retention requirements.

Before production exposure:

- require authentication on the MCP endpoint;
- preserve Recantor authorization at the product/domain boundary;
- use least-privilege tool scopes;
- keep the first production tool set read-oriented where possible;
- never expose provider secrets or internal storage paths;
- audit meaningful agent-triggered actions;
- bound file size, duration, concurrency, retries, and response sizes;
- defend remote media fetch against SSRF and unsafe redirects;
- apply explicit retention/deletion semantics to imported recordings;
- keep organization-specific identity/domain configuration downstream from the public repository.

API-key authentication can be acceptable for a tightly bounded development proof. Organization-facing deployment should prefer an identity-aware OAuth/OIDC design appropriate to that deployment.

## Failure semantics

MCP is downstream of capture and durable media truth.

An unavailable agent, MCP endpoint, local GPU, Groq provider, or enterprise file source must never weaken Recantor's recording durability guarantees.

Examples:

- MCP unavailable -> existing Recantor web/API workflows continue;
- local GPU unavailable -> provider router may delay/fallback according to policy;
- agent disconnects after starting a job -> job remains canonical in Recantor and can be inspected later;
- MCP retries a request -> domain operations must use existing idempotency/retry-safe semantics;
- transcript retrieval fails -> canonical transcript remains intact.

## Proposed implementation sequence

This feature should remain downstream of the current reliable transcript/upload work.

### M0 — contract-only proof

- add no production feature;
- freeze the minimal MCP tool names/inputs/outputs;
- confirm the chosen MCP SDK supports Streamable HTTP and the deployment runtime.

### M1 — health-only MCP

- run a local MCP process;
- expose only `recantor_health()`;
- connect one compatible MCP client;
- prove no Recantor internals/secrets leak.

### M2 — existing-recording orchestration

- add `start_transcription`, `get_transcription_status`, `get_transcript`, and `get_export`;
- route through existing canonical Recantor APIs/domain services;
- add idempotency and bounded-output tests.

### M3 — local GPU provider proof

- implement/finish the faster-whisper/CTranslate2 provider lane from Roadmap Phase 4;
- benchmark on actual target hardware;
- keep Groq available during comparison;
- select a documented routing policy.

### M4 — enterprise agent proof

- expose the MCP endpoint through an approved HTTPS path;
- connect a standard enterprise agent;
- prove end-to-end: agent -> MCP -> Recantor -> STT -> canonical transcript -> agent;
- keep the proof bounded to non-sensitive test media first.

### M5 — enterprise file source

- resolve a file reference from enterprise storage;
- transfer media server-to-server;
- process through the normal Recantor upload pipeline;
- return transcript/export references without pushing large media through LLM context.

### M6 — hardening

- production authentication/authorization;
- auditing;
- retention/deletion;
- quotas/rate limits;
- source allowlists/SSRF defenses;
- multi-user isolation;
- reconnect/retry/duplicate-invocation tests;
- operational dashboards/alerts where needed.

## Acceptance idea for a first real end-to-end witness

A later implementation can consider the feature proven only when one bounded test demonstrates:

1. an authenticated compatible agent calls the Recantor MCP endpoint over HTTPS;
2. the MCP endpoint starts transcription of a known non-sensitive recording;
3. Recantor stores/processes it through the ordinary existing-recording path;
4. STT runs through the selected provider;
5. canonical transcript rows are created;
6. the agent polls status without duplicate processing;
7. the agent retrieves a bounded transcript result;
8. a TXT or SRT export is produced from canonical transcript truth;
9. no secret, internal storage key, or unrestricted local endpoint appears in agent-visible output;
10. Recantor remains independently usable if the MCP process is stopped.

## Non-goals for the first MCP version

Do not include initially:

- direct database queries from the model;
- arbitrary shell/PowerShell execution;
- unrestricted filesystem tools;
- arbitrary URL fetch without SSRF controls;
- delete/retention administration;
- live microphone control;
- diarization or summary work solely to make the MCP demo impressive;
- organization-specific branding/domains in public upstream;
- a second transcript database or MCP-owned job model.

## Roadmap relationship

This direction belongs after the reliable transcript/upload core and aligns with two existing roadmap items:

- Phase 4: local GPU fallback;
- Phase 9: MCP/agent integrations.

A bounded MCP proof may be developed earlier when useful for integration research, but it must not pull later features into the recording durability critical path or cause current product claims to overstate what is implemented.
