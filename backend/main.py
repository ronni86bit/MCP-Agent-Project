"""FastAPI application factory. Run locally:

    uv run uvicorn backend.main:app --port 8000
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from backend.agent.service import AgentService
from backend.api import routes
from backend.config import get_settings
from backend.observability.logging import configure_logging
from backend.observability.metrics import REGISTRY
from backend.observability.middleware import CorrelationMiddleware


def create_app(service: AgentService | None = None) -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc = service or AgentService(settings)
        app.state.service = svc
        await svc.startup()
        yield
        await svc.shutdown()

    app = FastAPI(title="AI Operations Investigation Agent", version="0.3.0", lifespan=lifespan)
    app.add_middleware(CorrelationMiddleware)
    app.include_router(routes.root_router)
    app.include_router(routes.router)

    # exact-path route (not app.mount): Starlette's Mount would 307-redirect
    # "/metrics" to "/metrics/", which scrapers and curl do not follow
    @app.get("/metrics", include_in_schema=False)
    def metrics_endpoint() -> Response:
        return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()
