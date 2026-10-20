"""Evaluation result models, schema v1 (Phase 9 Gate 1).

Row-level models only: the benchmark manifest/rows-file format lands with the
Gate 2 runner. Everything here is pydantic and serializable so later gates can
emit JSONL without re-shaping.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

SCHEMA_VERSION = 1

CaseKind = Literal["deterministic", "model"]


class CheckResult(BaseModel):
    """One named pass/fail check with a human-readable detail line."""

    model_config = ConfigDict(frozen=True)

    name: str
    passed: bool
    detail: str = ""


def check(name: str, passed: bool, detail: str = "") -> CheckResult:
    return CheckResult(name=name, passed=bool(passed), detail=detail)


class CaseResult(BaseModel):
    """Outcome of one evaluation case run.

    `passed` is a computed field (all checks passed) — callers cannot assert a
    pass the recorded checks do not support, and it serializes with the row.
    `observed` carries the structured evidence the checks were evaluated
    against; `expected` the machine-readable contract. Snapshots stay JSON-safe.
    """

    model_config = ConfigDict(frozen=True)

    case_id: str
    suite: str = "deterministic"
    kind: CaseKind = "deterministic"
    input: dict[str, Any] = Field(default_factory=dict)
    expected: dict[str, Any] = Field(default_factory=dict)
    observed: dict[str, Any] = Field(default_factory=dict)
    checks: list[CheckResult] = Field(default_factory=list)
    error_class: str | None = None
    latency_ms: float = 0.0
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    # Failure-layer classification (Phase 9 Gate 3). "none" for deterministic
    # rows and passing cases; model-suite rows carry the conservative verdict.
    failure_layer: str = "none"
    failure_diagnostics: dict[str, Any] = Field(default_factory=dict)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)
