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

from agents.communication.writer import post_update
from agents.correlation.planner import gather
from agents.evidence import EvidenceStore
from agents.observer.critic import critique
from agents.observer.validator import review
from agents.rca.engine import RCAResult, run_rca
from agents.triage.reasoner import assess
from api.events import publish_event
from core.config import get_settings
from core.logging import get_logger
from core.tracing import traced
from db.enums import ActorType, AgentMessageType, EvidenceType, IncidentState
from db.repository import AgentRepository, AuditRepository, IncidentRepository
from memory.store import recall
from orchestrator.supervisor import available_steps, decide
from rag.store import search_runbooks


class InvestigationState(TypedDict, total=False):
    incident_id: str
    service_name: str
    title: str
    severity: str
    alert_kind: str
    alert_value: float | None
    evidence_store: Any  # EvidenceStore (kept opaque to LangGraph serialization)
    correlation_summary: str
    runbook_context: str
    rca_result: Any  # RCAResult
    observer_verdict: Any  # ObserverVerdict
    revision_count: int
    tokens_used: int
    completed_steps: list[str]
    next_step: str
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
        prompt_ref: str | None = None,
        citations: list[dict] | None = None,
        event: str = "agent_step",
    ) -> None:
        """The single side-effect point: step + message + citations + SSE, one transaction."""
        if prompt_ref:
            # Folded into the jsonb rather than given its own column: it stays queryable
            # (`structured_output->>'prompt_ref'`) and every step already carries a
            # structured payload, so an eval run can attribute a score to the exact prompt
            # version that produced it without a schema change per prompt-bearing agent.
            structured = {**(structured or {}), "prompt_ref": prompt_ref}
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

    @traced("agent:triage", run_type="chain")
    async def triage(state: InvestigationState) -> InvestigationState:
        outcome = await assess(
            services.provider,
            service=state["service_name"],
            kind=state.get("alert_kind") or "other",
            title=state["title"],
            value=state.get("alert_value"),
        )
        detail = []
        if outcome.escalated:
            detail.append(f"escalated from {outcome.floor_severity}")
        if outcome.clamped:
            detail.append(f"model proposed {outcome.model_severity}, clamped to floor")
        if outcome.degraded:
            detail.append("model unavailable — rule-based floor used")
        message = (
            f"Triage: {state['service_name']} assessed {outcome.severity}"
            f"{' (' + '; '.join(detail) + ')' if detail else ''}. "
            f"Customer impact: {outcome.customer_impact} {outcome.reasoning}"
        )
        await _persist(
            state["incident_id"],
            "triage",
            message=message,
            structured=outcome.model_dump(mode="json"),
            model=services.provider.name if not outcome.degraded else None,
            tokens=outcome.tokens_used,
            cost=outcome.cost_usd,
            prompt_ref=outcome.prompt_ref,
        )
        # FR-6.1: first plain-English update within 2 minutes of incident creation.
        async with services.sessionmaker() as session:
            await post_update(
                session,
                services.gateway,
                incident_id=uuid.UUID(state["incident_id"]),
                phase="opened",
                service=state["service_name"],
                severity=str(outcome.severity),
                provider=services.provider,
            )
            await session.commit()
        return {
            **state,
            "severity": str(outcome.severity),
            "tokens_used": state.get("tokens_used", 0) + outcome.tokens_used,
        }

    @traced("agent:correlation", run_type="chain")
    async def correlation(state: InvestigationState) -> InvestigationState:
        outcome = await gather(
            services.provider,
            services.gateway,
            service=state["service_name"],
            title=state["title"],
            severity=state.get("severity", "P3"),
        )
        plan_note = (
            f"{outcome.rounds_used} planning round(s), {outcome.calls_dispatched} tool call(s)"
            if outcome.llm_planned
            else "baseline sweep (planner unavailable)"
        )
        message = (
            f"Correlation: {plan_note} → {len(outcome.store.items)} evidence item(s), "
            f"{len(outcome.store.gaps)} gap(s): {outcome.store.gaps or 'none'}."
        )
        if outcome.synthesis:
            message += f" {outcome.synthesis.summary[:600]}"
        await _persist(
            state["incident_id"],
            "correlation",
            message=message,
            structured=json.loads(outcome.summary_json),
            model=services.provider.name if outcome.llm_planned else None,
            tokens=outcome.tokens_used,
            cost=outcome.cost_usd,
            prompt_ref=outcome.prompt_refs[0] if outcome.prompt_refs else None,
        )
        return {
            **state,
            "evidence_store": outcome.store,
            "correlation_summary": outcome.summary_json,
            "tokens_used": state.get("tokens_used", 0) + outcome.tokens_used,
            "completed_steps": [*state.get("completed_steps", []), "correlation"],
        }

    @traced("agent:rca", run_type="chain")
    async def rca(state: InvestigationState) -> InvestigationState:
        store: EvidenceStore = state["evidence_store"]
        async with services.sessionmaker() as session:
            # Metadata-filtered to this service: an OOM runbook for a service that is not the
            # one on fire is a distractor the model has no way to discount.
            hits = await search_runbooks(
                session,
                f"{state['service_name']} {state['title']}",
                service=state["service_name"],
            )
            memories = await recall(session, service_name=state["service_name"])
        # Chunks carry their section, so the citation the model sees names the passage rather
        # than the whole document.
        runbook_context = "\n---\n".join(f"[{h.citation}] {h.content}" for h in hits)
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
            correlation_summary=state.get("correlation_summary", ""),
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
            "completed_steps": [*state.get("completed_steps", []), "rca"],
            # A fresh hypothesis invalidates the previous verdict; leaving it would let the
            # supervisor route on a review of an analysis that no longer exists.
            "observer_verdict": None,
        }

    @traced("agent:observer", run_type="chain")
    async def observer(state: InvestigationState) -> InvestigationState:
        store: EvidenceStore = state["evidence_store"]
        result: RCAResult = state["rca_result"]

        # Deterministic first, and it holds the veto (see agents/observer/critic.py).
        verdict = review(result.claims, store, root_cause_category=result.root_cause_category)

        # Semantic critique runs only when the mechanical checks already passed. Running it
        # on an already-rejected hypothesis would spend tokens on an outcome that cannot
        # change: the critic can veto, never rescue.
        critique_outcome = None
        if verdict.approved:
            critique_outcome = await critique(
                services.provider,
                hypothesis=result.hypothesis,
                category=result.root_cause_category,
                confidence=result.confidence,
                claims=result.claims,
                store=store,
            )
            if not critique_outcome.approved:
                verdict = verdict.model_copy(
                    update={
                        "approved": False,
                        "notes": f"{verdict.notes}; semantic critique rejected: "
                        f"{critique_outcome.reason}",
                    }
                )

        message = (
            f"Observer: {'approved' if verdict.approved else 'REJECTED'} — {verdict.notes} "
            f"(flagged evidence: {[f['evidence_id'] for f in verdict.flagged_evidence] or 'none'})"
        )
        structured = verdict.model_dump()
        if critique_outcome is not None:
            structured["semantic_critique"] = critique_outcome.model_dump(mode="json")
        await _persist(
            state["incident_id"],
            "observer",
            message=message,
            structured=structured,
            model=services.provider.name if critique_outcome and critique_outcome.ran else None,
            tokens=critique_outcome.tokens_used if critique_outcome else None,
            cost=critique_outcome.cost_usd if critique_outcome else None,
            prompt_ref=critique_outcome.prompt_ref if critique_outcome else None,
            event="observer_verdict",
        )
        return {
            **state,
            "observer_verdict": verdict,
            "tokens_used": state.get("tokens_used", 0)
            + (critique_outcome.tokens_used if critique_outcome else 0),
            "completed_steps": [*state.get("completed_steps", []), "observer"],
        }

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
        return {
            **state,
            "revision_count": state.get("revision_count", 0) + 1,
            "completed_steps": [*state.get("completed_steps", []), "revise"],
            "rca_result": None,
            "observer_verdict": None,
        }

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

    @traced("agent:resolution", run_type="chain")
    async def resolution(state: InvestigationState) -> InvestigationState:
        """V1.5 Resolution Agent node: propose (and gate-checked Tier-1 execute)."""
        from agents.resolution.engine import execute_action, propose_remediation
        from db.enums import RemediationStatus

        verdict = state.get("observer_verdict")
        result: RCAResult | None = state.get("rca_result")
        approved = bool(verdict and verdict.approved)
        done = {**state, "completed_steps": [*state.get("completed_steps", []), "resolution"]}
        if result is None:
            return done

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
                return done
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
                return done
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
        return {**state, "completed_steps": [*state.get("completed_steps", []), "resolution"]}

    @traced("agent:supervisor", run_type="chain")
    async def supervisor(state: InvestigationState) -> InvestigationState:
        """LLM routing decision, recorded as its own step so it is auditable and replayable."""
        outcome = await decide(services.provider, dict(state))
        await _persist(
            state["incident_id"],
            "orchestrator",
            message=f"Supervisor → {outcome.step}: {outcome.reasoning}",
            structured={
                "next_step": outcome.step,
                "llm_decided": outcome.llm_decided,
                "overridden_from": outcome.overridden_from,
                "available": available_steps(dict(state)),
            },
            model=services.provider.name if outcome.llm_decided else None,
            tokens=outcome.tokens_used,
            cost=outcome.cost_usd,
            prompt_ref=outcome.prompt_ref,
        )
        return {
            **state,
            "next_step": outcome.step,
            "tokens_used": state.get("tokens_used", 0) + outcome.tokens_used,
        }

    async def escalate(state: InvestigationState) -> InvestigationState:
        """Hand to a human when the evidence cannot support a conclusion."""
        await _persist(
            state["incident_id"],
            "orchestrator",
            message="Escalated to a human: the gathered evidence does not support a "
            "conclusion, and further automated passes would not change that.",
            structured={"escalated": True},
            event="escalated",
        )
        return await finalize(state)

    def route_from_supervisor(state: InvestigationState) -> str:
        """Read the supervisor's already-validated choice. No decision is made here."""
        return state.get("next_step") or "finalize"

    graph = StateGraph(InvestigationState)
    graph.add_node("triage", triage)
    graph.add_node("supervisor", supervisor)
    graph.add_node("correlation", correlation)
    graph.add_node("rca", rca)
    graph.add_node("observer", observer)
    graph.add_node("revise", revise)
    graph.add_node("finalize", finalize)
    graph.add_node("escalate", escalate)
    graph.add_node("resolution", resolution)

    graph.set_entry_point("triage")
    # Every completed step returns to the supervisor, which is what makes routing a decision
    # rather than a fixed pipeline. The supervisor's choice is validated against
    # `available_steps` before it is honoured, so the cycle is bounded by state, not by luck.
    graph.add_edge("triage", "supervisor")
    graph.add_conditional_edges(
        "supervisor",
        route_from_supervisor,
        {
            "correlation": "correlation",
            "rca": "rca",
            "observer": "observer",
            "revise": "revise",
            "resolution": "resolution",
            "finalize": "finalize",
            "escalate": "escalate",
        },
    )
    graph.add_edge("correlation", "supervisor")
    graph.add_edge("rca", "supervisor")
    graph.add_edge("observer", "supervisor")
    graph.add_edge("revise", "supervisor")
    # Resolution returns to the supervisor rather than ending: proposing a remediation is a
    # step in the investigation, not its conclusion. Routing it straight to END skipped
    # `finalize`, so the incident never transitioned state and never wrote its audit record.
    graph.add_edge("resolution", "supervisor")
    graph.add_edge("finalize", END)
    graph.add_edge("escalate", END)
    # A cycle needs a hard stop independent of the routing logic: if the supervisor and the
    # step bounds ever disagree, this ends the run instead of billing until the budget dies.
    return graph.compile().with_config(recursion_limit=get_settings().graph_recursion_limit)
