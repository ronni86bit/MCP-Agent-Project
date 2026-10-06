"""Verifier grounding rules: refs exist, claims carry evidence, language policy."""

from backend.agent.evidence import EvidenceStore
from backend.agent.models import Claim, Confidence, InvestigationReport
from backend.agent.verifier import verify


def _store() -> EvidenceStore:
    store = EvidenceStore("inv-test")
    store.add(
        tool_name="get_daily_sales",
        server="postgres-mcp",
        step=1,
        arguments={"start_date": "2026-10-05"},
        payload={"count": 1, "rows": [{"date": "2026-10-05", "revenue": 100.0}]},
    )
    store.add(
        tool_name="compare_sales",
        server="postgres-mcp",
        step=2,
        arguments={},
        payload={"delta_pct": -31.0},
    )
    return store


def test_valid_claims_survive_and_verification_ok():
    report = InvestigationReport(
        conclusion="Revenue declined, consistent with the evidence.",
        claims=[
            Claim(text="Revenue dropped 31%.", evidence_ids=["ev-0001", "ev-0002"]),
            Claim(text="The last day was the weakest.", evidence_ids=["ev-0001"]),
        ],
        confidence=Confidence.medium,
    )
    verified, result = verify(report, _store())
    assert result.ok and not result.downgraded
    assert len(verified.claims) == 2
    assert verified.confidence == Confidence.medium


def test_unknown_and_missing_references_are_dropped():
    report = InvestigationReport(
        conclusion="Mixed support.",
        claims=[
            Claim(text="Good claim.", evidence_ids=["ev-0001"]),
            Claim(text="Unknown evidence.", evidence_ids=["ev-9999"]),
            Claim(text="No evidence at all.", evidence_ids=[]),
        ],
        confidence=Confidence.low,
    )
    verified, result = verify(report, _store())
    assert result.ok
    assert len(result.dropped_claims) == 2
    assert [claim.text for claim in verified.claims] == ["Good claim."]
    assert verified.evidence_gaps, "drops must be surfaced as evidence gaps"


def test_causal_language_is_downgraded_to_low_confidence():
    report = InvestigationReport(
        conclusion="The deployment definitely caused the sales drop and this proves it.",
        claims=[Claim(text="Revenue fell.", evidence_ids=["ev-0001"])],
        confidence=Confidence.high,
    )
    verified, result = verify(report, _store())
    assert result.downgraded
    assert verified.confidence == Confidence.low
    assert any("causal" in note for note in result.notes)


def test_high_confidence_requires_multiple_supported_claims():
    report = InvestigationReport(
        conclusion="Certain reading of one data point.",
        claims=[Claim(text="Only one claim.", evidence_ids=["ev-0001"])],
        confidence=Confidence.high,
    )
    verified, result = verify(report, _store())
    assert result.downgraded
    assert verified.confidence == Confidence.medium


def test_unsupported_report_is_overridden():
    report = InvestigationReport(
        conclusion="Totally unsupported conclusion.",
        claims=[Claim(text="Dropped.", evidence_ids=["ev-4242"])],
        confidence=Confidence.high,
    )
    verified, result = verify(report, _store())
    assert not result.ok
    assert verified.claims == []
    assert verified.confidence == Confidence.low
    assert verified.conclusion.startswith("Insufficient grounded evidence")
