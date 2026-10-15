"""Injected provider for the CI Redis/Celery lifecycle test. No live model."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

from recantor.meeting_essence.contracts import (
    AtomicRecord,
    EvidenceReference,
    RecordContentStatus,
    RecordKind,
    StageOperation,
)
from recantor.meeting_essence.deterministic_provider import DeterministicMeetingEssenceProvider
from recantor.meeting_essence.scheduling.provider_factory import SummaryProviderFactory
from recantor.meeting_essence.stage_contracts import ExtractStageRequest


class BrokerScriptProvider(DeterministicMeetingEssenceProvider):
    def __init__(self) -> None:
        control = json.loads(
            Path(os.environ["RECANTOR_SUMMARY_CONTROL"]).read_text(encoding="utf-8")
        )
        records = [_record(item) for item in control["records"]]
        mapping: dict[UUID, list[AtomicRecord]] = {}
        for record in records:
            for evidence in record.evidence:
                mapping.setdefault(evidence.segment_id, []).append(record)
        super().__init__(
            extraction_records=records,
            evaluation_records=list(records),
            extract_by_segment=mapping,
        )
        self._all_records = list(records)
        self._sleep = float(control.get("sleep_seconds", 0))
        self._block = bool(control.get("block", False))
        self._log = Path(control["log_path"])
        self._spawn = bool(control.get("spawn_tree", False))
        self.spawns_process_tree = self._spawn
        self._owned: list[object] = []
        self._fail_once = bool(control.get("fail_once", False))
        self._block_after = int(control.get("block_after_extracts", 0))
        self._extracts = 0
        self._identity_path = self._log.with_name("owned-identity.json")
        self._identities_path = self._log.with_name("owned-identities.json")
        self._counter_path = self._log.with_name("invocation-counter.json")
        self._gate = self._log.with_name("owned-gate.txt")
        self._saved: list[dict[str, object]] = []
        self._popens: list[tuple[object, object]] = []
        self._invocation_id = uuid4().hex

    async def prepare_owned_processes(self) -> list[object]:
        if not self._spawn or self._owned:
            return list(self._owned)
        import subprocess
        import sys

        from recantor.meeting_essence.scheduling.process_liveness import (
            bind_owned_session,
            identity_for_pid,
            local_process_namespace,
            owned_subprocess_kwargs,
        )

        if self._gate.is_file():
            self._gate.unlink()
        grandchild = self._log.with_name("owned-grandchild.txt")
        if grandchild.is_file():
            grandchild.unlink()
        code = (
            "import pathlib,subprocess,sys,time\n"
            "gate=pathlib.Path(sys.argv[1]); child_path=pathlib.Path(sys.argv[2])\n"
            "deadline=time.time()+60\n"
            "while not gate.is_file() and time.time()<deadline:\n"
            "    time.sleep(0.05)\n"
            "if not gate.is_file():\n"
            "    raise SystemExit(0)\n"
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'],"
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
            "child_path.write_text(str(child.pid))\n"
            "time.sleep(120)\n"
        )
        parent = subprocess.Popen(
            [sys.executable, "-c", code, str(self._gate), str(grandchild)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            **owned_subprocess_kwargs(),
        )
        identity = bind_owned_session(identity_for_pid(parent.pid))
        if identity is None:
            parent.kill()
            parent.wait(timeout=5)
            raise RuntimeError("owned session was not created")
        payload = {
            "role": "root",
            "pid": identity.pid,
            "created_ms": identity.created_ms,
            "start_ticks": identity.start_ticks,
            "namespace": local_process_namespace(),
            "owned_pgid": identity.owned_pgid,
            "owned_sid": identity.owned_sid,
            "invocation_id": self._invocation_id,
        }
        self._identity_path.write_text(json.dumps(payload), encoding="utf-8")
        self._remember(payload)
        note_owned_invocation(self._counter_path, identity, invocation_id=self._invocation_id)
        self._popens.append((identity, parent))
        self._log.with_name("owned-root.txt").write_text(str(parent.pid), encoding="utf-8")
        self._owned = [identity]
        return list(self._owned)

    async def aclose(self) -> None:
        """A completed turn must stop its prepared tree, just like a live adapter.

        The crash proof kills the worker while its stage is blocked, so this
        closer never runs there. Normal completion must not strand an earlier
        one-page session's child and prevent the crash session from starting.
        Keep the closer pending if bounded wrapper cleanup cancels its wait.
        """
        from recantor.meeting_essence.scheduling.process_liveness import terminate_process_tree

        def stop() -> None:
            for identity, proc in self._popens:
                if not terminate_process_tree(identity):
                    raise RuntimeError("broker fixture owned tree stop was not proven")
                proc.wait(timeout=5)

        cleanup = asyncio.create_task(asyncio.to_thread(stop))
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                continue
        cleanup.result()

    def _remember(self, payload: dict[str, object]) -> None:
        self._saved.append(payload)
        self._identities_path.write_text(json.dumps(self._saved), encoding="utf-8")

    async def _capture_grandchild(self) -> None:
        if not self._spawn:
            return
        path = self._log.with_name("owned-grandchild.txt")
        deadline = time.monotonic() + 5
        while not path.is_file() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        if not path.is_file():
            return
        if any(item.get("role") == "grandchild" for item in self._saved):
            return
        from recantor.meeting_essence.scheduling.process_liveness import (
            identity_for_pid,
            local_process_namespace,
        )

        try:
            pid = int(path.read_text(encoding="utf-8") or "0")
        except ValueError:
            return
        if pid <= 0:
            return
        identity = identity_for_pid(pid)
        payload = {
            "role": "grandchild",
            "pid": identity.pid,
            "created_ms": identity.created_ms,
            "start_ticks": identity.start_ticks,
            "namespace": local_process_namespace(),
            "invocation_id": self._invocation_id,
        }
        self._remember(payload)
        note_owned_invocation(self._counter_path, identity, invocation_id=self._invocation_id)

    def _open_gate(self) -> None:
        if self._spawn and not self._gate.is_file():
            self._gate.write_text("open", encoding="utf-8")

    async def run_stage(self, **kwargs: object):  # type: ignore[no-untyped-def]
        self._open_gate()
        await self._capture_grandchild()
        return await self._run_stage(**kwargs)

    async def _run_stage(self, **kwargs: object):  # type: ignore[no-untyped-def]
        stage_invocation = note_stage_observation(
            self._counter_path, invocation_id=self._invocation_id
        )
        try:
            return await self._run_stage_body(**kwargs)
        finally:
            finish_stage_observation(self._counter_path, stage_invocation)

    async def _run_stage_body(self, **kwargs: object):  # type: ignore[no-untyped-def]
        request = kwargs["request"]
        operation = kwargs["operation"]
        job = kwargs["job"]
        page = ""
        if operation == StageOperation.EXTRACT and isinstance(request, ExtractStageRequest):
            page = ",".join(str(fragment.segment_id) for fragment in request.segment_fragments)
        with self._log.open("a", encoding="utf-8") as handle:
            handle.write(f"{job.session_id} {operation.value} {page}\n")
        if self._fail_once and operation == StageOperation.EXTRACT:
            counter = self._log.with_name("fail-count.txt")
            count = int(counter.read_text(encoding="utf-8")) if counter.is_file() else 0
            counter.write_text(str(count + 1), encoding="utf-8")
            if count < 3:
                raise TimeoutError
        if self._block and operation == StageOperation.EXTRACT:
            self._extracts += 1
            if self._block_after <= 0 or self._extracts > self._block_after:
                while True:
                    await asyncio.sleep(0.05)
        if self._sleep:
            await asyncio.sleep(self._sleep)
        if operation == StageOperation.CHECK:
            fragments = getattr(request, "source_fragments", ()) or ()
            visible = {fragment.segment_id for fragment in fragments}
            self.evaluation_records = [
                record
                for record in self._all_records
                if any(evidence.segment_id in visible for evidence in record.evidence)
            ]
        return await super().run_stage(**kwargs)  # type: ignore[arg-type]


class BrokerFactory(SummaryProviderFactory):
    adapter_id = "injected-broker-test"

    def build(self) -> BrokerScriptProvider:
        return BrokerScriptProvider()


def factory() -> BrokerFactory:
    return BrokerFactory()


def note_owned_invocation(
    path: Path, identity: object, *, invocation_id: str | None = None
) -> None:
    """Add a process to one logical invocation.

    Root and child share ``invocation_id`` and count as one while either is
    alive. A separate call without that id is a separate invocation. A dead
    worker pid does not drop an invocation that still has a live process.
    """
    with _counter_lock(path):
        current = _read_counter(path)
        invocations = [item for item in current["invocations"] if _invocation_live(item)]
        payload = _holder_payload(identity)
        key = _holder_key(payload)
        if invocation_id is None:
            for item in invocations:
                if any(_holder_key(member) == key for member in item["members"]):
                    _store(path, current, invocations)
                    return
            invocation_id = uuid4().hex
        found = next((item for item in invocations if item["id"] == invocation_id), None)
        if found is None:
            invocations.append({"id": invocation_id, "members": [payload], "active": False})
        elif key not in {_holder_key(member) for member in found["members"]}:
            found["members"].append(payload)
        _store(path, current, invocations)


def note_stage_observation(path: Path, *, invocation_id: str | None = None) -> str:
    """Count one real stage and return the invocation it belongs to.

    A stage joins only the invocation id it was given. Root and child of that
    same call stay one invocation, including after the root dies. A stage with
    no id, or an id that is not already live, is its own invocation. This never
    joins an unrelated live invocation and never refuses to count overlap.
    """
    from recantor.meeting_essence.scheduling.process_liveness import current_process_identity

    owner = _holder_payload(current_process_identity())
    with _counter_lock(path):
        current = _read_counter(path)
        current["stage_calls"] = int(current["stage_calls"]) + 1
        invocations = [item for item in current["invocations"] if _invocation_live(item)]
        if invocation_id is not None:
            found = next((item for item in invocations if item["id"] == invocation_id), None)
            if found is None:
                invocations.append(
                    {"id": invocation_id, "members": [], "active": True, "stage_owner": owner}
                )
            else:
                found["active"] = True
                found["stage_owner"] = owner
            _store(path, current, invocations)
            return invocation_id
        fresh = uuid4().hex
        invocations.append({"id": fresh, "members": [], "active": True, "stage_owner": owner})
        _store(path, current, invocations)
        return fresh


def finish_stage_observation(path: Path, invocation_id: str) -> None:
    """End an in-process stage. A process-backed invocation is left to its members."""
    with _counter_lock(path):
        current = _read_counter(path)
        invocations = []
        for item in current["invocations"]:
            if item["id"] == invocation_id:
                item["active"] = False
            if _invocation_live(item):
                invocations.append(item)
        _store(path, current, invocations)


def note_admitted_work(path: Path) -> str:
    """Record work that is actually running and return its invocation id.

    This is not an admission gate. If another invocation is already live, this
    one is still recorded and the peak includes both. The caller finishes the
    id when that work exits.
    """
    from recantor.meeting_essence.scheduling.process_liveness import current_process_identity

    with _counter_lock(path):
        current = _read_counter(path)
        current["admitted_work"] = int(current["admitted_work"]) + 1
        current["stage_calls"] = int(current["stage_calls"]) + 1
        invocations = [item for item in current["invocations"] if _invocation_live(item)]
        invocation_id = uuid4().hex
        invocations.append(
            {
                "id": invocation_id,
                "members": [],
                "active": True,
                "stage_owner": _holder_payload(current_process_identity()),
            }
        )
        _store(path, current, invocations)
        return invocation_id


class ObserverUnreadable(RuntimeError):
    """The invocation counter is missing structure that would hide overlap."""


def invocation_witness(path: Path) -> dict[str, int | bool]:
    """Live logical invocations.

    A missing or malformed file is present=false. Callers that require the
    proof must fail on that, rather than treat it as an empty peak.
    """
    absent = {
        "present": False,
        "malformed": False,
        "live": 0,
        "peak": 0,
        "stage_calls": 0,
        "admitted_work": 0,
    }
    if not path.is_file():
        return absent
    try:
        with _counter_lock(path):
            current = _read_counter(path)
            invocations = [item for item in current["invocations"] if _invocation_live(item)]
            _store(path, current, invocations)
            return {
                "present": True,
                "malformed": False,
                "live": len(invocations),
                "peak": int(current["peak"]),
                "stage_calls": int(current["stage_calls"]),
                "admitted_work": int(current["admitted_work"]),
            }
    except ObserverUnreadable:
        return {
            "present": False,
            "malformed": True,
            "live": 0,
            "peak": 0,
            "stage_calls": 0,
            "admitted_work": 0,
        }


def _holder_payload(identity: object) -> dict[str, object]:
    from recantor.meeting_essence.scheduling.process_liveness import ProcessIdentity

    if not isinstance(identity, ProcessIdentity):
        raise TypeError("owned invocation identity is required")
    return {
        "pid": identity.pid,
        "created_ms": identity.created_ms,
        "start_ticks": identity.start_ticks,
        "namespace": identity.namespace,
    }


def _holder_key(item: dict[str, object]) -> tuple[object, ...]:
    return (item.get("pid"), item.get("created_ms"), item.get("start_ticks"), item.get("namespace"))


def _holder_alive(item: dict[str, object]) -> bool:
    from recantor.meeting_essence.scheduling.process_liveness import (
        Liveness,
        ProcessIdentity,
        process_liveness,
    )

    try:
        pid = int(item.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    created = item.get("created_ms")
    ticks = item.get("start_ticks")
    namespace = item.get("namespace")
    identity = ProcessIdentity(
        pid=pid,
        created_ms=None if created is None else int(created),
        start_ticks=None if ticks is None else int(ticks),
        namespace=None if namespace is None else str(namespace),
    )
    return process_liveness(identity) == Liveness.ALIVE


def _invocation_live(item: dict[str, object]) -> bool:
    if item.get("active") is True:
        owner = item.get("stage_owner")
        if not isinstance(owner, dict):
            raise ObserverUnreadable("active stage owner is missing")
        if _holder_alive(owner):
            return True
    members = item.get("members")
    if not isinstance(members, list):
        return False
    return any(_holder_alive(member) for member in members if isinstance(member, dict))


def _store(path: Path, current: dict[str, object], invocations: list[dict[str, object]]) -> None:
    live = sum(1 for item in invocations if _invocation_live(item))
    peak = max(int(current["peak"]), live)
    current["peak"] = peak
    current["invocations"] = invocations
    _write_counter(path, current)


def _empty_counter() -> dict[str, object]:
    return {
        "invocations": [],
        "peak": 0,
        "stage_calls": 0,
        "admitted_work": 0,
        "denied": 0,
    }


def _read_counter(path: Path) -> dict[str, object]:
    """Read the invocation list. A legacy worker-pid counter is not a live reset.

    Malformed JSON is an error. Replacing it with an empty counter would hide
    an overlap the proof is required to see.
    """
    current = _empty_counter()
    if not path.is_file():
        return current
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        raise ObserverUnreadable("invocation counter is unreadable") from exc
    if not isinstance(raw, dict):
        raise ObserverUnreadable("invocation counter is not an object")
    try:
        current["peak"] = int(raw.get("peak") or 0)
        current["stage_calls"] = int(raw.get("stage_calls") or 0)
        current["admitted_work"] = int(raw.get("admitted_work") or 0)
        current["denied"] = int(raw.get("denied") or 0)
    except (TypeError, ValueError) as exc:
        raise ObserverUnreadable("invocation counter counts are malformed") from exc
    invocations = raw.get("invocations")
    if invocations is None:
        current["invocations"] = []
        return current
    if not isinstance(invocations, list) or any(not isinstance(item, dict) for item in invocations):
        raise ObserverUnreadable("invocation counter list is malformed")
    current["invocations"] = list(invocations)
    return current


def _write_counter(path: Path, current: dict[str, object]) -> None:
    path.write_text(json.dumps(current), encoding="utf-8")


@contextlib.contextmanager
def _counter_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    handle = lock_path.open("a+b")
    try:
        if handle.seek(0, os.SEEK_END) < 1:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if sys.platform == "win32":
            import msvcrt

            deadline = time.monotonic() + 2
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.01)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if sys.platform == "win32":
                import msvcrt

                handle.seek(0)
                with contextlib.suppress(OSError):
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _record(item: dict[str, object]) -> AtomicRecord:
    return AtomicRecord(
        record_id=str(item["record_id"]),
        topic="Rapat",
        kind=RecordKind.FINDING,
        claim_text=str(item["claim"]),
        content_status=RecordContentStatus.FINDING,
        evidence=[
            EvidenceReference(
                segment_id=UUID(str(item["segment_id"])),
                start_ms=int(item["start_ms"]),
                end_ms=int(item["end_ms"]),
            )
        ],
    )
