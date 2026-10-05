# Recantor Alpha.3 Distribution Handoff — 2026-10-05

Result: the approved integration push, cloud prerelease publication, and final bounded Windows release-distribution witness are complete/PASS. The bounded #58/#63 release lifecycle and accepted #61 recovery behavior are shipped on the integration/release line. This is a prerelease acceptance, not a stable-product declaration or authorization to merge main.

## Authority and immutable identity

Bohan explicitly lifted the earlier Actions hold in this chat: "hold aku cabut, silakan". The approved plan covered pushing the tested candidate, publishing alpha.3, and completing the Windows distribution witness. No main merge, force push, new PR, or optional #47 work was performed.

- Published tag: [`v0.1.0-alpha.3`](https://github.com/bohanyt/recantor/releases/tag/v0.1.0-alpha.3), a GitHub prerelease.
- Exact release source: `64a6e2235af75f4d9652f488d0fc650f0531961a`.
- Annotated tag object: `02127d4e70c9a87f245bfd4cf701b11a717fb1bf`; it resolves to the exact source above.
- API: `ghcr.io/bohanyt/recantor-api@sha256:12de7229cc41466459495a8d5ba685d4876cba431c21572e604c6e988e3d60ad`.
- Web: `ghcr.io/bohanyt/recantor-web@sha256:9d2f34220c25fa3230fb171bc14294fa367316101abbc3808b2ea6ea5968e970`.
- Manifest: format 2, alpha channel, `linux/amd64`, schema `0009`, explicit rollback-safe target `v0.1.0-alpha.2`, `backup_required=false`.
- Release bundle SHA256: `a1f5d04698440d6505bbb923dd32c49043d825bbe5ae2bf8677e7a0224c4efd6`.
- Release manifest SHA256: `edeb96e4bc3262ec93202fd8957f13b64d35ca6519e79ac9fe23cc85f21c3875`.

The source includes accepted #63 `2a4aa7f4`, accepted #61 `4d2fe9ae`, reviewed core safety repairs at `2d95c4d`, release-context correction `5932df6`, and the earlier local proof documentation. Later status documentation may advance integration without moving this release tag or substituting its images.

## Cloud evidence

| Run | Result | Scope |
| --- | --- | --- |
| [CI 37270291070](https://github.com/bohanyt/recantor/actions/runs/37270291070) | SUCCESS | Backend, frontend, E2E, Compose smoke; optional alpha-cloud-proof skipped by its gate |
| [Upload foundation 37270291050](https://github.com/bohanyt/recantor/actions/runs/37270291050) | SUCCESS | Upload foundation E2E |
| [Media processing 37270291109](https://github.com/bohanyt/recantor/actions/runs/37270291109) | SUCCESS | Uploaded-media processing proof |
| [Release images 37270796641](https://github.com/bohanyt/recantor/actions/runs/37270796641) | SUCCESS | Exact-source image build/test, immutable GHCR publication, pull-back OCI verification, pull-only Compose smoke, manifest/bundle, GitHub prerelease |

The generated release assets were downloaded from GitHub. Their sizes/digests match release metadata; the embedded manifest matches the standalone asset. The four bundle files match the reviewed source after newline normalization. The API/Web OCI version and revision labels match the published manifest.

## Final Windows acceptance

The user's Windows host ran the published bundle's `recantor.ps1` through fresh Windows PowerShell processes and Docker Desktop Linux containers. An isolated empty Docker config (`auths={}`) forced anonymous pulls. No application source was compiled or application image built for this distribution witness. The actual alpha.3 API/Web digests were pulled from GHCR; the separate earlier localhost witness is not used as distribution acceptance.

Supported project: `recantor-r63-ghcr-20261005`. Unsafe-policy fixture project: `recantor-r63-ghcr-unsafe-20261005`. Both used synthetic local configuration/data and their own volumes; existing user stacks/volumes were not reused or deleted.

| Requirement | Observed evidence |
| --- | --- |
| Install N | Published alpha.2 immutable baseline installed with the alpha.3 bundle updater; API/Web healthy |
| Representative durable state | Actual Live `RecordingSession` created through the API, PostgreSQL sentinel row, and audio-volume sentinel file |
| Safe failed activation | Published alpha.3 candidate migration followed by deliberate API startup exit 42; exact alpha.2 API/Web/worker digest refs restored and health verified |
| Interrupted update | Persisted `pending.phase=pulling` with exact alpha.3 identity; a fresh PowerShell process retried successfully |
| Successful N+1 | Current alpha.3, previous alpha.2, pending null; all application-role image refs match the manifest; API health/readiness and Web HTTP 200 |
| Explicit application rollback | Alpha.3 -> exact alpha.2 -> alpha.3 reactivation; no database downgrade and data retained |
| Unsafe migration metadata | Separate fixture used the same published candidate images with an intentionally empty rollback-compatibility list; startup failure produced `rollback_refused_unsafe` / `manual_recovery_required`, retained applied schema authority and known-good identity, and preserved durable data |
| Normal user update path | Published bundle `update -Channel alpha`, with no source SHA or manifest path/URI, selected the real GitHub alpha.3 manifest and exact expected digests |
| Secret/config safety | `.env` SHA256 unchanged through updates, rollback, and browser extension; final scan of 20 log/state files found no generated proof secret |
| Visible client truth | Supported `status` output identifies alpha.3; actual published Web image in Microsoft Edge renders Live/Upload workflows, API Online, PostgreSQL readiness Online, and no page errors |
| Cleanup | All witness containers stopped; original Recantor PostgreSQL/audio volumes remain available |

The unsafe fixture modifies compatibility metadata only; it is not an additional published release or a real destructive schema migration. The fault-injection override was enabled only in explicit updater test mode. The first hook attempt was correctly refused before migration/activation because the harness omitted that mode; verification resumed from the unchanged installed baseline, retaining original anonymous-pull evidence. No product source repair or replacement image was needed.

This campaign did not repeat the already-accepted real microphone/Groq acceptance or real disk-file Edge reopen witness. Those remain independent evidence; the current cloud and selected local regressions preserve their behavior. Already-granted file-handle permission is covered by deterministic browser tests; the accepted real Edge reopen returned `prompt` and resumed through one truthful permission action.

## Evidence and current coordination

Public compact results: [Windows distribution](evidence/2026-10-05-alpha3-distribution.json), [automatic channel selection](evidence/2026-10-05-alpha3-channel.json), and [released Edge Web smoke](evidence/2026-10-05-alpha3-web.json).

Raw logs, manifests, bundle, state, proof scripts, screenshots, and synthetic `.env` remain under ignored `runtime/ct-20261005/` in `recantor-core-repair-0929`. Sanitized selected evidence and a verified Git bundle are preserved in the workspace's `checkpoints/` directory. Do not publish `.env` or raw state as release assets.

Fresh inspection after integration push showed PR #65 automatically marked merged because its accepted source was included in the pushed integration history. No separate merge API call was made. PR #53 remains OPEN/DRAFT/unmerged, and main remains `59ff57502a9a1d84ad332cab106c46c256482d23`.

The earlier Actions hold and missing-GHCR comments are historical and superseded by this authorized publication and its actual evidence. Do not retag alpha.3, rebuild client application source as release proof, repeat earned witnesses without a concrete regression, or delete persistent volumes. Main merge still requires explicit Bohan authorization. Optional #47 remains parked; production auth, retention/deletion UX, deployment hardening, diarization, summaries, and native clients remain separate work.
