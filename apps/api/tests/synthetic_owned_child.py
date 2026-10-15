"""Controlled subprocess adapter that registers a real owned child via pre_resume_hook."""

from __future__ import annotations

import subprocess
import sys
import threading
import time

from recantor.codex_process import ProcessResult, ProcessSpec
from recantor.meeting_essence.provider_adapters.spawn_gate import current_pre_resume_gate
from recantor.meeting_essence.scheduling.process_liveness import (
    Liveness,
    identity_for_pid,
    process_liveness,
    terminate_process_tree,
)

_EXEC_STDOUT = b"{}"
_AUTH_STDOUT = b"Logged in using ChatGPT\n"


def _sleeping_child(*, seconds: float) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({seconds})"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class SyntheticOwnedChildProcessAdapter:
    """Spawn a bounded sleeper child; admit its pid before any model-shaped work."""

    def __init__(
        self,
        *,
        child_sleep_seconds: float = 0.35,
        exec_stdout: bytes = _EXEC_STDOUT,
    ) -> None:
        self.child_sleep_seconds = child_sleep_seconds
        self.exec_stdout = exec_stdout
        self.hook_calls: list[bool] = []
        self.gate_inside_worker: list[bool | None] = []
        self.exec_started = threading.Event()
        self._active_child: subprocess.Popen[bytes] | None = None

    def _reap_child(self, child: subprocess.Popen[bytes]) -> None:
        if child.poll() is None:
            child.kill()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            terminate_process_tree(identity_for_pid(child.pid))
            child.wait(timeout=5)

    def _run_with_child(
        self,
        spec: ProcessSpec,
        *,
        stdout: bytes,
        mark_exec: bool,
    ) -> ProcessResult:
        self.gate_inside_worker.append(current_pre_resume_gate() is not None)
        child = _sleeping_child(seconds=self.child_sleep_seconds)
        self._active_child = child
        try:
            if spec.pre_resume_hook is not None:
                admitted = spec.pre_resume_hook(child.pid)
                self.hook_calls.append(admitted)
                if not admitted:
                    return ProcessResult(
                        returncode=1,
                        stdout=b"",
                        stderr=b"admission denied before work",
                    )
            if mark_exec:
                self.exec_started.set()
            cancel = spec.cancel_event
            deadline = time.monotonic() + max(self.child_sleep_seconds, 0.05) + 2.0
            while child.poll() is None and time.monotonic() < deadline:
                if cancel is not None and cancel.is_set():
                    break
                time.sleep(0.01)
            return ProcessResult(returncode=0, stdout=stdout, stderr=b"")
        finally:
            self._reap_child(child)
            self._active_child = None

    def run(self, spec: ProcessSpec) -> ProcessResult:
        if len(spec.argv) >= 2 and spec.argv[1] == "login":
            return self._run_with_child(spec, stdout=_AUTH_STDOUT, mark_exec=False)
        return self._run_with_child(spec, stdout=self.exec_stdout, mark_exec=True)


def assert_identity_dead(identity_pid: int) -> None:
    identity = identity_for_pid(identity_pid)
    assert process_liveness(identity) == Liveness.DEAD
