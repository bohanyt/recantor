# Recantor Control Tower Handoff — 2026-09-11

This handoff exists so a fresh control-tower conversation can continue Recantor without depending on the previous chat context.

## Authority and fresh-read rule

**GitHub is the technical source of truth.** Fresh repository, branch, PR, issue, and CI state always outrank this dated handoff. Do not trust a SHA, issue status, PR status, CI result, or next-step below if GitHub has moved.

Do not use chat history as durable technical state. Important conclusions belong in repository docs, issues, PRs, or CI evidence.

## Required read order for the next control tower

Before planning or changing code:

1. Fresh-inspect `bohanyt/recantor` and current `main`.
2. Read `AGENTS.md`.
3. Read `docs/CURRENT.md`.
4. Read this handoff.
5. Read Issue #38 in full and all new comments.
6. Read `docs/ROADMAP.md` Phase 2.
7. Inspect repository activity newer than this handoff before deciding the next action.

PR #37 / Issue #36 are historical evidence for the merged Phase 2D provider path. Re-read them only when implementation/review details are needed.

## Checkpoint before this handoff commit

Fresh `main` before creating the handoff branch was:

`0f391bd555089b5cb7606c56cebcd8ab09162970`

That commit reconciled documentation after Phase 2D merged. Its CI run `34456974070` completed successfully across backend, frontend, Chromium E2E, and Compose smoke.

The commit/PR that publishes this handoff will advance repository state again, so **fresh-fetch `main` rather than treating the SHA above as current HEAD**.

## Product / architecture invariants to preserve

Primary rule:

> Capture is infrastructure. Intelligence is downstream.

The archive durability path remains:

```text
MediaRecorder
  -> Dexie/IndexedDB recovery spool
  -> sequenced HTTP upload
  -> crash-safe audio storage + PostgreSQL acceptance metadata
  -> durable ACK
  -> only then local fragment deletion
```

Phase 2 intelligence is a separate downstream lane:

```text
same microphone
  -> AudioWorklet PCM
  -> bounded realtime transport
  -> VAD / endpointing
  -> durable independently-decodable TranscriptionUtterance WAV
  -> STT scheduling/execution
  -> canonical TranscriptSegment
```

Preserve these rules:

- archive capture/ACK/finalization must not depend on Redis, Celery, Groq, diarization, summaries, or LLMs;
- PostgreSQL + audio storage are durable truth; Redis/Celery are delivery/coordination only;
- raw MediaRecorder fragments are not assumed independently decodable for STT;
- STT consumes only committed `TranscriptionUtterance` media through the verified storage boundary;
- duplicate/retried background work must be safe; do not assume exactly-once delivery;
- one long session/backlog must not starve other sessions;
- no public upstream secrets, API keys, private deployment credentials, or organization-private data;
- do not merge implementation PRs without explicit user authorization.

## Phase 1 / Phase 2 merged foundation

The following dependency chain is already merged and should not be rebuilt:

- Phase 1 reliable browser archive recording and durable ACK semantics;
- Issue #15 / PR #29 terminal `audio_completeness` classification;
- Issue #30 / PR #31 canonical PostgreSQL transcript segments + reconnect cursor;
- Issue #32 / PR #33 durable independently-decodable transcription utterance work;
- Issue #34 / PR #35 independent realtime PCM/VAD producer;
- Issue #36 / PR #37 provider-neutral STT boundary + Groq utterance-to-canonical-transcript execution.

### Phase 2C real microphone evidence

The Windows/Edge witness session was:

`181b5dd8-41c8-49d8-86ea-f2e00ec74ae1`

It ended with archive `COMPLETE` / `audio_completeness=full`, final archive sequence `211`, pending local audio `0`, explicit gaps `0`, and `71` durable utterance WAV rows/files.

This proves realtime utterance mechanics on the bounded desktop target. It does **not** make current VAD thresholds product-quality; real-office VAD/noise/latency tuning remains later Phase 2 work.

## Phase 2D — MERGED / CLOSED

PR #37 was squash-merged as:

`f0a0f2e068cb1003619010595928c0273912e61c`

Post-merge CI run:

`34456347492` — SUCCESS across backend, frontend, Chromium E2E, and Compose smoke.

Issue #36 auto-closed/completed.

Merged causal path:

```text
durable TranscriptionUtterance
  -> verified manifest/path/hash/length read
  -> owned STTProvider
  -> Groq whisper-large-v3-turbo
  -> normalized text/language
  -> canonical TranscriptSegment
     producer_key = utterance:<work-uuid>
```

Important implementation behavior:

- provider execution occurs only after durable utterance verification;
- transcript timing is copied from the utterance work item;
- already-committed canonical transcript short-circuits sequential retry;
- provider/network failure creates no fake/blank transcript and leaves durable utterance evidence intact;
- Groq API key is server-side configuration only;
- Groq transport sends an explicit non-browser API `User-Agent` because Python `urllib`'s default signature was rejected at Groq's Cloudflare edge with HTTP 403 Error 1010.

### Real Groq witness

Two utterances from the earlier real-microphone session were exercised against real Groq before merge-ready:

1. `87aec8c6-f11a-50b5-84d8-160d448eec1c`, 3300 ms. Default `urllib` signature hit Cloudflare 1010; a one-shot explicit API `User-Agent` test succeeded and committed canonical transcript sequence 1.
2. `ad71e495-cdd6-5ae0-814b-758ba5133f85`, 2840 ms. After the permanent User-Agent fix was rebuilt into the API container, the direct provider path succeeded **without monkeypatch**, committing canonical transcript sequence 2.

The API key stayed local/server-side and was not committed or posted.

These witnesses prove provider-path mechanics, **not transcription accuracy**. The sampled utterances did not have known recorded ground-truth text.

## CURRENT NEXT: Issue #38 / Phase 2E

Issue #38 is now the active bounded dependency:

**“Phase 2E: durable live STT queue, reconciliation, retry, and fairness”**

No implementation PR for #38 existed at the time this handoff was written.

The goal is to turn the proven one-utterance executor into an **automatic** live STT pipeline without making Redis/Celery durable truth.

The next causal path should become:

```text
VAD commits durable TranscriptionUtterance
  -> durable PostgreSQL scheduling identity/state
  -> best-effort Celery/Redis wake-up
  -> atomic PostgreSQL claim / lease
  -> merged Phase 2D transcribe_utterance()
  -> canonical TranscriptSegment
  -> scheduling state converges to succeeded

lost/duplicate queue delivery
  -> periodic/deterministic PostgreSQL reconciliation
  -> unfinished work is rediscovered
```

### Required #38 semantics

Read the Issue #38 body as authority. In particular preserve:

- one durable schedulable identity per utterance;
- PostgreSQL-backed state such as pending/claimed/retry/succeeded/failed, with attempt and claim/lease evidence;
- worker claim acquired atomically before provider execution;
- **never hold a DB transaction/row lock across the provider network call**;
- expired claims reclaimable after process/worker death;
- canonical transcript remains authoritative completion evidence;
- if the canonical transcript already exists, reconciliation converges scheduling state to success without re-calling Groq;
- Redis/Celery delivery is a wake-up hint only; lost queue messages must be rediscoverable from PostgreSQL/audio evidence;
- duplicate task delivery and worker races must not produce concurrent provider calls for the same active claim;
- rate-limit/timeout/transient provider failures use bounded retry/backoff;
- configuration/permanent/malformed failures do not hot-loop and remain visible;
- fairness must be session-aware so one huge backlog cannot starve another active session;
- archive session/chunk state and durable utterance bytes are untouched by STT failures;
- ordinary CI uses fake/injected providers and does not require a real Groq key/network.

### #38 real witness gate before merge-ready

After the implementation candidate is stable and CI-green, perform one bounded Windows/Edge real-microphone witness using the existing local Groq configuration:

- fresh recording;
- speak several known short Indonesian phrases separated by silence;
- **do not manually call `transcribe_utterance`**;
- verify durable utterances appear and automatic workers produce canonical transcript rows;
- record coarse queue/provider latency;
- verify archive Stop/finalization remains healthy and independent;
- verify the API key is not logged or committed.

Accuracy observations may be recorded, but the full latency/accuracy benchmark harness is a later Phase 2 deliverable.

### #38 explicit non-goals

Do not bundle:

- transcript WebSocket fanout/UI;
- transcript editing/revision;
- faster-whisper local fallback/router;
- diarization/speaker labels;
- summaries/LLM processing;
- existing-recording upload;
- production auth/authorization;
- Kafka/Kubernetes/second durable database.

After #38, the next bounded Phase 2 slice is realtime transcript delivery/reconnect UI.

## Work discipline for the next chat

- Fresh-check GitHub before trusting this file.
- Treat Issue #38 as the implementation source of truth; do not invent a parallel roadmap.
- Inspect current models/migrations/dependencies/Compose before deciding exact schema/service shape.
- Use a focused branch and draft PR for #38; do not implement directly on `main`.
- Keep changes bounded to queue/scheduling/reconciliation/retry/fairness semantics.
- Add deterministic tests for lost delivery, duplicate delivery, claim races, claim expiry, retry policy, reconciliation, fairness, and session isolation.
- Run backend/frontend/E2E/Compose checks and inspect exact-head CI.
- Update `docs/CURRENT.md` in the same delivery when product/architecture truth materially changes.
- Put durable review findings in GitHub rather than only in chat.
- Do not merge the #38 implementation PR until the user explicitly authorizes merge.

## Local test-state note

The last bounded Groq tests were run from a detached implementation head before PR #37 merged. A fresh local session should first synchronize safely:

```powershell
git fetch origin
git switch main
git pull --ff-only origin main
```

The local `.env` already held a working `GROQ_API_KEY` during the witness. Do not print, commit, paste, or ask the user to paste the key. Preserve existing Docker volumes/evidence during follow-up testing; do not use destructive cleanup merely to start #38.

## Suggested fresh-chat wake prompt

```text
Lanjutkan Recantor Control Tower dari GitHub repo `bohanyt/recantor`.

GitHub adalah technical source of truth. Fresh-inspect repository dulu; jangan percaya SHA/status/next-step di prompt atau handoff kalau GitHub sudah bergerak.

Baca berurutan:
1. AGENTS.md
2. docs/CURRENT.md
3. docs/handoff/2026-09-11-CONTROL-TOWER-HANDOFF.md
4. Issue #38 body + latest comments
5. docs/ROADMAP.md Phase 2
6. semua PR/issue/commit/CI baru setelah handoff

Phase 2D / PR #37 sudah merged dan real Groq provider path sudah terbukti. Jangan membangun ulang #37. Ambil alih sebagai Control Tower dan lanjutkan Issue #38: durable live STT queue, PostgreSQL-backed claim/reconciliation/retry/fairness, dengan Redis/Celery hanya sebagai delivery hint. Preserve archive independence dan jangan merge implementation PR tanpa instruksi eksplisit.
```

## Handoff status

At handoff preparation, Recantor is ready to proceed with **Issue #38 / Phase 2E** after a fresh control tower confirms no newer GitHub activity supersedes this checkpoint.