// API client helpers. Auth rides on httpOnly cookies (ESD §5/§8): every request is
// credentialed and the frontend never reads or stores a token.

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000/api/v1";

export class ApiError extends Error {
  constructor(
    public status: number,
    public errorCode: string,
    message: string,
  ) {
    super(message);
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    let code = "http_error";
    let message = response.statusText;
    try {
      const body = await response.json();
      code = body.error_code ?? code;
      message = body.message ?? message;
    } catch {
      // non-JSON error body: keep the status text
    }
    throw new ApiError(response.status, code, message);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export interface Incident {
  id: string;
  title: string;
  service_name: string;
  severity: "P1" | "P2" | "P3" | "P4";
  state: string;
  alert_source: string;
  external_alert_id: string;
  created_at: string;
  updated_at: string;
  resolved_at: string | null;
}

export interface Citation {
  id: string;
  evidence_type: string;
  evidence_ref: string;
  evidence_snippet_redacted: string | null;
  validated_by_observer: boolean;
}

export interface AgentStep {
  id: string;
  agent_name: string;
  ensemble_pass_index: number | null;
  output_summary: string | null;
  structured_output: Record<string, unknown> | null;
  confidence: number | null;
  model_used: string | null;
  tokens_used: number | null;
  cost_usd: number | null;
  created_at: string;
  citations: Citation[];
}

export interface AgentMessage {
  id: string;
  agent_name: string;
  message_type: string;
  content: string;
  created_at: string;
}

export interface Transition {
  from_state: string;
  to_state: string;
  actor_type: string;
  actor_id: string;
  created_at: string;
}

export interface IncidentDetail extends Incident {
  transitions: Transition[];
  steps: AgentStep[];
  messages: AgentMessage[];
}

export interface ReplayEvent {
  sequence: number;
  at: string;
  kind: "transition" | "step" | "message";
  agent_name: string | null;
  summary: string;
  detail: Record<string, unknown>;
}

export interface Replay {
  incident: Incident;
  events: ReplayEvent[];
}

export interface User {
  id: string;
  email: string;
  role: string;
  display_name: string | null;
  photo_url: string | null;
}

export function eventSource(path: string): EventSource {
  return new EventSource(`${API_BASE}${path}`, { withCredentials: true });
}
