from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from recantor.meeting_essence.contracts import (
    MeetingIntelligenceJobMetadata,
    StageOperation,
)
from recantor.meeting_essence.progress import EngineCheckpoint, StageCallTrace
from recantor.meeting_essence.prompt_loader import PromptBundle
from recantor.meeting_essence.provider import MeetingEssenceProvider
from recantor.meeting_essence.provider_errors import (
    ProviderErrorCategory,
    StageInvocationError,
    classify_provider_error,
)
from recantor.meeting_essence.stage_contracts import StageRequest, StageResponse
from recantor.meeting_essence.wire_payload import WirePayloadAudit
from recantor.meeting_essence.wire_serialize import serialized_utf8_byte_length


@dataclass
class InvokeConfig:
    max_attempts_per_stage: int
    provider_retry_backoff_seconds: float
    deadline_monotonic: float | None
    max_provider_call_seconds: float


def _quantum_left(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return deadline - time.monotonic()


def _remaining(deadline: float | None, limits_cap: float) -> float | None:
    if deadline is None:
        return limits_cap
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    return min(limits_cap, remaining)


def _observed_model(response: StageResponse) -> str | None:
    metadata = getattr(response, "metadata", None)
    if metadata is None:
        return None
    observed = getattr(metadata, "observed_model", None)
    if observed is None or observed == "":
        return None
    return str(observed)


async def invoke_stage(
    *,
    provider: MeetingEssenceProvider,
    bundle: PromptBundle,
    config: InvokeConfig,
    operation: StageOperation,
    request: StageRequest,
    job: MeetingIntelligenceJobMetadata,
    checkpoint: EngineCheckpoint,
    trace_stage_calls: list[StageCallTrace],
    audit: WirePayloadAudit,
    requested_model: str | None,
) -> StageResponse:
    started = datetime.now(tz=UTC)
    attempts: list[dict[str, str | None]] = []
    last_category = ProviderErrorCategory.UNKNOWN
    last_message = "provider call failed"
    quantum_expired = False
    serialized = serialized_utf8_byte_length(request)
    for attempt_index in range(config.max_attempts_per_stage):
        quantum_left = _quantum_left(config.deadline_monotonic)
        if quantum_left is not None and quantum_left <= 0:
            quantum_expired = True
            last_message = "engine wall clock budget hit"
            attempts.append({"category": "wall_clock_exhausted", "observed_model": None})
            break
        call_cap = config.max_provider_call_seconds
        bounded_by_quantum = quantum_left is not None and quantum_left <= call_cap
        timeout = call_cap if quantum_left is None else min(call_cap, quantum_left)
        task = asyncio.create_task(
            provider.run_stage(
                operation=operation,
                request=request,
                prompt_bundle=bundle,
                job=job,
            )
        )
        try:
            response = await asyncio.wait_for(task, timeout=timeout)
        except asyncio.CancelledError:
            if not task.done():
                task.cancel()
            raise
        except TimeoutError:
            provider_raised = (
                task.done() and not task.cancelled() and isinstance(task.exception(), TimeoutError)
            )
            if not task.done():
                task.cancel()
            cleanup = _remaining(config.deadline_monotonic, 0.05)
            if cleanup is not None and not bounded_by_quantum and not provider_raised:
                with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=cleanup)
            if bounded_by_quantum and not provider_raised:
                quantum_expired = True
                last_message = "engine wall clock budget hit"
                attempts.append({"category": "wall_clock_exhausted", "observed_model": None})
                break
            last_category = ProviderErrorCategory.TIMEOUT
            last_message = "provider call exceeded deadline"
            attempts.append({"category": last_category.value, "observed_model": None})
        except Exception as exc:  # noqa: BLE001 - classified, never re-raised raw
            sanitized = classify_provider_error(exc)
            last_category = sanitized.category
            last_message = sanitized.message
            attempts.append({"category": last_category.value, "observed_model": None})
            if not sanitized.retryable:
                break
        else:
            observed = _observed_model(response)
            attempts.append({"category": None, "observed_model": observed})
            finished = datetime.now(tz=UTC)
            trace_stage_calls.append(
                StageCallTrace(
                    operation=operation,
                    started_at=started,
                    finished_at=finished,
                    wire_audit=audit,
                    requested_model=requested_model,
                    observed_model=observed,
                    attempt_count=attempt_index + 1,
                    attempts=attempts,
                    serialized_utf8_bytes=serialized,
                )
            )
            checkpoint.stage_traces = trace_stage_calls
            return response
        if (
            not classify_provider_error(
                TimeoutError()
                if last_category == ProviderErrorCategory.TIMEOUT
                else RuntimeError(last_message)
            ).retryable
            and last_category != ProviderErrorCategory.TIMEOUT
        ):
            break
        if last_category not in {
            ProviderErrorCategory.TIMEOUT,
            ProviderErrorCategory.RATE_LIMIT,
        }:
            break
        backoff_budget = _remaining(
            config.deadline_monotonic, config.provider_retry_backoff_seconds
        )
        if backoff_budget is None or backoff_budget <= 0:
            break
        await asyncio.sleep(min(config.provider_retry_backoff_seconds, backoff_budget))
    trace_stage_calls.append(
        StageCallTrace(
            operation=operation,
            started_at=started,
            finished_at=datetime.now(tz=UTC),
            wire_audit=audit,
            requested_model=requested_model,
            error=last_message,
            error_category="wall_clock_exhausted" if quantum_expired else last_category.value,
            attempt_count=len(attempts),
            attempts=attempts,
            serialized_utf8_bytes=serialized,
        )
    )
    checkpoint.stage_traces = list(trace_stage_calls)
    if quantum_expired:
        checkpoint.last_error_category = None
        checkpoint.last_error_message = None
        raise StageInvocationError(
            ProviderErrorCategory.TIMEOUT,
            "engine wall clock budget hit",
            quantum_expired=True,
        )
    checkpoint.last_error_category = last_category.value
    checkpoint.last_error_message = last_message
    raise StageInvocationError(last_category, last_message, quantum_expired=False)
