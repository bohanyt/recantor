# Recantor Control Tower Handoff — 2026-09-10

This handoff exists so a fresh ChatGPT control-tower conversation can take over without depending on the previous chat's context window.

## Authority and fresh-read rule

**GitHub is the technical source of truth.** Fresh repository, branch, PR, issue, and CI state always outrank this dated handoff. Do not assume a SHA, issue status, PR status, CI status, or next step below is still current if GitHub has moved.

Chat history is convenience only. Important technical conclusions must live in repository docs, issues, PRs, or CI evidence.

## Required read order for the next control tower

Before planning or changing anything:

1. Fresh-inspect `bohanyt/recantor` and current `main`.
2. Read `AGENTS.md`.
3. Read `docs/CURRENT.md`.
4. Read this handoff.
5. Read Issue #5 in full, including the latest control-tower comments.
6. Read Issue #13 and all new comments/PRs touching it.
7. Inspect any repository activity newer than this handoff before deciding the next action.

When #13 is complete, fresh-read Issue #10 before implementing it. Issue #15 is a later Phase 2 gate, not a Phase 1 blocker.

## Checkpoint at handoff preparation

Product-code checkpoint after the last implementation merge:

- PR #20 merged to `main` as `f989d88ec9765ae9678941ded11789ae810f6711`.
- Post-merge CI run `34410387405` completed successfully.
- Issue #11 is closed/completed by PR #20.
- Issue #12 is closed/completed; its bounded Phase 1 interim mitigation landed in PR #20.
- Issue #18 was already closed by PR #19.
- `docs/CURRENT.md` was reconciled after PR #20 in docs-only commit `80ac1ead845d3f009b3849a0e7207ee1fea7b6d1`.

The commit that adds this handoff necessarily advances `main` again, so **fresh-fetch main rather than treating the SHA above as the repository HEAD**.

## Product / architecture invariants to preserve

Most important:

> Capture is infrastructure. Intelligence is downstream.

The durability-critical path is:

```text
MediaRecorder
  -> Dexie/IndexedDB recovery spool + local high-water
  -> sequenced HTTP upload with ownership/timing/hash evidence
  -> crash-safe media storage + PostgreSQL acceptance metadata
  -> durable HTTP ACK
  -> only then local deletion
```

Do not weaken these rules while fixing UI/recovery behavior:

- IndexedDB is a recovery spool, not equivalent to server durability.
- A server ACK is the strong durability boundary.
- STT/Groq/diarization/LLM failure must never determine whether audio survives.
- One live session has one active capture generation at a time.
- Each resumed generation uses a fresh writer identity and fenced epoch.
- Gaps must be explicit; never fabricate continuity and never fabricate audio loss.
- Raw `MediaRecorder` fragments must not be assumed independently decodable.
- PostgreSQL + media storage own durable truth; Redis does not.
- Multiple simultaneous sessions/users are normal.
- Public upstream must remain free of private deployment secrets and organization-specific private data.

## Merged Phase 1 reliability work relevant to the current state

### PR #17 / Issue #14 — bounded requests and escapable FINALIZING

Merged at `11221961f91a3db6681a5970becacb4ad62fa9f5`.

Recorder HTTP requests have bounded per-attempt deadlines, upload retry remains idempotent-safe, local evidence is retained on exhaustion, `FINALIZING` has **Keep locally and finish later**, and lost successful finalize responses can reconcile remote `COMPLETE`.

### PR #19 / Issue #18 — stable stopped-spool sync

Merged at `50eaf450d4746569160876d81beb2f97c432c288`.

After `MediaRecorder` stops and `chunkChain` drains, Stop no longer trusts a pre-Stop single-flight sync snapshot. It settles/cancels the old pass and runs one fresh bounded sync over the stable stopped spool before finalization.

### PR #20 / Issues #11 + #12 — fenced capture + orphan safety

Merged at `f989d88ec9765ae9678941ded11789ae810f6711`.

Current browser behavior:

- authoritative stale-writer conflict from heartbeat or chunk upload terminally fences the active generation;
- `MediaRecorder`/mic stop and the capture lock is released;
- emitted old-generation evidence remains local and is labelled **orphaned local evidence — not safely syncable**;
- stale Start / Resume / Finish / Sync actions are withheld;
- reload checks server ownership truth before ordinary reconciliation so a newer generation, including an already-`COMPLETE` generation, cannot erase old-epoch orphan evidence;
- sequence-range reconciliation may delete a local fragment only if its `captureEpoch` equals the server session's current `capture_epoch` as well as having an accepted sequence.

The #12 rule is deliberately an **interim Phase 1 guard**, not the final native/multi-device reconciliation identity contract. Do not accidentally harden it into a cross-device protocol.

Executed regression evidence on PR #20 included a newer epoch accepting the same victim sequence with a different SHA, completing, reloading, and proving the exact older local fragment survived. The fenced-capture Playwright suite used `retries: 0`; CI run `34359040563` passed 20/20 E2E first-attempt, final PR-head CI `34359711581` was fully green, and post-merge CI `34410387405` was also green.

## Remaining Phase 1 order

The intended order at handoff is:

```text
#13 missing-sequence finalization recovery
        ↓
#10 liveness/interruption semantics
        ↓
real Chrome/Edge background/minimized desktop witness
        ↓
close Issue #5 / Phase 1
```

Do not start Phase 2 implementation before Phase 1 is trustworthy.

## NEXT: Issue #13

Issue #13 is the immediate implementation blocker:

**“Phase 1 blocker (B4): an absent middle spool fragment strands a session in FINALIZING with no user path out, and each retry appends another gap.”**

The server already has two valid continuity-accounting exits for an expected missing sequence:

1. the real fragment arrives later and is durably accepted; or
2. the missing sequence is deliberately represented as a sequence gap in finalization.

The current browser can reliably reach neither when the local middle fragment is absent.

### What is actually proven

The executed repro deliberately removed one middle IndexedDB chunk to establish the precondition. It proved the **product dead-end given an absent expected middle fragment**.

It did **not** prove that browsers naturally evict individual IndexedDB records. Do not add browser-storage theories or mitigations that claim otherwise.

### Required #13 behavior

The implementation must satisfy the issue's bounded acceptance criteria:

- surface `missing_sequences` from incomplete finalization in `RecorderSnapshot` and UI;
- give the user a clearly worded, deliberate action to declare those unresolved sequence(s) as gaps;
- only expose/consume that destructive continuity declaration after ordinary retry/recovery has been offered;
- never automatically fill `gap_sequences` merely because a finalize response reports missing evidence;
- retrying a failed/incomplete finalize must not append another wall-clock recovery gap each click;
- if the missing fragment is still/re-again locally present, the retry path uploads it and completes normally with no declared sequence gap;
- add deterministic E2E coverage for declared-gap completion and late-fill completion;
- add regression coverage proving gap count remains stable across repeated failed finalize attempts;
- update `docs/CURRENT.md` only for behavior actually proven by the final code/tests.

### #13 explicit non-goals

Do not bundle:

- allowing `claim_capture` from a non-complete `FINALIZING` session;
- theories about the natural mechanism causing a missing IndexedDB middle fragment;
- #10 liveness changes;
- #15 terminal completeness classification;
- STT/Groq/diarization/LLM work.

`claim_capture` from `FINALIZING` remains a separate lifecycle-contract decision with concurrency implications.

### #13 review traps

During implementation/review, specifically check:

- the UI cannot permanently declare loss on a transient failure without informed user action;
- `gap_sequences` are exactly the unresolved missing sequences the user saw/confirmed;
- repeated Finish/retry is idempotent with respect to gap evidence;
- existing wall-clock recovery-gap logic is not accidentally invoked once per retry;
- local chunks are never deleted before authoritative matching ACK/reconciliation rules permit it;
- remote-`COMPLETE` reconciliation from #14 remains intact;
- fresh stopped-spool sync from #18 remains intact;
- fenced/orphaned behavior from #11/#12 remains intact;
- E2E evidence tests use `retries: 0` for the failure-mode witness.

## AFTER #13: Issue #10

Issue #10 is the remaining code blocker after #13.

Current defect: server-side liveness interruption is effectively read-triggered; a long heartbeat stall can disappear when the next valid heartbeat overwrites `last_heartbeat_at` before the old interval is evaluated. Conversely, `interrupted_at` can remain populated after liveness is restored, making current-vs-historical meaning ambiguous.

Bounded requirement:

- evaluate heartbeat staleness **before** overwriting the previous heartbeat time;
- record that a liveness hole happened even if no external GET occurred during it;
- make current interruption state distinct/unambiguous from historical interruption evidence;
- make heartbeat and capture-claim semantics consistent;
- cover stall→read→heartbeat, stall→heartbeat with no read, and stall→claim.

Critical invariant: **a liveness hole is not automatically an audio gap.** The browser may have locally captured and later uploaded the complete audio. Do not convert every server heartbeat absence into permanent audio-loss evidence.

## Real desktop witness after code blockers

Automated Playwright background-tab tests cannot close Phase 1's desktop lifecycle claim. In the currently pinned environment, Playwright launches Chromium with background-throttling-disabling/headless flags.

After #13 and #10 are merged and green, run a real desktop witness on an awake desktop/laptop:

- Chrome or Edge, recording from a real microphone;
- background/minimize the browser while switching among normal applications for a bounded interval;
- return and Stop;
- record exact OS, browser/version, duration, and final sequence/ACK/continuity evidence.

Do not generalize the result to sleeping laptops, closed browsers, or mobile browser suspension.

## Phase 2 gate after Phase 1

Issue #15 is intentionally **not** a Phase 1 blocker. It must be resolved before the first STT/summary consumer.

Today a terminal `COMPLETE` session does not let a downstream consumer distinguish from the session resource alone among:

- fully durable audio through the boundary;
- partial/gapped completion;
- empty/no durable audio.

#15 requires a persisted terminal classification. #13 should settle first or concurrently because its declared-gap path supplies the partial/gapped case. Do not implement STT consumers before #15 is settled.

## Work discipline for the next chat

- Fresh-check GitHub before trusting this file.
- Use focused branches/PRs for non-trivial code work; do not modify `main` directly for implementation work.
- Do not merge implementation PRs without explicit user instruction.
- Run relevant backend/frontend/E2E/Compose checks and inspect CI, not just local assumptions.
- Failure-mode E2E evidence should be deterministic and use retries disabled where the acceptance criterion depends on observing the first attempt.
- Update `docs/CURRENT.md` in the same delivery when current truth materially changes.
- Put durable review findings in GitHub, not only in chat.
- If fresh review exposes another blocker, record it as a bounded issue rather than hiding it to preserve the roadmap.

## Local development traps already discovered

- `apps/api/tests/conftest.py` drops the entire schema on teardown. Never point the test suite at the same database an API process is actively using.
- `apps/web/openapi-ts.config.ts` defaults to `http://localhost:8000`; on IPv6-first hosts this can fail against an API bound only to `127.0.0.1`. Set `OPENAPI_INPUT` explicitly when needed.

## Suggested fresh-chat wake prompt

```text
Lanjutkan Recantor Control Tower dari GitHub repo `bohanyt/recantor`.

Fresh-inspect GitHub dulu; GitHub adalah technical source of truth dan mengalahkan SHA/status/next-step di handoff kalau repo sudah berubah.

Baca berurutan:
1. AGENTS.md
2. docs/CURRENT.md
3. docs/handoff/2026-09-10-CONTROL-TOWER-HANDOFF.md
4. Issue #5 + latest comments
5. Issue #13 + latest comments
6. PR/issue/commit/CI baru sejak handoff

Ambil alih sebagai Control Tower. Reconcile current truth dulu, lalu lanjutkan bounded next work dari Issue #13. Jangan mulai STT/Groq/diarization/LLM. Jangan merge implementation PR tanpa instruksi eksplisit.
```

## Handoff status

At the time this handoff was prepared, the repository was ready to proceed with **Issue #13** once a fresh control tower verifies no newer GitHub activity supersedes this checkpoint.
