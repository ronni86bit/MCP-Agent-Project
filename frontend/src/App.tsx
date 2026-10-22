import ApprovalsPanel from "./ApprovalsPanel";
import InvestigationPanel from "./InvestigationPanel";

export default function App() {
  return (
    <main>
      <header>
        <h1>AI Operations Investigation Agent</h1>
        <p className="meta">
          single-operator local demo — evidence-grounded investigations over MCP;
          every write action requires explicit human approval
        </p>
      </header>
      <InvestigationPanel />
      <ApprovalsPanel />
    </main>
  );
}
