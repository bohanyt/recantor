"""PID-reuse closure and invocation-witness overlap for #73 round 8. No live model."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, update
from summary_broker_provider import (
    ObserverUnreadable,
    finish_stage_observation,
    invocation_witness,
    note_admitted_work,
    note_owned_invocation,
    note_stage_observation,
)

from recantor.db import get_sessionmaker
from recantor.meeting_essence.scheduling import host_authority
from recantor.meeting_essence.scheduling.host_permit import HostInvocationPermit
from recantor.meeting_essence.scheduling.models import MIHostInvocationLease
from recantor.meeting_essence.scheduling.permit_provider import PermitBoundMeetingEssenceProvider
from recantor.meeting_essence.scheduling.process_liveness import (
    ContainmentResult,
    ProcessIdentity,
    contain_owned_tree,
    identity_for_pid,
    local_process_namespace,
)


def test_pid_reuse_does_not_close_a_tree_or_signal_the_replacement(monkeypatch) -> None:
    """Parent probe. Reuse of the root leaves the old descendant unchecked."""
    from recantor.meeting_essence.scheduling import process_liveness as module

    root, descendant = 101001, 101002
    alive = {descendant}
    signals: list[int] = []

    def stop(pid: int, _sig: int) -> bool:
        signals.append(pid)
        alive.discard(pid)
        return True

    def windows_kill(identity: ProcessIdentity) -> bool:
        signals.append(identity.pid)
        alive.discard(identity.pid)
        return True

    monkeypatch.setattr(module, "process_liveness", lambda _identity: module.Liveness.DEAD)
    monkeypatch.setattr(module, "_pid_occupied_by_other", lambda _identity: True)
    monkeypatch.setattr(module, "_posix_signal", stop)
    monkeypatch.setattr(module, "_windows_kill_tree", windows_kill)
    result = contain_owned_tree(ProcessIdentity(pid=root, start_ticks=1, created_ms=1))
    assert result.proven is False
    assert result.ancestry_lost is True
    assert signals == []
    assert descendant in alive


def test_distinct_stages_overlap_and_admitted_work_is_observed(tmp_path) -> None:
    """Same observer, no production permit. Two real stage calls reach peak 2."""
    path = tmp_path / "invocation-counter.json"
    active = 0
    peak = 0
    ready = asyncio.Event()
    release = asyncio.Event()

    async def actual_work() -> None:
        nonlocal active, peak
        witness_id = note_stage_observation(path)
        active += 1
        peak = max(peak, active)
        if active == 2:
            ready.set()
        try:
            await release.wait()
        finally:
            active -= 1
            finish_stage_observation(path, witness_id)

    async def probe() -> None:
        nonlocal active
        tasks = [asyncio.create_task(actual_work()) for _ in range(2)]
        try:
            await asyncio.wait_for(ready.wait(), 2)
            observed = invocation_witness(path)
            admitted = note_admitted_work(path)
            during = invocation_witness(path)
            assert isinstance(admitted, str) and admitted
            assert active == 2
            assert peak == 2
            assert observed["present"] is True
            assert int(observed["live"]) == 2
            assert int(observed["peak"]) == 2
            assert int(observed["stage_calls"]) == 2
            assert int(during["admitted_work"]) == 1
            assert int(during["live"]) == 3
            assert int(during["peak"]) == 3
            finish_stage_observation(path, admitted)
        finally:
            release.set()
            await asyncio.gather(*tasks)
        assert active == 0
        finished = invocation_witness(path)
        assert int(finished["live"]) == 0
        assert int(finished["peak"]) == 3

    asyncio.run(probe())


def test_bound_call_stays_one_and_an_unbound_stage_is_separate(tmp_path) -> None:
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
    stage_id = ""
    try:
        note_owned_invocation(path, identity_for_pid(first.pid), invocation_id="same-call")
        note_owned_invocation(path, identity_for_pid(second.pid), invocation_id="same-call")
        stage_id = note_stage_observation(path, invocation_id="same-call")
        witness = invocation_witness(path)
        assert int(witness["live"]) == 1
        assert int(witness["peak"]) == 1
        assert int(witness["stage_calls"]) == 1
        first.kill()
        first.wait(timeout=5)
        witness = invocation_witness(path)
        assert int(witness["live"]) == 1
        assert int(witness["peak"]) == 1
        unbound = note_stage_observation(path)
        overlapped = invocation_witness(path)
        assert unbound != "same-call"
        assert int(overlapped["live"]) == 2
        assert int(overlapped["peak"]) == 2
        finish_stage_observation(path, unbound)
        finish_stage_observation(path, stage_id)
        resumed = note_stage_observation(path, invocation_id="same-call")
        finish_stage_observation(path, resumed)
        after = invocation_witness(path)
        assert int(after["live"]) == 1
        assert int(after["stage_calls"]) == 3
        assert int(after["peak"]) == 2
    finally:
        for proc in (first, second):
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


def test_missing_and_malformed_observer_files_fail_the_proof(tmp_path) -> None:
    missing = invocation_witness(tmp_path / "missing.json")
    assert missing["present"] is False
    assert missing["malformed"] is False
    broken = tmp_path / "invocation-counter.json"
    broken.write_text("{", encoding="utf-8")
    with pytest.raises(ObserverUnreadable):
        note_stage_observation(broken)
    witness = invocation_witness(broken)
    assert witness["present"] is False
    assert witness["malformed"] is True
    assert broken.read_text(encoding="utf-8") == "{"


@pytest.mark.parametrize("observation", ["note_stage_observation", "note_admitted_work"])
def test_in_process_observation_ends_when_its_actual_owner_dies(tmp_path, observation) -> None:
    from test_summary_broker_lifecycle import _identity_from_saved, _kill_pid_only

    from recantor.meeting_essence.scheduling.process_liveness import Liveness, process_liveness

    path = tmp_path / "counter.json"
    ready = tmp_path / "ready.json"
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(Path(__file__).resolve().parents[1] / "src"), str(Path(__file__).resolve().parent)]
    )
    code = """
import json, sys, time
from dataclasses import asdict
from pathlib import Path
import summary_broker_provider as observer
from recantor.meeting_essence.scheduling.process_liveness import current_process_identity
getattr(observer, sys.argv[3])(Path(sys.argv[1]))
Path(sys.argv[2]).write_text(json.dumps(asdict(current_process_identity())))
time.sleep(30)
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", code, str(path), str(ready), observation],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    owner = None
    try:
        deadline = time.monotonic() + 10
        while not ready.is_file() and time.monotonic() < deadline:
            assert proc.poll() is None
            time.sleep(0.05)
        assert ready.is_file()
        owner = _identity_from_saved(json.loads(ready.read_text(encoding="utf-8")))
        assert process_liveness(owner) == Liveness.ALIVE
        witness = invocation_witness(path)
        assert int(witness["live"]) == int(witness["peak"]) == 1
        _kill_pid_only(owner.pid)
        proc.wait(timeout=5)
        assert process_liveness(owner) == Liveness.DEAD
        witness = invocation_witness(path)
        assert int(witness["live"]) == 0
        assert int(witness["peak"]) == int(witness["stage_calls"]) == 1
        assert int(witness["admitted_work"]) == (observation == "note_admitted_work")
    finally:
        if owner is None and ready.is_file():
            owner = _identity_from_saved(json.loads(ready.read_text(encoding="utf-8")))
        if owner is not None and process_liveness(owner) == Liveness.ALIVE:
            _kill_pid_only(owner.pid)
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


async def _stamp_tree_root(pid: int, root: int) -> None:
    async with get_sessionmaker()() as db, db.begin():
        raw = await db.scalar(
            select(MIHostInvocationLease.owned_runtime_json).where(MIHostInvocationLease.id == 1)
        )
        payload = json.loads(raw or "[]")
        rows = payload if isinstance(payload, list) else payload["processes"]
        for item in rows:
            if int(item["pid"]) == pid:
                item["tree_root"] = root
        await db.execute(
            update(MIHostInvocationLease)
            .where(MIHostInvocationLease.id == 1)
            .values(owned_runtime_json=json.dumps(payload))
        )


async def _free_lease(permit: HostInvocationPermit, token: str | None) -> None:
    for held in list(permit._held_os):
        permit._release_os(held)
    if token is not None:
        permit._release_os(token)
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
                lease_expires_at=None,
            )
        )


@pytest.mark.asyncio
async def test_reused_root_keeps_the_permit_while_an_old_descendant_lives(
    mi_db, monkeypatch
) -> None:
    """One real tree can stop. A reused root with a live orphan does not close."""
    del mi_db
    from recantor.meeting_essence.scheduling import host_permit as permit_module
    from recantor.meeting_essence.scheduling import process_liveness as live_module

    descendant = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    owned = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    reused = ProcessIdentity(
        pid=101001,
        start_ticks=1,
        created_ms=1,
        namespace=local_process_namespace(),
    )
    signals: list[int] = []
    real_liveness = live_module.process_liveness
    real_occupied = live_module._pid_occupied_by_other
    real_windows = live_module._windows_kill_tree
    real_signal = live_module._posix_signal

    def liveness(identity: ProcessIdentity) -> object:
        if identity.pid == reused.pid and identity.start_ticks == reused.start_ticks:
            return live_module.Liveness.DEAD
        return real_liveness(identity)

    def occupied(identity: ProcessIdentity) -> bool:
        if identity.pid == reused.pid and identity.start_ticks == reused.start_ticks:
            return True
        return real_occupied(identity)

    def windows_kill(identity: ProcessIdentity) -> bool:
        signals.append(identity.pid)
        return real_windows(identity)

    def posix_signal(pid: int, sig: int) -> bool:
        signals.append(pid)
        return real_signal(pid, sig)

    monkeypatch.setattr(live_module, "process_liveness", liveness)
    monkeypatch.setattr(permit_module, "process_liveness", liveness)
    monkeypatch.setattr(live_module, "_pid_occupied_by_other", occupied)
    monkeypatch.setattr(live_module, "_windows_kill_tree", windows_kill)
    monkeypatch.setattr(live_module, "_posix_signal", posix_signal)
    permit = HostInvocationPermit()
    handle = None
    try:
        direct = contain_owned_tree(reused)
        assert direct.proven is False
        assert direct.ancestry_lost is True
        assert reused.pid not in signals
        handle = await permit.acquire(entrypoint="round8-pid-reuse")
        owned_identity = identity_for_pid(owned.pid)
        assert await permit.register_owned_runtime(handle, [owned_identity, reused]) is True
        await permit.mark_admitted(handle, spawns_process_tree=True)
        assert await permit.terminate_owned_tree(handle) is False
        owned.wait(timeout=5)
        assert descendant.poll() is None
        assert reused.pid not in signals
        assert descendant.pid not in signals
        snapshot = host_authority.read_owned()
        assert snapshot.containment_open is True
        async with get_sessionmaker()() as db:
            stored = await db.scalar(
                select(MIHostInvocationLease.owned_runtime_json).where(
                    MIHostInvocationLease.id == 1
                )
            )
        assert '"containment": "open"' in str(stored)
        assert await permit.release(handle) is False
        assert await permit.acknowledge_termination(handle) is False
        assert await permit.recover_proven_dead() is False
        assert await permit.read_active_witness() == 1
        async with get_sessionmaker()() as db, db.begin():
            await db.execute(
                update(MIHostInvocationLease)
                .where(MIHostInvocationLease.id == 1)
                .values(lease_expires_at=datetime.now(tz=UTC) - timedelta(hours=1))
            )
        competing = HostInvocationPermit()
        refused = await competing.try_acquire(entrypoint="round8-after-expiry")
        if refused is not None:
            await competing.release(refused)
        assert refused is None
        assert await permit.recover_proven_dead() is False
        assert descendant.poll() is None
        assert await permit.read_active_witness() == 1
    finally:
        for proc in (descendant, owned):
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        await _free_lease(permit, None if handle is None else handle.token)


@pytest.mark.asyncio
async def test_proven_root_covers_only_its_own_dead_child(mi_db, monkeypatch) -> None:
    del mi_db
    from recantor.meeting_essence.scheduling import host_permit as permit_module
    from recantor.meeting_essence.scheduling import process_liveness as live_module

    namespace = local_process_namespace()
    leader = ProcessIdentity(pid=101011, start_ticks=1, created_ms=1, namespace=namespace)
    child = ProcessIdentity(pid=101012, start_ticks=2, created_ms=2, namespace=namespace)
    unrelated = ProcessIdentity(pid=101013, start_ticks=3, created_ms=3, namespace=namespace)
    stopped: set[int] = set()
    fake_pids = {leader.pid, child.pid, unrelated.pid}
    real_liveness = live_module.process_liveness

    def liveness(identity: ProcessIdentity) -> object:
        if identity.pid == leader.pid and identity.pid not in stopped:
            return live_module.Liveness.ALIVE
        if identity.pid in fake_pids:
            return live_module.Liveness.DEAD
        return real_liveness(identity)

    def contain(identity: ProcessIdentity) -> ContainmentResult:
        if identity.pid == leader.pid:
            stopped.add(leader.pid)
            return ContainmentResult(True, (identity,), False)
        return ContainmentResult(False, (identity,), True)

    monkeypatch.setattr(permit_module, "process_liveness", liveness)
    monkeypatch.setattr(permit_module, "contain_owned_tree", contain)
    permit = HostInvocationPermit()
    handle = None
    try:
        handle = await permit.acquire(entrypoint="round8-covered-child")
        assert await permit.register_owned_runtime(handle, [leader, child]) is True
        await _stamp_tree_root(child.pid, leader.pid)
        await permit.mark_admitted(handle, spawns_process_tree=True)
        assert await permit.terminate_owned_tree(handle) is True
        assert await permit.release(handle) is True
        handle = None
        stopped.clear()
        handle = await permit.acquire(entrypoint="round8-unrelated-root")
        assert await permit.register_owned_runtime(handle, [leader, unrelated]) is True
        await permit.mark_admitted(handle, spawns_process_tree=True)
        assert await permit.terminate_owned_tree(handle) is False
        assert await permit.release(handle) is False
        assert await permit.acknowledge_termination(handle) is False
        assert await permit.recover_proven_dead() is False
        competing = HostInvocationPermit()
        refused = await competing.try_acquire(entrypoint="round8-unrelated-caller")
        if refused is not None:
            await competing.release(refused)
        assert refused is None
        snapshot = host_authority.read_owned()
        assert snapshot.containment_open is True
    finally:
        await _free_lease(permit, None if handle is None else handle.token)


@pytest.mark.asyncio
async def test_permit_bound_work_stays_one_while_the_observer_can_see_two(mi_db, tmp_path) -> None:
    del mi_db
    control = tmp_path / "unpermitted.json"
    active = 0
    peak = 0
    ready = asyncio.Event()
    release = asyncio.Event()

    async def unbound() -> None:
        nonlocal active, peak
        witness_id = note_stage_observation(control)
        active += 1
        peak = max(peak, active)
        if active == 2:
            ready.set()
        try:
            await release.wait()
        finally:
            active -= 1
            finish_stage_observation(control, witness_id)

    tasks = [asyncio.create_task(unbound()) for _ in range(2)]
    await asyncio.wait_for(ready.wait(), 2)
    control_witness = invocation_witness(control)
    release.set()
    await asyncio.gather(*tasks)
    assert int(control_witness["live"]) == 2
    assert int(control_witness["peak"]) == 2

    permit_path = tmp_path / "permitted.json"
    entered = tmp_path / "entered.txt"
    exited = tmp_path / "exited.txt"
    started = asyncio.Event()
    finished = asyncio.Event()

    class Work:
        spawns_process_tree = False

        async def run_stage(self, **kwargs: object) -> None:
            del kwargs
            work_id = note_stage_observation(permit_path, invocation_id="permit-call")
            entered.write_text("1", encoding="utf-8")
            started.set()
            try:
                await finished.wait()
            finally:
                finish_stage_observation(permit_path, work_id)
                exited.write_text("1", encoding="utf-8")

    permit = HostInvocationPermit()
    handle = await permit.acquire(entrypoint="round8-permit-observer")
    wrapper = PermitBoundMeetingEssenceProvider(
        inner=Work(),
        permit=permit,
        entrypoint="round8-permit-observer",
        shared_handle=handle,
    )
    task = asyncio.create_task(
        wrapper.run_stage(operation=None, request=None, job=None, prompt_bundle=None)  # type: ignore[arg-type]
    )
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert entered.is_file()
        other = HostInvocationPermit()
        second = await other.try_acquire(entrypoint="round8-while-held")
        if second is not None:
            overlap = note_admitted_work(permit_path)
            finish_stage_observation(permit_path, overlap)
            await other.release(second)
        held = invocation_witness(permit_path)
        assert second is None
        assert int(held["live"]) == 1
        assert int(held["peak"]) == 1
        finished.set()
        await asyncio.wait_for(task, 2)
        assert exited.is_file()
        assert await wrapper.aclose() is True
        follow = await permit.acquire(entrypoint="round8-after-release")

        class Admitted:
            spawns_process_tree = False

            async def run_stage(self, **kwargs: object) -> None:
                del kwargs
                work_id = note_admitted_work(permit_path)
                try:
                    await asyncio.sleep(0)
                finally:
                    finish_stage_observation(permit_path, work_id)

        follow_wrapper = PermitBoundMeetingEssenceProvider(
            inner=Admitted(),
            permit=permit,
            entrypoint="round8-after-release",
            shared_handle=follow,
        )
        await follow_wrapper.run_stage(operation=None, request=None, job=None, prompt_bundle=None)  # type: ignore[arg-type]
        assert await follow_wrapper.aclose() is True
        after = invocation_witness(permit_path)
        assert after["present"] is True
        assert after["malformed"] is False
        assert int(after["live"]) == 0
        assert int(after["peak"]) == 1
        assert int(after["admitted_work"]) >= 1
        assert int(after["stage_calls"]) >= 2
    finally:
        finished.set()
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(task, 2)
        await _free_lease(permit, handle.token)
