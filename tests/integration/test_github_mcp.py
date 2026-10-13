"""github-mcp integration tests: subprocess server against a local mock GitHub
API (GITHUB_API_BASE_URL override) — deterministic, no live GitHub required."""

import sys

import pytest

from backend.mcp.client_manager import MCPClientManager
from backend.mcp.errors import MCPClientError
from backend.mcp.fixtures.loader import load_scenario
from tests.support.github_mock import build_github_mock

EXPECTED_TOOLS = {
    "github-mcp__get_recent_commits",
    "github-mcp__get_deployments",
    "github-mcp__get_open_issues",
    "github-mcp__get_repository_activity",
}


@pytest.fixture(scope="module")
def github_mock():
    server = build_github_mock()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def github_manager(github_mock):
    return MCPClientManager(
        "github-mcp",
        sys.executable,
        ["-m", "backend.mcp.servers.github_mcp"],
        env={"GITHUB_API_BASE_URL": github_mock},
        call_timeout_seconds=15,
    )


def mcp_data(result) -> dict:
    structured = getattr(result, "structured_content", None)
    if structured:
        return structured
    import json

    return json.loads("".join(getattr(b, "text", "") for b in result.content))


async def test_discovery_offers_exactly_the_four_approved_tools(github_manager):
    async with github_manager as mcp:
        assert {tool.name for tool in mcp.tools()} == {t.split("__")[1] for t in EXPECTED_TOOLS}


async def test_get_recent_commits(github_manager):
    async with github_manager as mcp:
        data = mcp_data(await mcp.call_tool("get_recent_commits", {"limit": 5}))
    assert data["count"] == 2
    assert data["commits"][0]["message"].startswith("checkout: fix mobile payment retry loop")
    assert len(data["commits"][0]["sha"]) == 10


async def test_get_deployments_includes_labeled_scenario_fixture(github_manager):
    spec = load_scenario()
    async with github_manager as mcp:
        data = mcp_data(await mcp.call_tool("get_deployments", {"limit": 10}))
    scenario_records = [d for d in data["deployments"] if d.get("source") == "scenario-fixture"]
    assert len(scenario_records) == 1, "exactly one labeled scenario deployment"
    record = scenario_records[0]
    assert record["service"] == "checkout"
    assert record["environment"] == "production"
    assert record["created_at"] == spec.deployment.deployed_at_utc.isoformat().replace("+00:00", "Z")
    assert data["note"], "the response must disclose the fixture record"


async def test_get_open_issues_filters_pull_requests(github_manager):
    async with github_manager as mcp:
        data = mcp_data(await mcp.call_tool("get_open_issues", {"limit": 10}))
    assert [issue["number"] for issue in data["issues"]] == [12, 11]
    assert data["issues"][0]["labels"] == ["incident", "checkout"]


async def test_get_repository_activity(github_manager):
    async with github_manager as mcp:
        data = mcp_data(await mcp.call_tool("get_repository_activity", {"days": 30, "limit": 30}))
    assert data["open_issues_count"] == 2
    assert data["commits_in_window"] == 2
    assert data["latest_commit"]["sha"] == "f3a1c2b4d5"


async def test_auth_failure_surfaces_typed_error(github_mock):
    server = build_github_mock(status_overrides={
        "/repos/ronni86bit/MCP-Agent-Project/commits": (401, {"message": "Bad credentials"}),
        "/repos/ronni86bit/MCP-Agent-Project/deployments": (401, {"message": "Bad credentials"}),
    })
    try:
        manager = MCPClientManager(
            "github-mcp",
            sys.executable,
            ["-m", "backend.mcp.servers.github_mcp"],
            env={"GITHUB_API_BASE_URL": f"http://127.0.0.1:{server.server_address[1]}"},
            call_timeout_seconds=15,
        )
        async with manager as mcp:
            with pytest.raises(MCPClientError) as excinfo:
                await mcp.call_tool("get_recent_commits", {"limit": 5})
        assert "GITHUB_TOKEN" in str(excinfo.value) or "rejected access" in str(excinfo.value)
    finally:
        server.shutdown()
        server.server_close()


async def test_upstream_5xx_surfaces_typed_error(github_mock):
    server = build_github_mock(status_overrides={
        "/repos/ronni86bit/MCP-Agent-Project/commits": (502, {"message": "bad gateway"}),
    })
    try:
        manager = MCPClientManager(
            "github-mcp",
            sys.executable,
            ["-m", "backend.mcp.servers.github_mcp"],
            env={"GITHUB_API_BASE_URL": f"http://127.0.0.1:{server.server_address[1]}"},
            call_timeout_seconds=15,
        )
        async with manager as mcp:
            with pytest.raises(MCPClientError) as excinfo:
                await mcp.call_tool("get_recent_commits", {"limit": 5})
        assert "github request failed" in str(excinfo.value)
    finally:
        server.shutdown()
        server.server_close()


async def test_unknown_repo_404_surfaces_typed_error(github_mock):
    server = build_github_mock(status_overrides={
        "/repos/ronni86bit/MCP-Agent-Project": (404, {"message": "Not Found"}),
        "/repos/ronni86bit/MCP-Agent-Project/commits": (404, {"message": "Not Found"}),
        "/repos/ronni86bit/MCP-Agent-Project/deployments": (404, {"message": "Not Found"}),
    })
    try:
        manager = MCPClientManager(
            "github-mcp",
            sys.executable,
            ["-m", "backend.mcp.servers.github_mcp"],
            env={"GITHUB_API_BASE_URL": f"http://127.0.0.1:{server.server_address[1]}"},
            call_timeout_seconds=15,
        )
        async with manager as mcp:
            with pytest.raises(MCPClientError) as excinfo:
                await mcp.call_tool("get_repository_activity", {"days": 7})
        assert "not found" in str(excinfo.value).lower()
    finally:
        server.shutdown()
        server.server_close()
