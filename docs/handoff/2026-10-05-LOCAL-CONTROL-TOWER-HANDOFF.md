# Recantor Local Control Tower Handoff — 2026-10-05

GitHub is the technical authority. Fresh-fetch refs and re-read issue/PR comments before continuing. This records local implementation and verification; it does not claim remote integration or stable acceptance.

## Checkpoint and authorization

- Repository: `bohanyt/recantor`.
- Remote `main`: `59ff57502a9a1d84ad332cab106c46c256482d23`.
- Remote `integration/cloud-alpha-2026-09-11`: `ea489862fab994eaa8d4520cdcf280ea37a14e11`.
- Local branch/worktree: `local-core-repair-0929`, `recantor-core-repair-0929`.
- Accepted local repair source: `2d95c4d499824d0b508997d2642283bde7432f6e`.
- Tested source including the release-context correction: `5932df62844e19058763a69af11519059b055c77`. This handoff and CURRENT are a documentation-only descendant; image identity remains the tested source.
- PR #53 (integration checkpoint) and PR #65 (#63 updater) remain draft/unmerged. No push, merge, publication, or workflow dispatch was performed on October 5.

The user authorized continuation as local implementor/control tower until user action is needed. The existing [local-CI substitution authority](https://github.com/bohanyt/recantor/issues/58#issuecomment-5884462317) permits local verification, while the later [Actions/push hold](https://github.com/bohanyt/recantor/issues/58#issuecomment-5884613575) remains unresolved. A calendar change is not evidence that the hold has been lifted. No main merge is authorized.

The [September 29 repair acceptance](https://github.com/bohanyt/recantor/issues/58#issuecomment-5887266485) accepted A1 schema-authority, A2 immutable image/environment isolation, and A3 Upload capability-boundary repairs at `2d95c4d`. This run closes their remaining local environment-dependent proof, and fixes a malformed Git ignore rule plus the missing root Web build-context exclusions.

## Changes

Commit `5932df6` repairs the literal escaped-newline `.recantor/` entry in `.gitignore` and adds root `.dockerignore`. The release Web Dockerfile uses the repository root as build context, so environment files, updater state, local recordings/storage, virtual environments, dependencies, generated artifacts, and test reports now stay outside that context. No product behavior was otherwise changed in this run.

## Verification

| Check | Result | Scope |
| --- | --- | --- |
| Backend | 160 passed, 5 warnings | Python 3.13 Linux container, real PostgreSQL 17/Redis 8, FFmpeg; includes Upload capability routes and Redis WebSocket integration |
| Frontend | 62 passed across 15 files | Frozen dependency install, lint, typecheck, format check, build; one existing Fast Refresh lint warning |
| Edge recovery/result/correction | 9 passed, 1 skipped | File-handle deterministic browser coverage; real fixture Upload test skipped |
| Edge Live/finalization/layout | 34 passed, 1 skipped | Fenced capture, finalize sync/timeout, transcript, missing sequence, recorder, realtime audio, product shell, safety layout; CI-only environment-loader test skipped |
| API/browser smoke | 1 passed | Actual isolated backend connection |
| Updater unit contracts | PASS | Windows PowerShell updater regression script |
| Release manifest | 6 passed | Manifest validation contracts |
| Ruff | PASS | Check and format, 69 files |
| API/Web release builds | PASS | Both OCI revision labels equal `5932df6`; Web uses actual release Dockerfile/root context |
| Updater real-container proof | PASS | Isolated Windows Docker release project and local registry; details below |

Selected browser total: 44 passed, 2 skipped. This is not a new human microphone/provider acceptance or a new real disk-file Upload witness. Preserve the accepted [real Edge reopen witness](https://github.com/bohanyt/recantor/issues/61#issuecomment-5882870731) and [alpha product witness](https://github.com/bohanyt/recantor/issues/48#issuecomment-5756111530) as their own evidence.

Exploratory harness failures were resolved before the successful runs: a missing temporary pytest directory, unsupported native-Windows Psycopg/Proactor combination (the backend suite ran on Linux), and a browser test service using the wrong CORS origins (corrected to the CI origins). None required a product code change. The updater proof finished its runtime assertions, then the final secret scan encountered an empty log; the harness switched to `File.ReadAllText` and resumed final verification without reinstalling or replaying the scenarios.

## Updater runtime evidence

Project `recantor-ct-20261005-release` uses the actual `infra/compose.release.yaml` and `scripts/recantor.ps1`. It installed the accepted remote immutable `v0.1.0-alpha.2` baseline, then tested distinct local candidate/failure image digests. Inherited `RECANTOR_API_IMAGE`/`RECANTOR_WEB_IMAGE` values deliberately named invalid images, proving manifest-selected images take precedence.

The witness verified:

1. Interruption after `pending.phase=pulling` persists the exact candidate identity; a fresh PowerShell process retries to known-good local `alpha.3`.
2. Candidate migration and all application-role activation succeed with immutable references.
3. A distinct local `alpha.4` failure candidate exits during API activation after migration; automatic recovery restores the exact `alpha.3` API/Web digests.
4. Applied schema authority remains `alpha.4`, with explicit compatibility only to `alpha.3`. A subsequent manual rollback to `alpha.2` is refused; current identity and running digest references remain unchanged.
5. A PostgreSQL sentinel row and audio sentinel file survive every stage. The synthetic `.env` hash remains unchanged and logs/state do not expose the generated secret.

Candidate identities (local evidence only):

- API: `localhost:15000/recantor-api@sha256:5debc9b22035959f00994ac0c780aec80c63faa736bd54fd3ecd108d7b3ffac6`.
- Web: `localhost:15000/recantor-web@sha256:d55a31f5a4eee43abf351be82ac03691bcb40c88ebdf7f897f7a6043d8198ff0`.

Raw local artifacts are under ignored `runtime/ct-20261005/`: `backend-linux.log`, `browser-live-corrected.log`, API/Web build logs, `updater-proof.ps1`, `finalize-proof.ps1`, per-step updater logs, manifests, preserved state, and `updater-result.json`. The result is `PASS / LOCAL_RUNTIME_PROOF_ONLY`, with `remote_distribution_acceptance=NOT_SATISFIED`. Do not publish `.env` or the raw state directory as a release asset.

## Environment isolation and cleanup

Docker Desktop was initially stopped and was started for this run. Existing `recantor`, `recantor-r63-local-witness`, old local registry, and `istw-onlyoffice` containers were already stopped and were not activated. Tests used separately named containers, separate databases, a separate release project, and synthetic credentials/data; no user recording or provider call was used. Test services are stopped after verification; project volumes and local artifacts are retained for inspection. Do not delete the existing Recantor volumes while cleaning up.

## Next authorized boundary

Local implementation and verification are ready. Before any remote action, obtain explicit authorization to lift the Actions hold and push the prepared local candidate to the integration branch. That push updates draft PR #53 and may trigger CI. A separate release publication must produce immutable GHCR images/manifest for the exact approved source, followed by the final pull-only Windows distribution witness required for #63. The local simulated alpha versions do not reserve tags; fresh-check tags/releases before choosing a real prerelease version.

Do not accept #63, call Recantor stable, close its issue, or merge PR #53 to main based only on these local proofs. Keep optional #47 parked. If the hold remains, the preserved local checkpoint and evidence are the continuation point.
