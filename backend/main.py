"""FastAPI application factory. Run locally:

    uv run uvicorn backend.main:app --port 8000
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.agent.service import AgentService
from backend.api import routes
from backend.config import get_settings


def create_app(service: AgentService | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.service = service or AgentService(get_settings())
        yield
        await app.state.service.shutdown()

    app = FastAPI(title="AI Operations Investigation Agent", version="0.3.0", lifespan=lifespan)
    app.include_router(routes.router)
    return app


app = create_app()
