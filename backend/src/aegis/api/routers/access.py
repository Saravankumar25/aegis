"""Authenticated identity scope and isolated deterministic exploration data."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter
from pydantic import BaseModel

from aegis.api.deps import RequireViewer, SettingsDep
from aegis.core.errors import NotFoundError

router = APIRouter(tags=["access"])


class AccessContext(BaseModel):
    uid: str
    email: str
    tenant_id: str
    roles: list[str]
    environments: list[str]
    sample_available: bool
    sample_tenant_id: str | None
    sample_environment: str | None


class SampleIncident(BaseModel):
    id: str
    version: int
    title: str
    severity: str
    state: str
    environment: str
    service: str
    signal: str
    summary: str
    learning_goal: str
    observed_at: datetime
    data_state: str = "sample"


class SampleIncidentPage(BaseModel):
    tenant_id: str
    environment: str
    data_state: str = "sample"
    items: list[SampleIncident]


@router.get("/session", response_model=AccessContext, summary="Authenticated access scope")
async def session_context(principal: RequireViewer, settings: SettingsDep) -> AccessContext:
    return AccessContext(
        uid=principal.uid,
        email=principal.email,
        tenant_id=principal.tenant_id,
        roles=sorted(principal.effective_roles),
        environments=sorted(principal.environments),
        sample_available=settings.sample_mode_enabled,
        sample_tenant_id=settings.sample_tenant_id if settings.sample_mode_enabled else None,
        sample_environment=(
            settings.sample_environment_name if settings.sample_mode_enabled else None
        ),
    )


@router.get(
    "/sample/incidents",
    response_model=SampleIncidentPage,
    summary="Deterministic isolated sample incidents",
)
async def sample_incidents(_: RequireViewer, settings: SettingsDep) -> SampleIncidentPage:
    if not settings.sample_mode_enabled:
        raise NotFoundError("sample mode is not available")
    environment = settings.sample_environment_name
    return SampleIncidentPage(
        tenant_id=settings.sample_tenant_id,
        environment=environment,
        items=[
            SampleIncident(
                id="LAT-SN-001",
                version=1,
                title="HomeTimelineService p99 latency above 1.5s",
                severity="P2",
                state="RECEIVED",
                environment=environment,
                service="home-timeline-service",
                signal="http_request_duration_seconds",
                summary="p99 for GET /home-timeline is 1.8s, baseline 0.4s",
                learning_goal="Trace a user-facing symptom through more than one dependency.",
                observed_at=datetime(2026, 1, 15, 9, 30, tzinfo=UTC),
            ),
            SampleIncident(
                id="AMB-REF-003",
                version=1,
                title="Intermittent gateway errors",
                severity="P3",
                state="RECEIVED",
                environment=environment,
                service="gateway",
                signal="http_requests_total",
                summary="Three short error bursts appeared in the last 30 minutes.",
                learning_goal="Choose an evidence window that does not hide intermittent behavior.",
                observed_at=datetime(2026, 1, 14, 16, 0, tzinfo=UTC),
            ),
        ],
    )


__all__ = ["AccessContext", "SampleIncidentPage", "router"]
