import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, Approval } from "./api";

const OPEN_STATUSES = new Set(["proposed", "approved", "uncertain"]);

export default function ApprovalsPanel() {
  const query = useQuery({ queryKey: ["approvals"], queryFn: api.listApprovals });
  const queryClient = useQueryClient();
  const [decidedBy, setDecidedBy] = useState("operator");
  const [message, setMessage] = useState<string | null>(null);

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["approvals"] });
  };

  const decide = useMutation({
    mutationFn: ({ approval, decision }: { approval: Approval; decision: "approved" | "rejected" }) =>
      api.decideApproval(approval.approval_id, decision, decidedBy),
    onSuccess: (record) => {
      setMessage(`${record.approval_id} → ${record.status}`);
      refresh();
    },
    onError: (error) => setMessage(describe(error)),
  });

  const execute = useMutation({
    mutationFn: (approval: Approval) => api.executeApproval(approval.approval_id, decidedBy),
    onSuccess: (record) => {
      setMessage(
        `${record.approval_id} → ${record.status}` +
          (record.verification_ok === null
            ? ""
            : ` (verification: ${record.verification_ok ? "ok" : "MISMATCH"})`),
      );
      refresh();
    },
    onError: (error) => setMessage(describe(error)),
  });

  const approvals = query.data?.approvals ?? [];

  return (
    <section className="panel">
      <h2>Approvals</h2>
      <div className="row">
        <label htmlFor="operator">operator label: </label>
        <input id="operator" value={decidedBy} onChange={(e) => setDecidedBy(e.target.value)} />
      </div>
      {query.isLoading && <p className="meta">loading approvals…</p>}
      {query.isError && <p className="error">{describe(query.error)}</p>}
      {message && <p className="meta">{message}</p>}
      {approvals.length === 0 && !query.isLoading && (
        <p className="meta">
          no approvals recorded (the loop proposes only when ACTION_MODE_ENABLED is true
          and an eligible unacknowledged alert appears in the evidence)
        </p>
      )}
      <ul className="approvals">
        {approvals.map((approval) => (
          <li key={approval.approval_id}>
            <div>
              <code>{approval.approval_id}</code> · <strong>{approval.tool}</strong> ·{" "}
              <span className={`status status-${approval.status}`}>{approval.status}</span>
            </div>
            <div className="meta">
              alert: <code>{approval.arguments.alert_id ?? "?"}</code>
              {approval.decided_by ? ` · decided_by: ${approval.decided_by}` : ""}
              {approval.executed_by ? ` · executed_by: ${approval.executed_by}` : ""}
              {approval.verification_ok !== null
                ? ` · verification: ${approval.verification_ok ? "ok" : "MISMATCH"}`
                : ""}
            </div>
            {approval.proposal.rationale && (
              <div className="meta">rationale: {approval.proposal.rationale}</div>
            )}
            {OPEN_STATUSES.has(approval.status) && (
              <div className="row">
                {approval.status === "proposed" && (
                  <>
                    <button
                      disabled={decide.isPending || !decidedBy.trim()}
                      onClick={() => decide.mutate({ approval, decision: "approved" })}
                    >
                      Approve
                    </button>
                    <button
                      disabled={decide.isPending || !decidedBy.trim()}
                      onClick={() => decide.mutate({ approval, decision: "rejected" })}
                    >
                      Reject
                    </button>
                  </>
                )}
                {approval.status === "approved" && (
                  <button
                    disabled={execute.isPending || !decidedBy.trim()}
                    onClick={() => execute.mutate(approval)}
                  >
                    Execute write
                  </button>
                )}
                {approval.status === "uncertain" && (
                  <span className="meta">
                    outcome unknown — resolve via recovery with a fresh operator label (no
                    automatic replay)
                  </span>
                )}
              </div>
            )}
            {approval.failure_reason && (
              <div className="error">{approval.failure_reason}</div>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return error instanceof Error ? error.message : String(error);
}
