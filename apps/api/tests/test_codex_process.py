import sys
import threading
import time
from pathlib import Path

import pytest

from recantor.codex_process import ProcessSpec, SubprocessAdapter


def test_subprocess_adapter_bounds_output_capture() -> None:
    adapter = SubprocessAdapter()
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
    assert output_result.containment_failed is False
    assert output_result.drain_incomplete is False


_GRANDCHILD_SCRIPT = (
    "import subprocess,sys,time;"
    "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],"
    "stdout=sys.stdout,stderr=sys.stderr);"
    "print(p.pid,flush=True);"
    "time.sleep(30)"
)


def _linux_descendant_stopped(pid: int) -> bool:
    stat_path = Path(f"/proc/{pid}/stat")
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline:
        try:
            parts = stat_path.read_text(encoding="utf-8").split()
        except FileNotFoundError:
            return True
        if len(parts) >= 3 and parts[2] in {"Z", "X"}:
            return True
        time.sleep(0.02)
    return False


@pytest.mark.skipif(sys.platform != "linux", reason="Linux exact descendant liveness witness")
def test_timeout_kills_grandchild_tree_and_returns_within_hard_bound() -> None:
    adapter = SubprocessAdapter()
    started = time.monotonic()
    result = adapter.run(
        ProcessSpec(
            argv=(sys.executable, "-c", _GRANDCHILD_SCRIPT),
            stdin=b"",
            env={},
            timeout_seconds=0.5,
            stdout_limit_bytes=1024,
            stderr_limit_bytes=1024,
        )
    )
    elapsed = time.monotonic() - started

    assert result.timed_out is True
    assert result.cancelled is False
    assert result.containment_failed is False
    assert result.drain_incomplete is False
    assert elapsed < 3.0
    grandchild_pid = int(result.stdout.splitlines()[0])
    assert _linux_descendant_stopped(grandchild_pid)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux exact descendant liveness witness")
def test_cancellation_kills_grandchild_tree_and_returns_within_hard_bound() -> None:
    adapter = SubprocessAdapter()
    cancel_event = threading.Event()
    timer = threading.Timer(0.8, cancel_event.set)
    timer.start()
    started = time.monotonic()
    try:
        result = adapter.run(
            ProcessSpec(
                argv=(sys.executable, "-c", _GRANDCHILD_SCRIPT),
                stdin=b"",
                env={},
                timeout_seconds=5,
                stdout_limit_bytes=1024,
                stderr_limit_bytes=1024,
                cancel_event=cancel_event,
            )
        )
    finally:
        timer.cancel()
    elapsed = time.monotonic() - started

    assert result.cancelled is True
    assert result.timed_out is False
    assert result.containment_failed is False
    assert result.drain_incomplete is False
    assert elapsed < 3.0
    grandchild_pid = int(result.stdout.splitlines()[0])
    assert _linux_descendant_stopped(grandchild_pid)
