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
bounded local process-tree adapter
        |
        +--> codex login status
        |
        +--> codex exec --ephemeral --ignore-user-config --ignore-rules ...
                 |
                 v
        existing saved ChatGPT Codex authentication
```

Nothing in this slice changes Groq STT. Nothing is added to Docker Compose. Do not copy or
mount `~/.codex`, ChatGPT browser state, cookies, OAuth tokens, `auth.json`, or other personal
credentials into a Recantor container.

## Authentication and billing boundary

The bridge never reads credential files itself. It lets Codex perform its normal host-local
authentication lookup and uses two independent checks:

- `codex login status` is a preflight UX check and must report ChatGPT mode;
- the actual `codex exec` invocation pins `forced_login_method="chatgpt"`, so an active
  credential that does not satisfy the ChatGPT constraint fails closed at execution time.

The Codex parent process receives only an explicit allowlist of host environment variables
needed to locate the executable, normal OS runtime/TLS state, and Codex's own saved-auth
location. Arbitrary host variables are not inherited. In particular, OpenAI/Codex API-key
and access-token variables are not passed to the child. This bridge never silently switches
to API-key billing.

`--ignore-user-config` prevents ambient user configuration from selecting a custom provider,
and `--ignore-rules` skips ambient user/project execution-policy `.rules` files for this
controlled invocation. These controls do not scrape or copy authentication material.

## Least-privilege model/tool boundary

Transcript text is untrusted input. A model instruction plus the legacy `--sandbox
read-only` mode is **not** treated as a confidentiality boundary.

Current Codex supports permission profiles that do not compose with the legacy `--sandbox`
flag, so this bridge deliberately omits `--sandbox` and supplies a dedicated permission
profile through command-line config overrides:

- filesystem `:root = "deny"`;
- filesystem `:minimal = "read"` for Codex's minimum runtime surface;
- the per-invocation temporary workspace root is `read`;
- network access is disabled.

The invocation runs from a fresh temporary directory that contains only the generated output
schema. The repository, home directory, and unrelated host paths are outside the allowed
workspace and are denied to model tool use.

This text-to-JSON operation does not need command execution or external retrieval, so the
bridge also disables the shell tool, shell snapshot, web search, apps/connectors, subagents,
remote plugin use, image viewing, skill dependency installation, history persistence, and
memory generation. Shell environment policy is pinned to `inherit = "none"`; if a shell
surface were unexpectedly reintroduced by a future Codex change, only the explicitly set
PATH and temporary-directory variables would be available to it.

The parent Codex process may still receive HOME/CODEX_HOME or equivalent platform variables
so Codex itself can locate its existing authentication. The permission profile and disabled
tool surfaces prevent those locations from becoming model-readable roots. No auth material
is copied into the temporary workspace.

## Process and cancellation boundary

Transcript text is written to child stdin. It is never interpolated into a shell command or
placed in argv. `subprocess.Popen` uses `shell=False`, bounded stdout/stderr capture, and an
OS-specific invocation-tree containment seam.

On POSIX, every invocation starts in a new session/process group and termination sends
`SIGKILL` to the whole process group. On Windows, the implementation creates a Job Object
with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, starts the child suspended, assigns it to the Job
Object before it can execute, then resumes it. Timeout/cancellation terminates the Job Object,
covering descendants created by Codex. Failure to establish containment is an explicit
`containment` error rather than a fallback to single-PID execution.

The direct process wait and stdout/stderr drain joins have fixed post-termination bounds.
Even if pipe cleanup is incomplete, the adapter returns a containment failure instead of
waiting indefinitely. The tree is also terminated after an apparently successful direct
process exit so a background descendant cannot outlive Codex and keep inherited pipes open.

Windows Job Object code is present for the supported final Windows path, but ordinary GitHub
CI runs the exact descendant-liveness regression on Linux only. A real Windows containment
runtime witness remains part of the final local #48 acceptance campaign.

## Safety and failure semantics

- disabled unless the host helper is launched with `--enable`;
- one active model invocation per provider instance;
- transcript input is capped at 120,000 characters;
- output must match the strict JSON shape for `key_points`, `decisions`, `action_items`, and
  `open_questions`;
- no automatic retry loop;
- missing CLI, missing auth, non-ChatGPT auth, rate/usage limits, requested-model
  unavailability, timeout, cancellation, containment failure, malformed output, and generic
  process failure are explicit statuses;
- derived intelligence failure has no effect on recording, durable audio, STT, or canonical
  transcript truth;
- transcript text and captured stderr are not logged or included in user-facing provider
  exceptions.

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
   switching to paid OpenAI API usage;
5. on Windows, exercise timeout/cancellation once to confirm Job Object containment on the
   target host runtime.

The presence of a model name in Codex documentation or in this probe recipe is **not**
evidence that the current ChatGPT account can select it.

## CI contract

Ordinary tests require no Codex installation, ChatGPT login, network access, or OpenAI API
key for provider behavior. Fake-process tests prove:

- the dedicated permission profile has root-deny/minimal-read/workspace-read semantics;
- shell/web/apps/subagents are disabled and shell environment inheritance is `none`;
- actual exec is pinned to `forced_login_method="chatgpt"` and ambient `.rules` are ignored;
- arbitrary secret environment variables and OpenAI API-key variables are absent from the
  child environment;
- an adversarial transcript that names a sentinel file outside the workspace and asks for a
  sentinel environment secret cannot receive either secret through the configured boundary;
- model-unavailable, malformed-output, auth, timeout, and containment states remain explicit.

The subprocess regression uses only the current Python executable. On Linux it launches a
child that launches a long-lived grandchild inheriting stdout/stderr, then proves both timeout
and explicit cancellation terminate that descendant tree and return inside the fixed wall
bound. This does not invoke Codex or any real account.
