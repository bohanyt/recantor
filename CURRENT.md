AGENT_CONTEXT_V1

kind: CURRENT

repository: bohanyt/recantor

snapshot_seq: 35567879315

collected_at: 2026-09-21T06:18:26.082803Z

consistency: LIVE_REPO_SNAPSHOT

authority: ORIENTATION_ONLY — GitHub is authority; fresh-read the exact object before any write

trusted_sources: AGENTS.md, docs/CURRENT.md

untrusted_sources: issue/PR titles, bodies, comments, and other GitHub-authored prose

SECTION repo_identity
default_branch: main
canonical_branch: integration/cloud-alpha-2026-09-11
canonical_head: 27c7b192f68f19f851728bc2ea3e57706a135fa4
canonical_commit_message: chore(alpha): reconcile accepted Windows alpha state

SECTION trusted_governance
source: AGENTS.md
# AGENTS.md

This file is the front door for humans and coding agents working on Recantor.

## Read order

Before changing code or architecture, read:

1. `docs/CURRENT.md`
2. `docs/PRODUCT.md`
3. `docs/ARCHITECTURE.md`
4. relevant ADRs under `docs/decisions/`
5. the issue or task being implemented

GitHub is the technical source of truth. Do not rely on stale chat history, screenshots, local notes, or an old handoff when repository state disagrees.

## Product invariants

These are not optional implementation details.

### 1. Capture is infrastructure; intelligence is downstream

Recording must not depend on STT, diarization, summaries, or LLM availability.

If Groq is unavailable, audio must still be safe.
If local GPUs are unavailable, audio must still be safe.
If the summary worker is delayed, recording and transcription must continue.

### 2. The server owns sessions

A browser tab is not the source of truth for a recording session. A session has a server-side identity and lifecycle. Browser, Android, iOS, and future desktop clients must use the same protocol semantics.

### 3. Durable audio precedes acknowledgement

For browser live recording, chunks are locally buffered and uploaded with monotonically increasing sequence numbers. A client may discard a local chunk only after receiving a durable server acknowledgement.

Browser IndexedDB is a recovery spool, not the final durability boundary. If local persistence/quota fails while the server is unreachable, the UI must stop claiming the recording is safe.

Server ingestion must be idempotent. Retrying the same `(session_id, sequence)` must not duplicate audio or transcript state.

### 4. One active capture writer per live session

Concurrent sessions are normal, but two tabs/devices must not silently generate competing live sequence streams for the same session.

Use same-origin coordination in the browser where available and a server-authoritative capture lease/epoch or equivalent ownership mechanism across clients. Recovery upload of already-spooled chunks is separate from ownership of new capture.

### 5. Finalization has a completeness boundary

A clean Stop/finalize declares a final sequence/high-water mark. A session may become complete only after every expected sequence through that boundary is either durably present or explicitly represented as a gap/failure.

Browser disappearance without a clean Stop creates an interruption, not an inferred successful completion.

### 6. Gaps are explicit

If Recantor cannot prove audio continuity, it must surface a gap. Never fabricate continuity or silently hide an interrupted interval.

### 7. Concurrent users are normal

Do not build global mutable recorder state. Every recording, transcript, summary, and job belongs to a session. Shared workers must be bounded and fair enough that one long meeting does not monopolize the system.

### 8. Durable state is not Redis

PostgreSQL and audio storage hold durable state. Redis is for queues, short-lived coordination, locks, rate limiting, and similar ephemeral concerns.

## Initial stack contract

Until superseded by an ADR:

- frontend: React + TypeScript + Vite + Tailwind CSS
- server state: TanStack Query
- backend: Python + FastAPI + Pydantic
- persistence: PostgreSQL + SQLAlchemy 2 + Alembic
- jobs: Celery + Redis
- browser recovery spool: Dexie / IndexedDB
- primary STT: Groq Whisper API
- local STT fallback: faster-whisper / CTranslate2
- audio processing: FFmpeg
- resumable large uploads: Uppy + tus/tusd
- deployment baseline: Docker Compose + Caddy
- initial runtimes: Node.js 24 LTS and Python 3.13 for the main API

GPU/ML services may use a different supported Python minor if selected dependencies require it; keep that exception inside the service/runtime boundary and document it.

Do not introduce a second backend framework, a second durable database, Kubernetes, Kafka, GraphQL, or additional distributed infrastructure without an ADR showing a concrete requirement that the current design cannot meet.

## Contract-first development

Prefer explicit types and generated contracts over duplicated handwritten shapes.

- Product HTTP APIs live under `/api/v1` from the beginning.
- `/healthz` is process/liveness only.
- `/readyz` is dependency readiness.
- FastAPI/Pydantic owns HTTP API schemas.
- OpenAPI is generated from the API.
- Web API types/clients are generated from OpenAPI once application scaffolding exists.
- Database schema changes require Alembic migrations.
- External providers live behind Recantor-owned interfaces.

Examples of provider boundaries:

- `STTProvider`
- `AudioStorage`
- `SummaryProvider`
- `DiarizationProvider`

Product/domain code must not depend directly on one vendor when a provider boundary is appropriate.

## Recording critical path

Treat the following as the reliability-critical path:

`capture -> local recovery spool -> upload -> durable server write + durable acceptance metadata -> ACK`

STT, diarization, live UI updates, and summaries are downstream consumers.

A change that improves live latency but weakens the durable path is not acceptable.

Do not assume `MediaRecorder` chunks are independently decodable standalone media files. Preserve ordering/container semantics and use a validated realtime/decoder path for STT.

## Job semantics

Background jobs must be retryable and idempotent where possible.

Do not assume Celery delivers exactly once. Database uniqueness constraints and state transitions should prevent duplicated output.

Keep latency classes separate conceptually:

- live STT: high priority
- finalization / repair: normal priority
- rolling summary: lower priority

A slow LLM queue must never block audio ingestion.

## Browser behavior

Desktop Chrome/Edge is the primary web recording target for the first production path. Responsive mobile web remains supported for active-page use.

Do not claim that a mobile browser can guarantee background or lock-screen recording. Future native Android/iOS recorder clients exist specifically for persistent mobile capture.

Web UX should use capability-aware warnings rather than pretending unsupported lifecycle behavior is reliable.

For the browser recovery spool, request/check persistent storage when supported and monitor approximate quota/usage. Storage failure must become visible recording state.

## Access and privacy

The Live Intelligence product is authenticated before production exposure. A trusted development proof may temporarily run without auth, but that is not a production capability claim.

Meeting audio/transcripts are sensitive data. Before production exposure, owned recordings need authorization, explicit retention behavior, and a user-visible deletion path.

Guest access uses bounded high-entropy capability tokens rather than public/guessable URLs.

Recantor is not designed for covert recording. Active recording state must be obvious to the operator. Deployment operators remain responsible for applicable notice/con
...[AGENTS_TRUNCATED chars=1637]

SECTION trusted_operational_current
source: docs/CURRENT.md
# Current

Last updated: 2026-09-21

This file is the short operational source of truth for Recantor. Fresh GitHub state outranks this summary if a branch, PR, issue, or CI run has moved.

## Active integration line

Canonical cloud-alpha integration branch:

`integration/cloud-alpha-2026-09-11`

The first installable alpha product candidate was accepted on Windows at exact product head:

`b6602a138fc2186fc37b09d09277871f256bceda`

That accepted product head contains the independently reviewed and integrated #42/#43/#44/#45/#46 work, the #48 cloud proof/witness helper, the Windows Docker build transport correction from PR #59, and the truthful alpha footer correction from PR #60.

Issue #48 is CLOSED / completed. Windows acceptance proved:

- documented Windows Docker build/start;
- exact-SHA health/readiness preflight;
- real microphone -> automatic durable STT -> visible canonical transcript -> healthy Stop/finalization;
- archive safety with zero pending local fragments and zero explicit gaps;
- native file-picker Upload -> durable completion -> server-side processing -> canonical transcript;
- TXT/JSON/VTT/SRT exports;
- truthful alpha capability copy at normal desktop viewport.

Manual clicking of the Upload Pause button is not an alpha acceptance requirement. The durable resumability contract is already proven by #44 persisted non-zero tus offset/restart evidence. Current alpha recovery after reopen restores saved upload state and may require reselecting the same local file; true browser-reopen automatic resume is tracked in #61.

Post-acceptance housekeeping reconciles the `main`-only agent-context compiler commit `59ff57502a9a1d84ad332cab106c46c256482d23` into the integration lineage without changing accepted product behavior.

PR #53 remains the integration checkpoint vehicle. No merge to `main` is authorized unless Bohan explicitly says to merge.

## Product truth on the integration line

Recantor currently has two bounded web workflows:

1. **Live** — reliable browser archive recording, an independent realtime speech lane, PostgreSQL-authoritative live STT scheduling, canonical transcript recovery, and the #42 reconnect-safe Live transcript surface.
2. **Upload recording** — Uppy+tus/tusd resumable transfer with durable completion evidence, followed by PostgreSQL-authoritative media processing, bounded ffprobe/FFmpeg normalization, deterministic D3-A segmentation, upload-class STT scheduling, and canonical `TranscriptSegment` production.

The Upload backend and product result path now reach canonical transcript truth. The capability-protected Upload UI exposes processing/result state, canonical recording-timeline transcript presentation, terminal recovery semantics, and TXT/JSON/VTT/SRT exports derived from canonical `TranscriptSegment` rows.

Desktop Chrome/Edge on an **awake** computer is the first browser recording reliability target. Recantor does not claim continuous browser capture through desktop sleep/shutdown, execution-suspending lock behavior, or mobile background suspension.

> Capture is infrastructure. Intelligence is downstream.

Archive recording safety is independent from STT, transcript delivery, diarization, and LLM availability.

## Durable archive recording — merged foundation

The archive path remains:

```text
MediaRecorder
  -> atomic Dexie/IndexedDB fragment + local high-water commit
  -> sequenced HTTP upload with hash/timing/ownership evidence
  -> crash-safe filesystem audio commit + PostgreSQL acceptance metadata
  -> durable HTTP ACK
  -> local fragment deletion
```

Required invariants:

- IndexedDB is a recovery spool, not final durability;
- one live session has one active capture generation fenced by writer identity + epoch;
- Stop/finalize declares an explicit final sequence boundary;
- every expected sequence through that boundary is durable or explicitly represented as loss before completion;
- liveness interruption is not automatically audio loss;
- raw MediaRecorder fragments are ordered source media, not assumed independently decodable;
- PostgreSQL/audio storage are durable truth; Redis and WebSocket delivery are not.

Phase 1 reliable archive capture and terminal `audio_completeness` (`full` / `partial` / `empty`) are merged foundations. The bounded real Edge/Windows witness remains evidence for an awake desktop only, not for sleep/shutdown/mobile suspension.

## Canonical transcript and realtime utterance foundation — merged

Canonical transcript truth is immutable PostgreSQL `TranscriptSegment` evidence with stable session/segment identity, per-session transcript sequence, producer identity, explicit timing, text, and optional language. HTTP cursor reads recover canonical state after reconnect.

The realtime audio lane reuses the microphone stream independently from archive capture:

```text
same microphone MediaStream
        |
        +---------------- archive lane ----------------+
        |  MediaRecorder -> IndexedDB -> HTTP -> durable ACK
        |
        +--------------- realtime lane ----------------+
           AudioWorklet -> sample-clock PCM
                         -> bounded WebSocket transport
                         -> server VAD / endpointing
                         -> durable TranscriptionUtterance WAV
```

Realtime failure is downstream degradation. It never participates in archive ACK/finalization semantics.

The Groq STT provider boundary consumes committed durable utterance work, verifies storage evidence before provider execution, and commits canonical transcript evidence with deterministic producer identity.

## Durable STT scheduling — integrated

PostgreSQL owns durable `STTJob` identity/state, retry budget, delivery reservations, claims, and canonical convergence. Redis/Celery carry generic wake signals only.

Current scheduler properties include:

- claim leases and fencing for worker death/duplicate delivery;
- PostgreSQL-backed admission and per-session fairness;
- bounded retry handling using provider error categories;
- reconciliation that rediscovers unfinished durable utterance work;
- canonical transcript evidence remains authoritative for success;
- terminal `no_speech` for valid blank provider results, without creating blank transcript rows;
- separate workload classes and worker/queue capacity for Live (`stt-live`) and Upload (`stt-upload`).

Upload backlog therefore does not consume the reserved Live STT frontier. Ordinary CI does not require a real Groq secret or provider network call; focused proof exercises the real `GroqSTTProvider` against a deterministic local compatible endpoint.

## #42 canonical Live transcript UI — integrated

The web transcript surface preserves these semantics:

- canonical HTTP cursor reads are truth;
- WebSocket delivery is an ephemeral wake hint, not persistence;
- duplicate/out-of-order wake hints converge through canonical reads;
- reconnect/degraded delivery can catch up without changing archive recording safety;
- transcript presentation does not own recorder lifecycle or archive durability.

## #44 resumable existing-recording upload foundation — integrated

The durable transfer path is:

```text
Browser file
  -> Uppy + tus
  -> tusd
  -> shared durable audio storage
  -> Recantor tusd completion hook
  -> PostgreSQL upload completion evidence
```

Current transfer properties:

- WAV/MP3/M4A/OGG/WebM/MP4 source admission;
- pause/retry/reload/reselection resumes the same tus upload when recovery evidence matches;
- capability-token protected upload-session reads/hooks;
- durable completion records internal storage identity, exact byte length, and SHA-256;
- whole-file completion hashing runs outside the asyncio event loop and outside a long upload-row lock, then revalidates authoritative binding before publish;
- UI may claim **durably uploaded** only after Recantor confirms durable completion.

## #45 uploaded-media processing — integrated

After immutable #44 completion, the backend path is:

```text
durable completed upload
  -> PostgreSQL UploadMediaProcessing
  -> bounded ffprobe
  -> bounded FFmpeg normalize to mono signed 16-bit PCM / 16 kHz
  -> claim-fenced atomic first-wins normalized evidence
  -> deterministic upload-energy-vad-180s-v1 segmentation
  -> existing commit_utterance_work
  -> existing STTJob / STTProvider
  -> canonical TranscriptSegment
```

Important properties:

- one PostgreSQL processing identity per immutable completed upload;
- PostgreSQL remains durable authority; media Redis/Celery messages are wake mechanisms only;
- dedicated `media-upload` worker has RW audio access, while live/upload STT workers keep audio storage RO;
- normalized publication does heavy digest/fsync work while private, then performs the bounded final first-wins install under the current PostgreSQL claim fence;
- stale/reclaimed workers cannot publish authoritative normalized identity;
- deterministic D3-A retry starts from normalized sample 0 and reproduces utterance/timeline identity or fails loudly;
- partial EOF timing preserves coverage with floor(start)/ceil(end);
- long silence creates no fake utterance work;
- valid blank provider text becomes terminal `no_speech`, not failure and not a blank `TranscriptSegment`;
- migration 0009 backfills completed #44 uploads exactly once and has a behavioral downgrade/re-upgrade proof.

Residual alpha boundaries remain documented: the atomic first-wins filesystem proof targets the Linux/local-filesystem Compose deployment shape; a crash before manifest commit can leave a non-authoritative orphan content object; out-of-band storage mutation is detected rather than repaired automatically.

## #43 product shell — integrated

The desktop productization pass provides:

- obvious `Live` / `Upload recording` top-level workflow shell;
- active microphone `requesting` or `recording` capture cannot be hidden behind Upload navigation;
- Live prioritizes lifecycle + elapsed time, one primary action, audio safety, transcription state, recovery
...[CURRENT_TRUNCATED chars=4262]

SECTION authority_issue
issue_number: 41
issue_state: closed
issue_updated_at: 2026-09-21T06:17:36Z
<<<UNTRUSTED_GITHUB_DATA source=issue-41>>>
title: Control Tower: cloud-first productization swarm to first installable alpha
body:
## Goal

Drive Recantor from the current Phase 2E cloud-clean candidate to a coherent **first installable alpha** without requiring Bohan's laptop for intermediate manual testing. Cloud CI/browser/Compose evidence should be exhausted first; the real Windows/Chrome-or-Edge acceptance pass is deliberately deferred until the end.

This is a coordination/control-plane issue, not an implementation lane.

## Current anchor

- GitHub is the technical source of truth.
- `main` is still the last merged stable line.
- Issue #38 / DRAFT PR #40 remains the unmerged Phase 2E candidate.
- Cloud-clean Phase 2E exact head at creation of this issue: `1cf0b3d69a356aaab17f6c42a4b687f7d42ea361`.
- Frozen integration anchor branch created from that exact candidate: `integration/cloud-alpha-2026-09-11`.
- Do **not** mutate PR #40's branch except for a concrete #38 fix owned by its implementation owner.
- No implementation PR is merged to `main` merely to make downstream work easier.

## Product target for this swarm

Reach a cloud-verified alpha where a user can eventually clone/install Recantor, provide only the required local secrets/config, open the laptop web UI, and see a product-oriented flow rather than an engineering diagnostics page.

Target user workflows:

1. **Live** — start/stop a reliable browser recording, automatic transcription, reconnect-safe transcript UI, human-readable degraded states.
2. **Upload recording** — resumable existing-file upload, durable processing, FFmpeg normalization where required, canonical transcript, progress/recovery, exports.
3. **Setup** — current supported provider configuration is truthful and minimal. Groq STT should reduce to a local server-side `GROQ_API_KEY` plus defaults.
4. **Optional development LLM bridge** — prepare a bounded local/development adapter that can invoke Codex non-interactively using an existing ChatGPT login/subscription for downstream text-only meeting intelligence experiments. It must not be treated as STT, a production API, or a credential-sharing mechanism.

## Explicit non-goals of this swarm

- production auth/SSO/organization policy;
- diarization/speaker identification unless a concrete dependency appears;
- native mobile clients;
- Kubernetes/Kafka/second durable DB;
- pretending a ChatGPT/Codex subscription is an OpenAI API key;
- using GPT-5.6 Luna for audio transcription (the model is text/image input, not audio input);
- local laptop manual acceptance before the final integration gate.

## Swarm ownership / lease protocol

For **implementation** work, every agent MUST post this before editing:

```text
AGENT_WORK_LEASE_V1
agent: <stable agent id>
issue: #<issue>
mode: implementation
branch: <branch>
base_sha: <exact sha>
write_scope:
  - <paths/modules>
lease_expires_at: <UTC timestamp, <=30 minutes from claim>
```

Rules:

- exactly one active implementation lease per issue;
- overlapping write scopes across issues are not allowed concurrently unless Control Tower explicitly declares the scopes non-conflicting;
- a lease is advisory coordination backed by GitHub comments, not a hidden lock;
- renew before expiry with `AGENT_WORK_LEASE_RENEW_V1`;
- after expiry, the agent must stop writes until it renews/reclaims;
- Control Tower may transfer an abandoned lane with `CONTROL_TOWER_OWNER_TRANSFER_V1`;
- implementation agents open **DRAFT PRs only** and never merge/mark-ready;
- every handoff/result ends `CONTROL_TOWER_READY`.

Read-only reviewers use `AGENT_REVIEW_CLAIM_V1`; they do not own implementation or branches.

## GBF read policy

`GitHub But Fast (GBF)` is a shared **read accelerator**, not coordination authority.

Agents should:

- prefer one GBF `execute` with `Promise.all()` for independent heavy reads relevant to their lane;
- filter/grep/compact inside the GBF sandbox before returning model-visible results;
- avoid repeating a full-repository takeover bundle when the issue/path is already known;
- avoid more than roughly 3–4 concurrent heavy fan-out executes across the swarm;
- use the normal GitHub connector for comments, claims, PRs, mutations, merge operations, and fallback;
- preserve GitHub as source of truth even when GBF is faster.

## Branching policy while #40 remains unmerged

- New lanes may branch from `integration/cloud-alpha-2026-09-11` or from a later Control-Tower-approved integration SHA.
- Each PR must record its exact base SHA and dependency issue(s).
- Prefer non-overlapping modules so lanes can run in parallel.
- Control Tower owns integration order and conflict reconciliation.
- The integration branch is allowed to move only through a bounded integration-owner lane or explicit CT action; ordinary specialists do not push directly to it.

## Execution waves

### Wave 1 — contracts and independent implementation

- Phase 2F realtime transcript delivery/reconnect + live transcript UI.
- truthful/minimal setup + desktop product shell/diagnostics simplification.
- resumable existing-recording upload foundation.
- optional ChatGPT-authenticated Codex non-interactive development LLM bridge feasibility/prototype.

### Wave 2 — processing and result UX

- uploaded-media FFmpeg normalization + durable processing into the canonical transcript pipeline;
- upload progress/recovery/result UX;
- TXT/JSON/VTT/SRT exports using canonical transcript truth.

### Wave 3 — cloud integration/hardening

- exact-head backend/frontend/E2E/Compose/migration checks;
- fault/restart/reconnect coverage;
- fresh-install/clean-volume smoke;
- config/docs truth reconciliation;
- no real user secrets in CI.

### Final gate — one real local acceptance pass

Only after cloud integration is coherent:

- clone/checkout the approved alpha candidate on Windows;
- install/start with documented commands;
- provide local `GROQ_API_KEY` if using Groq STT;
- run live real-mic recording + automatic transcript;
- run existing-file resumable upload + processing + export;
- inspect desktop Chrome/Edge UX at normal laptop viewport;
- optionally
...[AUTHORITY_BODY_TRUNCATED chars=340]
>>>UNTRUSTED_GITHUB_DATA

SECTION latest_authority_comments
<<<UNTRUSTED_GITHUB_DATA source=issue-41-latest-comments>>>
comment_id=5755652822 author=bohanyt
AGENT_WORK_LEASE_V1
agent: O
mode: implementation-extension
issue: #48
packet_key: RECANTOR-Q48-WINUI-TRUTH-f326cf-20260921
branch: agent-o/issue-48-ui-truth-footer
current_head: b6602a138fc2186fc37b09d09277871f256bceda
write_scope: same bounded two-path UI truth fix only
reason: finish exact-head frontend/normal CI after formatter correction
exclusions: unchanged; no integration/main write, merge, mark-ready, docs, #47, recorder/STT/upload behavior
lease_expires_at: 2026-09-21T05:35:00Z

CONTROL_TOWER_READY

---

comment_id=5755664729 author=bohanyt
AGENT_WORK_LEASE_RELEASE_V1
agent: O
issue: #48
lease: #41 comment 5755501934
branch: agent-o/issue-48-ui-truth-footer
final_head: e93af428615483ee2594ecf2b5e562c49c421aad
draft_pr: #60
handoff: #48 comment 5755663566
result: bounded Windows UI truth footer fix complete; exact-head frontend and normal CI green
release: implementation lease released
no integration/main/Windows authority transferred

CONTROL_TOWER_READY

---

comment_id=5755680001 author=bohanyt
AGENT_WORK_LEASE_RELEASE_V1
agent: O
issue: #48
lease: #41 comment 5755652822
branch: agent-o/issue-48-ui-truth-footer
final_head: b6602a138fc2186fc37b09d09277871f256bceda
draft_pr: #60
handoff: #48 comment 5755679438
result: bounded Windows UI truth footer fix complete; exact-head frontend and normal CI green
release: implementation-extension lease released
no integration/main/Windows authority transferred

CONTROL_TOWER_READY

---

comment_id=5755808828 author=bohanyt
AGENT_WORK_LEASE_V1
agent: CT-RECANTOR-20260921-R46C1
mode: integration-only
issue: #48
source_pr: #60
integration_branch: integration/cloud-alpha-2026-09-11
current_integration_head: f326cf1f88870804c50ed0468e1551a532f4b851
accepted_head: b6602a138fc2186fc37b09d09277871f256bceda
review_result: #48 comment 5755804213 CLEAN
write_scope: advance integration branch to exact accepted head only; force=false; no main write
lease_expires_at: 2026-09-21T05:55:00Z

---

comment_id=5755821789 author=bohanyt
CONTROL_TOWER_WINDOWS_UI_TRUTH_INTEGRATED_V1

accepted_review: #48 comment 5755804213 CLEAN
source_pr: #60
integration_branch: integration/cloud-alpha-2026-09-11
integrated_head: b6602a138fc2186fc37b09d09277871f256bceda
method: fast-forward exact accepted head, force=false
integration_lease: #41 comment 5755808828 RELEASED
exact_same_head_evidence:
- CI 35563300298 SUCCESS
- Upload foundation CI 35563300142 SUCCESS
- independent review #48 5755804213 CLEAN
media_processing_note: latest integrated Media processing CI 35559938123 SUCCESS at predecessor f326cf1f; PR #60 changes only apps/web/src/App.tsx and App.test.tsx
pr53_note: raw GitHub mergeability is currently unknown/recomputing; main-only commit 59ff5750 adds only agent-context compiler config/workflow and does not overlap the two-file UI truth patch
next_windows_scope: update clone to b6602a1 -> Preflight -> visually confirm corrected footer -> continue Upload witness -> PostRun helper; accepted Live mechanics evidence remains reusable
main_merge: not authorized

CONTROL_TOWER_READY

---

comment_id=5756117057 author=bohanyt
AGENT_WORK_LEASE_V1
agent: CT-RECANTOR-20260921-R46C1
mode: post-acceptance-integration-reconciliation
issue: #41
integration_branch: integration/cloud-alpha-2026-09-11
current_integration_head: b6602a138fc2186fc37b09d09277871f256bceda
main_only_commit_to_reconcile: 59ff57502a9a1d84ad332cab106c46c256482d23
write_scope:
  - docs/CURRENT.md truth reconciliation
  - .github/agent-context.toml from main
  - .github/workflows/agent-context-current.yml from main
  - one merge-lineage reconciliation commit on integration only
bounded_goal:
  - preserve accepted alpha product tree
  - incorporate main-only agent-context compiler infrastructure
  - record #48 Windows acceptance complete and next #58/#61 work
  - obtain exact reconciled-head CI before alpha freeze
exclusions:
  - no product behavior changes
  - no PR #53 merge to main
  - no #47 work
  - no updater/#58 or #61 implementation in this lease
lease_expires_at: 2026-09-21T06:35:00Z

CONTROL_TOWER_READY

---

comment_id=5756158420 author=bohanyt
CONTROL_TOWER_POST_ACCEPTANCE_RECONCILIATION_COMPLETE_V1

accepted_windows_product_head: b6602a138fc2186fc37b09d09277871f256bceda
reconciled_integration_head: 27c7b192f68f19f851728bc2ea3e57706a135fa4
reconciliation_commit: merge-lineage commit with parents:
- b6602a138fc2186fc37b09d09277871f256bceda
- 59ff57502a9a1d84ad332cab106c46c256482d23

reconciled_paths:
- docs/CURRENT.md — post-acceptance truth
- .github/agent-context.toml — preserved from main
- .github/workflows/agent-context-current.yml — preserved from main

product_behavior_change: NONE
integration_lease: #41 comment 5756117057 RELEASED

exact_reconciled_head_ci:
- CI 35567211494 SUCCESS
- Upload foundation CI 35567211502 SUCCESS
- Media processing CI 35567211492 SUCCESS

ancestry:
- main 59ff57502a9a1d84ad332cab106c46c256482d23 is now ancestor of integration
- integration is ahead of main and behind_by=0
- PR #53 remains OPEN / DRAFT / unmerged
- main merge remains NOT authorized

Alpha control-tower goal is complete. Next implementation lanes are #58 and #61 after immutable alpha freeze bookkeeping.

CONTROL_TOWER_READY

---

comment_id=5756203084 author=bohanyt
ALPHA_FREEZE_COMPLETE_V1

immutable_tag: v0.1.0-alpha.1
tagged_commit: 27c7b192f68f19f851728bc2ea3e57706a135fa4
tag_object: 806ccad24693f2b289adaacca0e14561e5d65f03
tag_message: Recantor first installable alpha
freeze_workflow_run: 35567740762 SUCCESS

Exact tagged-head evidence:
- CI 35567211494 SUCCESS
- Upload foundation CI 35567211502 SUCCESS
- Media processing CI 35567211492 SUCCESS

Policy: never move/reuse v0.1.0-alpha.1; future snapshots use a new version tag.

PR #53 remains DRAFT / unmerged to main.

CONTROL_TOWER_READY
>>>UNTRUSTED_GITHUB_DATA

SECTION active_work_frontier
<<<UNTRUSTED_GITHUB_DATA source=open-issues-and-prs>>>
ISSUE #47 state=open updated=2026-09-21T06:18:12Z title=Development-only Codex subscription LLM bridge for transcript-derived meeting intelligence experiments
ISSUE #61 state=open updated=2026-09-21T06:17:44Z title=Upload recovery: automatic resume after browser reopen with persisted file handle
ISSUE #58 state=open updated=2026-09-21T06:17:40Z title=Stable release lifecycle: versioning, updater, rollback, and known-good recovery
PR #53 state=open updated=2026-09-21T06:17:33Z title=Integration checkpoint: cloud alpha 2026-09-12
PR #50 state=open updated=2026-09-11T20:37:01Z title=feat: add development-only Codex subscription LLM bridge

OPEN_PRS
PR #53 draft=True updated=2026-09-21T06:17:33Z base=main head=integration/cloud-alpha-2026-09-11 title=Integration checkpoint: cloud alpha 2026-09-12
PR #50 draft=True updated=2026-09-11T20:37:01Z base=integration/cloud-alpha-2026-09-11 head=agent-i/issue-47-codex-subscription-bridge title=feat: add development-only Codex subscription LLM bridge
>>>UNTRUSTED_GITHUB_DATA

END_OF_AGENT_CONTEXT kind=CURRENT seq=35567879315 sections=6
