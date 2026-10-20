"""Conservative failure-layer classification (Phase 9 Gate 3).

Pure function over OBSERVED evidence only. Exactly five layers:

- `none`      the case passed.
- `infra`     observed typed infrastructure errors (the Phase 6/8 taxonomy:
              provider unavailable/timeout, model missing, transport, tool
              timeout, circuit open) or a verified dependency failure — the
              github server's deterministic rate-limit ToolError text counts
              as a verified dependency failure; nothing else inferred from
              message text.
- `budget`    recorded budget exhaustion, with no infra error observed.
- `reasoning` infrastructure VERIFIED healthy AND a terminal run was obtained
              AND grading failed. Never inferred from a mere exception.
- `unknown`   everything else: harness cancellation/timeouts, ambiguous
              partials, or insufficient evidence to establish a layer.

Diagnostics travel with the verdict (into result rows) — never into metric
labels — and contain only class names and the caller-supplied detail strings.
"""

from __future__ import annotations

from dataclasses import dataclass, field

LAYERS = ("none", "infra", "budget", "reasoning", "unknown")

# Typed error classes counted as infrastructure evidence (client-side taxonomy:
# backend/mcp/errors.py + backend/agent/llm/adapter.py + reliability.py).
INFRA_ERROR_CLASSES = frozenset({
    "ProviderUnavailableError", "ProviderTimeoutError", "ModelNotAvailableError",
    "TransportError", "ToolTimeoutError", "CircuitOpenError",
})

# The github-mcp server raises ToolError with this deterministic text when the
# REST API signals exhaustion; the client surfaces it as ToolExecutionError.
# Matching is exact-substring, case-insensitive, on this one documented phrase.
_RATE_LIMIT_SIGNATURE = "rate limit"


@dataclass(frozen=True)
class FailureEvidence:
    """What the runner observed about one model-suite case."""

    case_passed: bool
    terminal: bool = True                                # run reached a terminal status
    budget_exhausted: bool = False                       # recorded budget exhaustion
    infra_error_classes: tuple[str, ...] = ()            # typed classes actually observed
    rate_limit_signature_seen: bool = False              # verified dependency failure
    infra_verified_healthy: bool | None = None           # True/False/None (not established)
    harness_error_class: str | None = None               # runner/harness-level failure
    detail: dict = field(default_factory=dict)           # caller-supplied diagnostics


@dataclass(frozen=True)
class FailureVerdict:
    layer: str
    diagnostics: dict


def classify_failure(evidence: FailureEvidence) -> FailureVerdict:
    if evidence.case_passed:
        return FailureVerdict("none", {"detail": "all checks passed"})

    if evidence.harness_error_class is not None or not evidence.terminal:
        return FailureVerdict("unknown", {
            "reason": "harness-level failure or non-terminal run; layer not established",
            "harness_error_class": evidence.harness_error_class,
            **evidence.detail,
        })

    if evidence.infra_error_classes:
        return FailureVerdict("infra", {
            "infra_error_classes": sorted(set(evidence.infra_error_classes)),
            **evidence.detail,
        })

    if evidence.rate_limit_signature_seen or evidence.infra_verified_healthy is False:
        return FailureVerdict("infra", {
            "reason": ("dependency rate-limited" if evidence.rate_limit_signature_seen
                       else "dependency verified unavailable"),
            **evidence.detail,
        })

    if evidence.budget_exhausted:
        return FailureVerdict("budget", {"reason": "recorded budget exhaustion",
                                         **evidence.detail})

    if evidence.infra_verified_healthy is True:
        return FailureVerdict("reasoning", {
            "reason": "infrastructure verified healthy; grading failed on a terminal run",
            **evidence.detail,
        })

    return FailureVerdict("unknown", {
        "reason": "insufficient evidence to establish the failure layer",
        **evidence.detail,
    })
