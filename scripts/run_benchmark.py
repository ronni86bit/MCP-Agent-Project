"""Deterministic benchmark runner (Phase 9, Gate 2).

Runs the deterministic evaluation suite (scripted LLM + stub/failing doubles —
no Ollama, no network) from `backend.evaluation.cases_deterministic.CASES` and
persists crash-safe schema-v1 results:

    evaluation/results/<UTC>_<label>/rows.jsonl        one line per completed case
    evaluation/results/<UTC>_<label>/manifest.json     written LAST, atomically
    evaluation/results/<UTC>_<label>/run_incomplete.json   handled-interrupt sidecar

Usage:
    uv run python scripts/run_benchmark.py --suite deterministic --label myrun [--out DIR]

Persistence contract (see evaluation/results/README.md):
- The run directory is created exclusively; a name collision aborts before any
  existing file is touched, so previous runs are never overwritten.
- Completed rows are flushed AND fsynced one at a time — finished results
  survive interruption (including process kill).
- Per-case exceptions become explicit FAILED rows; a case is never silently
  omitted.
- `manifest.json` is written last via tmp-file + os.replace and is the ONLY
  success marker. On handled interruption the sidecar records the completed
  sequence numbers and the reason. A run without a manifest is never
  aggregated as successful (`iter_successful_runs`).
- Hard-kill honesty: fsync gives durable completed rows, but a hard kill can
  still leave a manifest-less directory with no sidecar — tooling reports such
  directories as `incomplete-assumed`; nothing reconstructs a success.

Scoring lives entirely in the Gate 1 graders; the runner only executes cases
and records their `CaseResult` rows.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import asyncpg  # noqa: E402

from backend.config import Settings, get_settings  # noqa: E402
from backend.evaluation import cases_deterministic as case_registry  # noqa: E402
from backend.evaluation.schema import CaseResult, CheckResult, SCHEMA_VERSION  # noqa: E402

DEFAULT_OUT_DIR = ROOT / "evaluation" / "results"

# DSN defaults mirror tests/conftest.py; env always wins (pydantic-settings).
_DEFAULT_ADMIN_DSN = "postgresql://ops_admin:ops_admin_dev@localhost:5433/ops_agent"
_DEFAULT_RO_DSN = "postgresql://postgres_mcp_ro:ro_dev_password@localhost:5433/ops_agent"

CaseRunner = Callable[[], Awaitable[CaseResult]]


@dataclass(frozen=True)
class CaseSpec:
    """One scheduled case execution (registry id + wired runner)."""

    case_id: str
    run: CaseRunner
    requires_postgres: bool = False


# -- filesystem primitives ------------------------------------------------------


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _fsync_write(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())


def _atomic_write(path: Path, text: str) -> None:
    """tmp file + fsync + os.replace: readers see either nothing or the whole file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    _fsync_write(tmp, text)
    os.replace(tmp, path)


def build_run_dir(out_dir: Path, label: str) -> Path:
    """Create a fresh, exclusive run directory. A collision aborts BEFORE any
    existing file is modified (mkdir without exist_ok is the guard)."""
    name = f"{_utc()}_{label}"
    run_dir = out_dir / name
    out_dir.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir()  # FileExistsError on collision — never overwrite
    return run_dir


class RowsWriter:
    """Append-only JSONL with a monotonic sequence number, flushed and fsynced
    per row so completed results survive interruption."""

    def __init__(self, path: Path):
        self.path = path
        self._fh = open(path, "a", encoding="utf-8", newline="\n")
        self._seq = 0
        self.completed_seq: list[int] = []

    def write(self, case: CaseResult) -> int:
        self._seq += 1
        row = {"schema_version": SCHEMA_VERSION, "seq": self._seq, **case.model_dump(mode="json")}
        self._fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self.completed_seq.append(self._seq)
        return self._seq

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()


def write_incomplete(run_dir: Path, completed_seq: list[int], reason: str) -> None:
    """Sidecar for a handled interruption: the run is explicitly NOT successful."""
    _atomic_write(run_dir / "run_incomplete.json", json.dumps({
        "schema_version": SCHEMA_VERSION,
        "status": "incomplete",
        "completed_seq": completed_seq,
        "reason": reason,
        "utc": _utc(),
    }, ensure_ascii=False, indent=1) + "\n")


def write_manifest(run_dir: Path, manifest: dict) -> None:
    _atomic_write(run_dir / "manifest.json", json.dumps(
        manifest, ensure_ascii=False, indent=1) + "\n")


def read_manifest(run_dir: Path) -> dict | None:
    """Return the parsed manifest, or None for any not-successfully-completed
    run (missing, truncated, or unparseable manifest). Never raises for the
    run-level decision; malformed artifacts surface via the returned status."""
    path = run_dir / "manifest.json"
    if not path.is_file():
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION:
        return None
    return manifest


def iter_successful_runs(out_dir: Path) -> list[Path]:
    """Only manifest-bearing (schema-valid) run directories qualify as
    successful runs; everything else — sidecar-only, hard-kill leftovers,
    corrupted manifests — is excluded."""
    if not out_dir.is_dir():
        return []
    return sorted(p for p in out_dir.iterdir() if p.is_dir() and read_manifest(p) is not None)


# -- deterministic case wiring (doubles single-sourced from the test suite) ------

from tests.integration.test_agent_loop import StubMulti, StubServerManager  # noqa: E402
from tests.integration.test_eval_deterministic import (  # noqa: E402
    HangingFactory,
    _daily_call,
    _scripted,
    _settings,
)
from tests.unit.test_approval_lifecycle import (  # noqa: E402
    ALERT,
    FakeSession,
    FakeSessionCM,
    FixedClock,
    _now,
)


def _admin_dsn() -> str:
    return get_settings().ops_admin_dsn or _DEFAULT_ADMIN_DSN


def _ro_dsn() -> str:
    return get_settings().postgres_mcp_dsn or _DEFAULT_RO_DSN


async def _require_postgres() -> None:
    try:
        conn = await asyncpg.connect(_admin_dsn(), timeout=5)
        await conn.close()
    except Exception as exc:
        raise RuntimeError(
            "the deterministic suite includes PostgreSQL-backed cases (D-SEL, "
            "D-APPROVE); start the database with: docker compose up -d postgres"
        ) from exc


def _pg_manager_factory():
    from backend.mcp.client_manager import MCPClientManager

    def _make() -> Any:
        return MCPClientManager(
            "postgres-mcp", sys.executable, ["-m", "backend.mcp.servers.postgres_mcp"],
            call_timeout_seconds=get_settings().mcp_call_timeout_seconds,
        )
    return _make


@asynccontextmanager
async def _eval_state_db():
    """A unique, freshly created state database for the D-APPROVE case; always
    dropped afterwards so runs never share or mutate each other's data."""
    admin_dsn = _admin_dsn()
    db_name = f"ops_agent_eval_{uuid.uuid4().hex[:8]}"
    admin = await asyncpg.connect(admin_dsn, timeout=10)
    try:
        await admin.execute(f"CREATE DATABASE {db_name}")
    finally:
        await admin.close()
    try:
        yield admin_dsn.rsplit("/", 1)[0] + f"/{db_name}"
    finally:
        admin = await asyncpg.connect(admin_dsn, timeout=10)
        try:
            await admin.execute(f"DROP DATABASE IF EXISTS {db_name} WITH (FORCE)")
        finally:
            await admin.close()


def build_case_specs() -> list[CaseSpec]:
    """Wire every registered deterministic case with its doubles. Ordering is
    stable: DB-dependent cases first (they fail fast if Postgres is missing)."""
    from backend.agent.llm.adapter import LLMToolCall
    from backend.agent.models import ActionProposal, Claim, Confidence, InvestigationReport

    unequal = LLMToolCall(
        id="c-unequal", name="postgres-mcp__compare_sales",
        arguments={
            "window_a_start": "2026-10-03", "window_a_end": "2026-10-05",
            "window_b_start": "2026-10-04", "window_b_end": "2026-10-05",  # 3 vs 2 days
        },
    )
    equal = LLMToolCall(
        id="c-equal", name="postgres-mcp__compare_sales",
        arguments={
            "window_a_start": "2026-10-03", "window_a_end": "2026-10-05",
            "window_b_start": "2026-09-30", "window_b_end": "2026-10-02",  # 3 vs 3 days
        },
    )
    daily = _daily_call()
    err_rate = LLMToolCall(
        id="c-err", name="monitoring-mcp__get_error_rate",
        arguments={"service": "checkout", "window_start": "2026-10-03",
                   "window_end": "2026-10-05"},
    )
    trend = LLMToolCall(
        id="c-trend", name="postgres-mcp__get_sales_trend",
        arguments={"start_date": "2026-09-28", "end_date": "2026-10-05", "granularity": "day"},
    )
    alerts_call = LLMToolCall(
        id="c-alerts", name="monitoring-mcp__get_recent_alerts",
        arguments={"window_start": "2026-10-03", "window_end": "2026-10-05"},
    )
    grounded_report = InvestigationReport(
        conclusion="Checkout revenue fell exactly 31% during the incident window.",
        claims=[Claim(text="Revenue fell exactly 31%, which proves the deployment caused it.",
                      evidence_ids=["ev-0001"])],
        confidence=Confidence.high,
        confidence_rationale="single strong signal",
    )
    proposal_report = InvestigationReport(
        conclusion="Revenue fell while checkout alert ALT-2071 was open and unacknowledged.",
        claims=[
            Claim(text="Revenue fell during the incident.", evidence_ids=["ev-0001"]),
            Claim(text="Alert ALT-2071 was open.", evidence_ids=["ev-0002"]),
        ],
        confidence=Confidence.medium,
        confidence_rationale="two sources agree",
    )
    proposal = ActionProposal(
        tool="acknowledge_alert",
        arguments={"alert_id": "ALT-2071", "note": "acknowledged during incident response"},
        rationale="unacknowledged high-severity checkout alert in evidence",
        evidence_ids=["ev-0002"],
    )

    async def _run_dsel() -> CaseResult:
        from backend.mcp.client_manager import MultiMCPManager
        from backend.mcp.fixtures.sales_seed import ensure_seeded

        # ensure_seeded is sync (asyncio.run internally) — keep it off our loop
        await asyncio.to_thread(ensure_seeded, _admin_dsn())
        managers = MultiMCPManager({"postgres-mcp": _pg_manager_factory()})
        async with managers:
            return await case_registry.CASES["D-SEL"](
                llm=_scripted([unequal, equal]), managers=managers, settings=_settings(),
            )

    async def _run_dapprove() -> CaseResult:
        from backend.agent.loop import InvestigationLoop
        from backend.agent.state import InvestigationRecord
        from backend.agent.persistence.store import InvestigationStore
        from backend.security.approval_service import ApprovalService

        async with _eval_state_db() as eval_dsn:
            store = InvestigationStore(dsn=eval_dsn)
            clock = FixedClock(_now())
            session = FakeSession({
                "get_recent_alerts": [
                    {"alerts": [dict(ALERT)], "count": 1},  # pre-check: unacknowledged
                    {"alerts": [dict(ALERT, acknowledged=True, acknowledged_by="op-1",
                                      acknowledged_at="2026-10-05T12:00:01Z")], "count": 1},
                ]
            })
            service = ApprovalService(
                store, lambda: FakeSessionCM({"current": session}),
                clock=clock, execution_timeout_seconds=5.0,
            )
            managers = StubMulti({
                "postgres-mcp": StubServerManager(["get_daily_sales"]),
                "monitoring-mcp": StubServerManager(
                    ["get_recent_alerts"],
                    payloads={"get_recent_alerts": {"alerts": [dict(ALERT)], "count": 1}},
                ),
            })
            record = InvestigationRecord(
                investigation_id=f"inv-eval-{time.monotonic_ns() % 10**10:010d}",
                request_id=f"req-eval-{time.monotonic_ns() % 10**10:010d}",
                question="Why did today's sales drop?",
            )
            loop = InvestigationLoop(
                llm=_scripted([daily, alerts_call], report=proposal_report, proposal=proposal),
                manager_factory=lambda: managers,
                record=record,
                settings=_settings(action_mode_enabled=True),
                state_store=store,
                approval_service=service,
            )
            await loop.run()
            if not record.proposals:
                return CaseResult(
                    case_id="D-APPROVE",
                    checks=[CheckResult(name="proposal_persisted", passed=False,
                                        detail="loop produced no proposal row")],
                )
            persisted = record.proposals[0]
            await service.decide(persisted.approval_id, "approved", "op-1")
            await service.execute(persisted.approval_id, "op-2")
            final = await store.get_approval(persisted.approval_id)
            return await case_registry.CASES["D-APPROVE"](
                proposal_row=persisted, executed_row=final,
            )

    async def _run_dev() -> CaseResult:
        managers = StubMulti({
            "postgres-mcp": StubServerManager(["get_daily_sales"]),
            "monitoring-mcp": StubServerManager(["get_error_rate"]),
        })
        return await case_registry.CASES["D-EV"](
            llm=_scripted([daily, err_rate]), manager_factory=lambda: managers,
            settings=_settings(), expected_servers=frozenset({"postgres-mcp", "monitoring-mcp"}),
        )

    async def _run_dground() -> CaseResult:
        managers = StubMulti({"postgres-mcp": StubServerManager(["get_daily_sales"])})
        return await case_registry.CASES["D-GROUND"](
            llm=_scripted([daily], report=grounded_report),
            manager_factory=lambda: managers, settings=_settings(),
            expect_ok=False, expect_flagged=True, expect_downgraded=True,
        )

    async def _run_dgap() -> CaseResult:
        managers = StubMulti({"postgres-mcp": StubServerManager(["get_daily_sales"])})
        return await case_registry.CASES["D-GAP"](
            llm=_scripted([]), manager_factory=lambda: managers, settings=_settings(),
        )

    async def _run_dfail() -> CaseResult:
        managers = StubMulti({"postgres-mcp": StubServerManager(
            ["get_daily_sales", "get_sales_trend"], fail_names={"get_daily_sales"},
        )})
        return await case_registry.CASES["D-FAIL"](
            llm=_scripted([daily, trend]), manager_factory=lambda: managers,
            settings=_settings(), failing_tool="postgres-mcp__get_daily_sales",
        )

    async def _run_dcircuit() -> CaseResult:
        from backend.reliability import CircuitBreaker, CircuitState

        breaker = CircuitBreaker("monitoring-mcp", failure_threshold=3, cooldown_seconds=60.0)
        breaker.state = CircuitState.open
        breaker.opened_at = time.monotonic()
        managers = StubMulti({
            "postgres-mcp": StubServerManager(["get_daily_sales"]),
            "monitoring-mcp": StubServerManager(["get_error_rate"]),
        })
        managers.breakers = {"monitoring-mcp": breaker}
        return await case_registry.CASES["D-CIRCUIT"](
            llm=_scripted([daily, err_rate]), manager_factory=lambda: managers,
            settings=_settings(), unavailable_server="monitoring-mcp",
        )

    async def _run_dbudget(budget_class: str) -> CaseResult:
        if budget_class == "llm_calls":
            settings = _settings(max_llm_calls=2)
            factory = lambda: StubMulti({"postgres-mcp": StubServerManager(["get_daily_sales"])})  # noqa: E731
            llm = _scripted([daily])
        elif budget_class == "steps":
            settings = _settings(max_investigation_steps=1, max_llm_calls=16)
            factory = lambda: StubMulti({"postgres-mcp": StubServerManager(["get_daily_sales"])})  # noqa: E731
            llm = _scripted([daily])
        else:  # wall_clock
            settings = _settings(max_investigation_seconds=0.2)
            factory = HangingFactory
            llm = _scripted([])
        return await case_registry.CASES["D-BUDGET"](
            llm=llm, manager_factory=factory, settings=settings, budget_class=budget_class,
        )

    async def _run_dcancel() -> CaseResult:
        return await case_registry.CASES["D-CANCEL"](
            llm=_scripted([]), manager_factory=HangingFactory, settings=_settings(),
        )

    return [
        CaseSpec("D-SEL", _run_dsel, requires_postgres=True),
        CaseSpec("D-EV", _run_dev),
        CaseSpec("D-GROUND", _run_dground),
        CaseSpec("D-GAP", _run_dgap),
        CaseSpec("D-FAIL", _run_dfail),
        CaseSpec("D-CIRCUIT", _run_dcircuit),
        CaseSpec("D-BUDGET:llm_calls", lambda: _run_dbudget("llm_calls")),
        CaseSpec("D-BUDGET:steps", lambda: _run_dbudget("steps")),
        CaseSpec("D-BUDGET:wall_clock", lambda: _run_dbudget("wall_clock")),
        CaseSpec("D-APPROVE", _run_dapprove, requires_postgres=True),
        CaseSpec("D-CANCEL", _run_dcancel),
    ]


# -- suite execution --------------------------------------------------------------


def _failed_row(spec: CaseSpec, exc: Exception) -> CaseResult:
    """A case runner that raised still becomes an explicit FAILED row."""
    return CaseResult(
        case_id=spec.case_id,
        error_class=type(exc).__name__,
        checks=[CheckResult(name="case_executed", passed=False,
                            detail=f"{type(exc).__name__}: {exc}")],
        diagnostics={"runner_exception": True},
    )


def _run_case_sync(spec: CaseSpec) -> CaseResult:
    """One event loop PER CASE: the stdio transport's anyio finalizer task can
    outlive its case, and a shared loop lets it interfere with a later case's
    cancellation scopes (observed as cross-case CancelledError). A fresh loop
    per case matches the pytest-isolation semantics the cases were written in."""
    return asyncio.run(spec.run())


async def run_suite(run_dir: Path, specs: list[CaseSpec]) -> list[tuple[int, CaseResult]]:
    """Execute the specs in order, writing one fsynced row per case. Per-case
    exceptions are recorded as failed rows; a RUNNER-level interruption
    (KeyboardInterrupt/SystemExit/IO failure) writes the sidecar and
    re-raises — the manifest is then never written by the caller."""
    writer = RowsWriter(run_dir / "rows.jsonl")
    try:
        results: list[tuple[int, CaseResult]] = []
        for spec in specs:
            started = time.perf_counter()
            try:
                case = await asyncio.to_thread(_run_case_sync, spec)
            except Exception as exc:  # the case failed -> explicit failed row
                case = _failed_row(spec, exc)
            # the runner owns run-level case identity: parameterized variants
            # (e.g. D-BUDGET:wall_clock) are distinguished by their spec id
            case = case.model_copy(update={
                "case_id": spec.case_id,
                "latency_ms": round(case.latency_ms or (time.perf_counter() - started) * 1000, 1),
            })
            seq = writer.write(case)
            results.append((seq, case))
        return results
    except BaseException as exc:  # noqa: BLE001  (interrupt must leave the sidecar)
        write_incomplete(run_dir, writer.completed_seq,
                         f"{type(exc).__name__}: {exc}")
        raise
    finally:
        writer.close()


def _git_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT,
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _git_dirty() -> bool:
    """True when the working tree has uncommitted changes — a manifest must
    never claim that such a run corresponds exactly to the HEAD commit."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"], cwd=ROOT,
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return bool(result.stdout.strip())
    except Exception:
        pass
    return True  # unknown tree state: assume dirty rather than claim clean


def _scripted_model_block() -> dict:
    return {"kind": "scripted", "name": "scripted", "ollama_required": False}


def _real_model_block() -> dict:
    """Endpoint identity WITHOUT credentials: host URL and sampling config
    only. Tokens/.env values are never read into manifests."""
    settings = get_settings()
    return {
        "kind": "real",
        "name": settings.ollama_model,
        "endpoint": settings.ollama_host,
        "temperature": settings.llm_temperature,
        "timeout_seconds": settings.llm_timeout_seconds,
        "retry_attempts": settings.llm_retry_attempts,
        "ollama_required": True,
    }


def _build_manifest(run_dir: Path, label: str, started_utc: str,
                    results: list[tuple[int, CaseResult]], *, suite: str = "deterministic",
                    model: dict | None = None) -> dict:
    from backend.mcp.fixtures.loader import DEFAULT_SCENARIO_PATH, load_scenario

    spec = load_scenario()
    settings = get_settings()
    cases_meta = [
        {"seq": seq, "case_id": case.case_id, "passed": case.passed,
         "failure_layer": case.failure_layer,
         "failed_checks": [c.name for c in case.checks if not c.passed]}
        for seq, case in results
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": f"bench-{uuid.uuid4().hex[:12]}",
        "label": label,
        "suite": suite,
        "kind": suite,
        "started_utc": started_utc,
        "finished_utc": _utc(),
        "code_commit": _git_commit(),
        "code_dirty": _git_dirty(),
        "scenario": {"name": spec.name, "seed": spec.seed,
                     "source_path": str(DEFAULT_SCENARIO_PATH)},
        "model": model or _scripted_model_block(),
        "config": {
            "max_investigation_steps": settings.max_investigation_steps,
            "max_llm_calls": settings.max_llm_calls,
            "max_investigation_seconds": settings.max_investigation_seconds,
            "mcp_call_timeout_seconds": settings.mcp_call_timeout_seconds,
        },
        "run_dir": run_dir.name,
        "cases": cases_meta,
        "summary": {
            "cases": len(results),
            "passed": sum(1 for _, c in results if c.passed),
            "failed": sum(1 for _, c in results if not c.passed),
        },
    }


def build_model_specs(llm) -> list[CaseSpec]:
    """Wire the model-dependent cases (Gate 3): real stdio servers, real LLM
    wrapped by the case in RecordingLLM. Action mode stays disabled."""
    from backend.evaluation import cases_model

    settings = Settings(max_investigation_seconds=1200, max_llm_calls=16)

    async def _run_msel() -> CaseResult:
        from backend.agent.service import default_server_factories
        from backend.mcp.client_manager import MultiMCPManager

        managers = MultiMCPManager(default_server_factories(get_settings()))
        async with managers:
            return await cases_model.case_msel(
                llm=llm, managers=managers, settings=get_settings())

    async def _run_mcanon() -> CaseResult:
        return await cases_model.case_mcanon(llm=llm, settings=settings)

    return [CaseSpec("M-SEL", _run_msel), CaseSpec("M-CANON", _run_mcanon)]


async def _build_real_llm():
    from backend.agent.llm.adapter import LLMError, OpenAICompatLLM

    settings = get_settings()
    llm = OpenAICompatLLM(
        base_url=f"{settings.ollama_host}/v1",
        model=settings.ollama_model,
        timeout_seconds=settings.llm_timeout_seconds,
        temperature=settings.llm_temperature,
        retry_attempts=settings.llm_retry_attempts,
        retry_backoff_seconds=settings.llm_retry_backoff_seconds,
    )
    try:
        await llm.check_provider()
    except LLMError as exc:
        raise RuntimeError(
            f"the model suite requires Ollama ({settings.ollama_model} at "
            f"{settings.ollama_host}); it is unreachable: {exc}"
        ) from exc
    return llm


async def main_async(args: argparse.Namespace) -> int:
    if args.suite == "deterministic":
        specs = build_case_specs()
        if any(s.requires_postgres for s in specs):
            await _require_postgres()
        model_block = None
    else:
        llm = await _build_real_llm()
        specs = build_model_specs(llm)
        model_block = _real_model_block()
    out_dir = Path(args.out) if args.out else DEFAULT_OUT_DIR
    run_dir = build_run_dir(out_dir, args.label)
    started_utc = _utc()
    print(f"[run_benchmark] suite={args.suite} run_dir={run_dir}")
    results = await run_suite(run_dir, specs)
    manifest = _build_manifest(run_dir, args.label, started_utc, results,
                               suite=args.suite, model=model_block)
    write_manifest(run_dir, manifest)  # LAST + atomic: the only success marker
    summary = manifest["summary"]
    print(f"[run_benchmark] cases={summary['cases']} "
          f"passed={summary['passed']} failed={summary['failed']}")
    for seq, case in results:
        if not case.passed:
            failed = [c.name for c in case.checks if not c.passed]
            print(f"[run_benchmark]   FAILED {case.case_id} "
                  f"[{case.failure_layer}]: {failed}")
    print(f"[run_benchmark] manifest: {run_dir / 'manifest.json'}")
    return 0 if summary["failed"] == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--suite", choices=["deterministic", "model"],
                        default="deterministic",
                        help="deterministic: scripted, no Ollama; model: real "
                             "Ollama runs (M-SEL probes + M-CANON investigation)")
    parser.add_argument("--label", required=True,
                        help="short label appended to the run directory name")
    parser.add_argument("--out", default=None,
                        help=f"output root (default: {DEFAULT_OUT_DIR})")
    args = parser.parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("[run_benchmark] interrupted; run_incomplete.json written (no manifest)", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"[run_benchmark] FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
