"""Evidence store: ids, sanitization, result summarization, digest."""

from backend.agent.evidence import EvidenceStore, sanitize, summarize_result


def test_ids_are_sequential_and_lookup_works():
    store = EvidenceStore("inv-1")
    first = store.add(tool_name="t", server="s", step=1, arguments={}, payload={"count": 0})
    second = store.add(tool_name="t2", server="s", step=2, arguments={}, payload={"count": 1})
    assert (first.evidence_id, second.evidence_id) == ("ev-0001", "ev-0002")
    assert store.get("ev-0002") is second
    assert store.get("ev-0003") is None
    assert len(store) == 2


def test_sanitize_truncates_long_strings():
    long = "x" * 500
    cleaned = sanitize({"a": long, "b": [long], "c": 1})
    assert len(cleaned["a"]) == 200 and cleaned["a"].endswith("...")
    assert len(cleaned["b"][0]) == 200
    assert cleaned["c"] == 1


def test_summarize_result_caps_row_previews():
    rows = [{"i": i} for i in range(50)]
    summary = summarize_result({"count": 50, "rows": rows})
    assert summary["count"] == 50
    assert len(summary["rows"]) == 3
    assert summary["rows_total"] == 50


def test_digest_mentions_evidence_ids():
    store = EvidenceStore("inv-1")
    store.add(tool_name="get_daily_sales", server="postgres-mcp", step=1,
              arguments={"start_date": "2026-10-05"}, payload={"count": 1})
    digest = store.digest()
    assert "[ev-0001]" in digest and "get_daily_sales" in digest
    assert store.digest() == "(no evidence collected)" if len(store) == 0 else True
