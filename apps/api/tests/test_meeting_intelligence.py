import sys
import threading

import pytest

from recantor.meeting_intelligence import (
    CodexSubscriptionMeetingIntelligenceProvider,
    MeetingIntelligenceError,
    MeetingIntelligenceErrorCategory,
    MeetingIntelligenceRequest,
    ProcessResult,
    ProcessSpec,
    ProcessUnavailableError,
    SubprocessAdapter,
)


class FakeProcessAdapter:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[ProcessSpec] = []

    def run(self, spec: ProcessSpec) -> ProcessResult:
        self.calls.append(spec)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def process_result(
    *,
    returncode: int = 0,
    stdout: bytes = b"",
    stderr: bytes = b"",
    timed_out: bool = False,
    stdout_truncated: bool = False,
    stderr_truncated: bool = False,
) -> ProcessResult:
    return ProcessResult(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )


@pytest.mark.asyncio
async def test_fake_adapter_proves_safe_command_shape_and_structured_result() -> None:
    transcript = '\"; touch /tmp/recantor-pwned; $(echo should-not-run)\\nKeputusan: lanjut pilot.'
    adapter = FakeProcessAdapter(
        [
            process_result(stdout=b"Logged in using ChatGPT\n"),
            process_result(
                stdout=(
                    b'{"key_points":["Pilot discussed"],'
                    b'"decisions":["Continue pilot"],'
                    b'"action_items":["Confirm pilot owner"],'
                    b'"open_questions":["Who owns the pilot?"]}'
                )
            ),
        ]
    )
    provider = CodexSubscriptionMeetingIntelligenceProvider(
        process_adapter=adapter,
        codex_binary="codex-test",
        model="fake-subscription-model",
        reasoning_effort="high",
        environment={
            "PATH": "/usr/bin",
            "OPENAI_API_KEY": "must-not-reach-child",
            "CODEX_API_KEY": "must-not-reach-child",
            "CODEX_ACCESS_TOKEN": "must-not-reach-child",
        },
    )

    result = await provider.derive(MeetingIntelligenceRequest(transcript_text=transcript))

    assert result.decisions == ("Continue pilot",)
    assert result.requested_model == "fake-subscription-model"
    assert result.requested_reasoning_effort == "high"
    assert len(adapter.calls) == 2

    auth_call, exec_call = adapter.calls
    assert auth_call.argv == ("codex-test", "login", "status")
    assert exec_call.argv[0:2] == ("codex-test", "exec")
    assert "--ignore-user-config" in exec_call.argv
    assert "--ephemeral" in exec_call.argv
    assert "--skip-git-repo-check" in exec_call.argv
    assert (
        exec_call.argv[exec_call.argv.index("--sandbox")],
        exec_call.argv[exec_call.argv.index("--sandbox") + 1],
    ) == ("--sandbox", "read-only")
    assert (
        exec_call.argv[exec_call.argv.index("--model")],
        exec_call.argv[exec_call.argv.index("--model") + 1],
    ) == ("--model", "fake-subscription-model")
    config_index = exec_call.argv.index("--config")
    assert exec_call.argv[config_index + 1] == 'model_reasoning_effort="high"'
    assert transcript not in " ".join(exec_call.argv)
    assert exec_call.stdin == transcript.encode()
    assert exec_call.cwd is not None
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"):
        assert key not in exec_call.env


@pytest.mark.asyncio
async def test_saved_api_key_auth_mode_is_rejected_before_exec() -> None:
    adapter = FakeProcessAdapter([process_result(stdout=b"Logged in using API key\n")])
    provider = CodexSubscriptionMeetingIntelligenceProvider(process_adapter=adapter)

    with pytest.raises(MeetingIntelligenceError) as exc_info:
        await provider.derive(MeetingIntelligenceRequest(transcript_text="hello"))

    assert exc_info.value.category == MeetingIntelligenceErrorCategory.UNSUPPORTED_AUTH_MODE
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_missing_codex_executable_is_explicitly_unavailable() -> None:
    adapter = FakeProcessAdapter([ProcessUnavailableError("missing")])
    provider = CodexSubscriptionMeetingIntelligenceProvider(process_adapter=adapter)

    with pytest.raises(MeetingIntelligenceError) as exc_info:
        await provider.check_chatgpt_auth()

    assert exc_info.value.category == MeetingIntelligenceErrorCategory.UNAVAILABLE


@pytest.mark.asyncio
async def test_fake_adapter_timeout_maps_to_timeout_without_retry() -> None:
    adapter = FakeProcessAdapter(
        [
            process_result(stdout=b"Logged in using ChatGPT\n"),
            process_result(returncode=-9, timed_out=True),
        ]
    )
    provider = CodexSubscriptionMeetingIntelligenceProvider(process_adapter=adapter)

    with pytest.raises(MeetingIntelligenceError) as exc_info:
        await provider.derive(MeetingIntelligenceRequest(transcript_text="bounded input"))

    assert exc_info.value.category == MeetingIntelligenceErrorCategory.TIMEOUT
    assert len(adapter.calls) == 2


@pytest.mark.asyncio
async def test_model_unavailable_is_not_replaced_with_another_model() -> None:
    adapter = FakeProcessAdapter(
        [
            process_result(stdout=b"Logged in using ChatGPT\n"),
            process_result(
                returncode=1,
                stderr=b"requested model fake-model is not available for this account",
            ),
        ]
    )
    provider = CodexSubscriptionMeetingIntelligenceProvider(
        process_adapter=adapter,
        model="fake-model",
    )

    with pytest.raises(MeetingIntelligenceError) as exc_info:
        await provider.derive(MeetingIntelligenceRequest(transcript_text="hello"))

    assert exc_info.value.category == MeetingIntelligenceErrorCategory.MODEL_UNAVAILABLE
    assert len(adapter.calls) == 2
    exec_call = adapter.calls[-1]
    model_index = exec_call.argv.index("--model")
    assert exec_call.argv[model_index + 1] == "fake-model"


@pytest.mark.asyncio
async def test_invalid_structured_output_is_rejected() -> None:
    adapter = FakeProcessAdapter(
        [
            process_result(stdout=b"Logged in using ChatGPT\n"),
            process_result(stdout=b'{"key_points":["only one field"]}'),
        ]
    )
    provider = CodexSubscriptionMeetingIntelligenceProvider(process_adapter=adapter)

    with pytest.raises(MeetingIntelligenceError) as exc_info:
        await provider.derive(MeetingIntelligenceRequest(transcript_text="hello"))

    assert exc_info.value.category == MeetingIntelligenceErrorCategory.INVALID_OUTPUT


@pytest.mark.asyncio
async def test_input_limit_fails_before_auth_or_model_call() -> None:
    adapter = FakeProcessAdapter([])
    provider = CodexSubscriptionMeetingIntelligenceProvider(
        process_adapter=adapter,
        max_transcript_chars=4,
    )

    with pytest.raises(MeetingIntelligenceError) as exc_info:
        await provider.derive(MeetingIntelligenceRequest(transcript_text="12345"))

    assert exc_info.value.category == MeetingIntelligenceErrorCategory.CONFIGURATION
    assert adapter.calls == []


def test_subprocess_adapter_kills_timeout_and_bounds_output_capture() -> None:
    adapter = SubprocessAdapter()
    timeout_result = adapter.run(
        ProcessSpec(
            argv=(sys.executable, "-c", "import time; time.sleep(5)"),
            stdin=b"",
            env={},
            timeout_seconds=0.05,
            stdout_limit_bytes=16,
            stderr_limit_bytes=16,
        )
    )
    assert timeout_result.timed_out is True
    assert timeout_result.returncode != 0

    output_result = adapter.run(
        ProcessSpec(
            argv=(
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('x'*1000); sys.stderr.write('y'*1000)",
            ),
            stdin=b"",
            env={},
            timeout_seconds=5,
            stdout_limit_bytes=32,
            stderr_limit_bytes=24,
        )
    )
    assert output_result.returncode == 0
    assert output_result.stdout == b"x" * 32
    assert output_result.stderr == b"y" * 24
    assert output_result.stdout_truncated is True
    assert output_result.stderr_truncated is True


def test_subprocess_adapter_kills_explicit_cancellation() -> None:
    adapter = SubprocessAdapter()
    cancel_event = threading.Event()
    timer = threading.Timer(0.05, cancel_event.set)
    timer.start()
    try:
        result = adapter.run(
            ProcessSpec(
                argv=(sys.executable, "-c", "import time; time.sleep(5)"),
                stdin=b"",
                env={},
                timeout_seconds=5,
                stdout_limit_bytes=16,
                stderr_limit_bytes=16,
                cancel_event=cancel_event,
            )
        )
    finally:
        timer.cancel()

    assert result.cancelled is True
    assert result.timed_out is False
    assert result.returncode != 0
