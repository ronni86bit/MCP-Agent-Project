"""In-process GitHub API mock served over real TCP.

github-mcp runs as a stdio subprocess and reads GITHUB_API_BASE_URL — tests
point it at this mock for deterministic, hermetic coverage of the REST paths,
error codes, and pagination behavior.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def build_github_mock(
    repo: str = "ronni86bit/MCP-Agent-Project",
    *,
    commits: list[dict] | None = None,
    deployments: list[dict] | None = None,
    issues: list[dict] | None = None,
    repo_payload: dict | None = None,
    status_overrides: dict[str, tuple[int, dict]] | None = None,
):
    """Start a local mock GitHub API; returns the server (use .server_address[1]).

    status_overrides maps a path (e.g. "/repos/{repo}/commits") to an
    (http_status, body) response for simulating auth/rate-limit/upstream
    failures.
    """
    commits = commits if commits is not None else [
        {
            "sha": "f3a1c2b4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0",
            "commit": {
                "author": {"name": "Dev One", "date": "2026-10-05T10:00:00Z"},
                "message": "checkout: fix mobile payment retry loop",
            },
        },
        {
            "sha": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
            "commit": {
                "author": {"name": "Dev Two", "date": "2026-10-02T18:29:00Z"},
                "message": "checkout: switch mobile payment flow to a new payment provider adapter",
            },
        },
    ]
    issues = issues if issues is not None else [
        {
            "number": 12,
            "title": "Mobile checkout failures spike after deployment",
            "created_at": "2026-10-03T08:00:00Z",
            "labels": [{"name": "incident"}, {"name": "checkout"}],
            "user": {"login": "ronni86bit"},
        },
        {
            "number": 11,
            "title": "Release checklist for October",
            "created_at": "2026-10-01T09:00:00Z",
            "labels": [],
            "user": {"login": "ronni86bit"},
        },
        {  # PR-shaped entry: must be filtered out of open_issues
            "number": 10,
            "title": "PR: payment adapter hotfix",
            "created_at": "2026-10-04T12:00:00Z",
            "labels": [],
            "user": {"login": "ronni86bit"},
            "pull_request": {"url": f"https://api.github.com/repos/{repo}/pulls/10"},
        },
    ]
    repo_payload = repo_payload or {
        "full_name": repo,
        "pushed_at": "2026-10-05T20:00:00Z",
        "open_issues_count": 2,
    }
    overrides = status_overrides or {}

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload: Any) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            path = self.path.split("?")[0]
            if path in overrides:
                code, payload = overrides[path]
                self._send(code, payload)
                return
            if path == f"/repos/{repo}":
                self._send(200, repo_payload)
            elif path == f"/repos/{repo}/commits":
                self._send(200, commits)
            elif path == f"/repos/{repo}/deployments":
                self._send(200, deployments or [])
            elif path == f"/repos/{repo}/issues":
                self._send(200, issues)
            elif path == f"/repos/{repo}/contributors":
                self._send(200, [{"login": "ronni86bit", "contributions": 42}])
            else:
                self._send(404, {"message": "Not Found"})

        def log_message(self, *args) -> None:  # keep test output clean
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
