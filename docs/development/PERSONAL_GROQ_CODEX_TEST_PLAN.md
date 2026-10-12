# Personal Groq → Codex test readiness (development only)

Reviewable **docs-only** artifact: Groq STT (server env) → canonical transcript → Codex subscription bridge ([Issue #47](https://github.com/bohanyt/recantor/issues/47), [PR #50](https://github.com/bohanyt/recantor/pull/50)) for derived meeting intelligence. No runtime wiring, UI auth flows, session credential files, Docker auth mounts, or production multi-user auth in this change.

**Acceptance owner:** [Issue #67](https://github.com/bohanyt/recantor/issues/67) tracks runtime evidence (SHAs, checklist, witnesses). Merging this plan and fixture alone does **not** close #67. Depends on #47 and a PR #50 head that includes `tools/codex_subscription_bridge.py` reconciled onto current `main`.

**Anchors (verify at run time):** `main` at `655a421d45444795a8d71e6140f522a3bcc3763e` had no bridge helper on raw tree inspection; PR #50 draft head `652581b62c6f7ca392a5e235f3a5231f25370709` (`agent-i/issue-47-codex-subscription-bridge`) carries the helper but is not on `main`. Rebase/refresh onto fresh `main` belongs in an isolated worktree checkout before bridge commands below.

Invariants (`AGENTS.md`, ADRs `0001-foundation-stack`, `0002-recording-access-guardrails`): capture/STT/transcript must not depend on LLM; intelligence fails explicitly downstream. Intelligence output must remain attributable to transcript session/range when wired; per-user BYOK and provider-picker UI are **out of scope** here (deferred).

## What this publication includes

- This runbook and `PERSONAL_GROQ_CODEX_SYNTHETIC.json` only, authored from fresh-`main` context.
- No claim that clone, worktree refresh, bridge reconciliation, offline pytest, `check-auth`, or `analyze` was executed in the publishing environment.
- Runtime steps below are **PENDING** until a candidate tree contains the bridge helper and operator tooling is available.

## Operator prerequisites (record per environment)

- Git clone of `https://github.com/bohanyt/recantor`, Python 3.13 + `uv`, Codex CLI on PATH **or** an explicit operator-configured executable path (do not rely on machine-specific defaults in shared docs).
- Host ChatGPT login for Codex on the test machine (**never** mount or copy auth into Docker or the repo).
- Groq credentials via **global** server env on the alpha deployment model (no Clerk in this path; per-user Groq BYOK deferred).
- Real Groq STT E2E is separate; synthetic text does not prove STT.
- **Do not** assign `HOME`, `USERPROFILE`, or `CODEX_HOME` for bridge tests.

Some sandboxes lack `git`, `gh`, or `uv` on PATH; that is environment-specific. Offline pytest needs `uv`; `gh` is not required to run tests.

## Privacy and data handling — real-data gate unresolved

Real or sensitive transcripts must **not** be sent to Codex until source-backed **no-training** requirements for the ChatGPT-authenticated Codex path are documented and accepted. **Retention** is a separate question from no-training; do not invent or require zero retention unless vendor source text explicitly says so.

`--ephemeral` and bridge-side `history.persistence=none` reduce local/history retention; they are **not** proof of provider no-training. References to verify: [non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode), [auth](https://learn.chatgpt.com/docs/auth). SIWC out of scope.

**Synthetic fixture rule:** After the candidate branch contains the bridge helper, required enable/bridge flags are set, and `check-auth` succeeds, the small provided fixture `PERSONAL_GROQ_CODEX_SYNTHETIC.json` may be used for offline pytest (mocked paths) and for live synthetic `analyze` smoke tests. Real or sensitive transcript content remains blocked until the real-data gate above closes. Offline pytest with the fixture does not require bridge auth.

## Bridge CLI (PR #50 — not on `main` until merged)

`tools/codex_subscription_bridge.py`: `--enable` required (else disabled, exit 2); `--codex-binary` (default `codex`); optional `--model`; `--reasoning-effort` choices `minimal|low|medium|high|xhigh` (default `high`); `--timeout-seconds` default 90. Subcommands: `check-auth` (no model call); `analyze` reads stdin JSON **`{"transcript_text":"..."}` only**. Success shape: `intelligence.{key_points,decisions,action_items,open_questions}` as string arrays. Explicit error statuses per `CODEX_SUBSCRIPTION_BRIDGE.md` on the bridge branch — no API-key fallback. One Codex subprocess at a time.

Set `$RecantorBridgeScript`, `$RecantorCodexBinary`, and `$RecantorBridgeArgs` once; use the same flags for `check-auth` and `analyze`:

```powershell
$RecantorTestRoot = "<absolute-path-to-recantor-checkout>"
$RecantorBridgeScript = Join-Path $RecantorTestRoot "tools\codex_subscription_bridge.py"
$RecantorCodexBinary = "codex"   # or full path to operator-installed binary if not on PATH
$RecantorBridgeArgs = @("--enable", "--codex-binary", $RecantorCodexBinary, "--reasoning-effort", "high", "--timeout-seconds", "90")
```

**1. Offline pytest** (after tree contains PR #50 files):

```powershell
Set-Location $RecantorTestRoot
uv run --project apps/api pytest apps/api/tests/test_meeting_intelligence.py apps/api/tests/test_codex_process.py
```

**2. Bridge auth (required before `analyze`)** — PENDING until helper exists and account is logged in:

```powershell
Set-Location $RecantorTestRoot
uv run --project apps/api python $RecantorBridgeScript @RecantorBridgeArgs check-auth
```

Record exit code and a safe auth-mode summary only (no tokens). IDE login ≠ bridge auth until this succeeds on the refreshed tree.

**3. Synthetic `analyze`** — PENDING until step 2 succeeds and the candidate tree has bridge helper + operator tooling (not blocked by the unresolved real-data privacy gate):

```powershell
Get-Content -Raw -Encoding utf8 (Join-Path $RecantorTestRoot "docs\development\PERSONAL_GROQ_CODEX_SYNTHETIC.json") |
  uv run --project apps/api python $RecantorBridgeScript @RecantorBridgeArgs analyze
```

Optional `--model`: when the operator selects an eligible candidate model to test availability, append `--model` to `$RecantorBridgeArgs` (or pass it on that run) and execute **one** explicit synthetic `analyze` with that choice — that run is the availability probe; do not require a separate live probe before the first `--model` attempt. The default effective model may remain **unknown** until observed on a successful run; record `model_unavailable` or other explicit errors honestly — no silent fallback.

**4. Windows containment** — **Required** before bridge acceptance on Windows, not optional. Witness Job Object / process-tree behavior per `CODEX_SUBSCRIPTION_BRIDGE.md` on the bridge branch. Linux CI covers other timeout/cancel paths.

**5. Groq STT E2E** — separate track; intelligence failure must not block capture/STT.

## Example host preflight (not bridge proof)

One operator machine on **2026-10-12** reported `codex-cli 0.162.1`, ChatGPT auth mode via `codex login status`, and `codex exec --help` showing `--ephemeral`, `--ignore-user-config`, `--ignore-rules`, `--output-schema`, `--skip-git-repo-check`. That documents CLI surface on that host only; it does **not** prove model availability, bridge `check-auth`/`analyze` behavior, or privacy acceptance. Bridge helper was not invoked on `main` at publish time.

## Known gaps (runtime)

- Bridge not on `main`; PR #50 base may lag integration — refresh onto `655a421d…` (or current `main`) before tests.
- No automatic API/worker wiring from transcript jobs to bridge; session/range attribution when wired is unspecified here.
- Global Groq env; subscription tier and reasoning levels (incl. `xhigh`) unproven without account evidence.
- Real-data privacy gate unresolved; live synthetic `analyze` not executed for this publication.

## Evidence checklist (for #67)

When runs complete, attach to #67: `repo_main_sha`, `bridge_branch_sha`, `codex_cli_version`, `bridge_check_auth_exit`, `auth_mode_safe_summary`, `requested_model`, `requested_reasoning_effort`, `actual_model_observed` (unknown unless observed), `analyze_status`, `synthetic_fixture_sha256`, `offline_pytest_result`, `windows_containment_witness`, `privacy_gate`, `groq_e2e` (out of scope). Full acceptance criteria live in #67 — not duplicated here.
