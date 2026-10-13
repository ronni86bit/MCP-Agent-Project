"""GitHubClient unit tests against a mocked transport (respx, classic httpx)."""

import httpx
import pytest
import respx

from backend.mcp.servers.github_mcp.client import (
    GitHubAuthError,
    GitHubClient,
    GitHubNotFoundError,
    GitHubRateLimitedError,
    GitHubUpstreamError,
)

BASE = "http://github-mock"


def make_client(token: str = "") -> GitHubClient:
    return GitHubClient(repo="acme/widgets", token=token, base_url=BASE)


@respx.mock
async def test_token_is_sent_as_header_when_present():
    route = respx.get(f"{BASE}/repos/acme/widgets/commits").respond(json=[])
    client = make_client(token="gh_test_token")
    try:
        await client.recent_commits()
    finally:
        await client.aclose()
    assert route.calls.last.request.headers["Authorization"] == "Bearer gh_test_token"


@respx.mock
async def test_recent_commits_maps_fields():
    respx.get(f"{BASE}/repos/acme/widgets/commits").respond(
        json=[
            {
                "sha": "f3a1c2b4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0",
                "commit": {
                    "author": {"name": "Dev", "date": "2026-10-05T10:00:00Z"},
                    "message": "subject line\n\nbody",
                },
            }
        ]
    )
    client = make_client()
    try:
        commits = await client.recent_commits()
    finally:
        await client.aclose()
    assert commits[0]["sha"] == "f3a1c2b4d5"
    assert commits[0]["message"] == "subject line"


@respx.mock
async def test_open_issues_filters_pull_requests():
    respx.get(f"{BASE}/repos/acme/widgets/issues").respond(
        json=[
            {"number": 2, "title": "Real issue", "created_at": "t", "labels": [], "user": {"login": "u"}},
            {"number": 1, "title": "A PR", "created_at": "t", "labels": [], "user": {"login": "u"},
             "pull_request": {"url": "x"}},
        ]
    )
    client = make_client()
    try:
        issues = await client.open_issues()
    finally:
        await client.aclose()
    assert [issue["number"] for issue in issues] == [2]


@respx.mock
async def test_auth_failure_maps_to_typed_error():
    respx.get(f"{BASE}/repos/acme/widgets/commits").respond(status_code=401, json={"message": "Bad credentials"})
    client = make_client(token="bad")
    try:
        with pytest.raises(GitHubAuthError):
            await client.recent_commits()
    finally:
        await client.aclose()


@respx.mock
async def test_rate_limit_retries_once_then_succeeds():
    route = respx.get(f"{BASE}/repos/acme/widgets/commits")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "slow down"}),
        httpx.Response(200, json=[]),
    ]
    client = make_client()
    try:
        commits = await client.recent_commits()
    finally:
        await client.aclose()
    assert commits == []
    assert route.call_count == 2


@respx.mock
async def test_persistent_rate_limit_maps_to_typed_error():
    respx.get(f"{BASE}/repos/acme/widgets/commits").respond(
        status_code=429, headers={"Retry-After": "0"}, json={"message": "slow down"}
    )
    client = make_client()
    try:
        with pytest.raises(GitHubRateLimitedError):
            await client.recent_commits()
    finally:
        await client.aclose()


@respx.mock
async def test_not_found_maps_to_typed_error():
    respx.get(f"{BASE}/repos/acme/widgets/commits").respond(status_code=404, json={"message": "Not Found"})
    client = make_client()
    try:
        with pytest.raises(GitHubNotFoundError):
            await client.recent_commits()
    finally:
        await client.aclose()


@respx.mock
async def test_unreachable_and_5xx_map_to_upstream_error():
    respx.get(f"{BASE}/repos/acme/widgets/commits").mock(side_effect=httpx.ConnectError("no route"))
    client = make_client()
    try:
        with pytest.raises(GitHubUpstreamError):
            await client.recent_commits()
    finally:
        await client.aclose()

    client2 = make_client()
    with respx.mock:
        respx.get(f"{BASE}/repos/acme/widgets/commits").respond(status_code=502, json={})
        try:
            with pytest.raises(GitHubUpstreamError):
                await client2.recent_commits()
        finally:
            await client2.aclose()
