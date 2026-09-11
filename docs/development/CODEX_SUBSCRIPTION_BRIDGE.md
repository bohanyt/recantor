# Development-only Codex subscription bridge

Issue #47 adds a deliberately narrow development helper for transcript-derived meeting
intelligence. It is **not** speech-to-text, is not wired into capture, and is not a
production inference service.

## Boundary

The implementation is intentionally host-local and opt-in:

```text
bounded canonical transcript text
        |
        v
MeetingIntelligenceProvider
        |
        v
local process adapter
        |
        +--> codex login status
        |
        +--> codex exec --ephemeral --ignore-user-config ...
                 |
                 v
        existing saved ChatGPT Codex authentication
```

Nothing in this slice changes Groq STT. Nothing is added to Docker Compose. Do not copy or
mount `~/.codex`, ChatGPT browser state, cookies, OAuth tokens, `auth.json`, or other personal
credentials into a Recantor container.

The helper accepts only Codex CLI authentication that `codex login status` reports as
ChatGPT mode. API-key/access-token environment variables are removed from the child
environment, and `codex exec --ignore-user-config` prevents a local custom provider from
being selected through user config. The bridge never reads credential files itself.

## Why `codex exec`

The official Codex CLI documents non-interactive `codex exec`, reuse of saved CLI
authentication, stdin as additional context, `--ephemeral`, model override, inline config
override, and `--output-schema`. The bridge uses those documented surfaces instead of
scraping ChatGPT/browser credentials or treating a ChatGPT subscription as an OpenAI API
credential.

Transcript text is written to child stdin. It is never interpolated into a shell command or
placed in argv. The subprocess adapter uses `shell=False`, kills the child on timeout, drains
stdout/stderr with bounded in-memory capture, and does not expose raw stderr in provider
errors.

## Safety and failure semantics

- disabled unless the host helper is launched with `--enable`;
- one active model invocation per provider instance;
- transcript input is capped at 120,000 characters;
- output must match the strict JSON shape for `key_points`, `decisions`, `action_items`, and
  `open_questions`;
- no automatic retry loop;
- missing CLI, missing auth, non-ChatGPT auth, rate/usage limits, requested-model
  unavailability, timeout, malformed output, and generic process failure are distinct
  statuses;
- derived intelligence failure has no effect on recording, durable audio, STT, or canonical
  transcript truth;
- transcript text and captured stderr are not logged by this module.

The provider intentionally does not persist meeting-intelligence state yet. This slice proves
the owned text/provider boundary and safe local invocation mechanics only.

## Host usage

Install/sign in to Codex normally on the development host first. Then from the repository
root:

```bash
uv run --project apps/api python tools/codex_subscription_bridge.py \
  --enable check-auth
```

`check-auth` verifies only that the saved CLI authentication mode is ChatGPT. It does **not**
call a model and does not establish that any particular model is available.

For a bounded development request, pipe a JSON object on stdin:

```bash
printf '%s\n' '{"transcript_text":"Rapat memutuskan pilot dilanjutkan minggu depan."}' \
  | uv run --project apps/api python tools/codex_subscription_bridge.py \
      --enable analyze
```

Omitting `--model` deliberately makes no claim about the actual Codex model selected by the
local installation. The bridge reports only requested model/effort, not an invented
"actual model".

## Deferred final local capability probe

Per the control-tower plan, the real subscription/auth/model probe belongs to Issue #48, not
ordinary cloud CI and not this implementation run.

At that final local gate:

1. verify `check-auth` reports ChatGPT mode;
2. run a tiny `analyze` request with `--model gpt-5.6-luna --reasoning-effort high`;
3. record success or the explicit `model_unavailable`/auth/limit status;
4. if Luna is unavailable, record the usable/default model honestly rather than silently
   switching to paid OpenAI API usage.

The presence of a model name in Codex documentation or in this probe recipe is **not**
evidence that the current ChatGPT account can select it.

## CI contract

Ordinary tests inject a fake process adapter. They prove command construction, transcript
injection safety, removal of API-key/access-token environment variables, timeout handling,
model-unavailable behavior, strict JSON parsing, and explicit unavailable/auth states without
requiring Codex, ChatGPT login, network access, or an OpenAI API key.

A small subprocess-adapter test uses the current Python executable only to prove kill-on-timeout
and bounded stdout/stderr capture. It does not invoke Codex.
