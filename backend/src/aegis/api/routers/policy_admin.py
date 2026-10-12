"""Governance surface: autonomy posture, kill switches, action registry.

Everything that changes safety posture requires ADMIN and is audited. The
registry endpoint exists so operators can see the real tool and action boundary
rather than trusting documentation (UX spec section 56).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from aegis.api.deps import RequireAdmin, RequireViewer, SettingsDep, require_environment
from aegis.api.security import Principal
from aegis.core.errors import AuthorizationError
from aegis.core.logging import get_logger
from aegis.domain.enums import ActionType
from aegis.policy.engine import POLICY_VERSION
from aegis.policy.store import PolicyStore
from aegis.policy.tiers import profile_for

log = get_logger(__name__)
router = APIRouter(prefix="/policy", tags=["governance"])

_VALID_SCOPES = {"global", "environment", "action_type", "service"}


@router.get("", summary="Current autonomy posture and policy configuration")
async def get_policy(
    principal: RequireViewer, settings: SettingsDep, request: Request
) -> dict[str, Any]:
    store = PolicyStore(request.app.state.db)
    state = await store.load_kill_switches()
    visible_environments = (
        set(state.environments)
        if "*" in principal.environments
        else set(state.environments) & set(principal.environments)
    )
    visible_services = (
        set(state.services)
        if "*" in principal.environments
        else {
            service for service in state.services
            if service.split(":", 1)[0] in principal.environments
        }
    )
    any_engaged = (
        state.global_engaged
        or bool(visible_environments)
        or bool(visible_services)
        or bool(state.action_types)
    )
    return {
        "policy_version": POLICY_VERSION,
        "autonomy": {
            "enabled": settings.autonomy_enabled,
            "mode": settings.autonomy_mode.value,
            "allowed_tiers": sorted(settings.allowed_tiers),
            "max_actions_per_hour": settings.autonomy_max_actions_per_hour,
        },
        "approval_ttl_seconds": settings.approval_ttl_seconds,
        "kill_switches": {
            "any_engaged": any_engaged,
            "global": state.global_engaged,
            "degraded": state.degraded,
            "environments": sorted(visible_environments),
            "services": sorted(visible_services),
            "action_types": sorted(a.value for a in state.action_types),
        },
    }


@router.get("/actions", summary="Action registry with risk classification")
async def action_registry(_: RequireViewer) -> dict[str, Any]:
    """The complete action vocabulary and how policy classifies each entry.

    Tier 3 entries are listed as ``executable: false`` because no executor is
    registered for them - policy cannot allow what does not exist.
    """
    items = []
    for action in ActionType:
        p = profile_for(action)
        items.append({
            "action_type": action.value,
            "risk_tier": int(p.tier),
            "executable": int(p.tier) < 3,
            "idempotent": p.idempotent,
            "reversible": p.reversible,
            "max_blast_radius": p.max_blast_radius,
            "min_confidence": p.min_confidence,
            "requires_verification": p.requires_verification,
            "description": p.description,
        })
    items.sort(key=lambda i: (i["risk_tier"], i["action_type"]))
    return {"items": items}


class KillSwitchIn(BaseModel):
    scope: str = Field(description="global | environment | action_type | service")
    target: str = Field(default="", max_length=256)
    reason: str = Field(min_length=1, max_length=500)


@router.post("/kill-switch", summary="Engage a kill switch")
async def engage_kill_switch(
    body: KillSwitchIn, principal: RequireAdmin, request: Request
) -> dict[str, Any]:
    if body.scope not in _VALID_SCOPES:
        from aegis.core.errors import ValidationError

        raise ValidationError(
            f"scope must be one of {sorted(_VALID_SCOPES)}", context={"scope": body.scope}
        )
    _validate_scope_target(body.scope, body.target)
    _authorize_scope(principal, body.scope, body.target)

    db = request.app.state.db
    store = PolicyStore(db)
    await store.engage(body.scope, body.target, reason=body.reason, actor=principal.uid)
    await db.execute(
        """
        INSERT INTO audit_log (actor, actor_type, event_type, resource_type,
                               resource_id, detail)
        VALUES ($1,'human','kill_switch_engaged','kill_switch',$2,$3)
        """,
        principal.uid, f"{body.scope}:{body.target}", {"reason": body.reason},
    )
    return {"engaged": True, "scope": body.scope, "target": body.target}


@router.delete("/kill-switch", summary="Release a kill switch")
async def release_kill_switch(
    scope: str, principal: RequireAdmin, request: Request, target: str = ""
) -> dict[str, Any]:
    if scope not in _VALID_SCOPES:
        from aegis.core.errors import ValidationError

        raise ValidationError(f"scope must be one of {sorted(_VALID_SCOPES)}")
    _validate_scope_target(scope, target)
    _authorize_scope(principal, scope, target)
    db = request.app.state.db
    await PolicyStore(db).release(scope, target, actor=principal.uid)
    await db.execute(
        """
        INSERT INTO audit_log (actor, actor_type, event_type, resource_type, resource_id)
        VALUES ($1,'human','kill_switch_released','kill_switch',$2)
        """,
        principal.uid, f"{scope}:{target}",
    )
    return {"engaged": False, "scope": scope, "target": target}


@router.get("/kill-switch", summary="List kill switches")
async def list_kill_switches(principal: RequireViewer, request: Request) -> dict[str, Any]:
    items = await PolicyStore(request.app.state.db).list_all()
    if "*" not in principal.environments:
        items = [
            item for item in items
            if item["scope"] == "environment" and item["target"] in principal.environments
            or item["scope"] == "service"
            and str(item["target"]).split(":", 1)[0] in principal.environments
        ]
    return {"items": items}


@router.get("/audit", summary="Audit log")
async def audit(
    principal: RequireAdmin,
    request: Request,
    limit: int = 100,
    incident_id: str | None = None,
) -> dict[str, Any]:
    db = request.app.state.db
    capped = max(1, min(limit, 500))
    if incident_id:
        rows = await db.fetch(
            """
            SELECT id, incident_id, actor, actor_type, event_type, resource_type,
                   resource_id, detail, correlation_id, created_at
            FROM audit_log a
            WHERE incident_id = $1
              AND EXISTS (SELECT 1 FROM incidents i WHERE i.id = a.incident_id
                          AND ($2::text[] @> ARRAY['*'] OR i.environment = ANY($2::text[])))
            ORDER BY created_at DESC LIMIT $3
            """,
            incident_id, list(principal.environments), capped,
        )
    else:
        rows = await db.fetch(
            """
            SELECT id, incident_id, actor, actor_type, event_type, resource_type,
                   resource_id, detail, correlation_id, created_at
            FROM audit_log a
            WHERE EXISTS (SELECT 1 FROM incidents i WHERE i.id = a.incident_id
                          AND ($1::text[] @> ARRAY['*'] OR i.environment = ANY($1::text[])))
               OR (a.incident_id IS NULL AND (
                    $1::text[] @> ARRAY['*']
                    OR (a.resource_type = 'kill_switch'
                        AND split_part(a.resource_id, ':', 1) IN ('environment', 'service')
                        AND split_part(a.resource_id, ':', 2) = ANY($1::text[]))
               ))
            ORDER BY created_at DESC LIMIT $2
            """,
            list(principal.environments), capped,
        )
    return {"items": [dict(r) for r in rows]}


def _authorize_scope(principal: Principal, scope: str, target: str) -> None:
    if scope in {"global", "action_type"}:
        if "*" not in principal.environments:
            raise AuthorizationError("tenant-wide policy authority is required")
        return
    if scope == "environment":
        require_environment(principal, target)
        return
    environment = target.split(":", 1)[0]
    require_environment(principal, environment)


def _validate_scope_target(scope: str, target: str) -> None:
    from aegis.core.errors import ValidationError

    if scope == "global" and target:
        raise ValidationError("global kill switches require an empty target")
    if scope == "environment" and (not target or target.strip() != target):
        raise ValidationError("environment kill switches require a canonical nonempty target")
    if scope == "service":
        parts = target.split(":")
        if len(parts) != 3 or not all(part and part.strip() == part for part in parts):
            raise ValidationError("service target must be '{environment}:{workload}:{service}'")
    if scope == "action_type":
        try:
            ActionType(target)
        except ValueError as exc:
            raise ValidationError("unknown action_type kill-switch target") from exc
