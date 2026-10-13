"""github-mcp: read-only engineering-activity MCP server over the GitHub REST API.

Read-only fine-grained token from the environment (GITHUB_TOKEN); the token is
never logged, echoed in errors, or embedded in tool results. Deployments
include one clearly labeled record from scenario.yaml (the demo storyline's
checkout deployment) so the demo can correlate engineering changes with the
operational and sales timelines without fabricating live GitHub data.
"""
