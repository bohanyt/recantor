"""Same-host invocation lock shared by every database and entrypoint.

The configured PostgreSQL row is not the host boundary. A non-reentrant named
mutex on Windows, or an exclusive flock elsewhere, plus a sidecar of owned
process identities, is. A permit pointed at a different database, host, port,
or cluster system identifier never becomes a second authority.

The lock and sidecar live in one OS namespace (this machine, or this container).
They are not a shared mount across containers. The PostgreSQL lease stores the
same namespace identity; a different namespace must not treat a missing local PID
as death. This module does not read or change HOME, USERPROFILE, or account
files, and it does not mount account directories.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from recantor.meeting_essence.scheduling.process_liveness import (
    Liveness,
    ProcessIdentity,
    local_process_namespace,
    owned_group_occupied,
    process_liveness,
)

_MUTEX_NAME = "Local\\RecantorMeetingEssenceHostInvocation"
_WAIT_OBJECT_0 = 0
_WAIT_ABANDONED = 0x00000080
_WAIT_TIMEOUT = 0x00000102
_thread_state = threading.local()


@dataclass(frozen=True)
class OwnedSnapshot:
    """Owned identities. ``readable`` false means the sidecar is unproven.

    ``containment_open`` stays busy after the root dies if a descendant was not
    captured. A dead root alone must not clear that state.
    """

    identities: list[ProcessIdentity]
    readable: bool
    containment_open: bool = False


@dataclass(frozen=True)
class ClusterAuthority:
    database: str
    host: str
    port: int
    system_identifier: str
    observed_host: str = ""
    server_host: str = ""


@dataclass
class HeldHostAuthority:
    """OS lock held by this thread. A second hold on the same thread fails."""

    kind: str
    handle: int | None = None
    fd: int | None = None
    _released: bool = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            if self.kind == "win32" and self.handle:
                kernel = _kernel32()
                kernel.ReleaseMutex(self.handle)
                kernel.CloseHandle(self.handle)
                self.handle = None
                return
            if self.fd is not None:
                import fcntl

                try:
                    fcntl.flock(self.fd, fcntl.LOCK_UN)
                finally:
                    os.close(self.fd)
                    self.fd = None
        finally:
            _thread_state.held = False


def authority_directory() -> Path:
    override = os.environ.get("RECANTOR_HOST_AUTHORITY_DIR")
    if override:
        path = Path(override)
    elif sys.platform == "win32":
        root = os.environ.get("TEMP") or os.environ.get("TMP") or "."
        path = Path(root) / "recantor-host-invocation"
    else:
        path = Path("/tmp/recantor-host-invocation")
    path.mkdir(parents=True, exist_ok=True)
    return path


def sidecar_path() -> Path:
    return authority_directory() / "owned-processes.json"


def identity_path() -> Path:
    return authority_directory() / "cluster-identity.json"


def try_hold() -> HeldHostAuthority | None:
    """Take the host lock once per thread. Windows mutexes are otherwise reentrant."""
    if getattr(_thread_state, "held", False):
        return None
    authority_directory()
    held = _try_hold_windows() if sys.platform == "win32" else _try_hold_posix()
    if held is None:
        return None
    _thread_state.held = True
    return held


def read_owned() -> OwnedSnapshot:
    path = sidecar_path()
    if not path.is_file():
        return OwnedSnapshot(identities=[], readable=True)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return OwnedSnapshot(identities=[], readable=False)
    if not isinstance(raw, dict):
        return OwnedSnapshot(identities=[], readable=False)
    items = raw.get("owned")
    if not isinstance(items, list):
        return OwnedSnapshot(identities=[], readable=False)
    owned: list[ProcessIdentity] = []
    for item in items:
        if not isinstance(item, dict) or "pid" not in item:
            return OwnedSnapshot(identities=[], readable=False)
        try:
            namespace = item.get("namespace")
            owned.append(
                ProcessIdentity(
                    pid=int(item["pid"]),
                    created_ms=None if item.get("created_ms") is None else int(item["created_ms"]),
                    start_ticks=(
                        None if item.get("start_ticks") is None else int(item["start_ticks"])
                    ),
                    namespace=None if namespace is None else str(namespace),
                    owned_pgid=None if item.get("owned_pgid") is None else int(item["owned_pgid"]),
                    owned_sid=None if item.get("owned_sid") is None else int(item["owned_sid"]),
                )
            )
        except (TypeError, ValueError):
            return OwnedSnapshot(identities=[], readable=False)
    return OwnedSnapshot(
        identities=owned,
        readable=True,
        containment_open=raw.get("containment") == "open",
    )


def read_cluster_authority() -> ClusterAuthority | None:
    """Return the durable cluster identity. None means missing; corrupt raises."""
    path = identity_path()
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        msg = "host cluster identity is unreadable"
        raise OSError(msg) from exc
    if not isinstance(raw, dict):
        msg = "host cluster identity is malformed"
        raise OSError(msg)
    try:
        return ClusterAuthority(
            database=str(raw["database"]),
            host=str(raw["host"]),
            port=int(raw["port"]),
            system_identifier=str(raw["system_identifier"]),
            observed_host=str(raw.get("observed_host") or ""),
            server_host=str(raw.get("server_host") or ""),
        )
    except (KeyError, TypeError, ValueError) as exc:
        msg = "host cluster identity is malformed"
        raise OSError(msg) from exc


def write_cluster_authority(identity: ClusterAuthority) -> None:
    payload = {
        "database": identity.database,
        "host": identity.host,
        "port": identity.port,
        "system_identifier": identity.system_identifier,
        "observed_host": identity.observed_host,
        "server_host": identity.server_host,
    }
    path = identity_path()
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(path)


def write_owned(identities: list[ProcessIdentity], *, containment_open: bool = False) -> None:
    payload = {
        "owned": [
            {
                "pid": item.pid,
                "created_ms": item.created_ms,
                "start_ticks": item.start_ticks,
                "namespace": item.namespace,
                "owned_pgid": item.owned_pgid,
                "owned_sid": item.owned_sid,
            }
            for item in identities
        ],
        "containment": "open" if containment_open else "closed",
    }
    path = sidecar_path()
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(path)


def owned_blocks_acquire(snapshot: OwnedSnapshot) -> bool:
    if not snapshot.readable or snapshot.containment_open:
        return True
    local = local_process_namespace()
    for item in snapshot.identities:
        if local is None or not item.namespace or item.namespace != local:
            return True
        if process_liveness(item) != Liveness.DEAD:
            return True
        if owned_group_occupied(item):
            return True
    return False


def reset_for_tests() -> None:
    """Clear the sidecar. Tests call this only when they own the lock."""
    write_owned([])


def _try_hold_windows() -> HeldHostAuthority | None:
    kernel = _kernel32()
    handle = kernel.CreateMutexW(None, False, _MUTEX_NAME)
    if not handle:
        return None
    waited = int(kernel.WaitForSingleObject(handle, 0))
    if waited not in {_WAIT_OBJECT_0, _WAIT_ABANDONED}:
        kernel.CloseHandle(handle)
        return None
    if waited == _WAIT_TIMEOUT:
        kernel.CloseHandle(handle)
        return None
    return HeldHostAuthority(kind="win32", handle=int(handle))


def _try_hold_posix() -> HeldHostAuthority | None:
    import fcntl

    path = authority_directory() / "invocation.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return HeldHostAuthority(kind="posix", fd=fd)


def _kernel32() -> object:
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel.ReleaseMutex.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel
