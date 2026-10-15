"""Shared host invocation permit.

Supported host assumptions:
- The configured database name, port, and PostgreSQL ``system_identifier`` must
  match this connection. A different database or cluster is busy.
- A configured host matches ``inet_server_addr``, the driver's connected
  address (``PQhostaddr`` or the peer of the libpq socket), or one bounded DNS
  lookup of that host. The lookup runs off the event loop and fails closed
  after 250ms. An unreachable name is not authority.
- A loopback URL whose connected peer is loopback is accepted when port
  publishing makes ``inet_server_addr`` a bridge address. The stored sidecar is
  compared by PostgreSQL ``system_identifier`` and those verified endpoints.
  Two names for that same observed endpoint are the same cluster. Resolving the
  stored hostname and comparing the result to the current hostname string is not
  the check.
- Process identity is this OS namespace: Linux boot id plus pid-namespace inode,
  or the Windows boot time. A foreign or unreadable namespace stays busy until
  the owning namespace acknowledges termination. Local PID lookup never clears it.
- This module does not mount account directories and does not claim cross-container
  model quality.

Acquire, renew, register, and release each commit in their own short transaction.
Process-tree stops copy identities, commit, then wait outside the row lock.
Lease expiry never clears a slot. A slot is recovered only when this namespace
can prove the caller and every owned process are dead.
"""

from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from recantor.db import get_sessionmaker
from recantor.meeting_essence.scheduling import host_authority
from recantor.meeting_essence.scheduling.host_authority import ClusterAuthority
from recantor.meeting_essence.scheduling.models import MIHostInvocationLease
from recantor.meeting_essence.scheduling.process_liveness import (
    Liveness,
    ProcessIdentity,
    capture_owned_descendants,
    contain_owned_tree,
    current_process_identity,
    local_process_namespace,
    owned_group_occupied,
    process_liveness,
)
from recantor.settings import get_settings

_DNS_TIMEOUT_SECONDS = 0.25
# Handles this process acquired and has not yet cleared. Tests release these
# after their own work settles; recovery still requires a proven-dead tree.
_open_handles: dict[str, tuple[HostInvocationPermit, PermitHandle]] = {}


class HostInvocationBusy(RuntimeError):
    """The shared host permit is held, quarantined, or unreachable."""


def open_permit_handles() -> tuple[tuple[HostInvocationPermit, PermitHandle], ...]:
    """Handles acquired in this process whose slot has not been cleared."""
    return tuple(_open_handles.values())


def _remember_handle(permit: HostInvocationPermit, handle: PermitHandle) -> None:
    _open_handles[handle.token] = (permit, handle)


def _forget_handle(token: str) -> None:
    _open_handles.pop(token, None)


@dataclass(frozen=True)
class PermitHandle:
    token: str
    identity: ProcessIdentity
    entrypoint: str
    acquired_at: datetime
    generation: int


@dataclass(frozen=True)
class _OwnedProcess:
    pid: int
    created_ms: int | None
    start_ticks: int | None
    namespace: str | None = None
    owned_pgid: int | None = None
    owned_sid: int | None = None
    # Pid of the root this record was captured under. Coverage of an already-dead
    # child applies only to that root's proven stop, not to a different tree.
    tree_root: int | None = None

    def as_identity(self) -> ProcessIdentity:
        return ProcessIdentity(
            pid=self.pid,
            created_ms=self.created_ms,
            start_ticks=self.start_ticks,
            namespace=self.namespace,
            owned_pgid=self.owned_pgid,
            owned_sid=self.owned_sid,
        )


class HostInvocationPermit:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession] | None = None) -> None:
        self._sessionmaker = sessionmaker or get_sessionmaker()
        self._held_os: dict[str, host_authority.HeldHostAuthority] = {}

    async def read_active_witness(self) -> int:
        async with self._sessionmaker() as db, db.begin():
            row = await self._load(db)
            if row is None:
                return 0
            return int(row.active_invocations)

    async def authority_reachable(self) -> bool:
        """True only for the configured database. SELECT 1 on an arbitrary DSN is not authority."""
        try:
            host_authority.authority_directory()
            async with self._sessionmaker() as db:
                if not await self._matches_configured_authority(db):
                    return False
                await db.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    async def blocks_new_invocation(self) -> bool:
        if host_authority.owned_blocks_acquire(host_authority.read_owned()):
            return True
        async with self._sessionmaker() as db, db.begin():
            if not await self._matches_configured_authority(db):
                return True
            row = await self._load(db)
            if row is None or not row.holder_token:
                return False
            return row.holder_state in {"held", "quarantine"} or int(row.active_invocations) > 0

    async def recover_proven_dead(self) -> bool:
        """Clear a crashed slot only after the caller and owned tree are proven dead."""
        try:
            async with self._sessionmaker() as db, db.begin():
                if not await self._matches_configured_authority(db):
                    return False
                await db.execute(text("SET LOCAL lock_timeout = '200ms'"))
                row = await self._lock_nowait(db)
                if row is None or not row.holder_token:
                    return False
                if not self._proven_dead(row):
                    if (
                        _namespace_is_local(row.holder_namespace)
                        and process_liveness(_caller_identity(row)) == Liveness.DEAD
                    ):
                        self._mark_quarantine(row, "owned-tree-not-proven-dead")
                    return False
                self._clear(row)
                host_authority.write_owned([], containment_open=False)
                return True
        except DBAPIError:
            return False

    async def try_acquire(
        self,
        *,
        entrypoint: str,
        lease_seconds: float | None = None,
        identity: ProcessIdentity | None = None,
    ) -> PermitHandle | None:
        settings = get_settings()
        lease = (
            float(settings.meeting_essence_host_permit_lease_seconds)
            if lease_seconds is None
            else lease_seconds
        )
        if lease <= 0:
            raise ValueError("host permit lease must be positive")
        holder = identity or current_process_identity()
        namespace = local_process_namespace()
        if holder.pid <= 0 or namespace is None:
            return None
        if holder.namespace and holder.namespace != namespace:
            return None
        normalized = entrypoint.strip()[:128] or "unknown"
        held_os: host_authority.HeldHostAuthority | None = None
        try:
            async with self._sessionmaker() as probe:
                if not await self._matches_configured_authority(probe):
                    return None
            if host_authority.owned_blocks_acquire(host_authority.read_owned()):
                return None
            await self.recover_proven_dead()
            if host_authority.owned_blocks_acquire(host_authority.read_owned()):
                return None
            held_os = host_authority.try_hold()
            if held_os is None:
                return None
            if host_authority.owned_blocks_acquire(host_authority.read_owned()):
                return None
            async with self._sessionmaker() as db:
                async with db.begin():
                    await db.execute(text("SET LOCAL lock_timeout = '200ms'"))
                    row = await self._lock_nowait(db)
                    if row is None:
                        return None
                    now = datetime.now(tz=UTC)
                    if row.holder_token and self._proven_dead(row):
                        self._clear(row)
                    elif row.holder_token and self._still_blocks(row):
                        if (
                            _namespace_is_local(row.holder_namespace)
                            and process_liveness(_caller_identity(row)) == Liveness.DEAD
                        ):
                            self._mark_quarantine(row, "owned-tree-not-proven-dead")
                        return None
                    elif row.holder_token:
                        self._mark_quarantine(row, "unproven-prior-invocation")
                        return None
                    token = _new_token()
                    row.holder_token = token
                    row.holder_pid = holder.pid
                    row.holder_pid_created_ms = holder.created_ms
                    row.holder_start_ticks = holder.start_ticks
                    row.holder_namespace = namespace
                    row.holder_entrypoint = normalized
                    row.lease_expires_at = now + timedelta(seconds=lease)
                    row.acquired_at = now
                    row.active_invocations = 1
                    row.holder_state = "held"
                    row.holder_generation = int(row.holder_generation or 0) + 1
                    row.owned_runtime_json = _encode_runtime([], containment_open=False)
                    row.termination_acked = False
                    row.quarantine_reason = None
                    row.invocation_admitted = False
                    row.spawns_process_tree = False
                    row.updated_at = now
                    generation = int(row.holder_generation)
                host_authority.write_owned([], containment_open=False)
                self._held_os[token] = held_os
                held_os = None
                handle = PermitHandle(
                    token=token,
                    identity=holder,
                    entrypoint=normalized,
                    acquired_at=now,
                    generation=generation,
                )
                _remember_handle(self, handle)
                return handle
        except DBAPIError:
            return None
        finally:
            if held_os is not None:
                held_os.release()

    async def acquire(
        self,
        *,
        entrypoint: str,
        lease_seconds: float | None = None,
        identity: ProcessIdentity | None = None,
    ) -> PermitHandle:
        handle = await self.try_acquire(
            entrypoint=entrypoint, lease_seconds=lease_seconds, identity=identity
        )
        if handle is None:
            raise HostInvocationBusy("host model invocation permit is busy or unreachable")
        return handle

    async def register_owned_runtime(
        self, handle: PermitHandle, identities: list[ProcessIdentity]
    ) -> bool:
        local = local_process_namespace()
        if local is None:
            return False
        stamped: list[ProcessIdentity] = []
        for item in identities:
            namespace = item.namespace or local
            if namespace != local:
                return False
            stamped.append(
                ProcessIdentity(
                    pid=item.pid,
                    created_ms=item.created_ms,
                    start_ticks=item.start_ticks,
                    namespace=namespace,
                    owned_pgid=item.owned_pgid,
                    owned_sid=item.owned_sid,
                )
            )
        encoded = [_owned_dict(item) for item in stamped]
        async with self._sessionmaker() as db, db.begin():
            await db.execute(text("SET LOCAL lock_timeout = '200ms'"))
            row = await self._lock_nowait(db)
            if row is None or row.holder_token != handle.token:
                return False
            if row.holder_state != "held":
                return False
            existing, containment_open = _runtime_state(row.owned_runtime_json)
            merged = {item.pid: item for item in existing}
            for item in encoded:
                merged[item.pid] = item
            for item in encoded:
                for child in capture_owned_descendants(item.as_identity()):
                    if child.pid not in merged:
                        merged[child.pid] = replace(_owned_dict(child), tree_root=item.pid)
            owned = list(merged.values())
            row.owned_runtime_json = _encode_runtime(owned, containment_open=containment_open)
            row.updated_at = datetime.now(tz=UTC)
        host_authority.write_owned(
            [item.as_identity() for item in owned],
            containment_open=containment_open,
        )
        return True

    async def mark_admitted(self, handle: PermitHandle, *, spawns_process_tree: bool) -> None:
        async with self._sessionmaker() as db, db.begin():
            await db.execute(text("SET LOCAL lock_timeout = '200ms'"))
            row = await self._lock_nowait(db)
            if row is None or row.holder_token != handle.token or row.holder_state != "held":
                raise HostInvocationBusy("host permit token is not current")
            row.invocation_admitted = True
            row.spawns_process_tree = spawns_process_tree
            row.updated_at = datetime.now(tz=UTC)

    async def terminate_owned_tree(self, handle: PermitHandle) -> bool:
        """Copy identities, commit, then stop the tree outside the row lock.

        A stop that loses ancestry stores every identity it did see and leaves
        containment open. A later recovery cannot clear that slot just because
        the root pid is dead or reused. A proven stop covers only identities that
        stop established, including an already-dead child captured under that
        same root. It does not cover an unrelated tree.
        """
        async with self._sessionmaker() as db, db.begin():
            await db.execute(text("SET LOCAL lock_timeout = '200ms'"))
            row = await self._lock_nowait(db, wait=True)
            if row is None or row.holder_token != handle.token:
                return False
            if int(row.holder_generation) != handle.generation:
                return False
            owned, already_open = _runtime_state(row.owned_runtime_json)
        proven = not already_open
        ancestry_lost = already_open
        covered_pids: set[int] = set()
        proven_roots: set[int] = set()
        seen: dict[int, _OwnedProcess] = {item.pid: item for item in owned}
        for item in _containment_order(owned):
            identity = item.as_identity()
            before = process_liveness(identity)
            if before == Liveness.UNKNOWN:
                proven = False
                ancestry_lost = True
                continue
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(contain_owned_tree, identity),
                    timeout=4.0,
                )
            except TimeoutError:
                proven = False
                ancestry_lost = True
                continue
            for found in result.identities:
                if found.pid not in seen:
                    seen[found.pid] = replace(_owned_dict(found), tree_root=identity.pid)
            if result.proven and not result.ancestry_lost:
                proven_roots.add(identity.pid)
                covered_pids.update(found.pid for found in result.identities)
                continue
            # An already-dead child is covered only by the root it was captured
            # under, or by a pid that root's proof returned. Another tree's
            # success does not close this one.
            tied_to_this_proof = identity.pid in covered_pids or (
                item.tree_root is not None and item.tree_root in proven_roots
            )
            covered_dead_member = (
                result.ancestry_lost
                and before == Liveness.DEAD
                and identity.owned_pgid is None
                and tied_to_this_proof
            )
            if not covered_dead_member:
                proven = False
                ancestry_lost = True
        for item in seen.values():
            if owned_group_occupied(item.as_identity()):
                proven = False
                ancestry_lost = True
                break
        persisted = list(seen.values())
        identities = [item.as_identity() for item in persisted]
        async with self._sessionmaker() as db, db.begin():
            await db.execute(text("SET LOCAL lock_timeout = '200ms'"))
            row = await self._lock_nowait(db, wait=True)
            if row is None or row.holder_token != handle.token:
                return False
            if int(row.holder_generation) != handle.generation:
                return False
            if not proven or ancestry_lost:
                row.owned_runtime_json = _encode_runtime(persisted, containment_open=True)
                self._mark_quarantine(row, "containment-unresolved")
                host_authority.write_owned(identities, containment_open=True)
                return False
            row.owned_runtime_json = _encode_runtime(persisted, containment_open=False)
            host_authority.write_owned(identities, containment_open=False)
            return True

    async def quarantine_unproven(self, handle: PermitHandle, reason: str) -> bool:
        """Keep the slot and the OS lock. An unproven inner task must not free either."""
        async with self._sessionmaker() as db, db.begin():
            await db.execute(text("SET LOCAL lock_timeout = '200ms'"))
            row = await self._lock_nowait(db, wait=True)
            if row is None or row.holder_token != handle.token:
                return False
            self._mark_quarantine(row, reason)
        return False

    async def renew(self, handle: PermitHandle, *, lease_seconds: float | None = None) -> None:
        settings = get_settings()
        lease = (
            float(settings.meeting_essence_host_permit_lease_seconds)
            if lease_seconds is None
            else lease_seconds
        )
        now = datetime.now(tz=UTC)
        async with self._sessionmaker() as db, db.begin():
            await db.execute(text("SET LOCAL lock_timeout = '200ms'"))
            row = await self._lock_nowait(db)
            if row is None or row.holder_token != handle.token or row.holder_state != "held":
                raise HostInvocationBusy("host permit token is not current")
            row.lease_expires_at = now + timedelta(seconds=lease)
            row.updated_at = now

    async def release(self, handle: PermitHandle) -> bool:
        """Clear only when this live caller acks and every owned process is proven dead.

        Returns True when the slot was cleared. A false return leaves the row
        quarantined and still counting as the one active invocation.
        """
        async with self._sessionmaker() as db, db.begin():
            await db.execute(text("SET LOCAL lock_timeout = '200ms'"))
            row = await self._lock_nowait(db, wait=True)
            if row is None or row.holder_token != handle.token:
                self._release_os(handle.token)
                _forget_handle(handle.token)
                return False
            owned, containment_open = _runtime_state(row.owned_runtime_json)
            if not self._termination_proven(row, handle):
                self._mark_quarantine(row, "owned-runtime-not-proven-dead")
                host_authority.write_owned(
                    [item.as_identity() for item in owned],
                    containment_open=containment_open,
                )
                self._release_os(handle.token)
                return False
            self._clear(row)
            host_authority.write_owned([], containment_open=False)
        self._release_os(handle.token)
        return True

    async def acknowledge_termination(self, handle: PermitHandle) -> bool:
        """Clear a quarantined slot after every registered process is proven dead.

        An empty owned-runtime list is not proof, so this refuses that case.
        """
        async with self._sessionmaker() as db, db.begin():
            await db.execute(text("SET LOCAL lock_timeout = '200ms'"))
            row = await self._lock_nowait(db)
            if row is None or row.holder_token != handle.token:
                self._release_os(handle.token)
                _forget_handle(handle.token)
                return False
            if not _namespace_is_local(row.holder_namespace):
                return False
            owned, containment_open = _runtime_state(row.owned_runtime_json)
            if containment_open or not owned:
                self._mark_quarantine(row, "no-owned-runtime-to-prove")
                return False
            if any(owned_group_occupied(item.as_identity()) for item in owned):
                self._mark_quarantine(row, "owned-group-not-proven-dead")
                return False
            if any(_owned_liveness(item) != Liveness.DEAD for item in owned):
                self._mark_quarantine(row, "owned-runtime-not-proven-dead")
                return False
            caller = _caller_identity(row)
            if process_liveness(caller) == Liveness.UNKNOWN:
                self._mark_quarantine(row, "caller-liveness-unknown")
                return False
            row.termination_acked = True
            self._clear(row)
            host_authority.write_owned([], containment_open=False)
        self._release_os(handle.token)
        return True

    def _proven_dead(self, row: MIHostInvocationLease) -> bool:
        if not row.holder_token or not _namespace_is_local(row.holder_namespace):
            return False
        try:
            snapshot = host_authority.read_owned()
        except OSError:
            return False
        if not snapshot.readable or snapshot.containment_open:
            return False
        owned, containment_open = _runtime_state(row.owned_runtime_json)
        if containment_open:
            return False
        caller = process_liveness(_caller_identity(row))
        if caller != Liveness.DEAD:
            return False
        if any(_owned_liveness(item) != Liveness.DEAD for item in owned):
            return False
        if any(owned_group_occupied(item.as_identity()) for item in owned):
            return False
        if any(_identity_liveness(item) != Liveness.DEAD for item in snapshot.identities):
            return False
        if any(owned_group_occupied(item) for item in snapshot.identities):
            return False
        return not (row.spawns_process_tree and not owned)

    def _still_blocks(self, row: MIHostInvocationLease) -> bool:
        if not row.holder_token:
            return False
        owned, containment_open = _runtime_state(row.owned_runtime_json)
        if containment_open:
            return True
        if not _namespace_is_local(row.holder_namespace):
            return True
        if any(owned_group_occupied(item.as_identity()) for item in owned):
            return True
        if row.holder_state == "quarantine":
            return True
        if int(row.active_invocations or 0) > 0 and row.holder_state == "held":
            caller = process_liveness(_caller_identity(row))
            if caller != Liveness.DEAD:
                return True
            owned, containment_open = _runtime_state(row.owned_runtime_json)
            if containment_open or not owned or not row.termination_acked:
                return True
            return any(_owned_liveness(item) != Liveness.DEAD for item in owned)
        return int(row.active_invocations or 0) > 0

    def _termination_proven(self, row: MIHostInvocationLease, handle: PermitHandle) -> bool:
        if not _namespace_is_local(row.holder_namespace):
            return False
        if process_liveness(handle.identity) != Liveness.ALIVE:
            return False
        if process_liveness(current_process_identity()) != Liveness.ALIVE:
            return False
        current = current_process_identity()
        if not _same_process(current, handle.identity):
            return False
        stored = _caller_identity(row)
        if process_liveness(stored) == Liveness.UNKNOWN:
            return False
        if stored.pid != handle.identity.pid:
            return False
        if (
            handle.identity.created_ms is not None
            and stored.created_ms != handle.identity.created_ms
        ):
            return False
        if (
            handle.identity.start_ticks is not None
            and stored.start_ticks != handle.identity.start_ticks
        ):
            return False
        owned, containment_open = _runtime_state(row.owned_runtime_json)
        if containment_open:
            return False
        if any(owned_group_occupied(item.as_identity()) for item in owned):
            return False
        if not row.invocation_admitted:
            return True
        if row.spawns_process_tree and not owned:
            return False
        return all(_owned_liveness(item) == Liveness.DEAD for item in owned)

    def _mark_quarantine(self, row: MIHostInvocationLease, reason: str) -> None:
        row.holder_state = "quarantine"
        row.active_invocations = 1
        row.termination_acked = False
        row.quarantine_reason = reason[:128]
        row.updated_at = datetime.now(tz=UTC)

    def _clear(self, row: MIHostInvocationLease) -> None:
        row.holder_token = None
        row.holder_pid = None
        row.holder_pid_created_ms = None
        row.holder_start_ticks = None
        row.holder_namespace = None
        row.holder_entrypoint = None
        row.lease_expires_at = None
        row.acquired_at = None
        row.active_invocations = 0
        row.holder_state = "free"
        row.owned_runtime_json = _encode_runtime([], containment_open=False)
        row.termination_acked = False
        row.quarantine_reason = None
        row.invocation_admitted = False
        row.spawns_process_tree = False
        row.updated_at = datetime.now(tz=UTC)
        _open_handles.clear()

    def _release_os(self, token: str) -> None:
        held = self._held_os.pop(token, None)
        if held is not None:
            held.release()

    async def _matches_configured_authority(self, db: AsyncSession) -> bool:
        expected = make_url(get_settings().database_url)
        database, server_host, port, system_identifier = await _server_observation(db)
        if not expected.database or database != expected.database:
            return False
        if port is None:
            return False
        if expected.port and int(port) != int(expected.port):
            return False
        peer = await _connected_endpoint(db)
        endpoint_matches = await _endpoint_matches(expected.host, server_host, port=int(port))
        peer_matches = _host_matches(expected.host, peer) or _port_published_loopback(
            expected.host, peer
        )
        if not endpoint_matches and not peer_matches:
            return False
        identity = ClusterAuthority(
            database=str(database),
            host=_canonical_host(expected.host),
            port=int(port),
            system_identifier=str(system_identifier),
            observed_host=_canonical_host(peer or server_host),
            server_host=_canonical_host(server_host),
        )
        try:
            stored = host_authority.read_cluster_authority()
        except OSError:
            return False
        if stored is None:
            host_authority.write_cluster_authority(identity)
            return True
        if stored.system_identifier != identity.system_identifier or stored.port != identity.port:
            return False
        if not _stored_endpoint_compatible(stored, identity):
            return False
        if stored.database != identity.database or not stored.observed_host:
            host_authority.write_cluster_authority(identity)
        return True

    async def _load(self, db: AsyncSession) -> MIHostInvocationLease | None:
        return await db.scalar(select(MIHostInvocationLease).where(MIHostInvocationLease.id == 1))

    async def _lock_nowait(
        self, db: AsyncSession, *, wait: bool = False
    ) -> MIHostInvocationLease | None:
        # Cleanup may overlap another cancellation's short transaction. Wait only
        # up to the caller's SET LOCAL lock_timeout; admission still uses NOWAIT.
        row = await db.scalar(
            select(MIHostInvocationLease)
            .where(MIHostInvocationLease.id == 1)
            .with_for_update(nowait=not wait)
        )
        if row is not None:
            return row
        db.add(MIHostInvocationLease(id=1, active_invocations=0, holder_state="free"))
        await db.flush()
        return await db.scalar(
            select(MIHostInvocationLease)
            .where(MIHostInvocationLease.id == 1)
            .with_for_update(nowait=not wait)
        )


def _canonical_host(host: str | None) -> str:
    if not host:
        return ""
    lowered = host.strip().lower().split("/", 1)[0]
    if lowered in {"localhost", "::1"}:
        return "127.0.0.1"
    return lowered


def _host_matches(expected: str | None, observed: str | None) -> bool:
    if not expected or not observed:
        return False
    return _canonical_host(expected) == _canonical_host(observed)


async def _endpoint_matches(expected: str | None, observed: str | None, *, port: int) -> bool:
    """Literal/loopback match, or a bounded DNS answer that includes the server address."""
    if _host_matches(expected, observed):
        return True
    if not expected or not observed:
        return False
    resolved = await _resolve_host(expected, port)
    return _canonical_host(observed) in resolved


async def _resolve_host(host: str, port: int) -> set[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP),
            timeout=_DNS_TIMEOUT_SECONDS,
        )
    except (TimeoutError, OSError, UnicodeError):
        return set()
    found: set[str] = set()
    for info in infos:
        sockaddr = info[4]
        if sockaddr:
            found.add(_canonical_host(str(sockaddr[0])))
    return found


async def _server_observation(db: AsyncSession) -> tuple[str, str | None, int | None, str]:
    observed = (
        await db.execute(
            text(
                "SELECT current_database(), inet_server_addr()::text, "
                "inet_server_port(), "
                "(SELECT system_identifier::text FROM pg_control_system())"
            )
        )
    ).one()
    database, address, port, system_identifier = observed
    server = None if address is None else str(address)
    server_port = None if port is None else int(port)
    return str(database), server, server_port, str(system_identifier)


def _port_published_loopback(expected_host: str | None, peer: str | None) -> bool:
    """URL and connected peer are loopback. ``inet_server_addr`` may be a bridge."""
    return _canonical_host(expected_host) == "127.0.0.1" and _canonical_host(peer) == "127.0.0.1"


def _stored_endpoint_compatible(stored: ClusterAuthority, current: ClusterAuthority) -> bool:
    """Same cluster when the stored observation is one this connection verified.

    A legacy sidecar without ``observed_host`` matches on ``system_identifier``
    and port, which were already compared. The stored hostname is not resolved
    and compared to the current hostname string, so ``postgres`` and ``db`` for
    one verified endpoint stay compatible. A stored address this connection did
    not observe is rejected.
    """
    stored_observed = _canonical_host(stored.observed_host)
    if not stored_observed:
        return True
    verified = {
        _canonical_host(current.observed_host),
        _canonical_host(current.server_host),
    }
    verified.discard("")
    return stored_observed in verified


async def _connected_endpoint(db: AsyncSession) -> str | None:
    """Address libpq actually connected to. Does not close the driver socket."""
    try:
        connection = await db.connection()
        raw = await connection.get_raw_connection()
    except Exception:
        return None
    return _endpoint_from_driver(raw)


async def _tcp_peer_host(db: AsyncSession) -> str | None:
    """Backward-compatible name for the connected endpoint."""
    return await _connected_endpoint(db)


def _endpoint_from_driver(raw: object, _seen: set[int] | None = None) -> str | None:
    seen = set() if _seen is None else _seen
    if raw is None or id(raw) in seen:
        return None
    seen.add(id(raw))
    hostaddr = _hostaddr_text(raw)
    if hostaddr:
        return hostaddr
    for name in ("driver_connection", "dbapi_connection", "connection", "_connection"):
        inner = getattr(raw, name, None)
        if inner is not None and inner is not raw:
            found = _endpoint_from_driver(inner, seen)
            if found:
                return found
    return _peer_from_libpq_socket(raw)


def _hostaddr_text(raw: object) -> str | None:
    pgconn = getattr(raw, "pgconn", None)
    if pgconn is None:
        return None
    try:
        hostaddr = pgconn.hostaddr
    except Exception:
        return None
    if isinstance(hostaddr, bytes):
        text_value = hostaddr.decode("utf-8", errors="replace").strip()
    else:
        text_value = str(hostaddr or "").strip()
    if not text_value or text_value.lower() == "none":
        return None
    return text_value


def _peer_from_libpq_socket(raw: object) -> str | None:
    """``PGconn.socket`` is a file descriptor, not a Python socket.

    Wrapping it must ``detach`` so the wrapper's destructor does not close libpq's fd.
    """
    pgconn = getattr(raw, "pgconn", None)
    if pgconn is None:
        return None
    try:
        handle = pgconn.socket
    except Exception:
        return None
    if isinstance(handle, int):
        if handle < 0:
            return None
        try:
            wrapped = socket.socket(fileno=handle)
        except OSError:
            return None
        try:
            try:
                peer = wrapped.getpeername()
            except OSError:
                return None
        finally:
            wrapped.detach()
        if not peer:
            return None
        return str(peer[0])
    getpeername = getattr(handle, "getpeername", None)
    if getpeername is None:
        return None
    try:
        peer = getpeername()
    except OSError:
        return None
    if not peer:
        return None
    return str(peer[0])


def _namespace_is_local(stored: str | None) -> bool:
    local = local_process_namespace()
    return bool(local and stored and stored == local)


def _owned_liveness(item: _OwnedProcess) -> Liveness:
    if not _namespace_is_local(item.namespace):
        return Liveness.UNKNOWN
    return process_liveness(item.as_identity())


def _identity_liveness(identity: ProcessIdentity) -> Liveness:
    if not _namespace_is_local(identity.namespace):
        return Liveness.UNKNOWN
    return process_liveness(identity)


def _new_token() -> str:
    from uuid import uuid4

    return uuid4().hex


def _caller_identity(row: MIHostInvocationLease) -> ProcessIdentity:
    return ProcessIdentity(
        pid=int(row.holder_pid or 0),
        created_ms=row.holder_pid_created_ms,
        start_ticks=row.holder_start_ticks,
    )


def _same_process(left: ProcessIdentity, right: ProcessIdentity) -> bool:
    if left.pid != right.pid:
        return False
    if (
        left.created_ms is not None
        and right.created_ms is not None
        and left.created_ms != right.created_ms
    ):
        return False
    if left.start_ticks is None or right.start_ticks is None:
        return True
    return left.start_ticks == right.start_ticks


def _containment_order(owned: list[_OwnedProcess]) -> list[_OwnedProcess]:
    """Stop a root before a child that was captured under that root.

    An unrelated record is not moved behind a different tree. Children follow
    their own root so a proven stop can cover them, and nothing else.
    """
    present = {item.pid for item in owned}

    def key(item: _OwnedProcess) -> tuple[int, int]:
        if _is_stored_leader(item):
            return (0, item.pid)
        captured_under_present = (
            item.tree_root is not None and item.tree_root != item.pid and item.tree_root in present
        )
        if captured_under_present:
            return (2, item.pid)
        return (1, item.pid)

    return sorted(owned, key=key)


def _is_stored_leader(item: _OwnedProcess) -> bool:
    return (
        item.owned_pgid is not None
        and item.owned_sid is not None
        and item.owned_pgid > 1
        and item.owned_pgid == item.owned_sid == item.pid
    )


def _owned_dict(identity: ProcessIdentity) -> _OwnedProcess:
    return _OwnedProcess(
        pid=identity.pid,
        created_ms=identity.created_ms,
        start_ticks=identity.start_ticks,
        namespace=identity.namespace,
        owned_pgid=identity.owned_pgid,
        owned_sid=identity.owned_sid,
    )


def _owned_dict_from(item: _OwnedProcess) -> dict[str, int | str | None]:
    return {
        "pid": item.pid,
        "created_ms": item.created_ms,
        "start_ticks": item.start_ticks,
        "namespace": item.namespace,
        "owned_pgid": item.owned_pgid,
        "owned_sid": item.owned_sid,
        "tree_root": item.tree_root,
    }


def _encode_runtime(owned: list[_OwnedProcess], *, containment_open: bool) -> str:
    processes = [_owned_dict_from(item) for item in owned]
    if not containment_open:
        return json.dumps(processes)
    return json.dumps({"processes": processes, "containment": "open"})


def _runtime_state(payload: str | None) -> tuple[list[_OwnedProcess], bool]:
    """Return owned processes and whether unresolved containment is still open."""
    if not payload:
        return [], False
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError:
        return [], True
    if isinstance(raw, dict):
        processes = raw.get("processes")
        if not isinstance(processes, list):
            return [], True
        return _parse_owned_list(processes), raw.get("containment") == "open"
    if isinstance(raw, list):
        return _parse_owned_list(raw), False
    return [], True


def _parse_owned(payload: str | None) -> list[_OwnedProcess]:
    owned, _open = _runtime_state(payload)
    return owned


def _parse_owned_list(raw: list[object]) -> list[_OwnedProcess]:
    owned: list[_OwnedProcess] = []
    for item in raw:
        if not isinstance(item, dict) or "pid" not in item:
            continue
        namespace = item.get("namespace")
        try:
            owned.append(
                _OwnedProcess(
                    pid=int(item["pid"]),
                    created_ms=None if item.get("created_ms") is None else int(item["created_ms"]),
                    start_ticks=None
                    if item.get("start_ticks") is None
                    else int(item["start_ticks"]),
                    namespace=None if namespace is None else str(namespace),
                    owned_pgid=None if item.get("owned_pgid") is None else int(item["owned_pgid"]),
                    owned_sid=None if item.get("owned_sid") is None else int(item["owned_sid"]),
                    tree_root=None if item.get("tree_root") is None else int(item["tree_root"]),
                )
            )
        except (TypeError, ValueError):
            continue
    return owned
