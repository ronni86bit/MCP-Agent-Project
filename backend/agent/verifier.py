"""VERIFY: deterministic grounding checks over the synthesized report.

Phase 3 scope only: references exist, claims carry evidence, required fields
present, confidence not overstated, and causal language downgraded to
correlation wording. Full anti-causation evaluation belongs to Phase 9.
"""

from __future__ import annotations

from backend.agent.evidence import EvidenceStore
from backend.agent.models import Confidence, InvestigationReport, VerificationResult

_CAUSAL_TERMS = (
    "proves",
    "proven",
    "definitely caused",
    "is caused by",
    "was caused by",
    "root cause is",
    "certainly",
    "without doubt",
    "confirms that",
)
_CAUTIOUS_HINT = (
    "Sales data alone cannot prove causation; phrase findings as 'consistent with', "
    "'likely contributor', or 'correlated with'."
)


def verify(report: InvestigationReport, store: EvidenceStore) -> tuple[InvestigationReport, VerificationResult]:
    notes: list[str] = []
    dropped: list[str] = []
    gaps = list(report.evidence_gaps)
    caveats = list(report.caveats)

    kept_claims = []
    for claim in report.claims:
        valid_refs = [ref for ref in claim.evidence_ids if store.get(ref) is not None]
        bad_refs = [ref for ref in claim.evidence_ids if store.get(ref) is None]
        if bad_refs:
            dropped.append(claim.text)
            gaps.append(f"claim dropped — references unknown evidence {bad_refs}: {claim.text[:120]}")
            continue
        if not valid_refs:
            dropped.append(claim.text)
            gaps.append(f"claim dropped — no evidence references: {claim.text[:120]}")
            continue
        if len(valid_refs) != len(claim.evidence_ids):
            claim = claim.model_copy(update={"evidence_ids": valid_refs})
        kept_claims.append(claim)

    confidence = report.confidence
    downgraded = False
    text_blob = " ".join([report.conclusion, *(claim.text for claim in kept_claims)]).lower()
    if any(term in text_blob for term in _CAUSAL_TERMS):
        downgraded = True
        confidence = Confidence.low
        caveats.append("Causal wording detected and downgraded. " + _CAUTIOUS_HINT)
        notes.append("causal language downgraded")

    cited = {ref for claim in kept_claims for ref in claim.evidence_ids}
    if confidence == Confidence.high and (len(kept_claims) < 2 or len(cited) < 2):
        downgraded = True
        confidence = Confidence.medium
        notes.append("high confidence requires >=2 claims citing >=2 distinct evidence records")

    if not kept_claims:
        downgraded = True
        confidence = Confidence.low
        caveats.append(_CAUTIOUS_HINT)
        gaps.append("no supported claims remained after verification")
        report = report.model_copy(
            update={
                "conclusion": (
                    "Insufficient grounded evidence is available to answer the question. "
                    + report.conclusion[:400]
                ),
                "confidence": confidence,
                "claims": [],
                "caveats": caveats,
                "evidence_gaps": gaps,
            }
        )
        result = VerificationResult(
            ok=False, dropped_claims=dropped, downgraded=True, notes=notes or ["report unsupported"]
        )
        return report, result

    report = report.model_copy(
        update={
            "claims": kept_claims,
            "confidence": confidence,
            "caveats": caveats,
            "evidence_gaps": gaps,
        }
    )
    return report, VerificationResult(
        ok=not downgraded, dropped_claims=dropped, downgraded=downgraded, notes=notes
    )
