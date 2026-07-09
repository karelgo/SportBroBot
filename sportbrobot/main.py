"""FastAPI application factory.

Route-order invariant: web routers (which own GET /mcp/setup...) are included
BEFORE the /mcp mount so specific routes win; the mount handles everything
else under /mcp (the actual MCP protocol endpoint).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .db import init_db
from .mcp_server.server import build_mcp_asgi_app, mcp_lifespan
from .web import auth, dashboard, landing, strava

PACKAGE_DIR = Path(__file__).resolve().parent


class _McpPathNormalizer:
    """Rewrite the exact path /mcp to /mcp/ so the advertised MCP URL
    (BASE_URL/mcp?apiKey=...) hits the mount directly instead of bouncing
    through a 307 that some MCP clients won't follow."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") == "/mcp":
            scope = dict(scope)
            scope["path"] = "/mcp/"
            if "raw_path" in scope and scope["raw_path"] is not None:
                raw = scope["raw_path"]
                scope["raw_path"] = raw[: len(b"/mcp")] + b"/" + raw[len(b"/mcp") :]
        await self.app(scope, receive, send)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    async with mcp_lifespan():
        yield


def create_app() -> FastAPI:
    app = FastAPI(title="SportBroBot", lifespan=lifespan)
    app.add_middleware(_McpPathNormalizer)

    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")

    app.include_router(landing.router)
    app.include_router(auth.router)
    app.include_router(dashboard.router)
    app.include_router(strava.router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # Must stay last — see route-order invariant above.
    app.mount("/mcp", build_mcp_asgi_app(), name="mcp")

    return app


app = create_app()
