"""FastAPI app for portfolio live view and sparse snapshots."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from kiteconnect.exceptions import KiteException

from instruments import api_router as instruments_router
from instruments import peek_as_of as instruments_peek_as_of

from .fetch import TokenInvalid, fetch_live
from .service import (
    DEFAULT_LIVE_TTL,
    DbBusy,
    Fetcher,
    PortfolioService,
    SnapshotNotFound,
)

TOKEN_STEPS = [
    "Open a terminal in the project root (J:\\src\\cursor\\zerodha1).",
    "Run: uv run kitetools login",
    "Finish the browser login (Zerodha credentials + TOTP).",
    "Reload this page.",
]

VITE_ORIGINS = (
    "http://127.0.0.1:5173",
    "http://localhost:5173",
)

# portfolio/src/portfolio/api.py → parents[3] = repo root
DEFAULT_UI_DIST = Path(__file__).resolve().parents[3] / "ui" / "dist"


def default_ui_dist() -> Path:
    return DEFAULT_UI_DIST


def _error_body(error: str, message: str, steps: list[str] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"error": error, "message": message}
    if steps is not None:
        body["steps"] = steps
    return body


def create_app(
    db_path: Path | None = None,
    fetcher: Fetcher = fetch_live,
    ttl: float = DEFAULT_LIVE_TTL,
    ui_dist: Path | None = None,
) -> FastAPI:
    app = FastAPI(title="portfolio", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(VITE_ORIGINS),
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )
    service = PortfolioService(db_path=db_path, fetcher=fetcher, ttl=ttl)
    dist = Path(ui_dist) if ui_dist is not None else default_ui_dist()
    ui_dist_exists = dist.is_dir() and (dist / "index.html").is_file()
    app.state.service = service
    app.state.ui_dist = dist
    app.state.ui_dist_exists = ui_dist_exists
    app.include_router(instruments_router)

    @app.exception_handler(TokenInvalid)
    async def token_invalid_handler(_request: Request, exc: TokenInvalid) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content=_error_body(
                "token_invalid",
                "Kite session token is invalid or expired.",
                steps=TOKEN_STEPS,
            ),
        )

    @app.exception_handler(SnapshotNotFound)
    async def snapshot_not_found_handler(
        _request: Request, exc: SnapshotNotFound
    ) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content=_error_body("snapshot_not_found", str(exc)),
        )

    @app.exception_handler(DbBusy)
    async def db_busy_handler(_request: Request, exc: DbBusy) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content=_error_body("db_busy", str(exc)),
        )

    @app.exception_handler(RuntimeError)
    async def runtime_handler(_request: Request, exc: RuntimeError) -> JSONResponse:
        msg = str(exc)
        if "schema_version" in msg:
            return JSONResponse(
                status_code=503,
                content=_error_body("db_schema", msg),
            )
        return JSONResponse(
            status_code=500,
            content=_error_body("internal_error", msg),
        )

    @app.exception_handler(KiteException)
    async def kite_handler(_request: Request, exc: KiteException) -> JSONResponse:
        return JSONResponse(
            status_code=502,
            content=_error_body("kite_error", str(exc)),
        )

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        body = service.health()
        body["ui_dist_exists"] = ui_dist_exists
        body["ui_dist"] = str(dist)
        as_of = instruments_peek_as_of()
        body["instruments_as_of"] = as_of.isoformat() if as_of else None
        return body

    @app.get("/api/portfolio/live")
    def portfolio_live(
        refresh: bool = Query(False, description="Bypass the live cache and refetch from Kite."),
    ) -> dict[str, Any]:
        return service.live(refresh=refresh)

    @app.post("/api/portfolio/capture")
    def portfolio_capture() -> dict[str, Any]:
        return service.capture()

    @app.get("/api/portfolio/snapshots")
    def portfolio_snapshots(
        limit: int = Query(50, ge=1, le=500),
    ) -> dict[str, Any]:
        return service.snapshots(limit=limit)

    @app.get("/api/portfolio/snapshots/{snapshot_id}")
    def portfolio_snapshot(snapshot_id: int) -> dict[str, Any]:
        return service.snapshot(snapshot_id)

    if ui_dist_exists:
        assets = dist / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{full_path:path}")
        def spa_fallback(full_path: str) -> FileResponse:
            # Never shadow /api — those routes are registered above.
            if full_path == "api" or full_path.startswith("api/"):
                return JSONResponse(
                    status_code=404,
                    content=_error_body("not_found", f"No API route for /{full_path}"),
                )
            return FileResponse(dist / "index.html")
    else:

        @app.get("/")
        def root_hint() -> dict[str, Any]:
            return {
                "message": "Portfolio API is running (UI build not found).",
                "hint": (
                    "Dev: cd ui && set NODE_OPTIONS=--use-system-ca "
                    "&& npm run dev (http://127.0.0.1:5173). "
                    "Single-process: npm run build then restart portfolio serve."
                ),
                "health": "/api/health",
                "ui_dist": str(dist),
            }

    return app


__all__ = ["TOKEN_STEPS", "create_app", "default_ui_dist"]
