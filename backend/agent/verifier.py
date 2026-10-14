"""VERIFY: deterministic grounding checks over the synthesized report.

Phase 5 scope: evidence-ID existence, provenance preservation, and
DETERMINISTIC value/entity checks — every percentage and currency figure and
every quoted or multi-word capitalized entity in a claim must be locatable in
the claim's cited evidence summaries, or the claim is flagged and confidence
is capped. This narrows (but does NOT solve) semantic grounding: claims whose
numbers are derived rather than quoted can still slip through; comprehensive
evaluation belongs to Phase 9. `verify()` stays the single entry point so
stronger checkers can be added there.
"""

from __future__ import annotations

import json
import re

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
_VALUE_RE = re.compile(r"\d+(?:\.\d+)?%|\$\d[\d,]*(?:\.\d+)?")
_QUOTED_RE = re.compile(r"\"([^\"]{3,60})\"")
_ENTITY_RE = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b")


def _normalized(text: str) -> str:
    return text.replace(",", "").lower()


_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _unsupported_tokens(claim_text: str, corpus: str) -> list[str]:
    """Value and entity tokens in the claim that appear nowhere in the corpus.

    Numbers are compared by numeric value (sign, percent suffix, and thousand
    separators ignored), so a claim's "31%" matches evidence "-31.0".
    """
    corpus_norm = _normalized(corpus)
    corpus_numbers: set[float] = set()
    for match in _NUMBER_RE.findall(corpus_norm):
        try:
            # magnitudes: a claim's "31%" may come from evidence "delta_pct: -31.0"
            corpus_numbers.add(round(abs(float(match)), 4))
        except ValueError:  # pragma: no cover
            continue
    missing: list[str] = []
    for token in _VALUE_RE.findall(claim_text):
        number = _NUMBER_RE.search(token.replace(",", ""))
        try:
            claim_number = round(abs(float(number.group(0))), 4) if number else None
        except ValueError:  # pragma: no cover
            claim_number = None
        if claim_number is None or claim_number not in corpus_numbers:
            missing.append(token)
    for quoted in _QUOTED_RE.findall(claim_text):
        if _normalized(quoted) not in corpus_norm:
            missing.append(f"\"{quoted}\"")
    for entity in _ENTITY_RE.findall(claim_text):
        if _normalized(entity) not in corpus_norm:
            missing.append(entity)
    return missing


def _claim_corpus(claim_evidence_ids: list[str], store: EvidenceStore) -> str:
    parts = []
    for ref in claim_evidence_ids:
        item = store.get(ref)
        if item is not None:
            parts.append(json.dumps(item.summary, default=str))
            parts.append(json.dumps(item.arguments, default=str))
    return " ".join(parts)


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

    # Deterministic value/entity grounding: percentages, currency amounts,
    # quoted strings, and multi-word capitalized entities in a claim must be
    # locatable in the cited evidence, else the claim is flagged (kept, but
    # confidence is capped and the gap is explicit).
    flagged: list[str] = []
    grounding_notes: list[str] = []
    for claim in kept_claims:
        corpus = _claim_corpus(claim.evidence_ids, store)
        if not corpus:
            continue
        missing = _unsupported_tokens(claim.text, corpus)
        if missing:
            flagged.append(claim.text)
            grounding_notes.append(
                f"claim cites {claim.evidence_ids} but these values/entities are not in that "
                f"evidence: {missing}"
            )
    if flagged:
        downgraded = True
        if confidence == Confidence.high:
            confidence = Confidence.medium
        caveats.append(
            "Deterministic grounding check flagged claims whose figures/entities could not be "
            "located in the cited evidence; treat those figures as unverified."
        )
        gaps.append(f"{len(flagged)} claim(s) flagged by value/entity grounding checks")

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
            ok=False,
            dropped_claims=dropped,
            flagged_claims=flagged,
            downgraded=True,
            grounding_checked=True,
            notes=(notes + grounding_notes) or ["report unsupported"],
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
        ok=not downgraded,
        dropped_claims=dropped,
        flagged_claims=flagged,
        downgraded=downgraded,
        grounding_checked=True,
        notes=notes + grounding_notes,
    )
