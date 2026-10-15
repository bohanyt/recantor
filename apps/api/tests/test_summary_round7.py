"""POSIX ownership and invocation-witness proofs for #73 round 7. No live model."""

from __future__ import annotations

import errno
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import update
from summary_broker_provider import invocation_witness, note_owned_invocation

from recantor.db import get_sessionmaker
from recantor.meeting_essence.scheduling import host_authority
from recantor.meeting_essence.scheduling.host_permit import HostInvocationPermit
from recantor.meeting_essence.scheduling.models import MIHostInvocationLease
from recantor.meeting_essence.scheduling.process_liveness import (
    ProcessIdentity,
    contain_owned_tree,
    identity_for_pid,
    local_process_namespace,
)


def _restore_signal(name: str, had: bool, old: object) -> None:
    if had:
        setattr(signal, name, old)
    elif hasattr(signal, name):
        delattr(signal, name)


def test_shared_process_group_is_not_ownership(monkeypatch) -> None:
    """Parent probe. Same pgid is not a tree. No OS signals."""
    from recantor.meeting_essence.scheduling import process_liveness as module

    had_kill = hasattr(signal, "SIGKILL")
    old_kill = getattr(signal, "SIGKILL", None)
    had_stop = hasattr(signal, "SIGSTOP")
    old_stop = getattr(signal, "SIGSTOP", None)
    signal.SIGKILL = 9
    signal.SIGSTOP = 19
    root, stranger, harness = 101001, 101003, 101004
    alive = {root, stranger, harness}
    killed: list[int] = []
    platform = sys.platform
    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(module.os, "getpgid", lambda _pid: 90000, raising=False)
    monkeypatch.setattr(
        module.os, "listdir", lambda _path: [str(root), str(stranger), str(harness)]
    )
    monkeypatch.setattr(module, "_posix_descendants", lambda _pid: [])
    monkeypatch.setattr(
        module,
        "process_liveness",
        lambda identity: module.Liveness.ALIVE if identity.pid in alive else module.Liveness.DEAD,
    )
    monkeypatch.setattr(
        module,
        "_posix_proc_stat",
        lambda pid: ("S", 1) if pid in alive else (None, None),
    )

    def stop(pid: int, _sig: int) -> bool:
        killed.append(pid)
        alive.discard(pid)
        return True

    monkeypatch.setattr(module, "_posix_signal", stop)
    try:
        selected = module._posix_visible_descendants(root)
        result = module._posix_contain_tree(ProcessIdentity(root, start_ticks=1))
    finally:
        monkeypatch.setattr(module.sys, "platform", platform)
        _restore_signal("SIGKILL", had_kill, old_kill)
        _restore_signal("SIGSTOP", had_stop, old_stop)
    assert selected == []
    assert stranger not in killed
    assert harness not in killed
    assert result.proven is False
    assert result.ancestry_lost is True


def test_root_fork_after_the_last_scan_is_not_proven(monkeypatch) -> None:
    """Parent probe. A child forked as the root is signalled stays alive and unproven."""
    from recantor.meeting_essence.scheduling import process_liveness as module

    had_kill = hasattr(signal, "SIGKILL")
    old_kill = getattr(signal, "SIGKILL", None)
    had_stop = hasattr(signal, "SIGSTOP")
    old_stop = getattr(signal, "SIGSTOP", None)
    signal.SIGKILL = 9
    signal.SIGSTOP = 19
    root, child, stranger = 101001, 101002, 101003
    alive = {root, child}
    killed: list[int] = []

    def visible(pid: int) -> list[int]:
        if pid == root and root in alive:
            return [pid_ for pid_ in (child, stranger) if pid_ in alive]
        return []

    def stop(pid: int, _sig: int) -> bool:
        if pid == root:
            alive.add(stranger)
        killed.append(pid)
        alive.discard(pid)
        return True

    monkeypatch.setattr(module, "_posix_visible_descendants", visible)
    monkeypatch.setattr(module, "_posix_signal", stop)
    monkeypatch.setattr(
        module,
        "process_liveness",
        lambda identity: module.Liveness.ALIVE if identity.pid in alive else module.Liveness.DEAD,
    )
    monkeypatch.setattr(
        module,
        "_posix_proc_stat",
        lambda pid: ("S", 1) if pid in alive else (None, None),
    )
    try:
        result = module._posix_contain_tree(ProcessIdentity(root, start_ticks=1))
    finally:
        _restore_signal("SIGKILL", had_kill, old_kill)
        _restore_signal("SIGSTOP", had_stop, old_stop)
    assert child in killed
    assert root in killed
    assert stranger in alive
    assert result.proven is False
    assert result.ancestry_lost is True


def test_owned_session_reaps_a_fork_during_the_group_signal(monkeypatch) -> None:
    """Owned session only. A child forked during the group signal is taken next."""
    from recantor.meeting_essence.scheduling import process_liveness as module

    had_kill = hasattr(signal, "SIGKILL")
    old_kill = getattr(signal, "SIGKILL", None)
    signal.SIGKILL = 9
    root, child, forked, stranger = 101001, 101002, 101003, 101004
    members = {root, child}
    calls: list[int] = []
    forked_once = False

    def getpgid(pid: int) -> int:
        if pid in members:
            return root
        return 50

    def kill(pid: int, sig: int) -> None:
        nonlocal forked_once
        if sig == 0:
            if pid not in members:
                raise OSError(errno.ESRCH, "gone")
            return
        if pid != -root:
            raise AssertionError(pid)
        calls.append(pid)
        if not forked_once:
            forked_once = True
            members.add(forked)
            members.discard(root)
            members.discard(child)
            return
        members.clear()

    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(module.os, "getpgid", getpgid, raising=False)
    monkeypatch.setattr(module.os, "getsid", getpgid, raising=False)
    monkeypatch.setattr(module.os, "getpgrp", lambda: 50, raising=False)
    monkeypatch.setattr(module.os, "kill", kill)
    monkeypatch.setattr(module.os, "listdir", lambda _path: [str(pid) for pid in sorted(members)])
    monkeypatch.setattr(module, "_reap_pid", lambda _pid: None)
    monkeypatch.setattr(module, "_posix_descendants", lambda _pid: [])
    monkeypatch.setattr(
        module,
        "process_liveness",
        lambda identity: module.Liveness.ALIVE if identity.pid in members else module.Liveness.DEAD,
    )
    monkeypatch.setattr(
        module,
        "_posix_proc_stat",
        lambda pid: ("S", 1) if pid in members or pid == root else (None, None),
    )
    try:
        result = contain_owned_tree(
            ProcessIdentity(root, start_ticks=1, owned_pgid=root, owned_sid=root)
        )
    finally:
        _restore_signal("SIGKILL", had_kill, old_kill)
    assert result.proven is True
    assert result.ancestry_lost is False
    assert calls == [-root, -root]
    assert stranger not in members
    assert not members


def test_caller_process_group_is_not_signalled(monkeypatch) -> None:
    from recantor.meeting_essence.scheduling import process_liveness as module

    calls: list[int] = []
    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(module.os, "getpgrp", lambda: 101001, raising=False)
    monkeypatch.setattr(module.os, "getpgid", lambda _pid: 101001, raising=False)
    monkeypatch.setattr(module.os, "getsid", lambda _pid: 101001, raising=False)
    monkeypatch.setattr(module.os, "kill", lambda pid, _sig: calls.append(pid))
    monkeypatch.setattr(
        module,
        "process_liveness",
        lambda _identity: module.Liveness.ALIVE,
    )
    monkeypatch.setattr(module, "_posix_proc_stat", lambda _pid: ("S", 1))
    result = contain_owned_tree(
        ProcessIdentity(101001, start_ticks=1, owned_pgid=101001, owned_sid=101001)
    )
    assert result.proven is False
    assert result.ancestry_lost is True
    assert calls == []


def test_dead_root_without_an_owned_session_is_ancestry_lost() -> None:
    result = contain_owned_tree(ProcessIdentity(pid=2_100_000_003, start_ticks=1, created_ms=1))
    assert result.proven is False
    assert result.ancestry_lost is True


def test_pid_reuse_during_stop_is_not_killed(monkeypatch) -> None:
    from recantor.meeting_essence.scheduling import process_liveness as module

    had_kill = hasattr(signal, "SIGKILL")
    old_kill = getattr(signal, "SIGKILL", None)
    had_stop = hasattr(signal, "SIGSTOP")
    old_stop = getattr(signal, "SIGSTOP", None)
    signal.SIGKILL = 9
    signal.SIGSTOP = 19
    ticks = {"value": 1}
    signals: list[int] = []

    def fake_signal(pid: int, sig: int) -> bool:
        del pid
        signals.append(sig)
        if sig == signal.SIGSTOP:
            ticks["value"] = 2
            return True
        return False

    monkeypatch.setattr(module, "_posix_signal", fake_signal)
    monkeypatch.setattr(module, "_posix_descendants", lambda _pid: [])
    monkeypatch.setattr(module, "_posix_proc_stat", lambda _pid: ("S", ticks["value"]))
    monkeypatch.setattr(module, "process_liveness", lambda _identity: module.Liveness.ALIVE)
    try:
        result = module._posix_contain_tree(ProcessIdentity(101001, start_ticks=1))
        assert result.proven is False
        assert result.ancestry_lost is True
        assert signals == [signal.SIGSTOP]
    finally:
        _restore_signal("SIGKILL", had_kill, old_kill)
        _restore_signal("SIGSTOP", had_stop, old_stop)


def test_stop_timeout_does_not_prove_the_tree(monkeypatch) -> None:
    from recantor.meeting_essence.scheduling import process_liveness as module

    had_kill = hasattr(signal, "SIGKILL")
    old_kill = getattr(signal, "SIGKILL", None)
    had_stop = hasattr(signal, "SIGSTOP")
    old_stop = getattr(signal, "SIGSTOP", None)
    signal.SIGKILL = 9
    signal.SIGSTOP = 19
    clock = {"now": 0.0}

    def monotonic() -> float:
        clock["now"] += 1.0
        return clock["now"]

    monkeypatch.setattr(time, "monotonic", monotonic)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(module, "_posix_descendants", lambda _pid: [])
    monkeypatch.setattr(module, "_posix_proc_stat", lambda _pid: ("S", 1))
    monkeypatch.setattr(module, "process_liveness", lambda _identity: module.Liveness.ALIVE)
    monkeypatch.setattr(module, "_posix_signal", lambda _pid, _sig: True)
    try:
        result = module._posix_contain_tree(ProcessIdentity(101001, start_ticks=1))
        assert result.proven is False
        assert result.ancestry_lost is True
    finally:
        _restore_signal("SIGKILL", had_kill, old_kill)
        _restore_signal("SIGSTOP", had_stop, old_stop)


def test_invocation_counts_one_while_any_owned_process_is_alive(tmp_path: Path) -> None:
    path = tmp_path / "invocation-counter.json"
    first = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    second = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        note_owned_invocation(path, identity_for_pid(first.pid), invocation_id="same")
        note_owned_invocation(path, identity_for_pid(second.pid), invocation_id="same")
        witness = invocation_witness(path)
        assert witness["present"] is True
        assert int(witness["live"]) == 1
        assert int(witness["peak"]) == 1
        first.kill()
        first.wait(timeout=5)
        witness = invocation_witness(path)
        assert int(witness["live"]) == 1
        assert int(witness["peak"]) == 1
        second.kill()
        second.wait(timeout=5)
        witness = invocation_witness(path)
        assert int(witness["live"]) == 0
        assert int(witness["peak"]) == 1
    finally:
        for proc in (first, second):
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


@pytest.mark.asyncio
async def test_already_dead_root_stays_quarantined(mi_db) -> None:
    del mi_db
    permit = HostInvocationPermit()
    handle = await permit.acquire(entrypoint="round7-dead-root")
    try:
        dead = ProcessIdentity(
            pid=2_100_000_003,
            created_ms=1,
            start_ticks=1,
            namespace=local_process_namespace(),
        )
        assert await permit.register_owned_runtime(handle, [dead]) is True
        await permit.mark_admitted(handle, spawns_process_tree=True)
        assert await permit.terminate_owned_tree(handle) is False
        assert await permit.read_active_witness() == 1
        snapshot = host_authority.read_owned()
        assert snapshot.containment_open is True
        assert await permit.release(handle) is False
        assert await permit.acknowledge_termination(handle) is False
        assert await permit.recover_proven_dead() is False
        assert await permit.try_acquire(entrypoint="round7-after-dead-root") is None
    finally:
        host_authority.write_owned([])
        async with get_sessionmaker()() as db, db.begin():
            await db.execute(
                update(MIHostInvocationLease)
                .where(MIHostInvocationLease.id == 1)
                .values(
                    holder_token=None,
                    holder_pid=None,
                    holder_pid_created_ms=None,
                    holder_start_ticks=None,
                    holder_namespace=None,
                    holder_entrypoint=None,
                    holder_state="free",
                    active_invocations=0,
                    owned_runtime_json="[]",
                    termination_acked=False,
                    quarantine_reason=None,
                    invocation_admitted=False,
                    spawns_process_tree=False,
                )
            )
