"""FastAPI dependencies."""

from __future__ import annotations

from fastapi import Request

from backend.agent.service import AgentService


def get_service(request: Request) -> AgentService:
    return request.app.state.service
