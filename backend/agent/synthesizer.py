"""REPORT: grounded synthesis from the evidence digest only."""

from __future__ import annotations

from backend.agent.llm.adapter import LLMClient, LLMMessage
from backend.agent.models import InvestigationReport, Plan


async def synthesize(
    llm: LLMClient, question: str, plan: Plan, evidence_digest: str, *, max_tokens: int = 700
) -> InvestigationReport:
    system = (
        "You write the final report of a sales-data investigation. "
        "Ground EVERY claim strictly in the numbered evidence records provided; cite their "
        "evidence ids in each claim. Never invent numbers or evidence. "
        "Use cautious language — 'consistent with', 'likely contributor', 'correlated with' — "
        "because sales data alone cannot prove causation. "
        "If the evidence is insufficient, say so explicitly and list what is missing. "
        "Be brief: at most 3 claims, each under 30 words; conclusion under 80 words; "
        "no markdown, JSON only."
    )
    user = (
        f"Question: {question}\n\nPlan: {plan.model_dump_json()}\n\n"
        f"Evidence records:\n{evidence_digest}\n\n"
        "Return JSON with fields: conclusion (string), claims (list of {text, evidence_ids}), "
        "confidence (\"high\"|\"medium\"|\"low\"), confidence_rationale (string), "
        "caveats (list of strings), evidence_gaps (list of strings)."
    )
    report, _ = await llm.complete_structured(
        [LLMMessage(role="system", content=system), LLMMessage(role="user", content=user)],
        InvestigationReport,
        max_tokens=max_tokens,
    )
    return report
