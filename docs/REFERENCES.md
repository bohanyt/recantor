# References and prior art

Recantor is a clean public upstream, not a fork of the projects below. These references document architectural research and possible dependencies so future contributors can distinguish inspiration from adopted code.

Before copying or vendoring third-party code, verify the exact license at the commit/version being adopted and record attribution/obligations here or in a dedicated notice.

## Vexa Desktop

Repository: https://github.com/Vexa-ai/vexa-desktop

Why it is relevant:

- durable continuous recording plus transcription chunks;
- pause/silence-based chunking;
- local session/history persistence;
- OpenAI-compatible STT requests;
- simple retry behavior;
- clear separation between archival audio and transcription work.

Useful architectural lesson:

> preserve the recording continuously while creating smaller voiced utterances for STT.

Recantor does not use Tauri/Rust as its primary web architecture. The desktop project is prior art for recorder behavior and failure handling.

Research observed initial chunking values around a minimum useful chunk of 1.5 seconds, a short silence tail, and a hard maximum around 8 seconds. Recantor treats those only as benchmark starting points, not copied constants or permanent product contracts.

## WhisperLiveKit

Repository: https://github.com/QuentinFuxa/WhisperLiveKit

Why it is relevant:

- browser microphone capture;
- realtime WebSocket speech processing;
- VAD;
- live transcription UX;
- multiple realtime ASR backends;
- diarization integrations;
- Python/FastAPI speech stack.

Useful architectural lesson:

> the browser can remain a thin realtime client while Python owns speech processing.

Recantor should not blindly reuse WhisperLiveKit's remote OpenAI-style LocalAgreement strategy for Groq if it repeatedly retranscribes overlapping accumulated audio. Remote paid/rate-limited APIs have different economics and limits from local streaming inference.

License note: verify optional backend dependencies individually. Some realtime speech backends can have licensing terms that differ from the top-level project license.

## Vexa

Repository: https://github.com/Vexa-ai/vexa

Why it is relevant:

- meeting-domain separation;
- standalone OpenAI-compatible transcription service;
- faster-whisper/CTranslate2 GPU service patterns;
- recordings/transcripts as explicit platform concepts;
- production-oriented API/worker decomposition.

Useful architectural lesson:

> split services at real runtime/resource boundaries, not merely because modules have different names.

Recantor intentionally starts much smaller and does not inherit Vexa's meeting-bot/platform topology.

## Speaches

Repository: https://github.com/speaches-ai/speaches

Why it is relevant:

- self-hosted OpenAI-compatible speech API;
- faster-whisper backend;
- local speech server deployment patterns.

Possible role:

A deployable local STT implementation or a reference for Recantor's local `STTProvider` adapter. Recantor should still own the provider boundary so local speech infrastructure can change later.

## tus protocol / tusd / Uppy

Projects:

- https://tus.io/
- https://github.com/tus/tusd
- https://github.com/transloadit/uppy

Why they are relevant:

- resumable large uploads;
- upload offset reconciliation;
- browser retry/resume UX;
- battle-tested transfer infrastructure.

Useful architectural lesson:

> Recantor should own upload authorization and product lifecycle, not reimplement resumable byte-transfer semantics.

## faster-whisper / CTranslate2

Projects:

- https://github.com/SYSTRAN/faster-whisper
- https://github.com/OpenNMT/CTranslate2

Why they are relevant:

- local GPU/CPU Whisper inference;
- practical self-hosted fallback;
- explicit device/compute configuration;
- common Python deployment ecosystem.

Recantor expects local STT to run behind a separate resource boundary so GPU runtime concerns do not leak into the main API process.

## Browser platform reliability references

Official/reference documentation:

- StorageManager persistent storage: https://developer.mozilla.org/en-US/docs/Web/API/StorageManager/persist
- StorageManager quota/usage estimate: https://developer.mozilla.org/en-US/docs/Web/API/StorageManager/estimate
- Storage quotas and eviction criteria: https://developer.mozilla.org/en-US/docs/Web/API/Storage_API/Storage_quotas_and_eviction_criteria
- Web Locks API: https://developer.mozilla.org/en-US/docs/Web/API/Web_Locks_API
- MediaRecorder `dataavailable` timing caveats: https://developer.mozilla.org/en-US/docs/Web/API/MediaRecorder/dataavailable_event

Important consequences for Recantor:

- ordinary browser storage is best-effort unless persistent storage is granted;
- the browser can expose approximate origin usage/quota, which Recantor should monitor while locally buffering unacknowledged audio;
- Web Locks can coordinate same-origin tabs, but server capture ownership is still required across tabs/devices;
- MediaRecorder `timeslice` is not an exact clock, and mobile/background behaviors can delay `dataavailable` events.

These facts inform ADR 0002. Browser behavior still needs real-device validation; documentation is not a substitute for the Phase 1 test matrix.

## Runtime references

The initial application scaffold uses maintained mainstream runtime lines:

- Node.js release status: https://nodejs.org/en/about/previous-releases
- Python active release status: https://www.python.org/downloads/

ADR 0001 currently selects Node.js 24 LTS for web/tooling and Python 3.13 for the main API. ML/GPU services may use a different supported Python minor if their selected dependency stack requires it.

## Speaker diarization research

Candidates to benchmark rather than preselect permanently:

- NVIDIA NeMo Streaming Sortformer for online diarization;
- pyannote.audio for offline/local diarization;
- WeSpeaker or comparable speaker-embedding toolkits for optional enrolled-speaker identification.

Diarization and named speaker identification are separate capabilities. Generic stable speaker labels should remain valid when identification confidence is insufficient.

## Groq speech-to-text

Groq is the intended primary external live STT provider for the initial product.

Recantor should use a server-side provider adapter and never expose API credentials to browsers.

Operational behavior such as model names, pricing, request limits, and rate limits can change and must be checked against current provider documentation at implementation/deployment time rather than hard-coded into architecture documentation.

## Adoption rule

A reference becomes a dependency only when the repository explicitly adds it.

When adopting third-party code or packages:

1. record the exact package/project and version/commit;
2. verify its license and transitive licensing concerns;
3. keep vendor-specific behavior behind Recantor-owned contracts where practical;
4. add required notices/attribution;
5. add tests around the behavior Recantor depends on.
