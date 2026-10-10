"""Idempotent workflow dispatch behind a small, testable port."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any, Protocol

from aegis.core.config import Settings
from aegis.core.errors import ConfigError, ExternalServiceError
from aegis.core.logging import get_logger
from aegis.core.resilience import guarded_call
from aegis.persistence.dispatch_outbox import DispatchOutbox, DispatchRecord
from aegis.telemetry.metrics import safe, workflow_dispatches

log = get_logger(__name__)


class DispatchAdapter(Protocol):
    destination: str

    async def dispatch(self, record: DispatchRecord) -> str:
        """Start the workflow and return a stable external reference."""
        ...


class PostgresDispatchAdapter:
    """Acknowledge the existing queue path; the job row is already durable."""

    destination = "postgres"

    async def dispatch(self, record: DispatchRecord) -> str:
        return f"postgres:{record.job_id}"


class StepFunctionsDispatchAdapter:
    """Start one Standard Workflow execution per durable job id."""

    destination = "step_functions"

    def __init__(
        self,
        settings: Settings,
        *,
        client: Any | None = None,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        if not settings.step_functions_state_machine_arn.strip():
            raise ConfigError(
                "STEP_FUNCTIONS_STATE_MACHINE_ARN is required for Step Functions dispatch"
            )
        self._settings = settings
        self._client = client
        self._client_factory = client_factory

    def _aws(self) -> Any:
        if self._client is not None:
            return self._client
        if self._client_factory is not None:
            self._client = self._client_factory()
            return self._client
        try:
            import boto3
            from botocore.config import Config  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ConfigError("boto3 is required for Step Functions dispatch") from exc
        self._client = boto3.session.Session(region_name=self._settings.aws_region).client(
            "stepfunctions",
            config=Config(
                connect_timeout=self._settings.workflow_dispatch_timeout_s,
                read_timeout=self._settings.workflow_dispatch_timeout_s,
                retries={"max_attempts": 0},
            ),
        )
        return self._client

    async def dispatch(self, record: DispatchRecord) -> str:
        body = json.dumps(
            {
                "job_id": record.job_id,
                "incident_id": record.incident_id,
                "kind": record.kind,
                "payload": record.payload,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

        def _start() -> str:
            try:
                response = self._aws().start_execution(
                    stateMachineArn=self._settings.step_functions_state_machine_arn,
                    name=record.job_id,
                    input=body,
                )
                return str(response["executionArn"])
            except Exception as exc:
                # Standard Workflow names are idempotent for 90 days. A retry
                # after the first call landed is success, not a second run.
                code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
                if code == "ExecutionAlreadyExists":
                    return f"step-functions:{record.job_id}"
                raise ExternalServiceError(
                    "Step Functions start_execution failed",
                    context={"job_id": record.job_id, "error_type": type(exc).__name__},
                ) from exc

        async def _call() -> str:
            return await asyncio.to_thread(_start)

        return await guarded_call(
            _call,
            dependency="step-functions-dispatch",
            timeout_s=self._settings.workflow_dispatch_timeout_s,
            attempts=self._settings.workflow_dispatch_attempts,
        )


class WorkflowDispatcher:
    """Drain committed outbox rows through one configured adapter."""

    def __init__(self, outbox: DispatchOutbox, adapter: DispatchAdapter) -> None:
        self._outbox = outbox
        self._adapter = adapter

    async def dispatch_batch(self, dispatcher_id: str, *, limit: int) -> int:
        await self._outbox.recover_stale()
        dispatched = 0
        for _ in range(max(1, limit)):
            record = await self._outbox.claim(dispatcher_id, self._adapter.destination)
            if record is None:
                break
            try:
                external_ref = await self._adapter.dispatch(record)
            except Exception as exc:  # noqa: BLE001 - persisted for a bounded retry
                retry_in_s = min(300, 2 ** min(record.attempts, 8))
                settled = await self._outbox.mark_failed(
                    record.id,
                    f"{type(exc).__name__}: {exc}",
                    retry_in_s=retry_in_s,
                    dispatcher_id=dispatcher_id,
                    lease_version=record.lease_version,
                )
                if not settled:
                    log.warning(
                        "workflow dispatch failure settlement refused after lease changed",
                        dispatch_id=record.id,
                        dispatcher_id=dispatcher_id,
                    )
                outcome = "failed" if record.attempts >= record.max_attempts else "retrying"
                safe(
                    workflow_dispatches.labels(destination=record.destination, outcome=outcome).inc
                )
                log.warning(
                    "workflow dispatch failed",
                    job_id=record.job_id,
                    destination=record.destination,
                    attempt=record.attempts,
                    error_type=type(exc).__name__,
                )
                continue
            settled = await self._outbox.mark_dispatched(
                record.id,
                external_ref,
                dispatcher_id=dispatcher_id,
                lease_version=record.lease_version,
            )
            if not settled:
                log.warning(
                    "workflow dispatch success settlement refused after lease changed",
                    dispatch_id=record.id,
                    dispatcher_id=dispatcher_id,
                )
                continue
            safe(
                workflow_dispatches.labels(destination=record.destination, outcome="dispatched").inc
            )
            dispatched += 1
            log.info(
                "workflow dispatched",
                job_id=record.job_id,
                incident_id=record.incident_id,
                destination=record.destination,
                external_ref=external_ref,
            )
        return dispatched


def build_dispatch_adapter(settings: Settings) -> DispatchAdapter:
    if settings.workflow_dispatch_backend == "postgres":
        return PostgresDispatchAdapter()
    if settings.workflow_dispatch_backend == "step_functions":
        return StepFunctionsDispatchAdapter(settings)
    raise ConfigError(
        "unknown workflow dispatch backend",
        context={"backend": settings.workflow_dispatch_backend},
    )
