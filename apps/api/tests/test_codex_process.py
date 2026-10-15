import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from recantor.codex_process import (
    ProcessResult,
    ProcessSpec,
    SubprocessAdapter,
    _WindowsJobProcessTree,
)


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


_GRANDCHILD_CMD_LITERAL = (
    "['ping','-n','30','127.0.0.1']"
    if os.name == "nt"
    else "[sys.executable,'-c','import time; time.sleep(30)']"
)

# Linux: grandchild inherits stdout/stderr so timeout/cancel must drain bounded pipes.
# Windows: DEVNULL avoids inheriting adapter PIPE handles into ping; liveness is proven
# via OpenProcess/WaitForSingleObject on the descendant PID (see _windows_run_with_alive_witness).
_GRANDCHILD_SCRIPT_LINUX = (
    "import subprocess,sys,time;"
    f"p=subprocess.Popen({_GRANDCHILD_CMD_LITERAL});"
    "print(p.pid,flush=True);"
    "time.sleep(30)"
)

_GRANDCHILD_FAST_PARENT_EXIT_SCRIPT_LINUX = (
    f"import subprocess,sys;p=subprocess.Popen({_GRANDCHILD_CMD_LITERAL});print(p.pid,flush=True)"
)


def _windows_grandchild_script(*, pid_file: Path, fast_parent_exit: bool = False) -> str:
    pid_path = pid_file.resolve().as_posix()
    tail = (
        "print(p.pid,flush=True)" if fast_parent_exit else "print(p.pid,flush=True);time.sleep(30)"
    )
    return (
        "import subprocess,time;"
        f"p=subprocess.Popen({_GRANDCHILD_CMD_LITERAL},"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
        f"open({pid_path!r}, 'w', encoding='utf-8').write(str(p.pid));"
        f"{tail}"
    )


def _read_descendant_pid_file(pid_file: Path, *, timeout_seconds: float = 2.0) -> int:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if pid_file.is_file():
            text = pid_file.read_text(encoding="utf-8").strip()
            if text.isdigit():
                return int(text)
        time.sleep(0.01)
    pytest.fail(f"descendant pid file not written: {pid_file}")


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


class _WindowsProcessWitness:
    SYNCHRONIZE = 0x00100000
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    WAIT_OBJECT_0 = 0x00000000
    WAIT_TIMEOUT = 0x00000102
    ERROR_ACCESS_DENIED = 5
    ERROR_INVALID_PARAMETER = 87

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self._kernel32.OpenProcess.restype = wintypes.HANDLE
        self._kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self._kernel32.WaitForSingleObject.restype = wintypes.DWORD
        self._kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL

    def _open_process(self, pid: int):
        handle = self._kernel32.OpenProcess(
            self.SYNCHRONIZE | self.PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            int(pid),
        )
        if handle:
            return handle, None
        return None, self._ctypes.get_last_error()

    def open_running_handle(self, pid: int, *, retry_seconds: float = 0.5):
        deadline = time.monotonic() + retry_seconds
        while True:
            handle, error = self._open_process(pid)
            if handle is not None:
                break
            if error == self.ERROR_INVALID_PARAMETER and time.monotonic() < deadline:
                time.sleep(0.01)
                continue
            if error == self.ERROR_INVALID_PARAMETER:
                pytest.fail(f"pid {pid} is not a live process (winerror {error})")
            if error == self.ERROR_ACCESS_DENIED:
                pytest.fail(
                    f"OpenProcess access denied for pid {pid}; descendant witness inconclusive"
                )
            pytest.fail(f"OpenProcess failed for pid {pid} (winerror {error})")
        wait_result = self._kernel32.WaitForSingleObject(handle, 0)
        if wait_result == self.WAIT_TIMEOUT:
            return handle
        if wait_result == self.WAIT_OBJECT_0:
            self._kernel32.CloseHandle(handle)
            pytest.fail(f"expected pid {pid} to be running before containment fired")
        self._kernel32.CloseHandle(handle)
        pytest.fail(f"WaitForSingleObject returned unexpected code {wait_result}")

    def assert_handle_terminated(self, handle, *, timeout_seconds: float = 1.5) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            wait_result = self._kernel32.WaitForSingleObject(handle, 0)
            if wait_result == self.WAIT_OBJECT_0:
                return
            if wait_result == self.WAIT_TIMEOUT:
                time.sleep(0.02)
                continue
            pytest.fail(f"WaitForSingleObject returned unexpected code {wait_result}")
        pytest.fail(f"descendant handle still running after {timeout_seconds}s")

    def close_handle(self, handle) -> None:
        self._kernel32.CloseHandle(handle)

    def assert_terminated(self, pid: int, *, timeout_seconds: float = 1.5) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            handle, error = self._open_process(pid)
            if handle is None:
                if error == self.ERROR_INVALID_PARAMETER:
                    return
                if error == self.ERROR_ACCESS_DENIED:
                    pytest.fail(
                        f"OpenProcess access denied for pid {pid}; descendant witness inconclusive"
                    )
                pytest.fail(f"OpenProcess failed for pid {pid} (winerror {error})")
            try:
                wait_result = self._kernel32.WaitForSingleObject(handle, 0)
                if wait_result == self.WAIT_OBJECT_0:
                    exit_code = self._wintypes.DWORD()
                    if not self._kernel32.GetExitCodeProcess(handle, self._ctypes.byref(exit_code)):
                        open_error = self._ctypes.get_last_error()
                        pytest.fail(
                            f"GetExitCodeProcess failed for pid {pid} (winerror {open_error})"
                        )
                    return
                if wait_result == self.WAIT_TIMEOUT:
                    time.sleep(0.02)
                    continue
                pytest.fail(f"WaitForSingleObject returned unexpected code {wait_result}")
            finally:
                self._kernel32.CloseHandle(handle)
        pytest.fail(f"pid {pid} still running after {timeout_seconds}s")


def _windows_assert_descendant_terminated(pid: int) -> None:
    _WindowsProcessWitness().assert_terminated(pid)


def _windows_run_with_alive_descendant_witness(
    adapter: SubprocessAdapter,
    spec: ProcessSpec,
    pid_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> ProcessResult:
    witness = _WindowsProcessWitness()
    live_handles: list = []
    original_terminate = _WindowsJobProcessTree.terminate

    def terminate_wrapper(self, process: subprocess.Popen[bytes]) -> None:
        descendant_pid = _read_descendant_pid_file(pid_file)
        handle = witness.open_running_handle(descendant_pid)
        live_handles.append(handle)
        original_terminate(self, process)

    monkeypatch.setattr(_WindowsJobProcessTree, "terminate", terminate_wrapper)
    result = adapter.run(spec)
    assert len(live_handles) == 1
    try:
        witness.assert_handle_terminated(live_handles[0])
    finally:
        witness.close_handle(live_handles[0])
    return result


@pytest.mark.skipif(sys.platform != "linux", reason="Linux exact descendant liveness witness")
def test_timeout_kills_grandchild_tree_and_returns_within_hard_bound() -> None:
    adapter = SubprocessAdapter()
    started = time.monotonic()
    result = adapter.run(
        ProcessSpec(
            argv=(sys.executable, "-c", _GRANDCHILD_SCRIPT_LINUX),
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


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object descendant witness")
def test_timeout_kills_grandchild_tree_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = SubprocessAdapter()
    pid_file = tmp_path / "descendant.pid"
    started = time.monotonic()
    result = _windows_run_with_alive_descendant_witness(
        adapter,
        ProcessSpec(
            argv=(sys.executable, "-c", _windows_grandchild_script(pid_file=pid_file)),
            stdin=b"",
            env={},
            timeout_seconds=0.5,
            stdout_limit_bytes=1024,
            stderr_limit_bytes=1024,
        ),
        pid_file,
        monkeypatch,
    )
    elapsed = time.monotonic() - started

    assert result.timed_out is True
    assert result.containment_failed is False
    assert elapsed < 3.0
    grandchild_pid = int(result.stdout.splitlines()[0])
    _windows_assert_descendant_terminated(grandchild_pid)


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
                argv=(sys.executable, "-c", _GRANDCHILD_SCRIPT_LINUX),
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


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object descendant witness")
def test_cancellation_kills_grandchild_tree_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = SubprocessAdapter()
    pid_file = tmp_path / "descendant.pid"
    cancel_event = threading.Event()
    timer = threading.Timer(0.8, cancel_event.set)
    timer.start()
    started = time.monotonic()
    try:
        result = _windows_run_with_alive_descendant_witness(
            adapter,
            ProcessSpec(
                argv=(sys.executable, "-c", _windows_grandchild_script(pid_file=pid_file)),
                stdin=b"",
                env={},
                timeout_seconds=5,
                stdout_limit_bytes=1024,
                stderr_limit_bytes=1024,
                cancel_event=cancel_event,
            ),
            pid_file,
            monkeypatch,
        )
    finally:
        timer.cancel()
    elapsed = time.monotonic() - started

    assert result.cancelled is True
    assert result.containment_failed is False
    assert elapsed < 3.0
    grandchild_pid = int(result.stdout.splitlines()[0])
    _windows_assert_descendant_terminated(grandchild_pid)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object descendant witness")
def test_successful_parent_exit_terminates_grandchild_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = SubprocessAdapter()
    pid_file = tmp_path / "descendant.pid"
    started = time.monotonic()
    result = _windows_run_with_alive_descendant_witness(
        adapter,
        ProcessSpec(
            argv=(
                sys.executable,
                "-c",
                _windows_grandchild_script(pid_file=pid_file, fast_parent_exit=True),
            ),
            stdin=b"",
            env={},
            timeout_seconds=5,
            stdout_limit_bytes=1024,
            stderr_limit_bytes=1024,
        ),
        pid_file,
        monkeypatch,
    )
    elapsed = time.monotonic() - started

    assert result.timed_out is False
    assert result.cancelled is False
    assert result.containment_failed is False
    assert result.returncode == 0
    assert elapsed < 3.0
    grandchild_pid = int(result.stdout.splitlines()[0])
    _windows_assert_descendant_terminated(grandchild_pid)


def test_pre_resume_hook_runs_before_user_code(tmp_path: Path) -> None:
    marker = tmp_path / "user-code-ran"
    script = (
        "from pathlib import Path; import sys; "
        f"Path({str(marker)!r}).write_text('ran', encoding='utf-8'); "
        "sys.stdout.write('ok')"
    )
    seen: list[int] = []

    def _allow(pid: int) -> bool:
        seen.append(pid)
        assert not marker.exists()
        return True

    adapter = SubprocessAdapter()
    result = adapter.run(
        ProcessSpec(
            argv=(sys.executable, "-c", script),
            stdin=b"",
            env={},
            timeout_seconds=5,
            stdout_limit_bytes=1024,
            stderr_limit_bytes=1024,
            pre_resume_hook=_allow,
        )
    )
    assert result.returncode == 0
    assert result.containment_failed is False
    assert seen
    assert marker.read_text(encoding="utf-8") == "ran"


@pytest.mark.skipif(os.name != "posix", reason="POSIX blocked-spawn cleanup")
def test_pre_resume_hook_error_aborts_blocked_child(tmp_path: Path) -> None:
    marker = tmp_path / "user-code-ran"
    script = f"from pathlib import Path; Path({str(marker)!r}).write_text('ran', encoding='utf-8')"

    def _boom(pid: int) -> bool:
        del pid
        raise RuntimeError("authority failure")

    adapter = SubprocessAdapter()
    with pytest.raises(RuntimeError, match="authority"):
        adapter.run(
            ProcessSpec(
                argv=(sys.executable, "-c", script),
                stdin=b"",
                env={},
                timeout_seconds=5,
                stdout_limit_bytes=1024,
                stderr_limit_bytes=1024,
                pre_resume_hook=_boom,
            )
        )
    assert not marker.exists()


def test_pre_resume_denial_never_runs_user_code(tmp_path: Path) -> None:
    marker = tmp_path / "user-code-ran"
    script = f"from pathlib import Path; Path({str(marker)!r}).write_text('ran', encoding='utf-8')"

    def _deny(pid: int) -> bool:
        del pid
        return False

    adapter = SubprocessAdapter()
    result = adapter.run(
        ProcessSpec(
            argv=(sys.executable, "-c", script),
            stdin=b"",
            env={},
            timeout_seconds=5,
            stdout_limit_bytes=1024,
            stderr_limit_bytes=1024,
            pre_resume_hook=_deny,
        )
    )
    assert result.containment_failed is True
    assert not marker.exists()
