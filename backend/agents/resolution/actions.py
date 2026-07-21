"""The remediation action catalog (FR-4.1): every action pre-classified into a tier,
defined together with its compensating action — there is no way to register a forward
action here without documenting its undo (CLAUDE.md §17).

Deterministic and side-effect free; the engine executes, this module only describes.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ActionSpec(BaseModel):
    """Static definition of one remediation action type."""

    action_type: str
    tier: int = Field(ge=1, le=3)  # 1 auto / 2 approve / 3 human-only (FR-4.1)
    target_resource_type: str
    mcp_server: str | None  # None = never machine-executed (Tier-3)
    mcp_tool: str | None
    description: str
    compensating: dict[str, Any]  # documented undo, stored verbatim on every action row


CATALOG: dict[str, ActionSpec] = {
    "restart_pod": ActionSpec(
        action_type="restart_pod",
        tier=1,
        target_resource_type="pod",
        mcp_server="k8s",
        mcp_tool="restart_pod",
        description="Delete a crash-looping pod so its Deployment recreates it.",
        compensating={
            "action_type": "none_required",
            "note": "Pod restart is non-destructive and self-healing; the Deployment "
            "controller owns recovery. Nothing to reverse.",
        },
    ),
    "scale_deployment": ActionSpec(
        action_type="scale_deployment",
        tier=2,
        target_resource_type="deployment",
        mcp_server="k8s",
        mcp_tool="scale_deployment",
        description="Change a deployment's replica count (latency/saturation relief).",
        compensating={
            "action_type": "scale_deployment",
            "note": "Scale back to the pre-action replica count recorded in "
            "parameters.previous_replicas at execution time.",
        },
    ),
    "rollback_deploy": ActionSpec(
        action_type="rollback_deploy",
        tier=3,
        target_resource_type="deployment",
        mcp_server=None,  # Tier-3: never machine-executed in V1.5
        mcp_tool=None,
        description="Roll back the most recent deploy. Proposed to a human with the "
        "suspect commit attached; Aegis never executes this itself in V1.5.",
        compensating={
            "action_type": "redeploy",
            "note": "Re-apply the rolled-back version through the normal deploy pipeline.",
        },
    ),
}
