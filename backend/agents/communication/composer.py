"""Communication Agent: plain-English status updates at defined transitions (FR-6).

Deterministic templates — no jargon, no evidence dumps (Journey D: a stakeholder in a
customer call must understand it at a glance). Updates land on the dashboard as
communication agent_messages and, when the Slack MCP server is reachable, in Slack too.
Slack being down never blocks anything: the dashboard message is the system of record.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from api.events import publish_event
from db.enums import AgentMessageType
from db.repository import AgentRepository

_SEVERITY_WORDS = {
    "P1": "a critical issue",
    "P2": "a significant issue",
    "P3": "a minor issue",
    "P4": "a low-impact issue",
}

# phase -> template. Kept deliberately free of internal terminology.
_TEMPLATES: dict[str, str] = {
    "opened": (
        "We are looking into {severity_words} affecting {service}. "
        "Automated investigation has started; next update when we know the likely cause."
    ),
    "root_cause": (
        "Update on {service}: the likely cause has been identified — {cause_plain}. {action_plain}"
    ),
    "remediation_proposed": (
        "A fix for {service} has been prepared and is waiting for an engineer's approval: "
        "{fix_plain}."
    ),
    "remediation_executed": (
        "A fix has been applied to {service}: {fix_plain}. We are watching the service "
        "to confirm recovery."
    ),
    "resolved": "The issue affecting {service} is resolved. A summary will follow.",
}

_CAUSE_PLAIN = {
    "deploy_regression": "a recent code change appears to have broken the service",
    "resource_exhaustion": "the service is running out of memory and restarting itself",
    "error_spike": "the service is returning errors at an elevated rate",
    "latency_degradation": "the service is responding much slower than normal",
    "unknown": "still under investigation",
}

_FIX_PLAIN = {
    "restart_pod": "restarting the affected instance",
    "scale_deployment": "adding capacity to absorb the load",
    "rollback_deploy": "rolling back the recent change",
}


def compose(phase: str, *, service: str, severity: str = "P3", **detail: Any) -> str:
    """Render one plain-English update. Deterministic; unit-testable (FR-6.2 phases)."""
    template = _TEMPLATES[phase]
    cause = detail.get("root_cause_category", "unknown")
    action_type = detail.get("action_type")
    action_plain = ""
    if phase == "root_cause":
        if action_type:
            action_plain = f"Next step: {_FIX_PLAIN.get(action_type, 'a fix is being prepared')}."
        else:
            action_plain = "An engineer is reviewing the findings."
    return template.format(
        service=service,
        severity_words=_SEVERITY_WORDS.get(severity, "an issue"),
        cause_plain=_CAUSE_PLAIN.get(cause, _CAUSE_PLAIN["unknown"]),
        fix_plain=_FIX_PLAIN.get(action_type, "a corrective action"),
        action_plain=action_plain,
    )


async def post_update(
    session: AsyncSession,
    gateway: Any,
    *,
    incident_id: uuid.UUID,
    phase: str,
    service: str,
    severity: str = "P3",
    **detail: Any,
) -> str:
    """Compose + persist a dashboard update, then best-effort mirror it to Slack."""
    text = compose(phase, service=service, severity=severity, **detail)
    await AgentRepository(session).add_message(
        incident_id=incident_id,
        agent_name="communication",
        message_type=AgentMessageType.action,
        content=text,
        message_metadata={"phase": phase},
    )
    await publish_event(session, incident_id, "communication", {"phase": phase, "text": text})
    if gateway is not None:
        # Best-effort: an unavailable slack server is a non-event for the incident.
        await gateway.call("slack", "post_message", {"text": f"[Aegis] {text}"})
    return text
