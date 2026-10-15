from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from recantor.meeting_essence.engine import EngineRunResult, EngineRunTrace, engine_trace_json


@dataclass
class SchedulerRunMetrics:
    dispatch_id: str
    session_id: str
    job_id: str | None = None
    attempt_id: str | None = None
    queue_wait_ms: float | None = None
    execution_ms: float | None = None
    extract_ms: float | None = None
    reconcile_ms: float | None = None
    check_ms: float | None = None
    render_ms: float | None = None
    first_result_ms: float | None = None
    final_result_ms: float | None = None
    provider_calls: int = 0
    cache_hits: int = 0
    retries: int = 0
    failures: int = 0
    observed_tokens: int | None = None
    processing_status: str | None = None
    served_from_cache: bool = False
    host_permit_busy: bool = False
    snapshot_content_hash: str | None = None
    counters: dict[str, int] = field(default_factory=dict)

    def to_safe_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


def safe_trace_projection(trace: EngineRunTrace) -> str:
    return engine_trace_json(trace)


def progress_token(checkpoint: object | None) -> str:
    if checkpoint is None:
        return ""
    acked = getattr(checkpoint, "acked_check_fingerprints", ()) or ()
    records = getattr(checkpoint, "atomic_records", ()) or ()
    return "|".join(
        str(item)
        for item in (
            int(bool(getattr(checkpoint, "extract_complete", False))),
            getattr(checkpoint, "extract_page_index", 0),
            getattr(checkpoint, "extract_fragment_cursor", 0),
            getattr(checkpoint, "source_pages_sent", 0),
            int(bool(getattr(checkpoint, "reconcile_complete", False))),
            getattr(checkpoint, "reconcile_page_index", 0),
            int(bool(getattr(checkpoint, "render_complete", False))),
            getattr(checkpoint, "render_record_cursor", 0),
            int(bool(getattr(checkpoint, "check_complete", False))),
            getattr(checkpoint, "check_page_index", 0),
            getattr(checkpoint, "source_check_cursor", 0),
            getattr(checkpoint, "output_check_cursor", 0),
            len(acked),
            len(records),
        )
    )


def useful_presentation(result: EngineRunResult | None) -> bool:
    """True only for a published layered result, not a withheld checkpoint draft."""
    if result is None or result.layered_result is None:
        return False
    layered = result.layered_result
    return bool(layered.overview_bullets and layered.topics)


def metrics_from_outcome(
    *,
    dispatch_id: str,
    session_id: str,
    started_at: datetime,
    finished_at: datetime,
    result: EngineRunResult | None,
    provider_calls: int,
    cache_hits: int,
    served_from_cache: bool,
    job_id: str | None = None,
    attempt_id: str | None = None,
    queued_at: datetime | None = None,
    retries: int = 0,
) -> SchedulerRunMetrics:
    elapsed_ms = (finished_at - started_at).total_seconds() * 1000.0
    status = None if result is None else result.processing_status.value
    stage_ms = _stage_totals(result)
    queue_wait = None
    if queued_at is not None:
        queue_wait = max(0.0, (started_at - queued_at).total_seconds() * 1000.0)
    failures = 0
    if result is not None:
        failures = sum(1 for call in result.trace.stage_calls if call.error_category)
    return SchedulerRunMetrics(
        dispatch_id=dispatch_id,
        session_id=session_id,
        job_id=job_id,
        attempt_id=attempt_id,
        queue_wait_ms=queue_wait,
        execution_ms=elapsed_ms,
        extract_ms=stage_ms.get("extract"),
        reconcile_ms=stage_ms.get("reconcile"),
        check_ms=stage_ms.get("check"),
        render_ms=stage_ms.get("render"),
        first_result_ms=None,
        final_result_ms=None,
        provider_calls=provider_calls,
        cache_hits=cache_hits,
        retries=retries,
        failures=failures,
        observed_tokens=None,
        processing_status=status,
        served_from_cache=served_from_cache,
        snapshot_content_hash=None if result is None else result.manifest.snapshot_content_hash,
    )


def metrics_input_fingerprint(
    *,
    manifest: object | None = None,
    source_fingerprint: str | None = None,
    coverage_fingerprint: str | None = None,
    stt_fingerprint: str | None = None,
) -> str:
    """Full logical input. A manifest fingerprint includes coverage and STT flags."""
    import hashlib

    if manifest is not None:
        from recantor.meeting_essence.identity import compute_input_fingerprint

        return compute_input_fingerprint(manifest)  # type: ignore[arg-type]
    raw = "|".join(
        (
            source_fingerprint or "",
            coverage_fingerprint or "",
            stt_fingerprint or "",
        )
    )
    return hashlib.sha256(raw.encode()).hexdigest()


async def persist_integrated_metrics(
    db: object,
    *,
    session_id: object,
    queued_at: datetime | None,
    finished_at: datetime,
    result: EngineRunResult | None,
    provider_calls: int,
    cache_hits: int,
    retries: int,
    source_fingerprint: str | None,
    config_fingerprint: str | None,
    served_from_cache: bool = False,
    input_fingerprint: str | None = None,
    job_id: str | None = None,
    attempt_id: str | None = None,
) -> None:
    """Fold new observations into one logical input. Tokens stay null when unobserved.

    Resume, a replay of the same attempt, and a cache response share this row.
    A different input fingerprint, including a coverage or STT change, is a new row.
    Event keys are the job, attempt, and progress token, not the mutable clock.
    """
    from sqlalchemy import select

    from recantor.meeting_essence.scheduling.models import MISummaryMetrics

    source = source_fingerprint or ""
    config = config_fingerprint or ""
    logical = input_fingerprint or ""
    if not logical and result is not None and result.manifest is not None:
        logical = metrics_input_fingerprint(manifest=result.manifest)
    job = job_id or ""
    attempt = attempt_id or ""
    cursor = progress_token(None if result is None else result.checkpoint)
    row = await db.scalar(  # type: ignore[attr-defined]
        select(MISummaryMetrics).where(
            MISummaryMetrics.session_id == session_id,
            MISummaryMetrics.input_fingerprint == logical,
            MISummaryMetrics.config_fingerprint == config,
        )
    )
    if row is None:
        row = MISummaryMetrics(
            session_id=session_id,
            input_fingerprint=logical,
            config_fingerprint=config,
            source_fingerprint=source,
            original_queued_at=queued_at,
        )
        db.add(row)  # type: ignore[attr-defined]
    if row.original_queued_at is None:
        row.original_queued_at = queued_at
    if source and not row.source_fingerprint:
        row.source_fingerprint = source
    seen = set(json.loads(row.observed_event_keys or "[]"))
    stage_ms, new_failure = _new_stage_totals(result, seen, attempt_id=attempt)
    if served_from_cache:
        cache_key = f"cache|{logical}|{config}|{attempt}"
        if cache_key not in seen:
            seen.add(cache_key)
            row.cache_hits = int(row.cache_hits or 0) + max(cache_hits, 1)
    elif cache_hits:
        hit_key = f"hits|{job}|{attempt}|{cache_hits}"
        if hit_key not in seen:
            seen.add(hit_key)
            row.cache_hits = int(row.cache_hits or 0) + cache_hits
    if provider_calls:
        call_key = f"calls|{job}|{attempt}|{cursor}|{provider_calls}"
        if call_key not in seen:
            seen.add(call_key)
            row.provider_calls = int(row.provider_calls or 0) + provider_calls
    if retries:
        retry_key = f"retry|{job}|{attempt}|{retries}"
        if retry_key not in seen:
            seen.add(retry_key)
            row.retries = int(row.retries or 0) + retries
    repeat_cache = served_from_cache and not stage_ms
    quantum_key = f"quantum|{job}|{attempt}|{cursor}"
    if not repeat_cache and quantum_key not in seen:
        seen.add(quantum_key)
        row.quantum_count = int(row.quantum_count or 0) + 1
    row.extract_ms = float(row.extract_ms or 0.0) + float(stage_ms.get("extract") or 0.0)
    row.reconcile_ms = float(row.reconcile_ms or 0.0) + float(stage_ms.get("reconcile") or 0.0)
    row.check_ms = float(row.check_ms or 0.0) + float(stage_ms.get("check") or 0.0)
    row.render_ms = float(row.render_ms or 0.0) + float(stage_ms.get("render") or 0.0)
    row.observed_tokens = None
    row.observed_event_keys = json.dumps(sorted(seen))
    if useful_presentation(result) and row.first_useful_at is None:
        row.first_useful_at = finished_at
    if (
        result is not None
        and result.processing_status.value == "succeeded"
        and result.layered_result is not None
        and row.final_result_at is None
    ):
        row.final_result_at = finished_at
    if new_failure:
        row.failures = int(row.failures or 0) + 1


def _trace_key(call: object, *, attempt_id: str, index: int) -> str:
    operation = getattr(getattr(call, "operation", None), "value", "")
    started = getattr(call, "started_at", None)
    return "|".join(
        (
            "stage",
            attempt_id,
            str(index),
            "" if started is None else started.isoformat(),
            str(operation),
            str(getattr(call, "error_category", None) or ""),
            str(getattr(call, "attempt_count", 0)),
            str(getattr(call, "serialized_utf8_bytes", 0) or 0),
        )
    )


def _new_stage_totals(
    result: EngineRunResult | None, seen: set[str], *, attempt_id: str
) -> tuple[dict[str, float], bool]:
    totals: dict[str, float] = {}
    new_failure = False
    if result is None or result.trace is None:
        return totals, False
    for index, call in enumerate(result.trace.stage_calls):
        key = _trace_key(call, attempt_id=attempt_id, index=index)
        if key in seen:
            continue
        seen.add(key)
        if call.error_category and call.error_category != "wall_clock_exhausted":
            new_failure = True
        if call.finished_at is None:
            continue
        elapsed = (call.finished_at - call.started_at).total_seconds() * 1000.0
        name = call.operation.value
        totals[name] = totals.get(name, 0.0) + elapsed
    return totals, new_failure


def _stage_totals(result: EngineRunResult | None) -> dict[str, float]:
    totals: dict[str, float] = {}
    if result is None:
        return totals
    for call in result.trace.stage_calls:
        if call.finished_at is None:
            continue
        elapsed = (call.finished_at - call.started_at).total_seconds() * 1000.0
        key = call.operation.value
        totals[key] = totals.get(key, 0.0) + elapsed
    return totals


def utcnow() -> datetime:
    return datetime.now(tz=UTC)
