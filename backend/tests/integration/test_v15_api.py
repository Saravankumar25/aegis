"""Integration tests: V1.5 API — approvals RBAC, kill switch, breaker, memory gate."""

from __future__ import annotations

import datetime
import uuid

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.security import hash_password
from db.enums import AlertSource, IncidentState, RemediationStatus, Severity, UserRole
from db.models import Incident, MemorySummary, RemediationAction, User
from db.repository import IncidentRepository
from memory.store import recall


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


async def _login(client: httpx.AsyncClient, session: AsyncSession, role: UserRole) -> None:
    email = f"{role}@example.com"
    existing = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if existing is None:
        session.add(User(email=email, hashed_password=hash_password("test-password-9"), role=role))
        await session.commit()
    response = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "test-password-9"}
    )
    assert response.status_code == 200


async def _proposed_action(
    session: AsyncSession, service="checkout-service"
) -> tuple[Incident, RemediationAction]:
    repo = IncidentRepository(session)
    incident, _ = await repo.upsert_incident(
        external_alert_id=f"v15-{uuid.uuid4().hex[:8]}",
        alert_source=AlertSource.prometheus,
        title=f"{service} needs scaling",
        service_name=service,
        severity=Severity.P2,
    )
    await repo.record_transition(
        incident, IncidentState.investigating, actor_type="agent", actor_id="t"
    )
    await repo.record_transition(
        incident, IncidentState.hypothesis_formed, actor_type="agent", actor_id="t"
    )
    await repo.record_transition(
        incident, IncidentState.remediation_proposed, actor_type="agent", actor_id="t"
    )
    action = RemediationAction(
        incident_id=incident.id,
        tier=2,
        action_type="scale_deployment",
        target_resource_type="deployment",
        target_resource_id=f"meridian/deployment/{service}",
        idempotency_key=uuid.uuid4().hex,
        parameters={"name": service, "replicas_delta": 1},
        compensating_action={"action_type": "scale_deployment", "note": "scale back"},
        reasoning="test proposal",
        blast_radius={"dependents": [], "count": 0},
        status=RemediationStatus.proposed,
        proposed_at=_now(),
        expires_at=_now() + datetime.timedelta(minutes=30),
        shadow=False,
    )
    session.add(action)
    await session.commit()
    return incident, action


async def test_viewer_cannot_approve(api_client, session):
    incident, action = await _proposed_action(session)
    await _login(api_client, session, UserRole.viewer)
    response = await api_client.post(
        f"/api/v1/incidents/{incident.id}/approvals",
        json={"action_id": str(action.id), "decision": "approved"},
    )
    assert response.status_code == 403  # server-side enforcement (ESD §8)


async def test_oncall_approval_flow(api_client, session):
    incident, action = await _proposed_action(session)
    await _login(api_client, session, UserRole.on_call_engineer)

    queue = (await api_client.get("/api/v1/approvals")).json()
    assert any(item["action"]["id"] == str(action.id) for item in queue)

    response = await api_client.post(
        f"/api/v1/incidents/{incident.id}/approvals",
        json={"action_id": str(action.id), "decision": "approved", "notes": "looks right"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    await session.refresh(incident)
    assert incident.state == IncidentState.remediation_approved

    # Double-decide: proposal is no longer pending.
    again = await api_client.post(
        f"/api/v1/incidents/{incident.id}/approvals",
        json={"action_id": str(action.id), "decision": "approved"},
    )
    assert again.status_code == 409


async def test_rejection_is_terminal_not_retried(api_client, session):
    incident, action = await _proposed_action(session)
    await _login(api_client, session, UserRole.on_call_engineer)
    response = await api_client.post(
        f"/api/v1/incidents/{incident.id}/approvals",
        json={"action_id": str(action.id), "decision": "rejected", "notes": "wrong fix"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"  # FR-5.2: flagged, never auto-retried
    # And the incident can still be resolved manually (rejected → manual fix path).
    resolve = await api_client.post(f"/api/v1/incidents/{incident.id}/resolve")
    assert resolve.status_code == 200
    assert resolve.json()["state"] == "resolved"


async def test_expired_proposal_cannot_be_approved(api_client, session):
    incident, action = await _proposed_action(session)
    action.expires_at = _now() - datetime.timedelta(minutes=1)
    await session.commit()
    await _login(api_client, session, UserRole.on_call_engineer)
    response = await api_client.post(
        f"/api/v1/incidents/{incident.id}/approvals",
        json={"action_id": str(action.id), "decision": "approved"},
    )
    assert response.status_code == 409
    assert response.json()["error_code"] == "proposal_expired"


async def test_kill_switch_roles(api_client, session):
    await _login(api_client, session, UserRole.on_call_engineer)
    engage = await api_client.post("/api/v1/kill-switch", json={"engaged": True})
    assert engage.status_code == 200 and engage.json()["engaged"] is True
    # on_call may engage but NOT disengage.
    disengage = await api_client.post("/api/v1/kill-switch", json={"engaged": False})
    assert disengage.status_code == 403
    await _login(api_client, session, UserRole.admin)
    disengage = await api_client.post("/api/v1/kill-switch", json={"engaged": False})
    assert disengage.status_code == 200 and disengage.json()["engaged"] is False


async def test_breaker_status_and_admin_clear(api_client, session):
    from core.config import get_settings
    from safety.circuit_breaker.breaker import record_tier1_execution

    for _ in range(get_settings().breaker_max_tier1_in_window):
        await record_tier1_execution(session)
    await session.commit()

    await _login(api_client, session, UserRole.viewer)
    status = (await api_client.get("/api/v1/circuit-breaker/status")).json()
    assert status["tripped"] is True
    clear_as_viewer = await api_client.post("/api/v1/circuit-breaker/clear")
    assert clear_as_viewer.status_code == 403

    await _login(api_client, session, UserRole.admin)
    cleared = (await api_client.post("/api/v1/circuit-breaker/clear")).json()
    assert cleared["tripped"] is False


async def test_resolve_drafts_memory_and_approval_gates_recall(api_client, session):
    incident, action = await _proposed_action(session, service="payment-service")
    await _login(api_client, session, UserRole.on_call_engineer)
    resolve = await api_client.post(f"/api/v1/incidents/{incident.id}/resolve")
    assert resolve.status_code == 200

    pending = (await api_client.get("/api/v1/memory/pending")).json()
    drafts = [p for p in pending if p["incident_id"] == str(incident.id)]
    assert len(drafts) == 1
    summary_id = drafts[0]["id"]

    # Unapproved drafts are NOT retrievable (FR-7.2).
    assert await recall(session, service_name="payment-service") == []

    approve = await api_client.post(
        f"/api/v1/memory/{summary_id}/approve",
        json={"edits": {"fix": "scaled payment-service and tuned pool size"}},
    )
    assert approve.status_code == 200
    assert approve.json()["fix"] == "scaled payment-service and tuned pool size"

    session.expire_all()
    memories = await recall(session, service_name="payment-service")
    assert len(memories) == 1  # approved → retrievable, compound-key scoped (FR-7.3)
    assert await recall(session, service_name="checkout-service") == []


async def test_memory_approve_rejects_unknown_edit_fields(api_client, session):
    incident, _ = await _proposed_action(session)
    await _login(api_client, session, UserRole.on_call_engineer)
    await api_client.post(f"/api/v1/incidents/{incident.id}/resolve")
    pending = (await api_client.get("/api/v1/memory/pending")).json()
    summary_id = next(p["id"] for p in pending if p["incident_id"] == str(incident.id))
    bad = await api_client.post(
        f"/api/v1/memory/{summary_id}/approve", json={"edits": {"root_cause": "x", "nope": "y"}}
    )
    assert bad.status_code == 422
    # Still pending — the bad request must not have half-approved it.
    still = (
        await session.execute(
            select(MemorySummary).where(MemorySummary.id == uuid.UUID(summary_id))
        )
    ).scalar_one()
    assert still.approved_by is None
