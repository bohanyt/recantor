# Personal Groq → Codex test readiness (development only)

Runbook for Groq STT (server env) → canonical transcript → Codex subscription bridge ([Issue #47](https://github.com/bohanyt/recantor/issues/47), [PR #50](https://github.com/bohanyt/recantor/pull/50)) for derived meeting intelligence. No runtime wiring, UI auth flows, session credential files, Docker auth mounts, or production multi-user auth in this track.

**Acceptance owner:** [Issue #67](https://github.com/bohanyt/recantor/issues/67) tracks runtime evidence (SHAs, checklist, witnesses). Depends on #47 and a PR #68 worktree that reconciles `tools/codex_subscription_bridge.py` onto current `main`.

**Anchors (verify at run time):** `main` at `655a421d45444795a8d71e6140f522a3bcc3763e` had no bridge helper; PR #50 pin `652581b62c6f7ca392a5e235f3a5231f25370709` carries the helper but is not on `main`. PR #68 draft branch `docs/issue-67-personal-groq-codex-test` holds the reconciled implementation and evidence for #67.

Invariants (`AGENTS.md`, ADRs `0001-foundation-stack`, `0002-recording-access-guardrails`): capture/STT/transcript must not depend on LLM; intelligence fails explicitly downstream. Intelligence output must remain attributable to transcript session/range when wired; per-user BYOK and provider-picker UI are **out of scope** here (deferred).

## What this publication includes

- This runbook, `PERSONAL_GROQ_CODEX_SYNTHETIC.json`, and `PERSONAL_GROQ_CODEX_RUNTIME_EVIDENCE.json`.
- Bridge implementation (`apps/api/src/recantor/meeting_intelligence.py`, `codex_process.py`, tests, `tools/codex_subscription_bridge.py`) on the #68 worktree — **not merged to `main`** until PR #68 lands.
- Honest platform limits: POSIX `analyze` uses strict root-deny/minimal-read/workspace-read/network-false config; unelevated Windows `analyze` is **unavailable** (fail-closed `containment` after auth, before model exec). `check-auth` may still run on Windows.

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

**Synthetic fixture rule:** After the candidate branch contains the bridge helper, required enable/bridge flags are set, and `check-auth` succeeds, the small provided fixture `PERSONAL_GROQ_CODEX_SYNTHETIC.json` may be used for offline pytest (mocked paths) and for live synthetic `analyze` smoke tests **on POSIX hosts where strict containment is supported**. Real or sensitive transcript content remains blocked until the real-data gate above closes. Offline pytest with the fixture does not require bridge auth.

## Bridge CLI (PR #68 worktree — not on `main` until merged)

`tools/codex_subscription_bridge.py`: `--enable` required (else disabled, exit 2); `--codex-binary` (default `codex`); optional `--model`; `--reasoning-effort` choices `minimal|low|medium|high|xhigh` (default `high`); `--timeout-seconds` default 90. Subcommands: `check-auth` (no model call); `analyze` reads stdin JSON **`{"transcript_text":"..."}` only** (512 KiB max). Success shape: `intelligence.{key_points,decisions,action_items,open_questions}` as string arrays. Explicit error statuses per `CODEX_SUBSCRIPTION_BRIDGE.md` — no API-key fallback. One Codex subprocess at a time. Invalid bridge configuration (e.g. non-positive timeout) emits structured JSON `configuration` and exit 2.

Set `$RecantorTestRoot`, `$RecantorBridgeScript`, `$RecantorCodexBinary`, and `$RecantorBridgeArgs` once; use the same flags for `check-auth` and `analyze`:

```powershell
$RecantorTestRoot = "<absolute-path-to-recantor-checkout>"
$RecantorBridgeScript = Join-Path $RecantorTestRoot "tools\codex_subscription_bridge.py"
$RecantorCodexBinary = "codex"   # or full path to operator-installed binary if not on PATH
$RecantorBridgeArgs = @("--enable", "--codex-binary", $RecantorCodexBinary, "--reasoning-effort", "high", "--timeout-seconds", "90")
```

**1. Offline pytest** (after tree contains bridge files):

```powershell
Set-Location $RecantorTestRoot
uv run --project apps/api pytest apps/api/tests/test_meeting_intelligence.py apps/api/tests/test_codex_process.py
```

**2. Bridge auth** — safe on Windows; does not call a model:

```powershell
Set-Location $RecantorTestRoot
uv run --project apps/api python $RecantorBridgeScript @RecantorBridgeArgs check-auth
```

Record exit code and a safe auth-mode summary only (no tokens).

**3. Synthetic `analyze`** — **POSIX only** with strict containment. On unelevated Windows expect `containment` without a model call; do not treat a historical read-only fallback success as accepted proof.

```powershell
Get-Content -Raw -Encoding utf8 (Join-Path $RecantorTestRoot "docs\development\PERSONAL_GROQ_CODEX_SYNTHETIC.json") |
  uv run --project apps/api python $RecantorBridgeScript @RecantorBridgeArgs analyze
```

Optional `--model`: when testing availability on a supported host, append `--model` and run **one** explicit synthetic `analyze`. Record `model_unavailable` or other explicit errors honestly — no silent fallback.

**4. Windows containment** — local pytest covers Job Object timeout/cancel/success-exit descendant cleanup with typed process witnesses. That is **not** the same as accepted Windows `analyze` containment.

**5. Groq STT E2E** — separate track; intelligence failure must not block capture/STT.

## Known gaps (runtime)

- Bridge implementation on PR #68 worktree; not merged to `main` yet.
- No automatic API/worker wiring from transcript jobs to bridge; session/range attribution when wired is unspecified here.
- Global Groq env; Groq STT E2E still out of scope for this track.
- Real-data privacy / no-training gate unresolved; synthetic fixture only for live Codex calls where `analyze` is supported.
- Windows unelevated strict filesystem containment for Codex `exec` remains unsupported; `analyze` stays fail-closed.

## Evidence checklist (for #67)

When runs complete, attach to #67: fields documented in `PERSONAL_GROQ_CODEX_RUNTIME_EVIDENCE.json`. Full acceptance criteria live in #67 — not duplicated here.
