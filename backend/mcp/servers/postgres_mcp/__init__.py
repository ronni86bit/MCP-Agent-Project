"""postgres-mcp: read-only business sales analytics MCP server.

Strictly read-only: the server connects with the `postgres_mcp_ro` role
(SELECT grants + `default_transaction_read_only`), and every statement it can
run lives in `queries.py` as a static, parameterized template. There is no
free-form SQL tool and the model never authors SQL.
"""
