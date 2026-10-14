"""Process identity for the shared host permit.

Unknown liveness is not death. Access denied, a missing creation time, and a
Windows handle that still opens after exit are not proof that an invocation ended.
"""

from __future__ import annotations

import errno
import os
import sys
from dataclasses import dataclass
from enum import StrEnum


class Liveness(StrEnum):
    ALIVE = "alive"
    DEAD = "dead"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    created_ms: int | None = None
    start_ticks: int | None = None
    namespace: str | None = None
    # Set only by ``bind_owned_session`` after this process created the session.
    # A pgid read from an inherited group is not ownership.
    owned_pgid: int | None = None
    owned_sid: int | None = None


@dataclass(frozen=True)
class ContainmentResult:
    """Owned-tree stop. ``ancestry_lost`` stays busy after the root is gone."""

    proven: bool
    identities: tuple[ProcessIdentity, ...]
    ancestry_lost: bool


def identity_for_pid(pid: int) -> ProcessIdentity:
    if sys.platform == "win32":
        return ProcessIdentity(pid=pid, created_ms=_windows_created_ms(pid))
    return ProcessIdentity(pid=pid, start_ticks=_linux_start_ticks(pid))


def current_process_identity() -> ProcessIdentity:
    pid = os.getpid()
    created_ms = _windows_created_ms(pid) if sys.platform == "win32" else None
    start_ticks = _linux_start_ticks(pid) if sys.platform != "win32" else None
    return ProcessIdentity(pid=pid, created_ms=created_ms, start_ticks=start_ticks)


def process_liveness(identity: ProcessIdentity) -> Liveness:
    if identity.pid <= 0:
        return Liveness.DEAD
    if sys.platform == "win32":
        return _windows_liveness(identity)
    return _posix_liveness(identity)


def process_is_alive(identity: ProcessIdentity) -> bool:
    """True only for proven life. Unknown and dead are both false."""
    return process_liveness(identity) == Liveness.ALIVE


def _posix_liveness(identity: ProcessIdentity) -> Liveness:
    try:
        os.kill(identity.pid, 0)
    except OSError as exc:
        if exc.errno == errno.EPERM:
            return Liveness.UNKNOWN
        if exc.errno == errno.ESRCH:
            return Liveness.DEAD
        return Liveness.UNKNOWN
    except SystemError:
        return Liveness.UNKNOWN
    state, start_ticks = _posix_proc_stat(identity.pid)
    if identity.start_ticks is None or start_ticks is None:
        return Liveness.UNKNOWN
    if start_ticks != identity.start_ticks:
        return Liveness.DEAD
    # A zombie is not executing. PID reuse is still fenced by start ticks above.
    if state == "Z":
        return Liveness.DEAD
    return Liveness.ALIVE


def _posix_proc_stat(pid: int) -> tuple[str | None, int | None]:
    """Return ``(state, start_ticks)``. Missing or unreadable proc is ``(None, None)``."""
    if sys.platform == "win32":
        return None, None
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            raw = handle.read()
    except OSError:
        return None, None
    marker = raw.rfind(b")")
    if marker < 0:
        return None, None
    fields = raw[marker + 2 :].split()
    if not fields:
        return None, None
    state = fields[0].decode("ascii", errors="replace")
    if len(fields) <= 19:
        return state, None
    try:
        return state, int(fields[19])
    except ValueError:
        return state, None


def _linux_start_ticks(pid: int) -> int | None:
    _state, ticks = _posix_proc_stat(pid)
    return ticks


def _windows_liveness(identity: ProcessIdentity) -> Liveness:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _configure_kernel32(kernel32)
    access = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION
    handle = kernel32.OpenProcess(access, False, wintypes.DWORD(identity.pid))
    if not handle:
        error = ctypes.get_last_error()
        if error == 5:  # ERROR_ACCESS_DENIED
            return Liveness.UNKNOWN
        return Liveness.DEAD
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return Liveness.UNKNOWN
        still_active = 259
        if int(exit_code.value) != still_active:
            return Liveness.DEAD
        if identity.created_ms is None:
            return Liveness.UNKNOWN
        observed = _created_ms_from_handle(kernel32, handle)
        if observed is None:
            return Liveness.UNKNOWN
        if observed != identity.created_ms:
            return Liveness.DEAD
        return Liveness.ALIVE
    finally:
        kernel32.CloseHandle(handle)


def _windows_created_ms(pid: int) -> int | None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _configure_kernel32(kernel32)
    handle = kernel32.OpenProcess(0x1000, False, wintypes.DWORD(pid))
    if not handle:
        return None
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return None
        if int(exit_code.value) != 259:
            return None
        return _created_ms_from_handle(kernel32, handle)
    finally:
        kernel32.CloseHandle(handle)


def _windows_pid_still_active(pid: int) -> bool:
    """True when this pid is running, whoever the process is."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _configure_kernel32(kernel32)
    handle = kernel32.OpenProcess(0x1000, False, wintypes.DWORD(pid))
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return int(exit_code.value) == 259
    finally:
        kernel32.CloseHandle(handle)


def _created_ms_from_handle(kernel32: object, handle: object) -> int | None:
    import ctypes
    from ctypes import wintypes

    created = wintypes.FILETIME()
    exited = wintypes.FILETIME()
    kernel = wintypes.FILETIME()
    user = wintypes.FILETIME()
    ok = kernel32.GetProcessTimes(  # type: ignore[attr-defined]
        handle,
        ctypes.byref(created),
        ctypes.byref(exited),
        ctypes.byref(kernel),
        ctypes.byref(user),
    )
    if not ok:
        return None
    combined = (int(created.dwHighDateTime) << 32) + int(created.dwLowDateTime)
    return combined // 10_000


def _configure_kernel32(kernel32: object) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]  # type: ignore[attr-defined]
    kernel32.OpenProcess.restype = wintypes.HANDLE  # type: ignore[attr-defined]
    kernel32.GetExitCodeProcess.argtypes = [  # type: ignore[attr-defined]
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL  # type: ignore[attr-defined]
    kernel32.GetProcessTimes.argtypes = [  # type: ignore[attr-defined]
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL  # type: ignore[attr-defined]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]  # type: ignore[attr-defined]
    kernel32.CloseHandle.restype = wintypes.BOOL  # type: ignore[attr-defined]


_TREE_STOP_TIMEOUT_SECONDS = 3.0


def terminate_process_tree(identity: ProcessIdentity) -> bool:
    """Stop an owned process and its descendants. See ``contain_owned_tree``."""
    return contain_owned_tree(identity).proven


def contain_owned_tree(identity: ProcessIdentity) -> ContainmentResult:
    """Stop one owned tree, or report that ownership is still incomplete.

    A shared process group is not ownership. ``kill`` of a process group is used
    only for a session this caller created and stored on the identity, and never
    for the caller's own group. Otherwise POSIX freezes the parent chain and
    signals the root only after that freeze is stable. A root that is already
    dead, or that dies before the freeze, is ancestry loss: reparented children
    are not an empty tree. PID reuse proves that root ended and is not signalled;
    it is not proof that the old descendants ended. Unknown liveness is not
    signalled. This process and its parent are not signalled.
    """
    if identity.pid <= 0 or _protected_pid(identity.pid):
        return ContainmentResult(False, (identity,), True)
    if identity.namespace and identity.namespace != local_process_namespace():
        return ContainmentResult(False, (identity,), True)
    if _is_owned_session_leader(identity):
        if not _owned_session_is_safe(identity):
            return ContainmentResult(False, (identity,), True)
        return _posix_contain_owned_group(identity)
    liveness = process_liveness(identity)
    if liveness == Liveness.UNKNOWN:
        return ContainmentResult(False, (identity,), True)
    if liveness == Liveness.DEAD:
        # The root has ended. A reused pid is someone else's process: do not signal
        # it, and do not treat that replacement as proof the old descendants ended.
        # Closure of a dead root requires an owned-session reap or a freeze that
        # actually ran against this tree.
        return ContainmentResult(False, (identity,), True)
    if sys.platform == "win32":
        proven = _windows_kill_tree(identity)
        return ContainmentResult(proven, (identity,), not proven)
    return _posix_contain_tree(identity)


def capture_owned_descendants(root: ProcessIdentity) -> list[ProcessIdentity]:
    """Descendants visible now. An already-dead root yields nothing.

    Callers store this list before the root can exit. Windows tree stop uses
    ``taskkill /T`` and does not have a ``/proc`` parent walk.
    """
    if sys.platform == "win32" or process_liveness(root) != Liveness.ALIVE:
        return []
    captured: list[ProcessIdentity] = []
    for pid in _posix_visible_descendants(root.pid):
        ident = ProcessIdentity(
            pid=pid,
            start_ticks=_linux_start_ticks(pid),
            namespace=root.namespace,
        )
        if process_liveness(ident) == Liveness.ALIVE:
            captured.append(ident)
    return captured


def _windows_kill_tree(identity: ProcessIdentity) -> bool:
    import subprocess

    try:
        completed = subprocess.run(
            ["taskkill", "/PID", str(identity.pid), "/T", "/F"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_TREE_STOP_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return process_liveness(identity) == Liveness.DEAD
    if completed.returncode not in {0, 128, 255}:
        return process_liveness(identity) == Liveness.DEAD
    return process_liveness(identity) == Liveness.DEAD


def posix_signal_order(root: int, descendants: list[int]) -> list[int]:
    """Deepest captured descendants first, then the root. The root is never first."""
    return [*reversed(descendants), root]


def _posix_kill_tree(identity: ProcessIdentity) -> bool:
    """Parent-chain freeze. True only when that freeze closed before the kill."""
    if process_liveness(identity) == Liveness.UNKNOWN:
        return False
    return _posix_contain_tree(identity).proven


def _posix_contain_tree(identity: ProcessIdentity) -> ContainmentResult:
    """Freeze the parent chain, then kill it. A shared group is not consulted.

    Descendants are stopped before the root so a child forked by an earlier
    signal is still visible. The root is signalled only after a scan under that
    stop shows nothing new, and it is killed only while the stop still holds.
    If the root dies before that, or this OS cannot stop a process, the result
    is ancestry loss rather than success.
    """
    import signal
    import time

    stop_sig = getattr(signal, "SIGSTOP", None)
    kill_sig = getattr(signal, "SIGKILL", None)
    if stop_sig is None or kill_sig is None:
        return ContainmentResult(False, (identity,), True)
    if process_liveness(identity) != Liveness.ALIVE or not _posix_still_same(identity):
        return ContainmentResult(False, (identity,), True)
    tracked: list[ProcessIdentity] = []
    known: set[int] = set()
    deadline = time.monotonic() + _TREE_STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process_liveness(identity) != Liveness.ALIVE or not _posix_still_same(identity):
            return ContainmentResult(False, _containment_ids(tracked, identity), True)
        if not _record_descendants(identity, tracked, known):
            return ContainmentResult(False, _containment_ids(tracked, identity), True)
        if not _freeze_tracked(tracked, stop_sig):
            return ContainmentResult(False, _containment_ids(tracked, identity), True)
        if _any_running(tracked) or _untracked_descendants(identity, known):
            time.sleep(0.02)
            continue
        state, _ticks = _posix_proc_stat(identity.pid)
        if state not in {"T", "t"}:
            if not _posix_signal(identity.pid, stop_sig):
                return ContainmentResult(False, _containment_ids(tracked, identity), True)
            time.sleep(0.02)
            continue
        if not _record_descendants(identity, tracked, known):
            return ContainmentResult(False, _containment_ids(tracked, identity), True)
        if _untracked_descendants(identity, known) or _any_running(tracked):
            continue
        if not _tree_is_frozen(tracked, identity):
            continue
        if not _kill_frozen_tree(tracked, identity, kill_sig):
            return ContainmentResult(False, _containment_ids(tracked, identity), True)
        if all(process_liveness(item) == Liveness.DEAD for item in (*tracked, identity)):
            return ContainmentResult(True, _containment_ids(tracked, identity), False)
        return ContainmentResult(False, _containment_ids(tracked, identity), True)
    return ContainmentResult(False, _containment_ids(tracked, identity), True)


def _containment_ids(
    tracked: list[ProcessIdentity], root: ProcessIdentity
) -> tuple[ProcessIdentity, ...]:
    return tuple([*tracked, root])


def _posix_signal(pid: int, sig: int) -> bool:
    """False on permission errors. Other misses are rechecked through liveness."""
    try:
        os.kill(pid, sig)
    except OSError as exc:
        if exc.errno == errno.EPERM:
            return False
    _reap_pid(pid)
    return True


def _posix_child_identity(pid: int) -> ProcessIdentity:
    _state, ticks = _posix_proc_stat(pid)
    return ProcessIdentity(pid=pid, start_ticks=ticks)


def _posix_still_same(identity: ProcessIdentity) -> bool:
    """True when this pid is still the process we snapshotted. A reused pid is not."""
    if identity.start_ticks is None:
        return process_liveness(identity) == Liveness.ALIVE
    _state, ticks = _posix_proc_stat(identity.pid)
    if ticks is None:
        return process_liveness(identity) == Liveness.ALIVE
    return ticks == identity.start_ticks


def _posix_visible_descendants(root: int) -> list[int]:
    """Parent-chain descendants. A shared process group is not a descendant list."""
    if _protected_pid(root):
        return []
    return [pid for pid in _posix_descendants(root) if not _protected_pid(pid)]


def owned_subprocess_kwargs() -> dict[str, bool]:
    """Put a POSIX child in a new session. Windows tree stop does not need one."""
    if sys.platform == "win32":
        return {}
    return {"start_new_session": True}


def bind_owned_session(identity: ProcessIdentity) -> ProcessIdentity | None:
    """Attach a process group only when this pid is the session leader we created.

    The caller's process group is refused. Windows returns the identity unchanged
    because tree stop there is ``taskkill /T``, not a POSIX group.
    """
    if sys.platform == "win32":
        return identity
    if not hasattr(os, "getpgid") or not hasattr(os, "getsid") or not hasattr(os, "getpgrp"):
        return None
    if identity.start_ticks is None or process_liveness(identity) != Liveness.ALIVE:
        return None
    try:
        pgid = int(os.getpgid(identity.pid))
        sid = int(os.getsid(identity.pid))
        caller_group = int(os.getpgrp())
    except OSError:
        return None
    if pgid != identity.pid or sid != identity.pid or pgid == caller_group or pgid <= 1:
        return None
    _state, ticks = _posix_proc_stat(identity.pid)
    if ticks != identity.start_ticks:
        return None
    return ProcessIdentity(
        pid=identity.pid,
        created_ms=identity.created_ms,
        start_ticks=identity.start_ticks,
        namespace=identity.namespace or local_process_namespace(),
        owned_pgid=pgid,
        owned_sid=sid,
    )


def owned_group_occupied(identity: ProcessIdentity) -> bool:
    """True when a stored owned session still has a live member, or cannot be read."""
    if not _is_owned_session_leader(identity) or sys.platform == "win32":
        return False
    pgid = identity.owned_pgid
    sid = identity.owned_sid
    if pgid is None or sid is None:
        return True
    members = _collect_owned_group(pgid, sid)
    if members is None:
        return True
    for pid in members:
        member = ProcessIdentity(
            pid=pid,
            start_ticks=_linux_start_ticks(pid),
            namespace=identity.namespace,
        )
        if process_liveness(member) != Liveness.DEAD:
            return True
    return False


def _is_owned_session_leader(identity: ProcessIdentity) -> bool:
    pgid = identity.owned_pgid
    sid = identity.owned_sid
    return pgid is not None and sid is not None and pgid > 1 and pgid == sid == identity.pid


def _owned_session_is_safe(identity: ProcessIdentity) -> bool:
    """The stored session still belongs to this identity and is not our group."""
    if not _is_owned_session_leader(identity) or sys.platform == "win32":
        return False
    if not hasattr(os, "getpgid") or not hasattr(os, "getsid"):
        return False
    pgid = identity.owned_pgid
    if pgid is None:
        return False
    try:
        if int(os.getpgrp()) == pgid:
            return False
    except OSError:
        return False
    liveness = process_liveness(identity)
    if liveness == Liveness.UNKNOWN:
        return False
    if liveness == Liveness.ALIVE:
        if not _posix_still_same(identity):
            return False
        try:
            return int(os.getpgid(identity.pid)) == pgid and int(os.getsid(identity.pid)) == pgid
        except OSError:
            return False
    # The leader has exited. Refuse when a different process now owns the pid.
    return not _pid_occupied_by_other(identity)


def _pid_occupied_by_other(identity: ProcessIdentity) -> bool:
    """True when ``pid`` is running and is not the process this identity names."""
    if sys.platform == "win32":
        return _windows_pid_still_active(identity.pid)
    try:
        os.kill(identity.pid, 0)
    except OSError:
        return False
    _state, ticks = _posix_proc_stat(identity.pid)
    if ticks is None or identity.start_ticks is None:
        return False
    return ticks != identity.start_ticks


def _protected_pid(pid: int) -> bool:
    return pid in {os.getpid(), os.getppid()}


def _collect_owned_group(pgid: int, sid: int) -> list[int] | None:
    """Members of one owned session. None means the group is not safe to signal."""
    if pgid <= 1 or sid <= 0 or sys.platform == "win32" or not hasattr(os, "getpgid"):
        return None
    try:
        if int(os.getpgrp()) == pgid:
            return None
    except OSError:
        return None
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    members: list[int] = []
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            same_group = int(os.getpgid(pid)) == pgid
            same_session = int(os.getsid(pid)) == sid if hasattr(os, "getsid") else False
        except OSError:
            continue
        if not same_group or not same_session:
            continue
        if _protected_pid(pid):
            return None
        members.append(pid)
    return members


def _posix_contain_owned_group(identity: ProcessIdentity) -> ContainmentResult:
    """Kill one owned session until a repeat read shows it empty.

    A child forked while the group signal is delivered inherits the group and
    is taken by the next signal. Success requires the leader to be dead, the
    group to be empty twice, and every descendant that left the group to be dead.
    """
    import signal
    import time

    pgid = identity.owned_pgid
    sid = identity.owned_sid
    kill_sig = getattr(signal, "SIGKILL", None)
    if pgid is None or sid is None or kill_sig is None:
        return ContainmentResult(False, (identity,), True)
    tracked: dict[int, ProcessIdentity] = {identity.pid: identity}
    deadline = time.monotonic() + _TREE_STOP_TIMEOUT_SECONDS
    empty_reads = 0
    while time.monotonic() < deadline:
        if process_liveness(identity) == Liveness.UNKNOWN:
            return ContainmentResult(False, tuple(tracked.values()), True)
        if process_liveness(identity) == Liveness.ALIVE and not _posix_still_same(identity):
            return ContainmentResult(False, tuple(tracked.values()), True)
        members = _collect_owned_group(pgid, sid)
        if members is None:
            return ContainmentResult(False, tuple(tracked.values()), True)
        for pid in members:
            tracked.setdefault(pid, _posix_child_identity(pid))
        escapees = _session_escapees(identity, set(members))
        for pid in escapees:
            tracked.setdefault(pid, _posix_child_identity(pid))
        if any(process_liveness(item) == Liveness.UNKNOWN for item in tracked.values()):
            return ContainmentResult(False, tuple(tracked.values()), True)
        leader_gone = process_liveness(identity) != Liveness.ALIVE
        escapees_dead = all(process_liveness(tracked[pid]) == Liveness.DEAD for pid in escapees)
        group_clear = not members and leader_gone and escapees_dead
        if group_clear and all(
            process_liveness(item) == Liveness.DEAD for item in tracked.values()
        ):
            empty_reads += 1
            if empty_reads >= 2:
                return ContainmentResult(True, tuple(tracked.values()), False)
            time.sleep(0.02)
            continue
        empty_reads = 0
        if members and not _posix_signal_group(pgid, kill_sig):
            return ContainmentResult(False, tuple(tracked.values()), True)
        for pid in escapees:
            item = tracked[pid]
            alive = process_liveness(item) == Liveness.ALIVE and _posix_still_same(item)
            if alive and not _posix_signal(pid, kill_sig):
                return ContainmentResult(False, tuple(tracked.values()), True)
        _reap_pid(identity.pid)
        time.sleep(0.02)
    return ContainmentResult(False, tuple(tracked.values()), True)


def _session_escapees(identity: ProcessIdentity, members: set[int]) -> list[int]:
    """Descendants that left the owned session while the leader was still alive."""
    if process_liveness(identity) != Liveness.ALIVE or not _posix_still_same(identity):
        return []
    return [
        pid
        for pid in _posix_descendants(identity.pid)
        if pid not in members and pid != identity.pid and not _protected_pid(pid)
    ]


def _posix_signal_group(pgid: int, sig: int) -> bool:
    """Signal one owned group. The caller's group and pid 1 are refused."""
    if pgid <= 1 or pgid == os.getpid():
        return False
    try:
        if int(os.getpgrp()) == pgid:
            return False
    except OSError:
        return False
    try:
        os.kill(-pgid, sig)
    except OSError as exc:
        if exc.errno == errno.EPERM:
            return False
        if exc.errno != errno.ESRCH:
            return False
    return True


def _record_descendants(
    identity: ProcessIdentity, tracked: list[ProcessIdentity], known: set[int]
) -> bool:
    for pid in _posix_visible_descendants(identity.pid):
        if pid == identity.pid or pid in known:
            continue
        if _protected_pid(pid):
            return False
        tracked.append(_posix_child_identity(pid))
        known.add(pid)
    return True


def _untracked_descendants(identity: ProcessIdentity, known: set[int]) -> bool:
    return any(
        pid not in known and pid != identity.pid for pid in _posix_visible_descendants(identity.pid)
    )


def _freeze_tracked(tracked: list[ProcessIdentity], stop_sig: int) -> bool:
    for item in tracked:
        if process_liveness(item) == Liveness.UNKNOWN or not _posix_still_same(item):
            if process_liveness(item) == Liveness.DEAD:
                continue
            return False
        if process_liveness(item) != Liveness.ALIVE:
            continue
        state, _ticks = _posix_proc_stat(item.pid)
        if state in {"T", "t"}:
            continue
        if not _posix_signal(item.pid, stop_sig):
            return False
    return True


def _any_running(tracked: list[ProcessIdentity]) -> bool:
    for item in tracked:
        if process_liveness(item) != Liveness.ALIVE:
            continue
        state, _ticks = _posix_proc_stat(item.pid)
        if state not in {"T", "t"}:
            return True
    return False


def _tree_is_frozen(tracked: list[ProcessIdentity], root: ProcessIdentity) -> bool:
    for item in (*tracked, root):
        if process_liveness(item) == Liveness.UNKNOWN or not _posix_still_same(item):
            return False
        if process_liveness(item) != Liveness.ALIVE:
            continue
        state, _ticks = _posix_proc_stat(item.pid)
        if state not in {"T", "t"}:
            return False
    return True


def _kill_frozen_tree(tracked: list[ProcessIdentity], root: ProcessIdentity, kill_sig: int) -> bool:
    for item in (*reversed(tracked), root):
        if process_liveness(item) != Liveness.ALIVE:
            continue
        if not _posix_still_same(item):
            return False
        if not _posix_signal(item.pid, kill_sig):
            return False
    return True


def _reap_pid(pid: int) -> None:
    if sys.platform == "win32" or pid <= 0:
        return
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        return


def _posix_descendants(root: int) -> list[int]:
    """Child and grandchild pids whose parent chain reaches ``root`` only."""
    if sys.platform == "win32":
        return []
    children: dict[int, list[int]] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return []
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        ppid = _posix_ppid(pid)
        if ppid is None:
            continue
        children.setdefault(ppid, []).append(pid)
    ordered: list[int] = []
    stack = list(children.get(root, []))
    seen: set[int] = set()
    while stack:
        pid = stack.pop()
        if pid in seen or pid == root:
            continue
        seen.add(pid)
        ordered.append(pid)
        stack.extend(children.get(pid, []))
    return ordered


def local_process_namespace() -> str | None:
    """Identity of this OS process namespace.

    Linux is the kernel boot id plus this process's pid namespace inode. Two
    containers on one host do not share that inode, so a foreign PID must not be
    judged from this container's ``/proc``. Windows is the boot time from
    ``NtQuerySystemInformation``. Unreadable identity is ``None`` and callers
    fail closed. This does not read account directories.
    """
    if sys.platform == "win32":
        boot = _windows_boot_time()
        if boot is None:
            return None
        return f"windows:{boot}"
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="utf-8") as boot_file:
            boot = boot_file.read().strip()
        pidns = os.readlink("/proc/self/ns/pid")
    except OSError:
        return None
    if not boot or not pidns.startswith("pid:"):
        return None
    return f"linux:{boot}:{pidns}"


def _windows_boot_time() -> int | None:
    import ctypes
    from ctypes import wintypes

    class _SystemTimeOfDay(ctypes.Structure):
        _fields_ = [
            ("BootTime", ctypes.c_longlong),
            ("CurrentTime", ctypes.c_longlong),
            ("TimeZoneBias", ctypes.c_longlong),
            ("TimeZoneId", wintypes.ULONG),
            ("Reserved", wintypes.ULONG),
            ("BootTimeBias", ctypes.c_ulonglong),
            ("SleepTimeBias", ctypes.c_ulonglong),
        ]

    info = _SystemTimeOfDay()
    status = ctypes.windll.ntdll.NtQuerySystemInformation(
        3, ctypes.byref(info), ctypes.sizeof(info), None
    )
    if int(status) != 0 or int(info.BootTime) <= 0:
        return None
    return int(info.BootTime)


def _posix_ppid(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("PPid:"):
                    return int(line.split()[1])
    except (OSError, ValueError):
        return None
    return None
