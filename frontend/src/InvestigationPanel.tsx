import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, SseStatus } from "./api";

const TERMINAL = new Set(["complete", "failed", "interrupted"]);
const MAX_SSE_ERRORS = 5;

type Phase =
  | { kind: "idle" }
  | { kind: "starting" }
  | { kind: "live"; id: string; sse: SseStatus }
  | { kind: "done"; id: string }
  | { kind: "error"; message: string };

export default function InvestigationPanel() {
  const [question, setQuestion] = useState("Why did today's sales drop?");
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });
  const [liveStatus, setLiveStatus] = useState<SseStatus | null>(null);
  const queryClient = useQueryClient();
  const sourceRef = useRef<EventSource | null>(null);
  const terminalRef = useRef(false);

  const id = phase.kind === "live" || phase.kind === "done" ? phase.id : null;
  const investigation = useQuery({
    queryKey: ["investigation", id],
    enabled: id !== null,
    queryFn: () => api.getInvestigation(id as string),
    // poll as a fallback while live; SSE remains the primary update channel
    refetchInterval: (query) =>
      TERMINAL.has(query.state.data?.status ?? "") ? false : 5000,
  });

  useEffect(() => () => sourceRef.current?.close(), []);

  async function submit() {
    setLiveStatus(null);
    terminalRef.current = false;
    sourceRef.current?.close();
    setPhase({ kind: "starting" });
    try {
      const created = await api.createInvestigation(question);
      setPhase({ kind: "live", id: created.investigation_id, sse: {
        investigation_id: created.investigation_id,
        status: created.status,
        stage: "created",
        evidence_count: 0,
      } });
      connect(created.investigation_id);
    } catch (error) {
      setPhase({ kind: "error", message: describe(error) });
    }
  }

  function connect(investigationId: string) {
    const source = new EventSource(api.sseUrl(investigationId));
    sourceRef.current = source;
    let errors = 0;

    source.addEventListener("status", (event) => {
      try {
        const sse = JSON.parse((event as MessageEvent).data) as SseStatus;
        setLiveStatus(sse);
        if (sse.status !== "running") void queryClient.invalidateQueries({
          queryKey: ["investigation", investigationId],
        });
      } catch {
        /* malformed frame: ignore, the next good frame replaces it */
      }
    });

    source.addEventListener("terminal", (event) => {
      try {
        const sse = JSON.parse((event as MessageEvent).data) as SseStatus;
        setLiveStatus(sse);
      } catch {
        /* ignore malformed terminal frame; the query refetch decides the UI */
      }
      terminalRef.current = true;
      source.close();
      void queryClient.invalidateQueries({ queryKey: ["investigation", investigationId] });
      setPhase({ kind: "done", id: investigationId });
    });

    // EventSource reconnects automatically on network errors; only give up
    // after repeated failures (e.g. 404 for an unknown id — never reconnect)
    source.onerror = () => {
      if (terminalRef.current) {
        source.close();
        return;
      }
      errors += 1;
      if (errors >= MAX_SSE_ERRORS) {
        source.close();
        setPhase({
          kind: "error",
          message: `event stream lost after ${errors} attempts; use the poll refresh`,
        });
        void queryClient.invalidateQueries({ queryKey: ["investigation", investigationId] });
      }
    };
  }

  const data = investigation.data;
  const statusLabel =
    liveStatus !== null
      ? `${liveStatus.status} / ${liveStatus.stage} (evidence: ${liveStatus.evidence_count})`
      : data
        ? `${data.status} / ${data.stage}`
        : phase.kind === "starting"
          ? "starting…"
          : "—";

  return (
    <section className="panel">
      <h2>Investigation</h2>
      <div className="row">
        <input
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="Ask an operations question"
        />
        <button onClick={submit} disabled={phase.kind === "starting" || phase.kind === "live"}>
          Investigate
        </button>
      </div>
      {id !== null && (
        <p className="meta">
          id: <code>{id}</code> · status: <strong>{statusLabel}</strong>
        </p>
      )}
      {phase.kind === "error" && <p className="error">{phase.message}</p>}
      {investigation.isError && <p className="error">{describe(investigation.error)}</p>}
      {investigation.isLoading && <p className="meta">loading investigation…</p>}
      {data && <ReportView data={data} />}
    </section>
  );
}

function ReportView({ data }: { data: Awaited<ReturnType<typeof api.getInvestigation>> }) {
  if (!TERMINAL.has(data.status)) {
    return <p className="meta">investigation in progress…</p>;
  }
  if (data.status !== "complete" || data.report === null) {
    return (
      <div className="report">
        <p className="error">
          investigation {data.status}: {data.failure_reason ?? "no report produced"}
        </p>
      </div>
    );
  }
  const { report, verification } = data;
  return (
    <div className="report">
      <h3>Report <span className={`confidence confidence-${report.confidence}`}>{report.confidence}</span></h3>
      <p>{report.conclusion}</p>
      {verification && (
        <p className="meta">
          verification: {verification.ok ? "ok" : "adjusted"}
          {verification.downgraded ? " · causal language downgraded" : ""}
          {verification.flagged_claims.length > 0
            ? ` · ${verification.flagged_claims.length} flagged claim(s)`
            : ""}
        </p>
      )}
      <h4>Claims</h4>
      <ul>
        {report.claims.map((claim, index) => (
          <li key={index}>
            {claim.text}{" "}
            <span className="meta">[{claim.evidence_ids.join(", ")}]</span>
          </li>
        ))}
      </ul>
      <h4>Evidence ({data.evidence.length})</h4>
      <ul>
        {data.evidence.map((evidence) => (
          <li key={evidence.evidence_id}>
            <code>{evidence.evidence_id}</code> — {evidence.server}/{evidence.tool_name}
          </li>
        ))}
      </ul>
      {(report.caveats.length > 0 || report.evidence_gaps.length > 0) && (
        <>
          <h4>Caveats &amp; gaps</h4>
          <ul>
            {report.caveats.map((caveat, index) => <li key={`c${index}`}>{caveat}</li>)}
            {report.evidence_gaps.map((gap, index) => <li key={`g${index}`}>{gap}</li>)}
          </ul>
        </>
      )}
      {data.warnings.length > 0 && (
        <p className="meta">warnings: {data.warnings.join(" · ")}</p>
      )}
    </div>
  );
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return error instanceof Error ? error.message : String(error);
}
