"""GitHub REST client for github-mcp.

Typed failures (auth, rate limit, not found, upstream) map cleanly onto
ToolError at the server boundary. Retry-After is honored once for 429s with a
bounded wait; deeper retry policy belongs to the Phase 6 reliability phase.
The token is sent as a header only — never in logs, errors, or results.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx


class GitHubError(Exception):
    """Base class for GitHub upstream failures."""


class GitHubAuthError(GitHubError):
    """GitHub rejected the credentials (401/403 without rate-limit signals)."""


class GitHubRateLimitedError(GitHubError):
    """Rate limit exhausted even after honoring Retry-Once."""


class GitHubNotFoundError(GitHubError):
    """The repository or resource does not exist (404)."""


class GitHubUpstreamError(GitHubError):
    """GitHub was unreachable or returned an unexpected upstream response."""


class GitHubClient:
    def __init__(
        self,
        repo: str,
        token: str = "",
        base_url: str | None = None,
        timeout_seconds: float = 15.0,
    ):
        self.repo = repo
        self._base = (base_url or os.environ.get("GITHUB_API_BASE_URL", "https://api.github.com")).rstrip("/")
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ops-investigation-agent",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        # explicit connect/read/write/pool budget instead of one flat timeout
        self._client = httpx.AsyncClient(
            base_url=self._base,
            headers=headers,
            timeout=httpx.Timeout(connect=5.0, read=timeout_seconds, write=10.0, pool=5.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        response: httpx.Response | None = None
        for attempt in (1, 2):
            try:
                response = await self._client.get(path, params=params)
            except httpx.TimeoutException as exc:
                raise GitHubUpstreamError(f"GitHub request to {path} timed out: {exc}") from exc
            except httpx.HTTPError as exc:
                raise GitHubUpstreamError(f"GitHub is unreachable ({path}): {exc}") from exc
            if response.status_code == 429 and attempt == 1:
                retry_after = min(float(response.headers.get("Retry-After", "2") or 2), 5.0)
                await asyncio.sleep(retry_after)
                continue
            break
        assert response is not None
        if response.status_code == 429 or (
            response.status_code == 403 and response.headers.get("X-RateLimit-Remaining") == "0"
        ):
            raise GitHubRateLimitedError(f"GitHub rate limit exhausted for {path}; retry later")
        if response.status_code in (401, 403):
            raise GitHubAuthError(
                f"GitHub rejected access to {path} (HTTP {response.status_code}); "
                "check that GITHUB_TOKEN is valid and has read access to the repository"
            )
        if response.status_code == 404:
            raise GitHubNotFoundError(f"GitHub resource not found: {path} (repository {self.repo!r})")
        if response.status_code >= 400:
            raise GitHubUpstreamError(f"GitHub returned HTTP {response.status_code} for {path}")
        return response

    async def _get_list(self, path: str, params: dict[str, Any], limit: int, max_pages: int = 3) -> list[dict]:
        items: list[dict] = []
        per_page = min(50, limit)
        for page in range(1, max_pages + 1):
            response = await self._get(path, {**params, "per_page": per_page, "page": page})
            batch = response.json()
            if not isinstance(batch, list):
                break
            items.extend(batch)
            if len(batch) < per_page or len(items) >= limit:
                break
        return items[:limit]

    async def recent_commits(self, limit: int = 10, branch: str = "main") -> list[dict[str, Any]]:
        raw = await self._get_list(f"/repos/{self.repo}/commits", {"sha": branch}, limit)
        result = []
        for entry in raw:
            commit = entry.get("commit", {}) or {}
            author = commit.get("author", {}) or {}
            message = (commit.get("message") or "").splitlines()[0][:200]
            result.append(
                {
                    "sha": (entry.get("sha") or "")[:10],
                    "author": author.get("name", ""),
                    "date": author.get("date", ""),
                    "message": message,
                }
            )
        return result

    async def deployments(self, limit: int = 10) -> list[dict[str, Any]]:
        raw = await self._get_list(f"/repos/{self.repo}/deployments", {}, limit)
        return [
            {
                "id": entry.get("id"),
                "sha": (entry.get("sha") or "")[:10],
                "ref": entry.get("ref", ""),
                "environment": entry.get("environment", ""),
                "created_at": entry.get("created_at", ""),
                "description": entry.get("description") or "",
                "creator": ((entry.get("creator") or {}).get("login")) or "",
                "source": "github",
            }
            for entry in raw
        ]

    async def open_issues(self, limit: int = 10) -> list[dict[str, Any]]:
        raw = await self._get_list(f"/repos/{self.repo}/issues", {"state": "open"}, limit)
        result = []
        for entry in raw:
            if "pull_request" in entry:  # the issues API also returns PRs
                continue
            result.append(
                {
                    "number": entry.get("number"),
                    "title": (entry.get("title") or "")[:200],
                    "created_at": entry.get("created_at", ""),
                    "labels": [label.get("name", "") for label in entry.get("labels", [])],
                    "author": ((entry.get("user") or {}).get("login")) or "",
                }
            )
        return result[:limit]

    async def repository_activity(self, days: int = 14, limit: int = 30) -> dict[str, Any]:
        repo = (await self._get(f"/repos/{self.repo}")).json()
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        commits = await self._get_list(f"/repos/{self.repo}/commits", {"since": since}, limit)
        latest = None
        if commits:
            entry = commits[0]
            commit = entry.get("commit", {}) or {}
            latest = {
                "sha": (entry.get("sha") or "")[:10],
                "date": (commit.get("author") or {}).get("date", ""),
                "message": ((commit.get("message") or "").splitlines() or [""])[0][:200],
            }
        return {
            "repository": self.repo,
            "window_days": days,
            "pushed_at": repo.get("pushed_at", ""),
            "open_issues_count": repo.get("open_issues_count", 0),
            "commits_in_window": len(commits),
            "latest_commit": latest,
        }
