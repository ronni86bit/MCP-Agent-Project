"""Phase 5 grounding checks: deterministic value/entity verification.

These checks narrow (not solve) semantic grounding: flagged claims stay in the
report with capped confidence and explicit caveats.
"""

from backend.agent.evidence import EvidenceStore
from backend.agent.models import Claim, Confidence, InvestigationReport
from backend.agent.verifier import verify


def _store() -> EvidenceStore:
    store = EvidenceStore("inv-ground")
    store.add(
        tool_name="get_error_rate",
        server="monitoring-mcp",
        step=1,
        arguments={"service": "checkout"},
        payload={"summary": {"window_avg_pct": 10.3, "peak_pct": 11.76}, "count": 3},
    )
    store.add(
        tool_name="compare_sales",
        server="postgres-mcp",
        step=2,
        arguments={},
        payload={"delta_pct": -31.0, "delta": -901971.33},
    )
    return store


def test_supported_percent_and_signed_values_pass():
    report = InvestigationReport(
        conclusion="Error rate 10.3% aligns with a 31% revenue decline.",
        claims=[
            Claim(
                text="Checkout error rate averaged 10.3% while revenue fell 31% versus baseline.",
                evidence_ids=["ev-0001", "ev-0002"],
            ),
        ],
        confidence=Confidence.medium,
    )
    verified, result = verify(report, _store())
    assert result.grounding_checked
    assert result.flagged_claims == []
    assert not result.downgraded
    assert verified.confidence == Confidence.medium


def test_unsupported_value_is_flagged_and_capped():
    report = InvestigationReport(
        conclusion="Error rate jumped by 44.7% according to telemetry.",
        claims=[
            Claim(text="The error rate jumped by 44.7% in the window.", evidence_ids=["ev-0001"]),
        ],
        confidence=Confidence.high,
    )
    verified, result = verify(report, _store())
    assert result.flagged_claims, "44.7% appears nowhere in the cited evidence"
    assert result.downgraded
    assert verified.confidence == Confidence.medium  # capped from high
    assert any("unverified" in caveat for caveat in verified.caveats)
    assert verified.claims, "flagged claims are retained, not silently dropped"


def test_unsupported_named_entity_is_flagged():
    report = InvestigationReport(
        conclusion="The Nimbus Keyboard line drove the decline.",
        claims=[
            Claim(text="Sales fell because the Nimbus Keyboard line went offline.", evidence_ids=["ev-0002"]),
        ],
        confidence=Confidence.low,
    )
    verified, result = verify(report, _store())
    assert result.flagged_claims
    assert any("Nimbus Keyboard" in note for note in result.notes)


def test_quoted_entities_are_checked():
    report = InvestigationReport(
        conclusion="Nothing to see.",
        claims=[Claim(text='Incident "INC-9999" was opened during the window.', evidence_ids=["ev-0001"])],
        confidence=Confidence.low,
    )
    verified, result = verify(report, _store())
    assert result.flagged_claims
    assert any('"INC-9999"' in note for note in result.notes)
