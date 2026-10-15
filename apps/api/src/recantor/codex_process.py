from __future__ import annotations

import ctypes
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol

_PROCESS_REAP_GRACE_SECONDS = 0.75
_PIPE_DRAIN_GRACE_SECONDS = 0.75
_MONITOR_POLL_SECONDS = 0.05


@dataclass(frozen=True)
class ProcessSpec:
    argv: tuple[str, ...]
    stdin: bytes
    env: Mapping[str, str]
    timeout_seconds: float
    stdout_limit_bytes: int
    stderr_limit_bytes: int
    cwd: str | None = None
    cancel_event: threading.Event | None = None
    # Called with the child pid after Popen and before containment resume/exec.
    # Return False to kill the child and treat the run as containment failure.
    pre_resume_hook: Callable[[int], bool] | None = None


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    cancelled: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    containment_failed: bool = False
    drain_incomplete: bool = False


class ProcessUnavailableError(RuntimeError):
    pass


class ProcessContainmentError(RuntimeError):
    pass


class ProcessAdapter(Protocol):
    def run(self, spec: ProcessSpec) -> ProcessResult: ...


class _BoundedCapture:
    def __init__(self, limit: int):
        self.limit = limit
        self.data = bytearray()
        self.truncated = False

    def consume(self, chunk: bytes) -> None:
        remaining = self.limit - len(self.data)
        if remaining > 0:
            self.data.extend(chunk[:remaining])
        if len(chunk) > max(remaining, 0):
            self.truncated = True


def _drain_pipe(pipe, capture: _BoundedCapture) -> None:
    try:
        while True:
            try:
                chunk = pipe.read(8192)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            capture.consume(chunk)
    finally:
        with suppress(OSError, ValueError):
            pipe.close()


class _ProcessTree(Protocol):
    def popen_kwargs(self) -> dict[str, object]: ...

    def attach_and_start(self, process: subprocess.Popen[bytes]) -> None: ...

    def terminate(self, process: subprocess.Popen[bytes]) -> None: ...

    def close(self) -> None: ...


class _PosixChild:
    """Child blocked in its own session until ``release_to_exec``.

    ``setsid`` runs before the barrier read, so user code and any fork cannot
    start until the parent has the pid and has admitted it.
    """

    def __init__(
        self,
        pid: int,
        stdout: object,
        stderr: object,
        barrier_w: int,
        argv: tuple[str, ...],
    ) -> None:
        self.pid = pid
        self.stdout = stdout
        self.stderr = stderr
        self.returncode: int | None = None
        self.argv = argv
        self._barrier_w = barrier_w

    def release_to_exec(self) -> None:
        fd = self._barrier_w
        if fd < 0:
            return
        self._barrier_w = -1
        try:
            os.write(fd, b"\0")
        finally:
            os.close(fd)

    def abort_blocked(self) -> None:
        """Close the exec barrier without releasing and reap the blocked child."""
        fd = self._barrier_w
        if fd >= 0:
            self._barrier_w = -1
            with suppress(OSError):
                os.close(fd)
        self.kill()
        with suppress(ChildProcessError, subprocess.TimeoutExpired):
            self.wait(timeout=_PROCESS_REAP_GRACE_SECONDS)

    def kill(self) -> None:
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except OSError as exc:
            raise ProcessContainmentError("failed to terminate POSIX process group") from exc

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        try:
            waited, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            self.returncode = -1
            return self.returncode
        if waited == 0:
            return None
        self.returncode = os.waitstatus_to_exitcode(status)
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.argv, 0 if timeout is None else timeout)
            time.sleep(0.01)
        assert self.returncode is not None
        return self.returncode


def _resolve_executable(argv0: str, env: Mapping[str, str]) -> str:
    if os.path.isabs(argv0) or os.sep in argv0:
        candidate = argv0
    else:
        found = shutil.which(argv0, path=env.get("PATH") or env.get("Path"))
        if found is None:
            raise ProcessUnavailableError("configured Codex executable is unavailable")
        candidate = found
    if not os.path.isfile(candidate) or not os.access(candidate, os.X_OK):
        raise ProcessUnavailableError("configured Codex executable is unavailable")
    return candidate


def _read_spawn_ready_byte(ready_r: int, spec: ProcessSpec) -> bytes:
    deadline = time.monotonic() + min(spec.timeout_seconds, 10.0)
    while True:
        if spec.cancel_event is not None and spec.cancel_event.is_set():
            return b""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return b""
        if os.name == "posix":
            import select

            readable, _, _ = select.select([ready_r], [], [], min(remaining, 0.2))
            if ready_r not in readable:
                continue
        return os.read(ready_r, 1)


def _cleanup_blocked_spawn(process: subprocess.Popen[bytes] | _PosixChild) -> None:
    abort = getattr(process, "abort_blocked", None)
    if callable(abort):
        abort()
        return
    with suppress(OSError, ProcessContainmentError):
        process.kill()
    _wait_bounded(process, _PROCESS_REAP_GRACE_SECONDS)


def _spawn_posix_blocked(spec: ProcessSpec, stdin_file: object) -> _PosixChild:
    """Fork a session leader that cannot exec until the parent releases it."""
    executable = _resolve_executable(spec.argv[0], spec.env)
    stdout_r, stdout_w = os.pipe()
    stderr_r, stderr_w = os.pipe()
    ready_r, ready_w = os.pipe()
    barrier_r, barrier_w = os.pipe()
    try:
        pid = os.fork()
    except OSError:
        for fd in (stdout_r, stdout_w, stderr_r, stderr_w, ready_r, ready_w, barrier_r, barrier_w):
            os.close(fd)
        raise
    if pid == 0:
        try:
            os.setsid()
            os.write(ready_w, b"\0")
            os.close(ready_w)
            if spec.cwd:
                os.chdir(spec.cwd)
            os.dup2(stdin_file.fileno(), 0)  # type: ignore[attr-defined]
            os.dup2(stdout_w, 1)
            os.dup2(stderr_w, 2)
            for fd in (stdout_r, stderr_r, stdout_w, stderr_w, ready_r, barrier_w):
                os.close(fd)
            while True:
                chunk = os.read(barrier_r, 1)
                if not chunk:
                    os._exit(0)
                if chunk == b"\0":
                    break
            os.close(barrier_r)
            os.closerange(3, 256)
            os.execvpe(executable, list(spec.argv), dict(spec.env))
        except OSError:
            os._exit(127)
        os._exit(127)
    os.close(stdout_w)
    os.close(stderr_w)
    os.close(ready_w)
    os.close(barrier_r)
    try:
        ready = _read_spawn_ready_byte(ready_r, spec)
    finally:
        os.close(ready_r)
    if not ready:
        with suppress(OSError):
            os.killpg(pid, signal.SIGKILL)
        with suppress(ChildProcessError):
            os.waitpid(pid, 0)
        for fd in (stdout_r, stderr_r, barrier_w):
            os.close(fd)
        raise ProcessContainmentError("POSIX child exited before its session was established")
    return _PosixChild(
        pid,
        os.fdopen(stdout_r, "rb"),
        os.fdopen(stderr_r, "rb"),
        barrier_w,
        spec.argv,
    )


class _PosixProcessTree:
    def popen_kwargs(self) -> dict[str, object]:
        return {"start_new_session": True}

    def attach_and_start(self, process: subprocess.Popen[bytes]) -> None:
        release = getattr(process, "release_to_exec", None)
        if release is not None:
            release()

    def terminate(self, process: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except OSError as exc:
            raise ProcessContainmentError("failed to terminate POSIX process group") from exc

    def close(self) -> None:
        return


class _WindowsJobProcessTree:
    """Contain one invocation in a kill-on-close Job Object before it can execute."""

    _CREATE_SUSPENDED = 0x00000004
    _CREATE_NEW_PROCESS_GROUP = 0x00000200
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _THREAD_SUSPEND_RESUME = 0x0002
    _TH32CS_SNAPTHREAD = 0x00000004

    def __init__(self) -> None:
        if os.name != "nt":
            raise ProcessContainmentError("Windows Job Objects are only available on Windows")

        from ctypes import wintypes

        self._wintypes = wintypes
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        class THREADENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD),
                ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", wintypes.LONG),
                ("tpDeltaPri", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
            ]

        self._thread_entry_type = THREADENTRY32
        self._invalid_handle = ctypes.c_void_p(-1).value

        self._kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self._kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self._kernel32.SetInformationJobObject.restype = wintypes.BOOL
        self._kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self._kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self._kernel32.TerminateJobObject.restype = wintypes.BOOL
        self._kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        self._kernel32.OpenProcess.restype = wintypes.HANDLE
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        self._kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        self._kernel32.Thread32First.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(THREADENTRY32),
        ]
        self._kernel32.Thread32First.restype = wintypes.BOOL
        self._kernel32.Thread32Next.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(THREADENTRY32),
        ]
        self._kernel32.Thread32Next.restype = wintypes.BOOL
        self._kernel32.OpenThread.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        self._kernel32.OpenThread.restype = wintypes.HANDLE
        self._kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
        self._kernel32.ResumeThread.restype = wintypes.DWORD

        job = self._kernel32.CreateJobObjectW(None, None)
        if not job:
            raise ProcessContainmentError("failed to create Windows Job Object")
        self._job = job

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = self._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._kernel32.SetInformationJobObject(
            self._job,
            self._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            self.close()
            raise ProcessContainmentError("failed to configure Windows Job Object")

    def popen_kwargs(self) -> dict[str, object]:
        return {
            "creationflags": self._CREATE_SUSPENDED | self._CREATE_NEW_PROCESS_GROUP,
        }

    def _resume_primary_process_threads(self, pid: int) -> None:
        snapshot = self._kernel32.CreateToolhelp32Snapshot(self._TH32CS_SNAPTHREAD, 0)
        if snapshot == self._invalid_handle:
            raise ProcessContainmentError("failed to enumerate suspended Windows process threads")
        resumed = 0
        try:
            entry = self._thread_entry_type()
            entry.dwSize = ctypes.sizeof(entry)
            has_entry = bool(self._kernel32.Thread32First(snapshot, ctypes.byref(entry)))
            while has_entry:
                if entry.th32OwnerProcessID == pid:
                    thread_handle = self._kernel32.OpenThread(
                        self._THREAD_SUSPEND_RESUME,
                        False,
                        entry.th32ThreadID,
                    )
                    if thread_handle:
                        try:
                            result = self._kernel32.ResumeThread(thread_handle)
                            if result != 0xFFFFFFFF:
                                resumed += 1
                        finally:
                            self._kernel32.CloseHandle(thread_handle)
                has_entry = bool(self._kernel32.Thread32Next(snapshot, ctypes.byref(entry)))
        finally:
            self._kernel32.CloseHandle(snapshot)

        if resumed == 0:
            raise ProcessContainmentError("failed to resume contained Windows process")

    def attach_and_start(self, process: subprocess.Popen[bytes]) -> None:
        access = (
            self._PROCESS_SET_QUOTA
            | self._PROCESS_TERMINATE
            | self._PROCESS_QUERY_LIMITED_INFORMATION
        )
        process_handle = self._kernel32.OpenProcess(access, False, process.pid)
        if not process_handle:
            raise ProcessContainmentError("failed to open suspended Windows process")
        try:
            if not self._kernel32.AssignProcessToJobObject(self._job, process_handle):
                raise ProcessContainmentError("failed to assign Windows process to Job Object")
        finally:
            self._kernel32.CloseHandle(process_handle)

        self._resume_primary_process_threads(process.pid)

    def terminate(self, process: subprocess.Popen[bytes]) -> None:
        del process
        if self._job and not self._kernel32.TerminateJobObject(self._job, 1):
            raise ProcessContainmentError("failed to terminate Windows Job Object")

    def close(self) -> None:
        job = getattr(self, "_job", None)
        if job:
            # KILL_ON_JOB_CLOSE is defense-in-depth for any descendant still attached.
            self._kernel32.CloseHandle(job)
            self._job = None


def _new_process_tree() -> _ProcessTree:
    if os.name == "posix":
        return _PosixProcessTree()
    if os.name == "nt":
        return _WindowsJobProcessTree()
    raise ProcessContainmentError(f"unsupported process-containment platform: {os.name}")


def _wait_bounded(process: subprocess.Popen[bytes], timeout: float) -> bool:
    if process.poll() is not None:
        return True
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return True


class SubprocessAdapter:
    """Run a bounded child tree with no shell and bounded output capture."""

    def run(self, spec: ProcessSpec) -> ProcessResult:
        stdout_capture = _BoundedCapture(spec.stdout_limit_bytes)
        stderr_capture = _BoundedCapture(spec.stderr_limit_bytes)
        try:
            tree = _new_process_tree()
        except ProcessContainmentError:
            raise

        process: subprocess.Popen[bytes] | None = None
        try:
            with tempfile.TemporaryFile() as stdin_file:
                stdin_file.write(spec.stdin)
                stdin_file.seek(0)
                try:
                    if os.name == "posix":
                        process = _spawn_posix_blocked(spec, stdin_file)
                    else:
                        process = subprocess.Popen(
                            list(spec.argv),
                            stdin=stdin_file,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            cwd=spec.cwd,
                            env=dict(spec.env),
                            shell=False,
                            **tree.popen_kwargs(),
                        )
                except ProcessUnavailableError:
                    raise
                except (FileNotFoundError, OSError) as exc:
                    raise ProcessUnavailableError(
                        "configured Codex executable is unavailable"
                    ) from exc

                try:
                    hook_ok = True
                    if spec.pre_resume_hook is not None:
                        hook_ok = spec.pre_resume_hook(process.pid)
                except Exception:
                    _cleanup_blocked_spawn(process)
                    raise
                if not hook_ok:
                    _cleanup_blocked_spawn(process)
                    return ProcessResult(
                        returncode=-1,
                        stdout=b"",
                        stderr=b"",
                        containment_failed=True,
                    )

                try:
                    tree.attach_and_start(process)
                except ProcessContainmentError:
                    _cleanup_blocked_spawn(process)
                    raise

            assert process.stdout is not None
            assert process.stderr is not None
            stdout_thread = threading.Thread(
                target=_drain_pipe,
                args=(process.stdout, stdout_capture),
                daemon=True,
            )
            stderr_thread = threading.Thread(
                target=_drain_pipe,
                args=(process.stderr, stderr_capture),
                daemon=True,
            )
            stdout_thread.start()
            stderr_thread.start()

            timed_out = False
            cancelled = False
            containment_failed = False
            deadline = time.monotonic() + spec.timeout_seconds

            while process.poll() is None:
                if spec.cancel_event is not None and spec.cancel_event.is_set():
                    cancelled = True
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                with suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=min(_MONITOR_POLL_SECONDS, remaining))

            # Always close the invocation tree, even after a successful direct-process exit.
            # This prevents a background descendant from outliving Codex and holding pipes open.
            try:
                tree.terminate(process)
            except ProcessContainmentError:
                containment_failed = True
                with suppress(OSError):
                    process.kill()

            if not _wait_bounded(process, _PROCESS_REAP_GRACE_SECONDS):
                containment_failed = True
                with suppress(OSError):
                    process.kill()
                _wait_bounded(process, _PROCESS_REAP_GRACE_SECONDS)

            drain_deadline = time.monotonic() + _PIPE_DRAIN_GRACE_SECONDS
            for thread in (stdout_thread, stderr_thread):
                remaining = drain_deadline - time.monotonic()
                if remaining > 0:
                    thread.join(timeout=remaining)
            drain_incomplete = stdout_thread.is_alive() or stderr_thread.is_alive()
            if drain_incomplete:
                containment_failed = True

            return ProcessResult(
                returncode=process.returncode if process.returncode is not None else -1,
                stdout=bytes(stdout_capture.data),
                stderr=bytes(stderr_capture.data),
                timed_out=timed_out,
                cancelled=cancelled,
                stdout_truncated=stdout_capture.truncated,
                stderr_truncated=stderr_capture.truncated,
                containment_failed=containment_failed,
                drain_incomplete=drain_incomplete,
            )
        finally:
            tree.close()
