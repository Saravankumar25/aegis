"""LangGraph Supervisor graph: triage → correlation → rca → observer (ESD §24, §9).

Agents never call each other — routing lives exclusively in this graph's edges (Supervisor
pattern). Node bodies delegate to the pure agent cores (`agents/*`); persistence and SSE
publication happen in one defined side-effect helper per node (ESD §4: effects isolated at
defined points). The Observer can send the investigation back to RCA exactly once with
injection-flagged evidence excluded; a second rejection finalizes as low-confidence rather
than looping (bounded work per incident, ESD §15).
"""

from __future__ import annotations

import json
import uuid
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph
from sqlalchemy.ext.asyncio import async_sessionmaker

from agents.communication.composer import post_update
from agents.correlation.collector import collect_evidence
from agents.evidence import EvidenceStore
from agents.observer.validator import review
from agents.rca.engine import RCAResult, run_rca
from agents.triage.classifier import classify_severity
from api.events import publish_event
from core.logging import get_logger
from db.enums import ActorType, AgentMessageType, EvidenceType, IncidentState
from db.repository import AgentRepository, AuditRepository, IncidentRepository
from memory.store import recall
from rag.store import search_runbooks


class InvestigationState(TypedDict, total=False):
    incident_id: str
    service_name: str
    title: str
    severity: str
    evidence_store: Any  # EvidenceStore (kept opaque to LangGraph serialization)
    correlation_summary: str
    runbook_context: str
    rca_result: Any  # RCAResult
    observer_verdict: Any  # ObserverVerdict
    revision_count: int
    tokens_used: int
    final_state: str


class InvestigationServices:
    """Everything a node needs to cause its defined side effects."""

    def __init__(self, sessionmaker: async_sessionmaker, gateway: Any, provider: Any) -> None:
        self.sessionmaker = sessionmaker
        self.gateway = gateway
        self.provider = provider


def build_graph(services: InvestigationServices):
    """Compile the MVP investigation graph."""

    async def _persist(
        incident_id: str,
        agent: str,
        *,
        message: str,
        structured: dict | None = None,
        confidence: float | None = None,
        model: str | None = None,
        tokens: int | None = None,
        cost: float | None = None,
        ensemble_pass: int | None = None,
        citations: list[dict] | None = None,
        event: str = "agent_step",
    ) -> None:
        """The single side-effect point: step + message + citations + SSE, one transaction."""
        async with services.sessionmaker() as session:
            agents_repo = AgentRepository(session)
            step = await agents_repo.add_step(
                incident_id=uuid.UUID(incident_id),
                agent_name=agent,
                ensemble_pass_index=ensemble_pass,
                output_summary=message[:2000],
                structured_output=structured,
                confidence=confidence,
                model_used=model,
                tokens_used=tokens,
                cost_usd=cost,
            )
            await agents_repo.add_message(
                incident_id=uuid.UUID(incident_id),
                agent_name=agent,
                message_type=AgentMessageType.reasoning,
                content=message[:4000],
            )
            for citation in citations or []:
                await agents_repo.add_citation(
                    agent_step_id=step.id,
                    evidence_type=EvidenceType(citation["evidence_type"]),
                    evidence_ref=citation["evidence_ref"],
                    evidence_snippet_redacted=citation.get("snippet"),
                    validated_by_observer=citation.get("validated", False),
                )
            await publish_event(
                session,
                uuid.UUID(incident_id),
                event,
                {"agent": agent, "summary": message[:300]},
            )
            await session.commit()

    async def triage(state: InvestigationState) -> InvestigationState:
        severity = classify_severity(state["service_name"], "error_rate")
        message = (
            f"Triage: {state['service_name']} classified {severity} "
            f"(ingestion severity {state['severity']}); handing off to correlation."
        )
        await _persist(
            state["incident_id"], "triage", message=message, structured={"severity": str(severity)}
        )
        # FR-6.1: first plain-English update within 2 minutes of incident creation.
        async with services.sessionmaker() as session:
            await post_update(
                session,
                services.gateway,
                incident_id=uuid.UUID(state["incident_id"]),
                phase="opened",
                service=state["service_name"],
                severity=state["severity"],
            )
            await session.commit()
        return state

    async def correlation(state: InvestigationState) -> InvestigationState:
        store, summary = await collect_evidence(services.gateway, state["service_name"])
        message = (
            f"Correlation: gathered {len(store.items)} evidence item(s), "
            f"{len(store.gaps)} source gap(s): {store.gaps or 'none'}."
        )
        await _persist(
            state["incident_id"],
            "correlation",
            message=message,
            structured=json.loads(summary),
        )
        return {**state, "evidence_store": store, "correlation_summary": summary}

    async def rca(state: InvestigationState) -> InvestigationState:
        store: EvidenceStore = state["evidence_store"]
        async with services.sessionmaker() as session:
            hits = await search_runbooks(session, f"{state['service_name']} {state['title']}", k=2)
            memories = await recall(session, service_name=state["service_name"])
        runbook_context = "\n---\n".join(f"[{r.title}] {r.content[:400]}" for r, _score in hits)
        if memories:
            # FR-3.3/FR-7.3: approved past-incident summaries, compound-key scoped.
            runbook_context += (
                "\n---\nPast approved incident memories for this service:\n"
                + "\n".join(
                    f"- [{m.incident_type}] symptom: {m.symptom}; cause: {m.root_cause}; "
                    f"fix: {m.fix}"
                    for m in memories
                )
            )
        result: RCAResult = await run_rca(
            services.provider,
            service=state["service_name"],
            title=state["title"],
            store=store,
            runbook_context=runbook_context,
            tokens_already_used=state.get("tokens_used", 0),
        )
        for i, ensemble in enumerate(result.passes):
            await _persist(
                state["incident_id"],
                "rca",
                message=f"Ensemble pass {i}: {ensemble.root_cause_category} "
                f"(confidence {ensemble.confidence})",
                structured=ensemble.model_dump(),
                confidence=ensemble.confidence,
                model=services.provider.name,
                ensemble_pass=i,
            )
        citations = []
        for claim in result.claims:
            item = store.get(str(claim.get("evidence_id", "")))
            if item is not None:
                citations.append(
                    {
                        "evidence_type": item.type,
                        "evidence_ref": item.ref,
                        "snippet": item.summary[:500],
                        "validated": False,
                    }
                )
        message = (
            f"RCA hypothesis: {result.hypothesis} "
            f"[category={result.root_cause_category}, confidence={result.confidence}, "
            f"agreement={result.agreement_score}"
            f"{', LOW CONFIDENCE' if result.low_confidence else ''}"
            f"{', BUDGET DEGRADED' if result.budget_degraded else ''}]"
        )
        await _persist(
            state["incident_id"],
            "rca",
            message=message,
            structured=result.model_dump(exclude={"passes"}),
            confidence=result.confidence,
            model=services.provider.name,
            tokens=result.tokens_used,
            cost=result.cost_usd,
            citations=citations,
            event="hypothesis",
        )
        return {
            **state,
            "rca_result": result,
            "tokens_used": state.get("tokens_used", 0) + result.tokens_used,
        }

    async def observer(state: InvestigationState) -> InvestigationState:
        store: EvidenceStore = state["evidence_store"]
        result: RCAResult = state["rca_result"]
        verdict = review(result.claims, store)
        message = (
            f"Observer: {'approved' if verdict.approved else 'REJECTED'} — {verdict.notes} "
            f"(flagged evidence: {[f['evidence_id'] for f in verdict.flagged_evidence] or 'none'})"
        )
        await _persist(
            state["incident_id"],
            "observer",
            message=message,
            structured=verdict.model_dump(),
            event="observer_verdict",
        )
        return {**state, "observer_verdict": verdict}

    async def revise(state: InvestigationState) -> InvestigationState:
        """Strip injection-flagged evidence before the single allowed RCA retry (FR-8.1)."""
        store: EvidenceStore = state["evidence_store"]
        verdict = state["observer_verdict"]
        poisoned = {f["evidence_id"] for f in verdict.flagged_evidence}
        store.items = [i for i in store.items if i.id not in poisoned]
        for evidence_id in poisoned:
            store.note_gap(evidence_id, "excluded by observer (injection screen)")
        await _persist(
            state["incident_id"],
            "observer",
            message=f"Revision requested: excluded {sorted(poisoned) or 'no'} evidence, "
            f"re-running RCA once.",
        )
        return {**state, "revision_count": state.get("revision_count", 0) + 1}

    async def finalize(state: InvestigationState) -> InvestigationState:
        verdict = state.get("observer_verdict")
        result: RCAResult | None = state.get("rca_result")
        approved = bool(verdict and verdict.approved)
        async with services.sessionmaker() as session:
            incidents = IncidentRepository(session)
            incident = await incidents.get(uuid.UUID(state["incident_id"]))
            if incident is not None and incident.state == IncidentState.investigating:
                await incidents.record_transition(
                    incident,
                    IncidentState.hypothesis_formed,
                    actor_type=ActorType.agent,
                    actor_id="orchestrator",
                )
            if approved and result is not None:
                # Stamp the surviving citations as observer-validated (FR-8.1).
                from sqlalchemy import update

                from db.models import AgentStep, EvidenceCitation

                step_ids = (
                    (
                        await session.execute(
                            AgentStep.__table__.select()
                            .with_only_columns(AgentStep.id)
                            .where(
                                AgentStep.incident_id == uuid.UUID(state["incident_id"]),
                                AgentStep.agent_name == "rca",
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if step_ids:
                    await session.execute(
                        update(EvidenceCitation)
                        .where(EvidenceCitation.agent_step_id.in_(step_ids))
                        .values(validated_by_observer=True)
                    )
            await AuditRepository(session).write(
                actor_type=ActorType.agent,
                actor_id="orchestrator",
                action="investigation_finalized",
                target=f"incident/{state['incident_id']}",
                incident_id=uuid.UUID(state["incident_id"]),
                audit_metadata={
                    "approved": approved,
                    "low_confidence": bool(result and result.low_confidence),
                    "tokens_used": state.get("tokens_used", 0),
                },
            )
            await publish_event(
                session,
                uuid.UUID(state["incident_id"]),
                "investigation_complete",
                {"approved": approved},
            )
            await session.commit()
        # FR-6.2: root-cause-identified update, only for a validated hypothesis.
        if approved and result is not None:
            from agents.resolution.actions import recommend_action

            spec = recommend_action(result.root_cause_category)
            async with services.sessionmaker() as session:
                await post_update(
                    session,
                    services.gateway,
                    incident_id=uuid.UUID(state["incident_id"]),
                    phase="root_cause",
                    service=state["service_name"],
                    severity=state["severity"],
                    root_cause_category=result.root_cause_category,
                    action_type=spec.action_type if spec else None,
                )
                await session.commit()
        get_logger(incident_id=state["incident_id"]).info(
            "investigation_complete", approved=approved
        )
        return {**state, "final_state": "hypothesis_formed"}

    async def resolution(state: InvestigationState) -> InvestigationState:
        """V1.5 Resolution Agent node: propose (and gate-checked Tier-1 execute)."""
        from agents.resolution.engine import execute_action, propose_remediation
        from db.enums import RemediationStatus

        verdict = state.get("observer_verdict")
        result: RCAResult | None = state.get("rca_result")
        approved = bool(verdict and verdict.approved)
        if result is None:
            return state

        # Pick a concrete unhealthy pod for pod-targeted actions.
        target_pod: str | None = None
        pods_result = await services.gateway.call("k8s", "list_pods", {})
        if pods_result.get("ok"):

            def unhealthy(pod: dict) -> bool:
                ready, _, total = pod["ready"].partition("/")
                return pod["restarts"] > 0 or ready != total

            candidates = [
                p for p in pods_result["data"] if p["name"].startswith(state["service_name"])
            ]
            target_pod = next(
                (p["name"] for p in candidates if unhealthy(p)),
                candidates[0]["name"] if candidates else None,
            )

        async with services.sessionmaker() as session:
            incident = await IncidentRepository(session).get(uuid.UUID(state["incident_id"]))
            if incident is None:
                return state
            action = await propose_remediation(
                session,
                incident,
                root_cause_category=result.root_cause_category,
                hypothesis=result.hypothesis,
                observer_approved=approved,
                target_pod=target_pod,
            )
            if action is None:
                await session.commit()
                await _persist(
                    state["incident_id"],
                    "resolution",
                    message=f"Resolution: no mechanical action for category "
                    f"'{result.root_cause_category}' — investigation stays with "
                    f"the on-call human.",
                )
                return state
            if action.tier == 1:
                action = await execute_action(
                    session, action, services.gateway, observer_approved=approved
                )
            # FR-6.2 updates: proposal awaiting approval, or a real (non-shadow) execution.
            if action.tier >= 2 and action.status == RemediationStatus.proposed:
                await post_update(
                    session,
                    services.gateway,
                    incident_id=incident.id,
                    phase="remediation_proposed",
                    service=state["service_name"],
                    severity=state["severity"],
                    action_type=action.action_type,
                )
            elif action.status == RemediationStatus.executed and not action.shadow:
                await post_update(
                    session,
                    services.gateway,
                    incident_id=incident.id,
                    phase="remediation_executed",
                    service=state["service_name"],
                    severity=state["severity"],
                    action_type=action.action_type,
                )
            await session.commit()
            status = action.status
            awaiting = status == RemediationStatus.proposed and action.tier >= 2
            message = (
                f"Resolution: {action.action_type} on {action.target_resource_id} — "
                f"tier {action.tier}, status {status}"
                f"{' (SHADOW: nothing touched)' if action.shadow else ''}"
                f"{'; awaiting human approval' if awaiting else ''}."
            )
        await _persist(
            state["incident_id"],
            "resolution",
            message=message,
            structured={
                "action_type": action.action_type,
                "tier": action.tier,
                "status": str(status),
                "shadow": action.shadow,
                "blast_radius": action.blast_radius,
                "compensating_action": action.compensating_action,
            },
            event="resolution",
        )
        return state

    def route_after_observer(state: InvestigationState) -> str:
        verdict = state["observer_verdict"]
        if verdict.approved or state.get("revision_count", 0) >= 1:
            return "finalize"
        return "revise"

    graph = StateGraph(InvestigationState)
    graph.add_node("triage", triage)
    graph.add_node("correlation", correlation)
    graph.add_node("rca", rca)
    graph.add_node("observer", observer)
    graph.add_node("revise", revise)
    graph.add_node("finalize", finalize)
    graph.add_node("resolution", resolution)
    graph.set_entry_point("triage")
    graph.add_edge("triage", "correlation")
    graph.add_edge("correlation", "rca")
    graph.add_edge("rca", "observer")
    graph.add_conditional_edges("observer", route_after_observer)
    graph.add_edge("revise", "rca")
    graph.add_edge("finalize", "resolution")
    graph.add_edge("resolution", END)
    return graph.compile()
