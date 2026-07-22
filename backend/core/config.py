"""Application settings, loaded from environment / .env (ESD §16: secrets via env only).

A single ``Settings`` instance is the one place configuration is read; nothing else reaches into
``os.environ`` directly, so tests can override values and no credential is hardcoded.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed configuration for the Aegis backend."""

    # Both paths so `python -m ...` works from backend/ and from the repo root; real env
    # vars always take precedence over either file.
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"), env_file_encoding="utf-8", extra="ignore"
    )

    # --- Database (single Postgres + pgvector, ESD §19) ---
    database_url: str = Field(
        default="postgresql+asyncpg://aegis:change-me-locally@localhost:5432/aegis",
        description="SQLAlchemy async URL for the single Postgres instance.",
    )

    # --- Redis (cache + rate limiting only, never a source of truth, ESD §11) ---
    redis_url: str = Field(default="redis://localhost:6379/0")
    redis_max_connections: int = Field(default=20)
    redis_timeout_seconds: float = Field(default=2.0)
    # Read-only MCP evidence cache. Short by design: the RCA ensemble makes several passes
    # over the same window within seconds, but infrastructure state moves fast enough that a
    # long TTL would let one incident's evidence bleed into the next.
    evidence_cache_ttl_seconds: int = Field(default=30)
    # HTTP rate limit, per client per window. DoS protection only — the load-bearing
    # remediation limits live in Postgres (safety/), not here.
    api_rate_limit_per_window: int = Field(default=120)
    api_rate_limit_window_seconds: int = Field(default=60)
    # Ingestion is a machine-to-machine path with a genuinely higher legitimate rate: an
    # alert storm is exactly when Aegis must not start dropping alerts.
    ingest_rate_limit_per_window: int = Field(default=600)

    # --- Auth (self-issued JWT in httpOnly cookies, ESD §8) ---
    # Firebase supplies *identity* only; Aegis still issues and owns the session, so the
    # browser's durable credential stays an httpOnly cookie (CLAUDE.md §12).
    jwt_secret: str = Field(default="change-me-generate-a-long-random-string")
    jwt_access_ttl_seconds: int = Field(default=900)
    jwt_refresh_ttl_seconds: int = Field(default=604800)

    # --- Firebase Authentication / Google OAuth (ESD §8) ---
    # The ID token's audience must equal this project id, so it is a security control,
    # not a convenience field: a token minted for another project is rejected.
    firebase_project_id: str = Field(default="")
    # Path to the gitignored service-account JSON. The key itself is NEVER inlined here
    # and never logged (CLAUDE.md §12).
    firebase_service_account_file: str = Field(default="../.secrets/firebase-service-account.json")

    # --- OAuth role allowlists (fail closed, CLAUDE.md §12) ---
    # Any authenticated email absent from both lists is provisioned `viewer`, which cannot
    # approve a remediation. There is no path by which an unknown Google account is elevated.
    aegis_admin_emails: str = Field(default="")
    aegis_approver_emails: str = Field(default="")

    @property
    def admin_email_set(self) -> set[str]:
        return {e.strip().lower() for e in self.aegis_admin_emails.split(",") if e.strip()}

    @property
    def approver_email_set(self) -> set[str]:
        return {e.strip().lower() for e in self.aegis_approver_emails.split(",") if e.strip()}

    # --- LLM provider (Strategy pattern, ESD §20). Real models only — there is no
    # stub/offline provider. Keys come from env, never committed (CLAUDE.md §12). ---
    llm_provider: str = Field(default="openrouter")
    openrouter_base_url: str = Field(default="https://openrouter.ai/api/v1")
    # Comma-separated; the provider rotates across them when one is rate-limited.
    openrouter_api_keys: str = Field(default="")

    # --- Gemini (ESD §20). An independent capacity pool from OpenRouter, whose free
    # models share one account-wide DAILY cap; when that is spent no agent can reason
    # until the reset, which is not something the retry sweep can ride out. Model names
    # are namespaced per provider because the two vendors' identifiers do not overlap
    # and a shared setting would silently send an OpenRouter slug to Google. ---
    gemini_base_url: str = Field(default="https://generativelanguage.googleapis.com/v1beta")
    # Comma-separated; the provider rotates across them when one is rate-limited.
    gemini_api_keys: str = Field(default="")
    gemini_model_rca: str = Field(default="gemini-3.1-flash-lite")
    gemini_model_default: str = Field(default="gemini-3.1-flash-lite")
    # Free Gemini capacity genuinely moves between models during the day (503 "high
    # demand" on one while another serves normally), so the chain is load-bearing.
    gemini_model_fallbacks: str = Field(
        default="gemini-3.1-flash-lite,gemini-flash-latest,gemini-3.5-flash"
    )

    # Per-agent model assignment (ESD §20: right-size capability to task difficulty).
    # RCA does the actual reasoning and must emit strict JSON; the others summarize.
    llm_model_rca: str = Field(default="nvidia/nemotron-3-super-120b-a12b:free")
    llm_model_default: str = Field(default="nvidia/nemotron-nano-9b-v2:free")
    # Tried in order when the primary is rate-limited or unavailable upstream.
    llm_model_fallbacks: str = Field(
        default="nvidia/nemotron-nano-9b-v2:free,google/gemma-4-26b-a4b-it:free,openai/gpt-oss-20b:free"
    )
    llm_temperature: float = Field(default=0.2)
    # Real prompts (5 evidence blocks) plus hidden reasoning tokens overrun 900 and
    # truncate the JSON mid-object, which silently drops an entire ensemble pass.
    llm_max_tokens: int = Field(default=2500)
    llm_max_tokens_on_truncation: int = Field(default=4000)
    llm_timeout_seconds: float = Field(default=90.0)
    # Schema-repair budget for structured outputs. Each repair feeds the validation error
    # back to the model; two is enough to fix formatting without burning quota on a model
    # that fundamentally cannot satisfy the schema.
    llm_structured_repair_attempts: int = Field(default=2)

    @property
    def openrouter_key_list(self) -> list[str]:
        return [k.strip() for k in self.openrouter_api_keys.split(",") if k.strip()]

    @property
    def llm_fallback_list(self) -> list[str]:
        return [m.strip() for m in self.llm_model_fallbacks.split(",") if m.strip()]

    @property
    def gemini_key_list(self) -> list[str]:
        return [k.strip() for k in self.gemini_api_keys.split(",") if k.strip()]

    @property
    def gemini_fallback_list(self) -> list[str]:
        return [m.strip() for m in self.gemini_model_fallbacks.split(",") if m.strip()]

    # --- RAG: embedding model (ESD §20). Local ONNX BGE — no per-call cost, no network at
    # request time. Changing model/dim requires migrating the pgvector columns to match;
    # the embedder fails loudly at load if they disagree. ---
    embedding_model: str = Field(default="BAAI/bge-small-en-v1.5")
    embedding_dim: int = Field(default=384)
    # Kept inside the repo (gitignored) rather than the OS temp dir, which is periodically
    # cleared and would silently re-download the model on an incident-response path.
    embedding_cache_dir: str = Field(default="../.model-cache")
    embedding_batch_size: int = Field(default=32)

    # --- RAG: chunking (ESD §20) ---
    chunk_max_chars: int = Field(default=1200)
    chunk_overlap_chars: int = Field(default=150)
    chunk_min_chars: int = Field(default=80)

    # --- RAG: retrieval ---
    rag_top_k: int = Field(default=5, description="Chunks returned to the caller.")
    # Over-fetch before reranking: a cross-encoder can only reorder what retrieval handed it,
    # so the candidate pool must be wider than the final k or reranking cannot recover a hit
    # that vector search ranked 12th.
    rag_candidate_k: int = Field(default=30)
    rag_rerank_enabled: bool = Field(default=True)
    rag_reranker_model: str = Field(default="Xenova/ms-marco-MiniLM-L-6-v2")
    # Reciprocal Rank Fusion constant. 60 is the value from the original RRF paper and damps
    # the influence of any single retriever's top rank.
    rag_rrf_k: int = Field(default=60)
    # Relevance floor, in cross-encoder logit space, applied ONLY when reranking actually
    # ran — a fused RRF score is a rank artefact and thresholding it would drop everything or
    # nothing depending on corpus size. Calibrated, not chosen: `python -m evaluation.calibration`
    # sweeps this against the golden dataset and reports hit rate, refusal rate and the
    # relevant chunks each candidate costs. At -11.0 the measured result is hit_rate 1.00 and
    # refusal_rate 1.00 — every answerable query still finds its runbook, and the
    # out-of-domain query correctly returns nothing.
    #
    # The value is negative because the model saturates near -11.2 for content it considers
    # unrelated, while a *relevant* passage phrased in an operator's own words scores around
    # -4. A naive "score > 0" floor would discard exactly the paraphrased retrieval the
    # semantic embedder exists to serve. Re-run the calibration after any corpus or reranker
    # change; the number is only meaningful against the distribution it was measured on.
    rag_min_score: float = Field(
        default=-11.0,
        description="Drop reranked hits below this logit. Calibrated by evaluation.calibration.",
    )

    # --- LangSmith tracing (ESD §13). Unset = tracing is a no-op; an observability
    # backend must never be able to stall an investigation. ---
    langsmith_api_key: str = Field(default="")
    langsmith_project: str = Field(default="aegis")
    langsmith_endpoint: str = Field(default="https://api.smith.langchain.com")

    # --- Agentic loop bounds (ESD §15: bounded work per incident). These are enforced in
    # Python, never requested in a prompt — an agent that could talk itself past them would
    # turn a prompt injection in a pod log into an unbounded spend. ---
    correlation_max_rounds: int = Field(
        default=3, description="Plan→dispatch→observe iterations before synthesis."
    )
    correlation_max_calls_per_round: int = Field(default=4)
    # How many times the supervisor may route back to correlation. "Gather more evidence" is
    # always a plausible next step, so without a cap a model that favours it never concludes.
    correlation_max_invocations: int = Field(default=2)
    supervisor_max_revisions: int = Field(
        default=1, description="RCA re-runs the supervisor may request."
    )
    # Hard stop on the supervisor cycle, independent of the routing logic. If the supervisor
    # and the step bounds ever disagree, LangGraph ends the run rather than billing until the
    # token budget dies.
    graph_recursion_limit: int = Field(default=25)

    # --- Incident tuning (PRD FR-1.2, FR-3.1; ESD §15) ---
    dedup_window_seconds: int = Field(default=300)
    rca_ensemble_passes: int = Field(default=3)
    incident_token_budget: int = Field(default=200_000)
    # Per-step human-readable explanations (ESD §5). One extra, tightly capped call per
    # reasoning agent — worth it because the alternative is an operator reading raw prompts
    # to decide whether to trust a conclusion. Switchable because it is a reading aid, not
    # part of the investigation: turning it off must change what is *shown*, never what is
    # *decided*.
    explanations_enabled: bool = Field(default=True)
    deploy_lookback_hours: float = Field(default=2.0, description="FR-2.2 change window.")
    rca_agreement_threshold: float = Field(
        default=0.6, description="Below this, the hypothesis is flagged low-confidence."
    )

    # --- Infra evidence sources (read-only MCP servers in MVP, ESD §3/§16) ---
    prometheus_url: str = Field(default="http://localhost:9090")
    github_repo: str = Field(default="Saravankumar25/aegis")
    github_api_url: str = Field(default="https://api.github.com")
    # Optional; unauthenticated works for public repos (lower rate limit). Never logged.
    github_token: str | None = Field(default=None)
    k8s_api_url: str = Field(
        default="https://127.0.0.1:6443",
        description="Kubernetes API server URL (kind maps a host port; see infra/README.md).",
    )
    # Short-lived ServiceAccount token + cluster CA, minted by infra/gen-mcp-credentials.sh.
    # Both files are git-ignored; the token is read per-call so rotation needs no restart.
    k8s_token_file: str = Field(default="../infra/.k8s-mcp-token")
    # V1.5 write credential (separate SA: delete pods + deployments/scale ONLY, ESD §16).
    # Only the k8s MCP server process reads it; absent file = write tools degrade.
    k8s_writer_token_file: str = Field(default="../infra/.k8s-mcp-writer-token")
    k8s_ca_cert_file: str = Field(default="../infra/.k8s-mcp-ca.crt")
    k8s_namespace: str = Field(default="meridian")

    # --- MCP tool-call resilience (ESD §12: 3 attempts, exponential backoff, then degrade) ---
    mcp_retry_attempts: int = Field(default=3)
    mcp_retry_base_delay_seconds: float = Field(default=0.2)
    mcp_http_timeout_seconds: float = Field(default=10.0)

    # --- V1.5 safety thresholds (FR-4.2, FR-12, ESD §17). Changing any of these is a
    # safety-relevant change and must be called out in the PR (CLAUDE.md §15). ---
    tier1_rate_limit_per_hour: int = Field(default=3, description="FR-4.2 per-service cap.")
    breaker_window_minutes: int = Field(default=10)
    breaker_max_tier1_in_window: int = Field(
        default=10, description="Global mass-action trip threshold."
    )
    proposal_expiry_minutes: int = Field(default=30)
    # Tier-1 shadow mode: log what WOULD execute without touching infrastructure.
    # Deliberately ON by default; flipping it off is an explicit operator decision.
    resolution_shadow_mode: bool = Field(default=True)
    max_blast_radius_dependents: int = Field(
        default=1, description="FR-8.3: Tier-1 auto-exec only if dependents <= this."
    )
    slack_webhook_url: str | None = Field(default=None)

    # --- API surface (ESD §7) ---
    cors_origins: str = Field(
        default="http://localhost:3000",
        description="Comma-separated allowed origins for the frontend.",
    )
    # Shared secret for the ingestion webhook; unset locally (kind cluster is trusted).
    ingest_webhook_token: str | None = Field(default=None)

    # Runtime environment marker; "test" disables some production-only guards.
    environment: str = Field(default="local")


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
