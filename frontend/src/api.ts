// Typed client for the EXISTING backend API contracts (backend/api/schemas.py,
// backend/agent/state.py::dict_for_api, backend/api/routes.py::approval_out).
// Relative paths — the container demo proxies /api through nginx.

export interface InvestigationCreated {
  investigation_id: string;
  request_id: string;
  status: string;
}

export interface Evidence {
  evidence_id: string;
  tool_name: string;
  server: string;
  step: number;
  summary: Record<string, unknown>;
}

export interface ReportClaim {
  text: string;
  evidence_ids: string[];
}

export interface VerificationResult {
  ok: boolean;
  dropped_claims: string[];
  flagged_claims: string[];
  downgraded: boolean;
  grounding_checked: boolean;
  notes: string[];
}

export interface InvestigationReport {
  conclusion: string;
  confidence: string;
  confidence_rationale: string;
  claims: ReportClaim[];
  caveats: string[];
  evidence_gaps: string[];
}

export interface Investigation {
  investigation_id: string;
  request_id: string;
  question: string;
  status: string;
  stage: string;
  evidence: Evidence[];
  evidence_ids: string[];
  report: InvestigationReport | null;
  verification: VerificationResult | null;
  failure_reason: string | null;
  warnings: string[];
  llm_calls: number;
}

export interface SseStatus {
  investigation_id: string;
  status: string;
  stage: string;
  evidence_count: number;
}

export interface Approval {
  approval_id: string;
  investigation_id: string;
  tool: string;
  arguments: { alert_id?: string; note?: string };
  status: string;
  proposal: { rationale?: string; evidence_ids?: string[] };
  decided_by: string | null;
  executed_by: string | null;
  verification: { ok: boolean; details?: string } | null;
  executed: boolean;
  verification_ok: boolean | null;
  failure_reason?: string | null;
}

export class ApiError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
  }
}

async function unwrap(response: Response): Promise<Response> {
  if (response.ok) return response;
  let detail = `${response.status} ${response.statusText}`;
  try {
    const body = await response.json();
    if (body && typeof body.detail === "string") detail = body.detail;
  } catch {
    /* non-JSON error body: keep the status line */
  }
  throw new ApiError(response.status, detail);
}

export async function apiGet<T>(path: string): Promise<T> {
  const response = await unwrap(await fetch(path));
  return (await response.json()) as T;
}

export async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const response = await unwrap(
    await fetch(path, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }),
  );
  return (await response.json()) as T;
}

export const api = {
  createInvestigation: (question: string) =>
    apiPost<InvestigationCreated>("/api/investigations", { question }),
  getInvestigation: (id: string) =>
    apiGet<Investigation>(`/api/investigations/${encodeURIComponent(id)}`),
  listApprovals: () => apiGet<{ count: number; approvals: Approval[] }>("/api/approvals"),
  decideApproval: (id: string, decision: "approved" | "rejected", decidedBy: string, comment?: string) =>
    apiPost<Approval>(`/api/approvals/${encodeURIComponent(id)}/decision`, {
      decision,
      decided_by: decidedBy,
      comment: comment || undefined,
    }),
  executeApproval: (id: string, executedBy: string) =>
    apiPost<Approval>(`/api/approvals/${encodeURIComponent(id)}/execute`, {
      executed_by: executedBy,
    }),
  sseUrl: (id: string) => `/api/investigations/${encodeURIComponent(id)}/events`,
};
