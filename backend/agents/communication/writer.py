"""Communication Agent: LLM-written stakeholder updates (FR-6).

Writing for a non-technical reader is a paradigm language task, and it was being done by
`str.format()` over five fixed templates. Those templates could only ever say the same five
things: they could not reflect what was actually found, could not vary with severity beyond a
word swap, and could not describe a cause the enum did not already contain.

Two properties are *not* delegated to the model, because asking politely is not enforcement:

* **No internal identifiers.** Service names, pod names and metric names are passed to
  `guard_output(forbid_terms=...)`, which blocks the update if any appear. The prompt also
  asks — but the guardrail is what makes it true.
* **No invented cause.** The model receives the cause as a field and is told to say
  "investigation ongoing" when it is unknown. A fabricated cause reaching a customer call is
  the specific harm this agent could do, so the template fallback remains the safety net when
  the model is unavailable or its output is rejected.

The deterministic templates in `composer.py` are retained *as that fallback*, not as the
primary path. A stakeholder update that never arrives because a free-tier model was throttled
is a worse outcome than a plainer one that does.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from agents.communication.composer import compose
from agents.prompts.library import COMMUNICATION_UPDATE
from api.events import publish_event
from core.logging import get_logger
from db.enums import AgentMessageType
from db.repository import AgentRepository
from guardrails import GuardrailViolation, guard_output
from providers.base import LLMProvider

_log = get_logger(component="agent.communication")

_CAUSE_PLAIN = {
    "deploy_regression": "a recent change to the system",
    "resource_exhaustion": "the service running out of memory",
    "error_spike": "an elevated rate of failed requests",
    "latency_degradation": "unusually slow responses",
    "unknown": "unknown",
}

_ACTION_PLAIN = {
    "restart_pod": "restarting the affected instance",
    "scale_deployment": "adding capacity",
    "rollback_deploy": "reversing the recent change",
}


class StakeholderUpdate(BaseModel):
    """A plain-English update for a non-technical reader."""

    update: str = Field(
        description="Two or three sentences, plain language, no technical identifiers.",
        max_length=600,
    )


def _forbidden_terms(service: str, **detail: Any) -> list[str]:
    """Internal identifiers that must never appear in a stakeholder-facing message."""
    terms = [service]
    # The bare service word ("checkout" from "checkout-service") is deliberately NOT
    # forbidden: it is also ordinary English that a stakeholder update legitimately needs
    # ("customers cannot complete checkout"). Only the internal identifier is blocked.
    for key in ("pod_name", "action_type", "metric"):
        value = detail.get(key)
        if isinstance(value, str) and value:
            terms.append(value)
    return terms


async def write_update(
    provider: LLMProvider | None,
    *,
    phase: str,
    service: str,
    severity: str = "P3",
    **detail: Any,
) -> tuple[str, bool]:
    """Compose one update. Returns ``(text, used_llm)``.

    Falls back to the deterministic template on any failure — model unavailable, invalid
    structure, or a guardrail rejection. A guardrail rejection is a *success* of the guardrail,
    not an error state: the update still goes out, in the form that cannot leak.
    """
    if provider is None:
        return compose(phase, service=service, severity=severity, **detail), False

    cause = _CAUSE_PLAIN.get(detail.get("root_cause_category", "unknown"), "unknown")
    action = _ACTION_PLAIN.get(detail.get("action_type") or "", "none")
    prompt = COMMUNICATION_UPDATE.render(
        phase=phase, service=service, severity=severity, cause=cause, action=action
    )

    try:
        structured = await provider.complete_structured(
            prompt,
            schema=StakeholderUpdate,
            agent="communication",
            system=COMMUNICATION_UPDATE.system,
            prompt_ref=COMMUNICATION_UPDATE.ref,
        )
        checked = guard_output(
            structured.value.update,
            agent="communication",
            forbid_terms=_forbidden_terms(service, **detail),
        )
        return checked.text.strip(), True
    except GuardrailViolation as exc:
        # The model leaked an internal identifier. Fall back rather than send it.
        _log.warning("communication_guardrail_fallback", phase=phase, rule=exc.rule)
    except Exception as exc:  # noqa: BLE001 — a stakeholder update must still go out
        _log.warning("communication_llm_unavailable", phase=phase, error=str(exc))

    return compose(phase, service=service, severity=severity, **detail), False


async def post_update(
    session: AsyncSession,
    gateway: Any,
    *,
    incident_id: uuid.UUID,
    phase: str,
    service: str,
    severity: str = "P3",
    provider: LLMProvider | None = None,
    **detail: Any,
) -> str:
    """Write, persist, publish, and best-effort mirror an update to Slack."""
    text, used_llm = await write_update(
        provider, phase=phase, service=service, severity=severity, **detail
    )
    await AgentRepository(session).add_message(
        incident_id=incident_id,
        agent_name="communication",
        message_type=AgentMessageType.action,
        content=text,
        message_metadata={"phase": phase, "llm_generated": used_llm},
    )
    await publish_event(session, incident_id, "communication", {"phase": phase, "text": text})
    if gateway is not None:
        # Best-effort: an unavailable Slack server is a non-event for the incident.
        await gateway.call("slack", "post_message", {"text": f"[Aegis] {text}"})
    return text
