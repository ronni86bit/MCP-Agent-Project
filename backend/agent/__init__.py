"""Agent runtime: planning, tool selection, execution, evidence, verification, synthesis.

The loop in `loop.py` is the product; every component here is explicit and
typed. Model access only ever goes through `agent/llm/` — the runtime must not
know which provider is behind the adapter.
"""
