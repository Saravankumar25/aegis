from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Response

from aegis.api.routers.access import sample_incidents, session_context
from aegis.api.routers.stream import _resume_point, poll_incident_events
from aegis.api.security import Principal, Role
from aegis.core.config import Settings
from aegis.core.errors import AuthorizationError


class _Incident:
    environment = "prod-a"


class _Incidents:
    async def get(self, incident_id: str) -> _Incident:
        assert incident_id == "inc_1"
        return _Incident()


class _EventType:
    value = "evidence"


class _Event:
    event_type = _EventType()

    def model_dump(self, *, mode: str) -> dict[str, Any]:
        assert mode == "json"
        return {"message": "observed"}


class _Store:
    async def events(
        self, incident_id: str, *, after_seq: int, limit: int
    ) -> list[tuple[int, _Event]]:
        assert (incident_id, after_seq, limit) == ("inc_1", 7, 3)
        return [(8, _Event()), (9, _Event()), (10, _Event())]


def _principal(*environments: str) -> Principal:
    return Principal(
        uid="user-1",
        email="operator@example.com",
        roles=frozenset({Role.VIEWER.value}),
        tenant_id="tenant-1",
        environments=frozenset(environments),
    )


def test_resume_point_never_moves_backwards() -> None:
    assert _resume_point("7", 4) == 7
    assert _resume_point("invalid", 4) == 4


@pytest.mark.asyncio
async def test_polling_is_replay_safe_bounded_and_environment_scoped() -> None:
    response = Response()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(horizon_store=_Store())))

    page = await poll_incident_events(
        "inc_1", response, request, _principal("prod-a"), _Incidents(), "7", 0, 2
    )

    assert [event.id for event in page.events] == [8, 9]
    assert page.last_event_id == 9
    assert page.has_more is True
    assert response.headers["last-event-id"] == "9"
    assert response.headers["cache-control"] == "private, no-store"

    with pytest.raises(AuthorizationError):
        await poll_incident_events(
            "inc_1", Response(), request, _principal("prod-b"), _Incidents(), None, 0, 2
        )


@pytest.mark.asyncio
async def test_access_and_sample_contracts_are_explicit_and_deterministic() -> None:
    settings = Settings(
        sample_mode_enabled=True,
        sample_tenant_id="sample-tenant",
        sample_environment_name="sample",
    )
    principal = _principal("prod-a")

    context = await session_context(principal, settings)
    first = await sample_incidents(principal, settings)
    second = await sample_incidents(principal, settings)

    assert context.tenant_id == "tenant-1"
    assert context.environments == ["prod-a"]
    assert first == second
    assert first.tenant_id == "sample-tenant"
    assert all(item.data_state == "sample" for item in first.items)
