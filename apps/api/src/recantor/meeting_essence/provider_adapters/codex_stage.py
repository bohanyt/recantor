from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from recantor.codex_process import ProcessAdapter, ProcessSpec, SubprocessAdapter
from recantor.meeting_essence.contracts import MeetingIntelligenceJobMetadata, StageOperation
from recantor.meeting_essence.prompt_loader import PromptBundle
from recantor.meeting_essence.provider_adapters.spawn_gate import (
    current_pre_resume_gate,
    held_permit_handle,
    reset_held_permit,
    reset_pre_resume_gate,
    set_held_permit,
    set_pre_resume_gate,
)
from recantor.meeting_essence.provider_errors import ProviderErrorCategory, StageInvocationError
from recantor.meeting_essence.stage_contracts import (
    CheckStageResponse,
    ExtractStageResponse,
    ReconcileStageResponse,
    RenderStageResponse,
    StageRequest,
    StageResponse,
)
from recantor.meeting_essence.wire_serialize import serialized_json_text

_MAX_STDOUT_BYTES = 512 * 1024
_MAX_STDERR_BYTES = 32 * 1024
_DEFAULT_TIMEOUT_SECONDS = 90.0

_STAGE_RESPONSE: dict[StageOperation, type[BaseModel]] = {
    StageOperation.EXTRACT: ExtractStageResponse,
    StageOperation.RECONCILE: ReconcileStageResponse,
    StageOperation.CHECK: CheckStageResponse,
    StageOperation.RENDER: RenderStageResponse,
}


def _pre_resume_hook(pid: int) -> bool:
    gate = current_pre_resume_gate()
    if gate is None:
        return False
    return gate.admit_pid(pid)


def _uncancel_current_task() -> None:
    current = asyncio.current_task()
    if current is None:
        return
    while current.cancelling():
        current.uncancel()


@dataclass
class CodexStructuredStageProvider:
    """Codex JSON stage runner. Observed model stays null unless runtime exposes it."""

    codex_binary: str = "codex"
    model: str | None = None
    reasoning_effort: str | None = "high"
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    process_adapter: ProcessAdapter | None = None
    environment: dict[str, str] | None = None
    spawns_process_tree: bool = True
    _inflight: asyncio.Future[BaseModel] | None = field(default=None, init=False, repr=False)
    _cancel_event: threading.Event | None = field(default=None, init=False, repr=False)
    _inflight_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        from recantor.meeting_intelligence import _codex_parent_env

        self.process_adapter = self.process_adapter or SubprocessAdapter()
        self.environment = _codex_parent_env(self.environment)

    def _prompt_for(self, operation: StageOperation, prompt_bundle: PromptBundle) -> str:
        name = {
            StageOperation.EXTRACT: "extract",
            StageOperation.RECONCILE: "reconcile",
            StageOperation.CHECK: "check",
            StageOperation.RENDER: "render",
        }[operation]
        for item in prompt_bundle.prompts:
            if item.identity.name == name:
                return item.text
        msg = f"missing prompt asset for {operation.value}"
        raise StageInvocationError(ProviderErrorCategory.CONFIG, msg)

    def _exec_json_sync(
        self,
        *,
        instruction: str,
        payload: dict[str, Any],
        response_model: type[BaseModel],
        cancel_event: threading.Event | None = None,
    ) -> BaseModel:
        from recantor.meeting_intelligence import (
            CodexSubscriptionMeetingIntelligenceProvider,
            MeetingIntelligenceError,
            MeetingIntelligenceErrorCategory,
            _classify_process_failure,
            _controlled_exec_config,
            _exec_environment,
            _strict_filesystem_containment_supported,
        )

        if not _strict_filesystem_containment_supported():
            raise MeetingIntelligenceError(
                MeetingIntelligenceErrorCategory.CONTAINMENT,
                "strict filesystem containment is unavailable on this host",
            )
        probe = CodexSubscriptionMeetingIntelligenceProvider(
            process_adapter=self.process_adapter,
            codex_binary=self.codex_binary,
            environment=self.environment,
        )
        probe._check_chatgpt_auth_sync(cancel_event)
        import tempfile

        schema = response_model.model_json_schema()
        with tempfile.TemporaryDirectory(prefix="recantor-codex-stage-") as temp_dir:
            schema_path = Path(temp_dir) / "stage.schema.json"
            schema_path.write_text(json.dumps(schema, separators=(",", ":")), encoding="utf-8")
            exec_env = _exec_environment(self.environment or {}, temp_dir)
            argv: list[str] = [
                self.codex_binary,
                "exec",
                "--ignore-user-config",
                "--ignore-rules",
                "--ephemeral",
                "--skip-git-repo-check",
                "--output-schema",
                str(schema_path),
            ]
            argv.extend(
                _controlled_exec_config(
                    temp_dir,
                    (self.environment or {}).get("PATH") or (self.environment or {}).get("Path"),
                )
            )
            if self.model:
                argv.extend(["--model", self.model])
            if self.reasoning_effort:
                argv.extend(["--config", f'model_reasoning_effort="{self.reasoning_effort}"'])
            argv.append(instruction)
            stdin = serialized_json_text(payload).encode("utf-8")
            result = probe._run(
                ProcessSpec(
                    argv=tuple(argv),
                    stdin=stdin,
                    env=exec_env,
                    cwd=temp_dir,
                    timeout_seconds=self.timeout_seconds,
                    stdout_limit_bytes=_MAX_STDOUT_BYTES,
                    stderr_limit_bytes=_MAX_STDERR_BYTES,
                    cancel_event=cancel_event,
                    pre_resume_hook=_pre_resume_hook,
                )
            )
        if (
            result.returncode != 0
            or result.timed_out
            or result.cancelled
            or result.containment_failed
            or result.drain_incomplete
        ):
            category = _classify_process_failure(result)
            raise MeetingIntelligenceError(
                category,
                f"Codex stage invocation failed ({category.value})",
            )
        if result.stdout_truncated:
            raise MeetingIntelligenceError(
                MeetingIntelligenceErrorCategory.INVALID_OUTPUT,
                "Codex stage output exceeded capture limit",
            )
        try:
            raw = json.loads(result.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MeetingIntelligenceError(
                MeetingIntelligenceErrorCategory.INVALID_OUTPUT,
                "Codex returned invalid JSON",
            ) from exc
        try:
            return response_model.model_validate(raw)
        except ValidationError as exc:
            raise MeetingIntelligenceError(
                MeetingIntelligenceErrorCategory.INVALID_OUTPUT,
                "Codex stage output failed validation",
            ) from exc

    async def _wait_executor_future(
        self,
        future: asyncio.Future[BaseModel],
        *,
        absorb_caller_cancel: bool,
    ) -> None:
        wrapped = asyncio.wrap_future(future)
        while not future.done():
            try:
                await asyncio.shield(wrapped)
                break
            except asyncio.CancelledError:
                if not absorb_caller_cancel:
                    raise
                _uncancel_current_task()
        if future.done():
            with contextlib.suppress(Exception):
                future.result()

    async def run_stage(
        self,
        *,
        operation: StageOperation,
        request: StageRequest,
        prompt_bundle: PromptBundle,
        job: MeetingIntelligenceJobMetadata,
    ) -> StageResponse:
        from recantor.meeting_intelligence import MeetingIntelligenceError

        del job
        with self._inflight_lock:
            prior = self._inflight
        if prior is not None and not prior.done():
            await self._wait_executor_future(prior, absorb_caller_cancel=True)
        response_type = _STAGE_RESPONSE[operation]
        instruction = self._prompt_for(operation, prompt_bundle)
        payload = request.model_dump(mode="json")
        cancel_event = threading.Event()
        loop = asyncio.get_running_loop()
        gate_snapshot = current_pre_resume_gate()
        held_snapshot = held_permit_handle()

        def _run_sync() -> BaseModel:
            gate_token = set_pre_resume_gate(gate_snapshot)
            held_token = set_held_permit(held_snapshot)
            try:
                return self._exec_json_sync(
                    instruction=instruction,
                    payload=payload,
                    response_model=response_type,
                    cancel_event=cancel_event,
                )
            finally:
                reset_held_permit(held_token)
                reset_pre_resume_gate(gate_token)

        ctx = contextvars.copy_context()
        future: asyncio.Future[BaseModel] = loop.run_in_executor(None, ctx.run, _run_sync)
        with self._inflight_lock:
            self._inflight = future
            self._cancel_event = cancel_event
        cancelled = False
        parsed: BaseModel | None = None
        try:
            while not future.done():
                try:
                    await asyncio.shield(asyncio.wrap_future(future))
                    break
                except asyncio.CancelledError:
                    cancelled = True
                    cancel_event.set()
                    _uncancel_current_task()
                    break
            if not cancelled:
                parsed = future.result()
        except MeetingIntelligenceError as exc:
            from recantor.meeting_intelligence import MeetingIntelligenceErrorCategory

            category = {
                MeetingIntelligenceErrorCategory.CONFIGURATION: ProviderErrorCategory.CONFIG,
                MeetingIntelligenceErrorCategory.UNAVAILABLE: ProviderErrorCategory.UNAVAILABLE,
                MeetingIntelligenceErrorCategory.UNAUTHENTICATED: ProviderErrorCategory.AUTH,
                MeetingIntelligenceErrorCategory.UNSUPPORTED_AUTH_MODE: ProviderErrorCategory.AUTH,
                MeetingIntelligenceErrorCategory.RATE_LIMIT: ProviderErrorCategory.RATE_LIMIT,
                MeetingIntelligenceErrorCategory.MODEL_UNAVAILABLE: (
                    ProviderErrorCategory.UNAVAILABLE
                ),
                MeetingIntelligenceErrorCategory.TIMEOUT: ProviderErrorCategory.TIMEOUT,
                MeetingIntelligenceErrorCategory.CANCELLED: ProviderErrorCategory.CANCELLED,
                MeetingIntelligenceErrorCategory.BUSY: ProviderErrorCategory.PROVIDER_BUSY,
                MeetingIntelligenceErrorCategory.INVALID_OUTPUT: (
                    ProviderErrorCategory.INVALID_OUTPUT
                ),
                MeetingIntelligenceErrorCategory.CONTAINMENT: ProviderErrorCategory.CONTAINMENT,
                MeetingIntelligenceErrorCategory.PROCESS_FAILED: ProviderErrorCategory.UNKNOWN,
            }.get(exc.category, ProviderErrorCategory.UNKNOWN)
            raise StageInvocationError(category, exc.category.value) from exc
        finally:
            if future.done():
                with self._inflight_lock:
                    if self._inflight is future:
                        self._inflight = None
                        self._cancel_event = None
        if cancelled:
            raise asyncio.CancelledError
        assert parsed is not None
        return parsed

    async def aclose(self) -> None:
        """Wait until the in-flight thread has left the process adapter."""
        with self._inflight_lock:
            event = self._cancel_event
            future = self._inflight
        if event is not None:
            event.set()
        if future is None:
            return
        try:
            await asyncio.shield(asyncio.wrap_future(future))
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        with self._inflight_lock:
            if self._inflight is future and future.done():
                self._inflight = None
                self._cancel_event = None
