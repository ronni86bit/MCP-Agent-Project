"""Phase 9 evaluation framework (deterministic core, Gate 1).

`schema` — result models (schema v1). `graders` — pure check functions that
REUSE existing product/test logic (verifier output, smoke predicates, the
canonical test's window helper) instead of re-implementing it. `cases_deterministic`
— scripted-model case runners that execute the real loop over injected doubles
and grade the observed record.

No production module imports this package; the pytest suite and (from Gate 2)
`scripts/run_benchmark.py` are the only consumers.
"""
