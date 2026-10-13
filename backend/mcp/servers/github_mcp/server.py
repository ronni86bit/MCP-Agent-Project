"""github-mcp tool definitions (four approved read-only tools, nothing else).

Real GitHub REST data via the read-only token from configuration. The
deployments result additionally carries ONE clearly labeled record from
scenario.yaml — the demo storyline's checkout deployment — so cross-system
correlation is demonstrable without presenting invented data as real GitHub
state (its `source` field says `scenario-fixture`).
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

import structlog
from pydantic import ValidationError
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from backend.config import get_settings
from backend.mcp.fixtures.loader import load_scenario
from backend.mcp.servers.github_mcp.client import GitHubClient, GitHubError
from backend.mcp.servers.github_mcp.models import ActivityInputs, CommitsInputs, LimitInputs

mcp = MCPServer(
    "github-mcp",
    instructions=(
        "Read-only engineering activity for one GitHub repository: recent commits, "
        "deployments, open issues, and repository activity summary. Use it to see which "
        "software changes shipped and when. Deployments may include one record labeled "
        "source=scenario-fixture that belongs to the demo storyline rather than live "
        "GitHub state. All tools are read-only."
    ),
)

log = structlog.get_logger(__name__)
_settings = get_settings()
_client = GitHubClient(repo=_settings.github_repo, token=_settings.github_token)


def _inputs(model: type, **kwargs: Any) -> Any:
    try:
        return model(**kwargs)
    except ValidationError as exc:
        reasons = "; ".join(
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise ToolError(f"invalid arguments: {reasons}") from None


def _finish(tool: str, started: float, payload: dict[str, Any]) -> dict[str, Any]:
    log.info(
        "github_tool_executed",
        tool=tool,
        count=payload.get("count", 0),
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
    )
    return payload


def scenario_deployment_record() -> dict[str, Any]:
    """The demo storyline's checkout deployment, from scenario.yaml."""
    deployment = load_scenario().deployment
    seed = f"{deployment.repository}:{deployment.deployed_at_utc.isoformat()}"
    return {
        "id": 0,
        "sha": hashlib.sha256(seed.encode()).hexdigest()[:10],
        "ref": "main",
        "environment": deployment.environment,
        "service": deployment.service,
        "created_at": deployment.deployed_at_utc.isoformat().replace("+00:00", "Z"),
        "description": deployment.commit_message,
        "creator": "demo-scenario",
        "source": "scenario-fixture",
    }


@mcp.tool()
async def get_recent_commits(limit: int = 10, branch: str = "main") -> dict[str, Any]:
    """Recent commits on a branch of the configured repository, newest first.

    Args:
        limit: maximum number of commits to return (1-30, default 10)
        branch: branch name (default "main")
    """
    inp: CommitsInputs = _inputs(CommitsInputs, limit=limit, branch=branch)
    started = time.perf_counter()
    try:
        commits = await _client.recent_commits(limit=inp.limit, branch=inp.branch)
    except GitHubError as exc:
        raise ToolError(f"github request failed: {exc}") from None
    return _finish(
        "get_recent_commits", started,
        {"repository": _client.repo, "branch": inp.branch, "count": len(commits), "commits": commits},
    )


@mcp.tool()
async def get_deployments(limit: int = 10) -> dict[str, Any]:
    """Deployment records for the repository, newest first.

    Includes live GitHub deployments plus ONE record labeled
    source=scenario-fixture that belongs to the demo storyline (not live
    GitHub state) — use its timestamp for timeline correlation in the demo.

    Args:
        limit: maximum number of deployments to return (1-30, default 10)
    """
    inp: LimitInputs = _inputs(LimitInputs, limit=limit)
    started = time.perf_counter()
    try:
        live = await _client.deployments(limit=inp.limit)
    except GitHubError as exc:
        raise ToolError(f"github request failed: {exc}") from None
    records = [*live, scenario_deployment_record()]
    records.sort(key=lambda record: record.get("created_at") or "", reverse=True)
    return _finish(
        "get_deployments", started,
        {
            "repository": _client.repo,
            "count": len(records),
            "deployments": records,
            "note": "records with source=scenario-fixture are demo storyline data, not live GitHub state",
        },
    )


@mcp.tool()
async def get_open_issues(limit: int = 10) -> dict[str, Any]:
    """Currently open issues in the repository, newest first.

    Args:
        limit: maximum number of issues to return (1-30, default 10)
    """
    inp: LimitInputs = _inputs(LimitInputs, limit=limit)
    started = time.perf_counter()
    try:
        issues = await _client.open_issues(limit=inp.limit)
    except GitHubError as exc:
        raise ToolError(f"github request failed: {exc}") from None
    return _finish(
        "get_open_issues", started,
        {"repository": _client.repo, "count": len(issues), "issues": issues},
    )


@mcp.tool()
async def get_repository_activity(days: int = 14, limit: int = 30) -> dict[str, Any]:
    """Repository activity summary for a trailing window of days.

    Returns push timestamp, open-issue count, commit count in the window,
    and the latest commit.

    Args:
        days: trailing window size in days (1-90, default 14)
        limit: maximum number of commits to scan for the window (1-30, default 30)
    """
    inp: ActivityInputs = _inputs(ActivityInputs, days=days, limit=limit)
    started = time.perf_counter()
    try:
        activity = await _client.repository_activity(days=inp.days, limit=inp.limit)
    except GitHubError as exc:
        raise ToolError(f"github request failed: {exc}") from None
    return _finish("get_repository_activity", started, dict(activity, count=1))
